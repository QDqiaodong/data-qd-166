"""离线材料成本月末关账（SQLite，单操作者，标准库）。

工厂已用 material-trace 记录实际收货、完工领料与发货；本模块把采购成本
沿实际领料分配到成品与已发货明细，产出可核对期末库存估值与销售成本的
月度账单。发票晚到或更正时旧账单保持原样：已发走部分只在当前月入账差
额，仍在库部分按最新成本更新期末价值。只核算材料成本，不另计工费。

存储为独立本地 SQLite 文件；``db`` 接受 ``str`` 或 ``pathlib.Path``。
成功返回账单字典，失败抛 ``ValueError`` 且不留下任何部分变化。
"""
import json
import sqlite3
from pathlib import Path

from .common import fields, text, integer, array

MAX_LOTS = 10000
MAX_SHIPMENTS = 10000
MAX_RECALLS = 10000
MAX_LINES = 100
MAX_EXPOSURES = 100000
MAX_UPDATES = 10000
MAX_TOTAL_CENTS = 10**12

_SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE lots (
    lot_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    produced_quantity INTEGER NOT NULL,
    first_period TEXT NOT NULL,
    cost_cents INTEGER
);
CREATE TABLE lot_inputs (
    lot_id TEXT NOT NULL,
    input_lot_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    PRIMARY KEY (lot_id, input_lot_id)
);
CREATE TABLE shipments (
    shipment_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    first_period TEXT NOT NULL
);
CREATE TABLE shipment_allocations (
    shipment_id TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    recognized_cents INTEGER NOT NULL,
    PRIMARY KEY (shipment_id, lot_id)
);
CREATE TABLE periods (
    period TEXT PRIMARY KEY,
    physical_version INTEGER NOT NULL,
    bill TEXT NOT NULL
);
"""


def _month(value):
    """校验规范 ``YYYY-MM`` 期间字符串并原样返回。"""
    text(value)
    if len(value) != 7 or value[4] != '-' \
            or not value[:4].isdigit() or not value[5:].isdigit():
        raise ValueError('YYYY-MM period required')
    year, month = int(value[:4]), int(value[5:])
    if not 1 <= year <= 9999 or not 1 <= month <= 12:
        raise ValueError('YYYY-MM period required')
    return value


def _next_month(period):
    year, month = int(period[:4]), int(period[5:]) + 1
    if month == 13:
        year, month = year + 1, 1
    if year > 9999:
        raise ValueError('period overflow')
    return '%04d-%02d' % (year, month)


def _lines(value, owner):
    lines = {}
    for line in array(value, MAX_LINES):
        fields(line, 'lot_id quantity')
        lot_id = text(line['lot_id'])
        if lot_id in lines:
            raise ValueError('duplicate lot reference in ' + owner)
        lines[lot_id] = integer(line['quantity'], 1)
    return lines


def validate_physical(value):
    """校验整份实物快照，返回 ``(facts, topo_order)``；不修改入参。

    质量状态（``recalls``/``exposures``/``blocked_by``）只做结构校验，
    不参与估值与实物历史比较。``facts`` 只含可核对实物事实。
    """
    fields(value, 'version lots shipments recalls exposures')
    version = integer(value['version'], 0)
    lots = {}
    for row in array(value['lots'], MAX_LOTS):
        fields(row, 'lot_id sku produced_quantity remaining_quantity'
                    ' inputs blocked_by')
        lot_id = text(row['lot_id'])
        if lot_id in lots:
            raise ValueError('duplicate lot_id: ' + lot_id)
        for recall_id in array(row['blocked_by'], MAX_RECALLS):
            text(recall_id)
        lots[lot_id] = {'sku': text(row['sku']),
                        'produced': integer(row['produced_quantity'], 1),
                        'remaining': integer(row['remaining_quantity'], 0),
                        'inputs': _lines(row['inputs'], lot_id)}
    shipments = {}
    for row in array(value['shipments'], MAX_SHIPMENTS):
        fields(row, 'shipment_id customer_id sku quantity allocations')
        shipment_id = text(row['shipment_id'])
        if shipment_id in shipments:
            raise ValueError('duplicate shipment_id: ' + shipment_id)
        allocations = _lines(row['allocations'], shipment_id)
        if not allocations:
            raise ValueError('shipment requires at least one allocation')
        shipments[shipment_id] = {'customer_id': text(row['customer_id']),
                                  'sku': text(row['sku']),
                                  'quantity': integer(row['quantity'], 1),
                                  'allocations': allocations}
    # 质量状态：仅结构校验。
    seen_recalls = set()
    for row in array(value['recalls'], MAX_RECALLS):
        fields(row, 'recall_id lot_id status')
        recall_id = text(row['recall_id'])
        text(row['lot_id'])
        if row['status'] not in ('active', 'closed'):
            raise ValueError('invalid recall status')
        if recall_id in seen_recalls:
            raise ValueError('duplicate recall_id: ' + recall_id)
        seen_recalls.add(recall_id)
    for row in array(value['exposures'], MAX_EXPOSURES):
        fields(row, 'recall_id shipment_id customer_id sku lot_id quantity')
        for key in ('recall_id', 'shipment_id', 'customer_id', 'sku',
                    'lot_id'):
            text(row[key])
        integer(row['quantity'], 1)

    # 引用唯一有效、发货分配与发货一致、谱系无环、每批数量守恒。
    for lot_id, lot in lots.items():
        for input_lot in lot['inputs']:
            if input_lot not in lots:
                raise ValueError('unknown input lot: ' + input_lot)
    for shipment_id, shipment in shipments.items():
        total = 0
        for lot_id, quantity in shipment['allocations'].items():
            lot = lots.get(lot_id)
            if lot is None:
                raise ValueError('unknown allocation lot: ' + lot_id)
            if lot['sku'] != shipment['sku']:
                raise ValueError('allocation lot sku mismatch: ' + lot_id)
            total += quantity
        if total != shipment['quantity']:
            raise ValueError('allocations do not sum to shipment quantity')
    consumers = {lot_id: [] for lot_id in lots}
    indegree = {}
    for lot_id, lot in lots.items():
        indegree[lot_id] = len(lot['inputs'])
        for input_lot in lot['inputs']:
            consumers[input_lot].append(lot_id)
    ready = sorted(lot_id for lot_id in lots if indegree[lot_id] == 0)
    order = []
    while ready:
        lot_id = ready.pop(0)
        order.append(lot_id)
        for consumer in sorted(consumers[lot_id]):
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                ready.append(consumer)
                ready.sort()
    if len(order) != len(lots):
        raise ValueError('lot genealogy cycle')
    consumed = {lot_id: 0 for lot_id in lots}
    for lot in lots.values():
        for input_lot, quantity in lot['inputs'].items():
            consumed[input_lot] += quantity
    for shipment in shipments.values():
        for lot_id, quantity in shipment['allocations'].items():
            consumed[lot_id] += quantity
    for lot_id, lot in lots.items():
        if lot['produced'] != lot['remaining'] + consumed[lot_id]:
            raise ValueError('lot balance does not match consumption: '
                             + lot_id)
    return {'version': version, 'lots': lots, 'shipments': shipments}, order


def validate_close(close):
    """纯结构与快照校验（不依赖数据库），失败抛 ``ValueError``。"""
    fields(close, 'period physical cost_updates')
    period = _month(close['period'])
    phys, order = validate_physical(close['physical'])
    updates = {}
    for row in array(close['cost_updates'], MAX_UPDATES):
        fields(row, 'lot_id total_cents source_ref')
        lot_id = text(row['lot_id'])
        if lot_id in updates:
            raise ValueError('duplicate cost update lot: ' + lot_id)
        updates[lot_id] = (integer(row['total_cents'], 0, MAX_TOTAL_CENTS),
                           text(row['source_ref']))
    for lot_id in updates:
        lot = phys['lots'].get(lot_id)
        if lot is None:
            raise ValueError('cost update references unknown lot: ' + lot_id)
        if lot['inputs']:
            raise ValueError('cost update requires a receipt lot: ' + lot_id)
    return period, phys, order, updates


def _connect_initialized(db):
    """打开已初始化成本账簿；缺失路径绝不隐式建库。"""
    path = Path(db)
    if not path.is_file():
        raise ValueError('database not initialized: ' + str(path))
    conn = sqlite3.connect(path)
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        kind = conn.execute(
            "SELECT value FROM meta WHERE key = 'kind'").fetchone() \
            if found else None
    except sqlite3.DatabaseError as error:
        conn.close()
        raise ValueError('unreadable database: ' + str(error))
    if kind is None or kind[0] != 'material-cost':
        conn.close()
        raise ValueError('database not initialized: ' + str(path))
    return conn


def _load_state(conn):
    meta = dict(conn.execute('SELECT key, value FROM meta').fetchall())
    lots = {}
    for lot_id, sku, produced, first_period, cost in conn.execute(
            'SELECT lot_id, sku, produced_quantity, first_period, cost_cents'
            ' FROM lots'):
        lots[lot_id] = {'sku': sku, 'produced': produced,
                        'first_period': first_period, 'cost': cost,
                        'inputs': {}}
    for lot_id, input_lot, quantity in conn.execute(
            'SELECT lot_id, input_lot_id, quantity FROM lot_inputs'):
        lots[lot_id]['inputs'][input_lot] = quantity
    shipments = {}
    for shipment_id, customer_id, sku, quantity, first_period in conn.execute(
            'SELECT shipment_id, customer_id, sku, quantity, first_period'
            ' FROM shipments'):
        shipments[shipment_id] = {'customer_id': customer_id, 'sku': sku,
                                  'quantity': quantity,
                                  'first_period': first_period,
                                  'allocations': {}}
    for shipment_id, lot_id, quantity, recognized in conn.execute(
            'SELECT shipment_id, lot_id, quantity, recognized_cents'
            ' FROM shipment_allocations'):
        shipments[shipment_id]['allocations'][lot_id] = {
            'quantity': quantity, 'recognized': recognized}
    last_period = meta.get('last_period')
    opening = 0
    if last_period is not None:
        row = conn.execute('SELECT bill FROM periods WHERE period = ?',
                           (last_period,)).fetchone()
        opening = json.loads(row[0])['totals']['ending_cents']
    return {'version': (int(meta['physical_version'])
                        if 'physical_version' in meta else None),
            'last_period': last_period, 'opening': opening,
            'lots': lots, 'shipments': shipments}


def _check_baseline(phys, state):
    """实物基线核对：版本不倒退；同版本只允许实物事实相同。

    版本前进时旧批次、旧发货不得消失或改写；旧批次唯一可变的余额字段由
    整份快照的数量守恒与新增消耗自动约束。
    """
    if state['version'] is None:
        return
    if phys['version'] < state['version']:
        raise ValueError('physical version regressed')
    same = phys['version'] == state['version']
    consumed = {lot_id: 0 for lot_id in state['lots']}
    for lot in state['lots'].values():
        for input_lot, quantity in lot['inputs'].items():
            consumed[input_lot] += quantity
    for shipment in state['shipments'].values():
        for lot_id, line in shipment['allocations'].items():
            consumed[lot_id] += line['quantity']
    for lot_id, prev in state['lots'].items():
        current = phys['lots'].get(lot_id)
        if current is None:
            raise ValueError('physical history lost lot: ' + lot_id)
        if current['sku'] != prev['sku'] \
                or current['produced'] != prev['produced'] \
                or current['inputs'] != prev['inputs']:
            raise ValueError('physical history rewrote lot: ' + lot_id)
        if same and current['remaining'] != prev['produced'] - consumed[lot_id]:
            raise ValueError('same physical version with different facts')
    for shipment_id, prev in state['shipments'].items():
        current = phys['shipments'].get(shipment_id)
        if current is None:
            raise ValueError('physical history lost shipment: ' + shipment_id)
        previous_allocations = {lot_id: line['quantity']
                                for lot_id, line in prev['allocations'].items()}
        if current['customer_id'] != prev['customer_id'] \
                or current['sku'] != prev['sku'] \
                or current['quantity'] != prev['quantity'] \
                or current['allocations'] != previous_allocations:
            raise ValueError('physical history rewrote shipment: '
                             + shipment_id)
    if same and (len(phys['lots']) != len(state['lots'])
                 or len(phys['shipments']) != len(state['shipments'])):
        raise ValueError('same physical version with different facts')


def _compute(period, phys, order, updates, state):
    """按当前成本与消耗顺序估值，返回 ``(bill, costs, recognized)``。"""
    old_lots = state['lots']
    old_shipments = state['shipments']
    # 收货批次取最新采购成本；首次出现的收货批次本期必须有成本。
    costs = {}
    for lot_id, lot in phys['lots'].items():
        if lot['inputs']:
            continue
        if lot_id in updates:
            costs[lot_id] = updates[lot_id][0]
        elif lot_id in old_lots:
            costs[lot_id] = old_lots[lot_id]['cost']
        else:
            raise ValueError('new receipt lot requires a cost update: '
                             + lot_id)
    # 事实归属：以首次进入关账快照的月份为准，不推测实际业务日期。
    first_period = {}
    for lot_id in phys['lots']:
        prev = old_lots.get(lot_id)
        first_period[lot_id] = prev['first_period'] if prev else period
    for shipment_id in phys['shipments']:
        prev = old_shipments.get(shipment_id)
        first_period[shipment_id] = prev['first_period'] if prev else period
    # 每批消耗顺序：首次入账月份；同月先领料（领入批次升序）后发货
    # （发货号升序）。该顺序固定，后加入的记录不能挤到旧记录前面。
    schedules = {lot_id: [] for lot_id in phys['lots']}
    for lot_id, lot in phys['lots'].items():
        for input_lot, quantity in lot['inputs'].items():
            schedules[input_lot].append(
                (first_period[lot_id], 0, lot_id, quantity))
    for shipment_id, shipment in phys['shipments'].items():
        for lot_id, quantity in shipment['allocations'].items():
            schedules[lot_id].append(
                (first_period[shipment_id], 1, shipment_id, quantity))
    # 逐批分摊：完工批次价值为各投入批次转入金额之和，再按自身产量分摊。
    values = {}
    made = {lot_id: 0 for lot_id, lot in phys['lots'].items()
            if lot['inputs']}
    should = {}
    for lot_id in order:
        lot = phys['lots'][lot_id]
        value = made[lot_id] if lot['inputs'] else costs[lot_id]
        values[lot_id] = value
        produced = lot['produced']
        consumed_before = 0
        for _, kind, key, quantity in sorted(schedules[lot_id]):
            consumed_after = consumed_before + quantity
            transfer = value * consumed_after // produced \
                - value * consumed_before // produced
            consumed_before = consumed_after
            if kind == 0:
                made[key] += transfer
            else:
                should[(key, lot_id)] = transfer
    # 销售成本：首次入账分配列 sale；往期分配只在变化时列 adjustment。
    cogs = []
    recognized = {}
    for shipment_id, shipment in phys['shipments'].items():
        for lot_id in shipment['allocations']:
            amount = should[(shipment_id, lot_id)]
            recognized[(shipment_id, lot_id)] = amount
            if first_period[shipment_id] == period:
                cogs.append({'category': 'sale', 'shipment_id': shipment_id,
                             'lot_id': lot_id, 'amount_cents': amount})
            else:
                prev = old_shipments[shipment_id]['allocations'][lot_id]
                delta = amount - prev['recognized']
                if delta:
                    cogs.append({'category': 'adjustment',
                                 'shipment_id': shipment_id, 'lot_id': lot_id,
                                 'amount_cents': delta})
    cogs.sort(key=lambda row: (row['shipment_id'], row['lot_id']))
    # 期末库存：仅剩余量大于 0 的批次，余款为在库价值。
    inventory = []
    for lot_id in sorted(phys['lots']):
        lot = phys['lots'][lot_id]
        if lot['remaining'] > 0:
            consumed_total = lot['produced'] - lot['remaining']
            amount = values[lot_id] \
                - values[lot_id] * consumed_total // lot['produced']
            inventory.append({'lot_id': lot_id, 'sku': lot['sku'],
                              'quantity': lot['remaining'],
                              'amount_cents': amount})
    purchases = 0
    for lot_id, (total, _) in updates.items():
        prev = old_lots[lot_id]['cost'] if lot_id in old_lots else 0
        purchases += total - prev
    cogs_net = sum(row['amount_cents'] for row in cogs)
    ending = sum(row['amount_cents'] for row in inventory)
    opening = state['opening']
    if opening + purchases - cogs_net != ending:
        raise ValueError('internal conservation failure')
    bill = {
        'period': period,
        'physical_version': phys['version'],
        'cost_updates': [{'lot_id': lot_id, 'total_cents': total,
                          'source_ref': source_ref}
                         for lot_id, (total, source_ref)
                         in sorted(updates.items())],
        'inventory': inventory,
        'cogs': cogs,
        'totals': {'opening_cents': opening, 'purchases_cents': purchases,
                   'cogs_cents': cogs_net, 'ending_cents': ending},
    }
    return bill, costs, recognized


def init_db(db):
    """创建全新成本账簿；路径已存在（含空文件/符号链接）即拒绝。"""
    path = Path(db)
    if path.exists() or path.is_symlink():
        raise ValueError('database already exists: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        conn.execute('INSERT INTO meta (key, value) VALUES (?, ?)',
                     ('kind', 'material-cost'))
        conn.commit()
    except Exception:
        if conn is not None:
            conn.close()
        if path.exists():
            path.unlink()
        raise ValueError('initialization failed')
    else:
        conn.close()
    return {'closed_periods': []}


def close_period(db, close):
    """提交并冻结一个新月的账单；失败整期回滚，修正后可重新提交。"""
    period, phys, order, updates = validate_close(close)
    conn = _connect_initialized(db)
    try:
        state = _load_state(conn)
        last_period = state['last_period']
        if last_period is not None and period != _next_month(last_period):
            raise ValueError('period is not the next unclosed month')
        _check_baseline(phys, state)
        bill, costs, recognized = _compute(period, phys, order, updates,
                                           state)
        bill_text = json.dumps(bill, sort_keys=True, ensure_ascii=False,
                               allow_nan=False)
        try:
            with conn:
                for lot_id, lot in phys['lots'].items():
                    if lot_id in state['lots']:
                        continue
                    conn.execute(
                        'INSERT INTO lots (lot_id, sku, produced_quantity,'
                        ' first_period, cost_cents) VALUES (?, ?, ?, ?, ?)',
                        (lot_id, lot['sku'], lot['produced'], period,
                         costs.get(lot_id)))
                    for input_lot, quantity in lot['inputs'].items():
                        conn.execute(
                            'INSERT INTO lot_inputs (lot_id, input_lot_id,'
                            ' quantity) VALUES (?, ?, ?)',
                            (lot_id, input_lot, quantity))
                for lot_id, (total, _) in updates.items():
                    if lot_id in state['lots']:
                        conn.execute(
                            'UPDATE lots SET cost_cents = ?'
                            ' WHERE lot_id = ?', (total, lot_id))
                for shipment_id, shipment in phys['shipments'].items():
                    if shipment_id not in state['shipments']:
                        conn.execute(
                            'INSERT INTO shipments (shipment_id, customer_id,'
                            ' sku, quantity, first_period)'
                            ' VALUES (?, ?, ?, ?, ?)',
                            (shipment_id, shipment['customer_id'],
                             shipment['sku'], shipment['quantity'], period))
                        for lot_id, quantity in shipment['allocations'].items():
                            conn.execute(
                                'INSERT INTO shipment_allocations'
                                ' (shipment_id, lot_id, quantity,'
                                ' recognized_cents) VALUES (?, ?, ?, ?)',
                                (shipment_id, lot_id, quantity,
                                 recognized[(shipment_id, lot_id)]))
                    else:
                        for lot_id in shipment['allocations']:
                            conn.execute(
                                'UPDATE shipment_allocations'
                                ' SET recognized_cents = ?'
                                ' WHERE shipment_id = ? AND lot_id = ?',
                                (recognized[(shipment_id, lot_id)],
                                 shipment_id, lot_id))
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value)"
                    " VALUES ('physical_version', ?)", (str(phys['version']),))
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value)"
                    " VALUES ('last_period', ?)", (period,))
                conn.execute(
                    'INSERT INTO periods (period, physical_version, bill)'
                    ' VALUES (?, ?, ?)',
                    (period, phys['version'], bill_text))
        except sqlite3.Error as error:
            raise ValueError('close commit failed: ' + str(error))
        return bill
    finally:
        conn.close()


def show_period(db, period):
    """返回该月首次关账时冻结的账单；未关账期间抛 ``ValueError``。"""
    period = _month(period)
    conn = _connect_initialized(db)
    try:
        row = conn.execute('SELECT bill FROM periods WHERE period = ?',
                           (period,)).fetchone()
        if row is None:
            raise ValueError('period not closed: ' + period)
        return json.loads(row[0])
    finally:
        conn.close()
