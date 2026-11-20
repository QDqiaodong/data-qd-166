"""跨进程可持久化的客户订单承诺簿（SQLite，单操作者，标准库）。

预约（reserve）/撤销（cancel）以批次为单位提交；每个操作后的全部 active
订单都作为同一次净算的 demands，复用固定主数据与 material_plan.planning.plan
的净算口径。任何新计划订单 release_date 早于 as_of 即整批拒绝。
"""
import json
import sqlite3
from pathlib import Path

from .common import fields, text, integer, array
from .planning import plan, day

MAX_ORDERS = 10000
MAX_OPERATIONS = 100
HORIZON_DAYS = 3650

_SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    date TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'cancelled'))
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


def validate_seed(seed):
    """按 demands=[] 的原净算契约校验初始化主数据，失败抛 ValueError。"""
    fields(seed, 'as_of items receipts')
    array(seed['items'], 100)
    array(seed['receipts'], 10000)
    plan({'as_of': seed['as_of'], 'items': seed['items'],
          'demands': [], 'receipts': seed['receipts']})
    return seed


def validate_request(request):
    """纯结构校验（不依赖数据库），失败抛 ValueError。"""
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
        if kind == 'reserve':
            fields(operation, 'op order_id sku date quantity')
            text(operation['order_id'])
            text(operation['sku'])
            day(operation['date'])
            integer(operation['quantity'], 1)
        elif kind == 'cancel':
            fields(operation, 'op order_id')
            text(operation['order_id'])
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
    rows = conn.execute('SELECT key, value FROM meta').fetchall()
    table = dict(rows)
    if 'version' not in table or 'seed' not in table:
        raise ValueError('database not initialized')
    return {'version': int(table['version']), 'seed': json.loads(table['seed'])}


def _load_orders(conn):
    return {row[0]: {'order_id': row[0], 'sku': row[1], 'date': row[2],
                     'quantity': row[3], 'status': row[4]}
            for row in conn.execute(
                'SELECT order_id, sku, date, quantity, status FROM orders')}


def _run_netting(meta, state):
    """以全部 active 订单为 demands 跑一次原口径净算。"""
    demands = [{'id': order['order_id'], 'sku': order['sku'],
                'date': order['date'], 'quantity': order['quantity']}
               for order in state.values() if order['status'] == 'active']
    seed = meta['seed']
    return plan({'as_of': seed['as_of'], 'items': seed['items'],
                 'demands': demands, 'receipts': seed['receipts']})


def _build_snapshot(meta, state):
    orders = [{'order_id': order['order_id'], 'sku': order['sku'],
               'date': order['date'], 'quantity': order['quantity'],
               'status': order['status']}
              for order in sorted(state.values(), key=lambda o: o['order_id'])]
    return {'version': meta['version'], 'orders': orders,
            'plan': _run_netting(meta, state)}


def init_db(db, seed):
    """创建全新的承诺簿数据库；路径已存在（含空文件/符号链接）即拒绝。"""
    path = Path(db)
    if path.exists() or path.is_symlink():
        raise ValueError('database already exists: ' + str(path))
    validate_seed(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        conn.execute('INSERT INTO meta (key, value) VALUES (?, ?)',
                     ('version', '0'))
        conn.execute('INSERT INTO meta (key, value) VALUES (?, ?)',
                     ('seed', _canonical(seed)))
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

        seed = meta['seed']
        as_of = day(seed['as_of'])
        items = {row['sku'] for row in seed['items']}
        state = _load_orders(conn)

        # 先在内存中模拟全部操作并逐次净算；任何失败都发生在写库之前。
        for operation in request['operations']:
            kind = operation['op']
            order_id = operation['order_id']
            if kind == 'reserve':
                if order_id in state:
                    raise ValueError('order_id already exists: ' + order_id)
                if len(state) >= MAX_ORDERS:
                    raise ValueError('order identity limit exceeded')
                sku = operation['sku']
                if sku not in items:
                    raise ValueError('unknown sku: ' + sku)
                when = day(operation['date'])
                if when < as_of or (when - as_of).days > HORIZON_DAYS:
                    raise ValueError('date outside planning horizon')
                state[order_id] = {'order_id': order_id, 'sku': sku,
                                   'date': when.isoformat(),
                                   'quantity': integer(operation['quantity'], 1),
                                   'status': 'active'}
            else:  # cancel（结构校验已保证只可能是 reserve/cancel）
                if order_id not in state or state[order_id]['status'] != 'active':
                    raise ValueError('cancel requires an active order: ' + order_id)
                state[order_id]['status'] = 'cancelled'

            result = _run_netting(meta, state)
            if any(order['past_due'] for order in result['orders']):
                raise ValueError('planned release earlier than as_of')

        new_snapshot = _build_snapshot(meta, state)
        new_snapshot['version'] = meta['version'] + 1
        response_text = json.dumps(new_snapshot, sort_keys=True,
                                   ensure_ascii=False, allow_nan=False)
        try:
            with conn:
                conn.execute('DELETE FROM orders')
                conn.executemany(
                    'INSERT INTO orders (order_id, sku, date, quantity, status)'
                    ' VALUES (?, ?, ?, ?, ?)',
                    [(o['order_id'], o['sku'], o['date'], o['quantity'],
                      o['status']) for o in new_snapshot['orders']])
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'version'",
                    (str(new_snapshot['version']),))
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
    """返回当前已提交状态的完整快照。"""
    conn = _connect_initialized(db)
    try:
        meta = _load_meta(conn)
        state = _load_orders(conn)
        return _build_snapshot(meta, state)
    finally:
        conn.close()
