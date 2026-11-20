"""实物批次谱系与召回出库闸（SQLite，单操作者，标准库）。

净算与承诺簿给出的是建议/承诺；本模块记录真实的收货、完工、发货与召回。
每个批次终身唯一，完工只消耗明确列出的实际输入批次，并按主数据配方逐
SKU 精确核对。召回沿实际 make 输入关系传播到所有后代批次，阻断其继续
投料或出库，并把此前已发给客户的原始批次分配列入暴露清单。

存储为独立本地 SQLite 文件；``db`` 接受 ``str`` 或 ``pathlib.Path``。
成功返回当前快照字典，失败抛 ``ValueError`` 且不改变任何已提交事实。
"""
import json
import sqlite3
from pathlib import Path

from .common import fields, text, integer, array

MAX_ITEMS = 100
MAX_COMPONENTS = 100
MAX_OPERATIONS = 100
MAX_LINES = 100
MAX_IDENTITIES = 10000

_SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE lots (
    lot_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    produced_quantity INTEGER NOT NULL,
    remaining_quantity INTEGER NOT NULL,
    seq INTEGER NOT NULL UNIQUE
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
    quantity INTEGER NOT NULL
);
CREATE TABLE shipment_allocations (
    shipment_id TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    PRIMARY KEY (shipment_id, lot_id)
);
CREATE TABLE recalls (
    recall_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'closed'))
);
CREATE TABLE requests (
    request_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    response TEXT NOT NULL
);
"""


def _canonical(value):
    """键序无关的稳定 JSON 序列化（数组顺序保持不变）。"""
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def validate_master(master):
    """校验 ``master``（字段恰为 items）并返回 ``sku -> components``。"""
    fields(master, 'items')
    rows = array(master['items'], MAX_ITEMS)
    if not rows:
        raise ValueError('empty item master')
    items = {}
    for row in rows:
        fields(row, 'sku components')
        sku = text(row['sku'])
        if sku in items:
            raise ValueError('duplicate sku')
        components = {}
        for component in array(row['components'], MAX_COMPONENTS):
            fields(component, 'sku quantity')
            child = text(component['sku'])
            if child in components:
                raise ValueError('duplicate component')
            components[child] = integer(component['quantity'], 1, 10**6)
        items[sku] = components
    for components in items.values():
        for child in components:
            if child not in items:
                raise ValueError('unknown component')
    # Kahn 拓扑排序：允许共享子件，但拒绝任何环。
    indegree = dict.fromkeys(items, 0)
    for components in items.values():
        for child in components:
            indegree[child] += 1
    ready = sorted(sku for sku in items if indegree[sku] == 0)
    order = []
    while ready:
        sku = ready.pop(0)
        order.append(sku)
        for child in sorted(items[sku]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(order) != len(items):
        raise ValueError('BOM cycle')
    return items


def _line(row, names):
    fields(row, names)
    return text(row['lot_id']), integer(row['quantity'], 1)


def validate_request(request):
    """纯结构校验（不依赖数据库），失败抛 ``ValueError``。"""
    fields(request, 'request_id expected_version operations')
    text(request['request_id'])
    integer(request['expected_version'], 0)
    operations = array(request['operations'], MAX_OPERATIONS)
    if not operations:
        raise ValueError('at least one operation required')
    for operation in operations:
        if not isinstance(operation, dict) or 'op' not in operation \
                or not isinstance(operation['op'], str):
            raise ValueError('operation requires string op')
        kind = operation['op']
        if kind == 'receive':
            fields(operation, 'op lot_id sku quantity')
            text(operation['lot_id'])
            text(operation['sku'])
            integer(operation['quantity'], 1)
        elif kind == 'make':
            fields(operation, 'op lot_id sku quantity inputs')
            text(operation['lot_id'])
            text(operation['sku'])
            integer(operation['quantity'], 1)
            lines = array(operation['inputs'], MAX_LINES)
            if not lines:
                raise ValueError('make requires at least one input')
            seen = set()
            for line in lines:
                lot_id = _line(line, 'lot_id quantity')[0]
                if lot_id in seen:
                    raise ValueError('duplicate input lot')
                seen.add(lot_id)
        elif kind == 'ship':
            fields(operation, 'op shipment_id customer_id sku quantity allocations')
            text(operation['shipment_id'])
            text(operation['customer_id'])
            text(operation['sku'])
            integer(operation['quantity'], 1)
            lines = array(operation['allocations'], MAX_LINES)
            if not lines:
                raise ValueError('ship requires at least one allocation')
            seen = set()
            for line in lines:
                lot_id = _line(line, 'lot_id quantity')[0]
                if lot_id in seen:
                    raise ValueError('duplicate allocation lot')
                seen.add(lot_id)
        elif kind == 'recall':
            fields(operation, 'op recall_id lot_id')
            text(operation['recall_id'])
            text(operation['lot_id'])
        elif kind == 'close_recall':
            fields(operation, 'op recall_id')
            text(operation['recall_id'])
        else:
            raise ValueError('unknown op: ' + kind)
    return request


def _connect_initialized(db):
    """打开已初始化数据库；缺失路径绝不隐式建库。"""
    path = Path(db)
    if not path.is_file():
        raise ValueError('database not initialized: ' + str(path))
    conn = sqlite3.connect(path)
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if not found:
            raise ValueError('database not initialized: ' + str(path))
    except sqlite3.DatabaseError as error:
        conn.close()
        raise ValueError('unreadable database: ' + str(error))
    return conn


def _load_meta(conn):
    rows = dict(conn.execute('SELECT key, value FROM meta').fetchall())
    if not {'version', 'master'} <= set(rows):
        raise ValueError('database not initialized')
    return {'version': int(rows['version']), 'master': json.loads(rows['master'])}


def _load_state(conn):
    lots = {}
    for lot_id, sku, produced, remaining, seq in conn.execute(
            'SELECT lot_id, sku, produced_quantity, remaining_quantity, seq '
            'FROM lots'):
        lots[lot_id] = {'lot_id': lot_id, 'sku': sku,
                        'produced_quantity': produced,
                        'remaining_quantity': remaining, 'inputs': {},
                        'seq': seq}
    for lot_id, input_lot_id, quantity in conn.execute(
            'SELECT lot_id, input_lot_id, quantity FROM lot_inputs'):
        lots[lot_id]['inputs'][input_lot_id] = quantity
    shipments = {}
    for shipment_id, customer_id, sku, quantity in conn.execute(
            'SELECT shipment_id, customer_id, sku, quantity FROM shipments'):
        shipments[shipment_id] = {'shipment_id': shipment_id,
                                  'customer_id': customer_id, 'sku': sku,
                                  'quantity': quantity, 'allocations': {}}
    for shipment_id, lot_id, quantity in conn.execute(
            'SELECT shipment_id, lot_id, quantity FROM shipment_allocations'):
        shipments[shipment_id]['allocations'][lot_id] = quantity
    recalls = {}
    for recall_id, lot_id, status in conn.execute(
            'SELECT recall_id, lot_id, status FROM recalls'):
        recalls[recall_id] = {'recall_id': recall_id, 'lot_id': lot_id,
                              'status': status}
    return {'lots': lots, 'shipments': shipments, 'recalls': recalls,
            'next_seq': max((lot['seq'] for lot in lots.values()), default=-1) + 1,
            'blocked': None}


def _blocked_map(state):
    """按批次创建顺序传播 active 召回，返回 ``lot_id -> {recall_id}``。

    创建顺序天然是拓扑序：make 的每个输入批次都先于成品存在。因此一次
    线性扫描即可把每个输入祖先的召回集合并入成品；不同路径经集合去重
    后同一 ``recall_id`` 只计一次，与余额、是否已发货完全无关。
    """
    if state['blocked'] is None:
        roots = {}
        for recall in state['recalls'].values():
            if recall['status'] == 'active':
                roots.setdefault(recall['lot_id'], set()).add(recall['recall_id'])
        blocked = {}
        for lot in sorted(state['lots'].values(), key=lambda row: row['seq']):
            affected = set(roots.get(lot['lot_id'], ()))
            for input_lot in lot['inputs']:
                affected |= blocked[input_lot]
            blocked[lot['lot_id']] = affected
        state['blocked'] = blocked
    return state['blocked']


def _is_blocked(state, lot_id):
    return bool(_blocked_map(state).get(lot_id))


def _build_snapshot(version, state):
    blocked = _blocked_map(state)
    lots = []
    for lot_id in sorted(state['lots']):
        lot = state['lots'][lot_id]
        lots.append({
            'lot_id': lot_id,
            'sku': lot['sku'],
            'produced_quantity': lot['produced_quantity'],
            'remaining_quantity': lot['remaining_quantity'],
            'inputs': [{'lot_id': input_lot, 'quantity': quantity}
                       for input_lot, quantity in sorted(lot['inputs'].items())],
            'blocked_by': sorted(blocked.get(lot_id, ())),
        })
    shipments = []
    for shipment_id in sorted(state['shipments']):
        shipment = state['shipments'][shipment_id]
        shipments.append({
            'shipment_id': shipment_id,
            'customer_id': shipment['customer_id'],
            'sku': shipment['sku'],
            'quantity': shipment['quantity'],
            'allocations': [{'lot_id': lot_id, 'quantity': quantity}
                            for lot_id, quantity in sorted(
                                shipment['allocations'].items())],
        })
    recalls = [dict(state['recalls'][recall_id])
               for recall_id in sorted(state['recalls'])]
    exposures = []
    for recall_id in sorted(state['recalls']):
        if state['recalls'][recall_id]['status'] != 'active':
            continue
        for shipment_id in sorted(state['shipments']):
            shipment = state['shipments'][shipment_id]
            for lot_id, quantity in sorted(shipment['allocations'].items()):
                if recall_id in blocked.get(lot_id, ()):
                    exposures.append({
                        'recall_id': recall_id,
                        'shipment_id': shipment_id,
                        'customer_id': shipment['customer_id'],
                        'sku': shipment['sku'],
                        'lot_id': lot_id,
                        'quantity': quantity,
                    })
    exposures.sort(key=lambda row: (row['recall_id'], row['shipment_id'],
                                    row['lot_id']))
    return {'version': version, 'lots': lots, 'shipments': shipments,
            'recalls': recalls, 'exposures': exposures}


def _receive(state, master, operation):
    lot_id = operation['lot_id']
    sku = operation['sku']
    quantity = operation['quantity']
    if sku not in master:
        raise ValueError('unknown sku: ' + sku)
    if lot_id in state['lots']:
        raise ValueError('lot_id already exists: ' + lot_id)
    if len(state['lots']) >= MAX_IDENTITIES:
        raise ValueError('lot identity limit exceeded')
    state['lots'][lot_id] = {'lot_id': lot_id, 'sku': sku,
                             'produced_quantity': quantity,
                             'remaining_quantity': quantity, 'inputs': {},
                             'seq': state['next_seq']}
    state['next_seq'] += 1
    state['blocked'] = None


def _make(state, master, operation):
    lot_id = operation['lot_id']
    sku = operation['sku']
    quantity = operation['quantity']
    if sku not in master:
        raise ValueError('unknown sku: ' + sku)
    recipe = master[sku]
    if not recipe:
        raise ValueError('make requires a manufactured sku: ' + sku)
    if lot_id in state['lots']:
        raise ValueError('lot_id already exists: ' + lot_id)
    if len(state['lots']) >= MAX_IDENTITIES:
        raise ValueError('lot identity limit exceeded')

    aggregated = {}
    lines = {}
    for line in operation['inputs']:
        input_lot = line['lot_id']
        used = line['quantity']
        lot = state['lots'].get(input_lot)
        if lot is None:
            raise ValueError('unknown input lot: ' + input_lot)
        if _is_blocked(state, input_lot):
            raise ValueError('input lot blocked by an active recall: ' + input_lot)
        if lot['remaining_quantity'] < used:
            raise ValueError('insufficient input balance: ' + input_lot)
        aggregated[lot['sku']] = aggregated.get(lot['sku'], 0) + used
        lines[input_lot] = used

    expected = {component: quantity * per_unit
                for component, per_unit in recipe.items()}
    if aggregated != expected:
        raise ValueError('inputs do not match the item recipe')

    # 所有输入同时扣减，并创建完工批次及永久实际用料谱系。
    for input_lot, used in lines.items():
        state['lots'][input_lot]['remaining_quantity'] -= used
    state['lots'][lot_id] = {'lot_id': lot_id, 'sku': sku,
                             'produced_quantity': quantity,
                             'remaining_quantity': quantity,
                             'inputs': dict(lines),
                             'seq': state['next_seq']}
    state['next_seq'] += 1
    # 每个输入都已确认未被召回；新批次因此不可能继承召回，缓存仍有效。
    _blocked_map(state)[lot_id] = set()


def _ship(state, operation):
    shipment_id = operation['shipment_id']
    sku = operation['sku']
    quantity = operation['quantity']
    if shipment_id in state['shipments']:
        raise ValueError('shipment_id already exists: ' + shipment_id)
    if len(state['shipments']) >= MAX_IDENTITIES:
        raise ValueError('shipment identity limit exceeded')
    allocations = {}
    total = 0
    for line in operation['allocations']:
        lot_id = line['lot_id']
        used = line['quantity']
        lot = state['lots'].get(lot_id)
        if lot is None:
            raise ValueError('unknown allocation lot: ' + lot_id)
        if lot['sku'] != sku:
            raise ValueError('allocation lot sku mismatch: ' + lot_id)
        if _is_blocked(state, lot_id):
            raise ValueError('allocation lot blocked by an active recall: ' + lot_id)
        if lot['remaining_quantity'] < used:
            raise ValueError('insufficient allocation balance: ' + lot_id)
        allocations[lot_id] = used
        total += used
    if total != quantity:
        raise ValueError('allocations do not sum to shipment quantity')
    for lot_id, used in allocations.items():
        state['lots'][lot_id]['remaining_quantity'] -= used
    state['shipments'][shipment_id] = {
        'shipment_id': shipment_id, 'customer_id': operation['customer_id'],
        'sku': sku, 'quantity': quantity, 'allocations': allocations}


def _recall(state, operation):
    recall_id = operation['recall_id']
    lot_id = operation['lot_id']
    if recall_id in state['recalls']:
        raise ValueError('recall_id already exists: ' + recall_id)
    if len(state['recalls']) >= MAX_IDENTITIES:
        raise ValueError('recall identity limit exceeded')
    if lot_id not in state['lots']:
        raise ValueError('unknown recall lot: ' + lot_id)
    state['recalls'][recall_id] = {'recall_id': recall_id, 'lot_id': lot_id,
                                   'status': 'active'}
    state['blocked'] = None


def _close_recall(state, operation):
    recall_id = operation['recall_id']
    recall = state['recalls'].get(recall_id)
    if recall is None or recall['status'] != 'active':
        raise ValueError('close_recall requires an active recall: ' + recall_id)
    recall['status'] = 'closed'
    state['blocked'] = None


def init_db(db, master):
    """创建全新实物账数据库；路径已存在（含空文件/符号链接）即拒绝。"""
    path = Path(db)
    if path.exists() or path.is_symlink():
        raise ValueError('database already exists: ' + str(path))
    validate_master(master)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        conn.execute('INSERT INTO meta (key, value) VALUES (?, ?)',
                     ('version', '0'))
        conn.execute('INSERT INTO meta (key, value) VALUES (?, ?)',
                     ('master', _canonical(master)))
        conn.commit()
    except Exception:
        if conn is not None:
            conn.close()
        if path.exists():
            path.unlink()
        raise ValueError('initialization failed')
    else:
        conn.close()
    return snapshot(db)


def apply_batch(db, request):
    """按数组顺序提交一个批次；成功返回提交时快照，失败整体不留痕。"""
    validate_request(request)
    payload = _canonical(request)
    conn = _connect_initialized(db)
    try:
        meta = _load_meta(conn)
        state = _load_state(conn)
        # 成功重放优先于 expected_version 检查；同 ID 异内容即冲突。
        stored = conn.execute(
            'SELECT payload, response FROM requests WHERE request_id = ?',
            (request['request_id'],)).fetchone()
        if stored is not None:
            if stored[0] != payload:
                raise ValueError('request_id reused with different payload')
            return json.loads(stored[1])
        if request['expected_version'] != meta['version']:
            raise ValueError('expected_version does not match current version')

        master = validate_master(meta['master'])
        for operation in request['operations']:
            kind = operation['op']
            if kind == 'receive':
                _receive(state, master, operation)
            elif kind == 'make':
                _make(state, master, operation)
            elif kind == 'ship':
                _ship(state, operation)
            elif kind == 'recall':
                _recall(state, operation)
            else:  # close_recall（结构校验已限定）
                _close_recall(state, operation)

        version = meta['version'] + 1
        new_snapshot = _build_snapshot(version, state)
        response_text = json.dumps(new_snapshot, sort_keys=True,
                                   ensure_ascii=False, allow_nan=False)
        try:
            with conn:
                for table in ('lot_inputs', 'lots', 'shipment_allocations',
                              'shipments', 'recalls'):
                    conn.execute('DELETE FROM ' + table)
                conn.executemany(
                    'INSERT INTO lots (lot_id, sku, produced_quantity,'
                    ' remaining_quantity, seq) VALUES (?, ?, ?, ?, ?)',
                    [(lot['lot_id'], lot['sku'], lot['produced_quantity'],
                      lot['remaining_quantity'], lot['seq'])
                     for lot in state['lots'].values()])
                conn.executemany(
                    'INSERT INTO lot_inputs (lot_id, input_lot_id, quantity)'
                    ' VALUES (?, ?, ?)',
                    [(lot_id, input_lot, quantity)
                     for lot_id, lot in state['lots'].items()
                     for input_lot, quantity in lot['inputs'].items()])
                conn.executemany(
                    'INSERT INTO shipments (shipment_id, customer_id, sku,'
                    ' quantity) VALUES (?, ?, ?, ?)',
                    [(row['shipment_id'], row['customer_id'], row['sku'],
                      row['quantity'])
                     for row in state['shipments'].values()])
                conn.executemany(
                    'INSERT INTO shipment_allocations (shipment_id, lot_id,'
                    ' quantity) VALUES (?, ?, ?)',
                    [(shipment_id, lot_id, quantity)
                     for shipment_id, row in state['shipments'].items()
                     for lot_id, quantity in row['allocations'].items()])
                conn.executemany(
                    'INSERT INTO recalls (recall_id, lot_id, status)'
                    ' VALUES (?, ?, ?)',
                    [(row['recall_id'], row['lot_id'], row['status'])
                     for row in state['recalls'].values()])
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'version'",
                    (str(version),))
                conn.execute(
                    'INSERT INTO requests (request_id, payload, response)'
                    ' VALUES (?, ?, ?)',
                    (request['request_id'], payload, response_text))
        except sqlite3.Error as error:
            raise ValueError('batch commit failed: ' + str(error))
        return new_snapshot
    finally:
        conn.close()


def snapshot(db):
    """返回当前已提交状态的完整快照（永远按当前召回与余额计算）。"""
    conn = _connect_initialized(db)
    try:
        meta = _load_meta(conn)
        state = _load_state(conn)
        return _build_snapshot(meta['version'], state)
    finally:
        conn.close()
