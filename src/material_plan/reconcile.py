"""采购到货核销（SQLite，单操作者，标准库）。

仓库已用 material-trace 记录真实收到的批次，但实物账没有采购单行信息；
同一供应商的两张采购单可能同时到货，送货单只写料号。本模块按收货与验收
证据核对哪些采购行可能已履约，并为财务记录每行已授权放行的合格品数量。
证据不能唯一区分采购单时绝不替调用方挑选匹配：每个上下限都按同一份证据
下所有完整合法分配取值；已放行量在重导证据时不得被悄悄改归其他采购行。

存储为独立本地 SQLite 文件；``db`` 接受 ``str`` 或 ``pathlib.Path``。
成功返回报告或凭证字典，失败抛 ``ValueError`` 且不留下任何部分变化。
"""
import json
import sqlite3
from collections import deque
from pathlib import Path

from .common import day, fields, text, integer, array

MAX_ORDERS = 10000
MAX_ARRIVALS = 10000
MAX_BINDINGS = 10000
MAX_APPROVAL_LINES = 10000
MAX_LOTS = 10000
MAX_LOT_INPUTS = 100
MAX_SHIPMENTS = 10000
MAX_RECALLS = 10000
MAX_EXPOSURES = 100000

_SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE order_lines (
    line_id TEXT PRIMARY KEY,
    supplier TEXT NOT NULL,
    sku TEXT NOT NULL,
    earliest TEXT NOT NULL,
    latest TEXT NOT NULL,
    ordered_quantity INTEGER NOT NULL
);
CREATE TABLE arrivals (
    lot_id TEXT PRIMARY KEY,
    supplier TEXT NOT NULL,
    sku TEXT NOT NULL,
    received_quantity INTEGER NOT NULL,
    received_on TEXT NOT NULL,
    accepted_quantity INTEGER NOT NULL
);
CREATE TABLE bindings (
    lot_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    portion TEXT NOT NULL CHECK (portion IN ('accepted', 'other')),
    quantity INTEGER NOT NULL,
    PRIMARY KEY (lot_id, line_id, portion)
);
CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    voucher TEXT NOT NULL
);
CREATE TABLE released (
    line_id TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL
);
"""


def _canonical(value):
    """键序无关的稳定 JSON 序列化（数组顺序保持不变）。"""
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def validate_orders(document):
    """校验 ``{"orders": [...]}``，返回 ``line_id -> 采购行``（日期为 date）。"""
    fields(document, 'orders')
    lines = {}
    for row in array(document['orders'], MAX_ORDERS):
        fields(row, 'line_id supplier sku earliest latest ordered_quantity')
        line_id = text(row['line_id'])
        if line_id in lines:
            raise ValueError('duplicate line_id: ' + line_id)
        earliest = day(row['earliest'])
        latest = day(row['latest'])
        if latest < earliest:
            raise ValueError('latest before earliest: ' + line_id)
        lines[line_id] = {'line_id': line_id,
                          'supplier': text(row['supplier']),
                          'sku': text(row['sku']),
                          'earliest': earliest,
                          'latest': latest,
                          'ordered_quantity':
                              integer(row['ordered_quantity'], 1)}
    return lines


def _validate_physical(value):
    """校验实物快照结构并返回 ``lot_id -> lot``；不核对谱系与余额。"""
    fields(value, 'version lots shipments recalls exposures')
    integer(value['version'], 0)
    lots = {}
    for row in array(value['lots'], MAX_LOTS):
        fields(row, 'lot_id sku produced_quantity remaining_quantity'
                    ' inputs blocked_by')
        lot_id = text(row['lot_id'])
        if lot_id in lots:
            raise ValueError('duplicate lot_id: ' + lot_id)
        produced = integer(row['produced_quantity'], 1)
        integer(row['remaining_quantity'], 0)
        inputs = array(row['inputs'], MAX_LOT_INPUTS)
        for line in inputs:
            fields(line, 'lot_id quantity')
            text(line['lot_id'])
            integer(line['quantity'], 1)
        for recall_id in array(row['blocked_by'], MAX_RECALLS):
            text(recall_id)
        lots[lot_id] = {'sku': text(row['sku']), 'produced': produced,
                        'receipt': not inputs}
    array(value['shipments'], MAX_SHIPMENTS)
    array(value['recalls'], MAX_RECALLS)
    array(value['exposures'], MAX_EXPOSURES)
    return lots


def validate_evidence(evidence):
    """纯结构校验（不依赖数据库），返回 ``(arrivals, bindings)``。"""
    fields(evidence, 'expected_version physical arrivals bindings')
    integer(evidence['expected_version'], 0)
    lots = _validate_physical(evidence['physical'])
    arrivals = {}
    for row in array(evidence['arrivals'], MAX_ARRIVALS):
        fields(row, 'lot_id supplier received_on accepted_quantity')
        lot_id = text(row['lot_id'])
        if lot_id in arrivals:
            raise ValueError('duplicate arrival lot: ' + lot_id)
        supplier = text(row['supplier'])
        received_on = day(row['received_on'])
        accepted = integer(row['accepted_quantity'], 0)
        lot = lots.get(lot_id)
        if lot is None:
            raise ValueError('arrival references unknown lot: ' + lot_id)
        if not lot['receipt']:
            raise ValueError('arrival lot is not an external receipt: '
                             + lot_id)
        if accepted > lot['produced']:
            raise ValueError('accepted quantity exceeds received quantity: '
                             + lot_id)
        arrivals[lot_id] = {'lot_id': lot_id, 'supplier': supplier,
                            'sku': lot['sku'], 'received': lot['produced'],
                            'received_on': received_on, 'accepted': accepted}
    bindings = []
    seen = set()
    for row in array(evidence['bindings'], MAX_BINDINGS):
        fields(row, 'lot_id line_id portion quantity')
        lot_id = text(row['lot_id'])
        line_id = text(row['line_id'])
        portion = row['portion']
        if portion not in ('accepted', 'other'):
            raise ValueError('invalid portion')
        quantity = integer(row['quantity'], 1)
        key = (lot_id, line_id, portion)
        if key in seen:
            raise ValueError('duplicate binding')
        seen.add(key)
        bindings.append({'lot_id': lot_id, 'line_id': line_id,
                         'portion': portion, 'quantity': quantity})
    return arrivals, bindings


def validate_approval(approval):
    """纯结构校验（不依赖数据库），返回按 ``line_id`` 排序的放行行。"""
    fields(approval, 'approval_id expected_version lines')
    text(approval['approval_id'])
    integer(approval['expected_version'], 0)
    rows = array(approval['lines'], MAX_APPROVAL_LINES)
    if not rows:
        raise ValueError('at least one line required')
    seen = set()
    normalized = []
    for row in rows:
        fields(row, 'line_id quantity')
        line_id = text(row['line_id'])
        if line_id in seen:
            raise ValueError('duplicate approval line: ' + line_id)
        seen.add(line_id)
        normalized.append({'line_id': line_id,
                           'quantity': integer(row['quantity'], 1)})
    normalized.sort(key=lambda row: row['line_id'])
    return normalized


def _min_cost_flow(graph, source, sink, need):
    """沿最短路增广（SPFA），把 ``need`` 单位流量以最小费用送达。

    初始残量图是按层有向无环图，不含负环；每次沿最小费用路增广保持该
    不变式，因此结果为该流量下的精确最小费用。不可行时返回 ``None``。
    容量为任意大的整数，按瓶颈整段增广，不按单件展开。
    """
    flow = 0
    cost = 0
    size = len(graph)
    while flow < need:
        dist = [None] * size
        prev = [None] * size
        in_queue = [False] * size
        dist[source] = 0
        queue = deque([source])
        in_queue[source] = True
        while queue:
            node = queue.popleft()
            in_queue[node] = False
            for index, edge in enumerate(graph[node]):
                if edge[1] <= 0:
                    continue
                candidate = dist[node] + edge[2]
                if dist[edge[0]] is None or candidate < dist[edge[0]]:
                    dist[edge[0]] = candidate
                    prev[edge[0]] = (node, index)
                    if not in_queue[edge[0]]:
                        queue.append(edge[0])
                        in_queue[edge[0]] = True
        if prev[sink] is None:
            return None
        bottleneck = need - flow
        node = sink
        while node != source:
            parent, index = prev[node]
            bottleneck = min(bottleneck, graph[parent][index][1])
            node = parent
        node = sink
        while node != source:
            parent, index = prev[node]
            edge = graph[parent][index]
            edge[1] -= bottleneck
            graph[node][edge[3]][1] += bottleneck
            cost += bottleneck * edge[2]
            node = parent
        flow += bottleneck
    return cost


def _group_flow(supplies, caps, eligible, need, target, cost_a, cost_o):
    """一个（供应商、SKU）组内的一次最小费用流。

    ``supplies`` 按收货日给出 ``(合格余量, 其他余量)``；``caps`` 为各采购行
    剩余额度；``eligible[i][j]`` 表示第 i 个收货日可入第 j 行。进入目标行
    ``target`` 的合格边费用为 ``cost_a``、其他边费用为 ``cost_o``，其余边
    费用为 0；``target`` 为 ``None`` 时全部费用为 0（只做可行性判定）。
    """
    dates = len(supplies)
    lines = len(caps)
    graph = [[] for _ in range(2 + 2 * dates + lines)]

    def add(tail, head, capacity, cost):
        graph[tail].append([head, capacity, cost, len(graph[head])])
        graph[head].append([tail, 0, -cost, len(graph[tail]) - 1])

    for i, (accepted, other) in enumerate(supplies):
        add(0, 2 + 2 * i, accepted, 0)
        add(0, 3 + 2 * i, other, 0)
        for j in range(lines):
            if eligible[i][j]:
                add(2 + 2 * i, 2 + 2 * dates + j, need,
                    cost_a if j == target else 0)
                add(3 + 2 * i, 2 + 2 * dates + j, need,
                    cost_o if j == target else 0)
    for j in range(lines):
        add(2 + 2 * dates + j, 1, caps[j], 0)
    return _min_cost_flow(graph, 0, 1, need)


def _compute_bounds(lines, arrivals, bindings):
    """逐行 ``t_min/t_max/a_min/a_max``（收货与合格收货的可能端点）。

    认领先作为基线预扣（认领是下界而非精确归属，余量仍可入同一行）；
    余量按（供应商、SKU、收货日）汇成供应节点，按（供应商、SKU）分组。
    每个端点都在“全部供应必须分完”的完整合法分配集合上取精确最优——
    逐行四个端点分别用一次最小费用流求取，不是逐行独立统计，也不是
    任意一次匹配的结果。找不到任何完整分配时抛 ``ValueError``。
    """
    base_total = {line_id: 0 for line_id in lines}
    base_accepted = {line_id: 0 for line_id in lines}
    capacity = {line_id: line['ordered_quantity']
                for line_id, line in lines.items()}
    supply_a = {}
    supply_o = {}
    for arrival in arrivals.values():
        key = (arrival['supplier'], arrival['sku'], arrival['received_on'])
        supply_a[key] = supply_a.get(key, 0) + arrival['accepted']
        supply_o[key] = supply_o.get(key, 0) \
            + arrival['received'] - arrival['accepted']
    for binding in bindings:
        arrival = arrivals[binding['lot_id']]
        key = (arrival['supplier'], arrival['sku'], arrival['received_on'])
        if binding['portion'] == 'accepted':
            supply_a[key] -= binding['quantity']
            base_accepted[binding['line_id']] += binding['quantity']
            if supply_a[key] < 0:
                raise ValueError('bindings exceed lot accepted quantity: '
                                 + binding['lot_id'])
        else:
            supply_o[key] -= binding['quantity']
            if supply_o[key] < 0:
                raise ValueError('bindings exceed lot other quantity: '
                                 + binding['lot_id'])
        base_total[binding['line_id']] += binding['quantity']
        capacity[binding['line_id']] -= binding['quantity']
        if capacity[binding['line_id']] < 0:
            raise ValueError('bindings exceed ordered quantity: '
                             + binding['line_id'])
    groups = sorted(
        {(supplier, sku) for supplier, sku, _ in supply_a}
        | {(line['supplier'], line['sku']) for line in lines.values()})
    bounds = {line_id: {'t_min': 0, 't_max': 0, 'a_min': 0, 'a_max': 0}
              for line_id in lines}
    for supplier, sku in groups:
        dates = sorted({received_on for s, k, received_on in supply_a
                        if s == supplier and k == sku})
        group_lines = [line_id for line_id in sorted(lines)
                       if lines[line_id]['supplier'] == supplier
                       and lines[line_id]['sku'] == sku]
        supplies = [(supply_a[(supplier, sku, received_on)],
                     supply_o[(supplier, sku, received_on)])
                    for received_on in dates]
        need = sum(accepted + other for accepted, other in supplies)
        if not need:
            continue
        caps = [capacity[line_id] for line_id in group_lines]
        eligible = [[lines[line_id]['earliest'] <= received_on
                     <= lines[line_id]['latest']
                     for line_id in group_lines] for received_on in dates]
        if _group_flow(supplies, caps, eligible, need, None, 0, 0) is None:
            raise ValueError('evidence admits no complete allocation')
        for index, line_id in enumerate(group_lines):
            row = bounds[line_id]
            row['t_min'] = _group_flow(supplies, caps, eligible, need,
                                       index, 1, 1)
            row['t_max'] = -_group_flow(supplies, caps, eligible, need,
                                        index, -1, -1)
            row['a_min'] = _group_flow(supplies, caps, eligible, need,
                                       index, 1, 0)
            row['a_max'] = -_group_flow(supplies, caps, eligible, need,
                                        index, -1, 0)
    for line_id, row in bounds.items():
        row['t_min'] += base_total[line_id]
        row['t_max'] += base_total[line_id]
        row['a_min'] += base_accepted[line_id]
        row['a_max'] += base_accepted[line_id]
    return bounds


def _check_bindings(bindings, arrivals, lines):
    """认领必须指向已纳入批次与已存在采购行，且不跨供应商/SKU/窗口。

    同一批次同一部分的认领合计不得超过该批该类数量——必须按批核对，
    不能等供应按收货日合并后再看，否则同日的其他批次会掩盖超认。
    """
    claimed = {}
    for binding in bindings:
        arrival = arrivals.get(binding['lot_id'])
        if arrival is None:
            raise ValueError('binding references unknown arrival lot: '
                             + binding['lot_id'])
        line = lines.get(binding['line_id'])
        if line is None:
            raise ValueError('binding references unknown purchase line: '
                             + binding['line_id'])
        if arrival['supplier'] != line['supplier']:
            raise ValueError('binding crosses supplier: ' + binding['lot_id'])
        if arrival['sku'] != line['sku']:
            raise ValueError('binding crosses sku: ' + binding['lot_id'])
        if not line['earliest'] <= arrival['received_on'] <= line['latest']:
            raise ValueError('binding outside receipt window: '
                             + binding['lot_id'])
        pool = arrival['accepted'] if binding['portion'] == 'accepted' \
            else arrival['received'] - arrival['accepted']
        key = (binding['lot_id'], binding['portion'])
        claimed[key] = claimed.get(key, 0) + binding['quantity']
        if claimed[key] > pool:
            raise ValueError('bindings exceed lot portion quantity: '
                             + binding['lot_id'])


def _connect_initialized(db):
    """打开已初始化核销数据库；缺失路径绝不隐式建库。"""
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
    if kind is None or kind[0] != 'material-reconcile':
        conn.close()
        raise ValueError('database not initialized: ' + str(path))
    return conn


def _load_meta(conn):
    rows = dict(conn.execute('SELECT key, value FROM meta').fetchall())
    if 'version' not in rows:
        raise ValueError('database not initialized')
    return {'version': int(rows['version'])}


def _load_lines(conn):
    lines = {}
    for line_id, supplier, sku, earliest, latest, quantity in conn.execute(
            'SELECT line_id, supplier, sku, earliest, latest,'
            ' ordered_quantity FROM order_lines'):
        lines[line_id] = {'line_id': line_id, 'supplier': supplier,
                          'sku': sku, 'earliest': day(earliest),
                          'latest': day(latest), 'ordered_quantity': quantity}
    return lines


def _load_arrivals(conn):
    arrivals = {}
    for row in conn.execute(
            'SELECT lot_id, supplier, sku, received_quantity, received_on,'
            ' accepted_quantity FROM arrivals'):
        lot_id, supplier, sku, received, received_on, accepted = row
        arrivals[lot_id] = {'lot_id': lot_id, 'supplier': supplier,
                            'sku': sku, 'received': received,
                            'received_on': day(received_on),
                            'accepted': accepted}
    return arrivals


def _load_bindings(conn):
    return [{'lot_id': lot_id, 'line_id': line_id, 'portion': portion,
             'quantity': quantity}
            for lot_id, line_id, portion, quantity in conn.execute(
                'SELECT lot_id, line_id, portion, quantity FROM bindings')]


def _load_released(conn):
    return {line_id: quantity for line_id, quantity in conn.execute(
        'SELECT line_id, quantity FROM released')}


def _load_vouchers(conn):
    return {approval_id: json.loads(voucher)
            for approval_id, voucher in conn.execute(
                'SELECT approval_id, voucher FROM approvals')}


def _build_report(version, lines, bounds, released, vouchers):
    rows = []
    for line_id in sorted(lines):
        row = bounds[line_id]
        total_released = released.get(line_id, 0)
        rows.append({'line_id': line_id,
                     'received_min': row['t_min'],
                     'received_max': row['t_max'],
                     'accepted_min': row['a_min'],
                     'accepted_max': row['a_max'],
                     'released': total_released,
                     'releasable': row['a_min'] - total_released})
    return {'version': version, 'lines': rows,
            'approvals': [vouchers[key] for key in sorted(vouchers)]}


def _current_report(conn):
    meta = _load_meta(conn)
    lines = _load_lines(conn)
    bounds = _compute_bounds(lines, _load_arrivals(conn),
                             _load_bindings(conn))
    return _build_report(meta['version'], lines, bounds,
                         _load_released(conn), _load_vouchers(conn))


def init_db(db, orders):
    """创建全新核销数据库；路径已存在（含空文件/符号链接）即拒绝。"""
    path = Path(db)
    if path.exists() or path.is_symlink():
        raise ValueError('database already exists: ' + str(path))
    lines = validate_orders(orders)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        conn.execute('INSERT INTO meta (key, value) VALUES (?, ?)',
                     ('kind', 'material-reconcile'))
        conn.execute('INSERT INTO meta (key, value) VALUES (?, ?)',
                     ('version', '0'))
        conn.executemany(
            'INSERT INTO order_lines (line_id, supplier, sku, earliest,'
            ' latest, ordered_quantity) VALUES (?, ?, ?, ?, ?, ?)',
            [(line['line_id'], line['supplier'], line['sku'],
              line['earliest'].isoformat(), line['latest'].isoformat(),
              line['ordered_quantity']) for line in lines.values()])
        conn.commit()
    except Exception:
        if conn is not None:
            conn.close()
        if path.exists():
            path.unlink()
        raise ValueError('initialization failed')
    else:
        conn.close()
    return show(db)


def reconcile(db, evidence):
    """提交一份完整的新证据；找不到完整合法分配或不再保证已放行量时拒绝。"""
    arrivals, bindings = validate_evidence(evidence)
    conn = _connect_initialized(db)
    try:
        meta = _load_meta(conn)
        if evidence['expected_version'] != meta['version']:
            raise ValueError('expected_version does not match current version')
        lines = _load_lines(conn)
        # 曾纳入的收货批次必须继续提交，原 SKU 与原收货量不可改写。
        for lot_id, previous in _load_arrivals(conn).items():
            current = arrivals.get(lot_id)
            if current is None:
                raise ValueError('previously reconciled lot missing: '
                                 + lot_id)
            if current['sku'] != previous['sku'] \
                    or current['received'] != previous['received']:
                raise ValueError('receipt history rewritten for lot: '
                                 + lot_id)
        _check_bindings(bindings, arrivals, lines)
        bounds = _compute_bounds(lines, arrivals, bindings)
        # 放行不缩小分配集合：在不带历史放行约束的边界上重核合格下限，
        # 任一行的合格下限低于其累计已放行量时整份更正失败。
        released = _load_released(conn)
        for line_id, row in bounds.items():
            if row['a_min'] < released.get(line_id, 0):
                raise ValueError('evidence no longer guarantees released'
                                 ' quantity: ' + line_id)
        version = meta['version'] + 1
        report = _build_report(version, lines, bounds, released,
                               _load_vouchers(conn))
        try:
            with conn:
                conn.execute('DELETE FROM arrivals')
                conn.execute('DELETE FROM bindings')
                conn.executemany(
                    'INSERT INTO arrivals (lot_id, supplier, sku,'
                    ' received_quantity, received_on, accepted_quantity)'
                    ' VALUES (?, ?, ?, ?, ?, ?)',
                    [(arrival['lot_id'], arrival['supplier'], arrival['sku'],
                      arrival['received'], arrival['received_on'].isoformat(),
                      arrival['accepted']) for arrival in arrivals.values()])
                conn.executemany(
                    'INSERT INTO bindings (lot_id, line_id, portion,'
                    ' quantity) VALUES (?, ?, ?, ?)',
                    [(binding['lot_id'], binding['line_id'],
                      binding['portion'], binding['quantity'])
                     for binding in bindings])
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'version'",
                    (str(version),))
        except sqlite3.Error as error:
            raise ValueError('reconcile commit failed: ' + str(error))
        return report
    finally:
        conn.close()


def approve(db, approval):
    """按当前证据放行合格品数量；成功返回并永久保存本次凭证。"""
    lines = validate_approval(approval)
    payload = _canonical({'approval_id': approval['approval_id'],
                          'expected_version': approval['expected_version'],
                          'lines': lines})
    conn = _connect_initialized(db)
    try:
        # 成功重放优先于 expected_version 检查；同 ID 异内容即冲突。
        stored = conn.execute(
            'SELECT payload, voucher FROM approvals WHERE approval_id = ?',
            (approval['approval_id'],)).fetchone()
        if stored is not None:
            if stored[0] != payload:
                raise ValueError('approval_id reused with different payload')
            return json.loads(stored[1])
        meta = _load_meta(conn)
        if approval['expected_version'] != meta['version']:
            raise ValueError('expected_version does not match current version')
        order_lines = _load_lines(conn)
        for line in lines:
            if line['line_id'] not in order_lines:
                raise ValueError('unknown purchase line: ' + line['line_id'])
        bounds = _compute_bounds(order_lines, _load_arrivals(conn),
                                 _load_bindings(conn))
        released = _load_released(conn)
        for line in lines:
            line_id = line['line_id']
            if line['quantity'] > bounds[line_id]['a_min'] \
                    - released.get(line_id, 0):
                raise ValueError('quantity exceeds releasable: ' + line_id)
        version = meta['version'] + 1
        voucher = {'approval_id': approval['approval_id'],
                   'expected_version': approval['expected_version'],
                   'committed_version': version,
                   'lines': lines}
        try:
            with conn:
                conn.execute(
                    'INSERT INTO approvals (approval_id, payload, voucher)'
                    ' VALUES (?, ?, ?)',
                    (approval['approval_id'], payload, _canonical(voucher)))
                for line in lines:
                    conn.execute(
                        'INSERT OR REPLACE INTO released (line_id, quantity)'
                        ' VALUES (?, ?)',
                        (line['line_id'],
                         released.get(line['line_id'], 0)
                         + line['quantity']))
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'version'",
                    (str(version),))
        except sqlite3.Error as error:
            raise ValueError('approve commit failed: ' + str(error))
        return voucher
    finally:
        conn.close()


def show(db):
    """返回当前版本、逐采购行核对结果与全部已入库凭证。"""
    conn = _connect_initialized(db)
    try:
        return _current_report(conn)
    finally:
        conn.close()
