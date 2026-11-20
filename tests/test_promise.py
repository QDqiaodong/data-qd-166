"""承诺簿（material_plan.promise）集成测试。

覆盖：验收五场景、共享子件与批量余量、同日到货、过去释放、批次回滚、
身份冲突、重放、旧版本、跨进程恢复与输入边界。
"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from material_plan.promise import init_db, apply_batch, snapshot, MAX_ORDERS
from material_plan.planning import plan

FIX = ROOT / 'fixtures' / 'promise'


def load(name):
    return json.loads((FIX / name).read_text())


def base_seed():
    return load('seed.json')


def reserve(order_id, sku='KIT', date='2026-01-08', quantity=1):
    return {'op': 'reserve', 'order_id': order_id, 'sku': sku,
            'date': date, 'quantity': quantity}


def cancel(order_id):
    return {'op': 'cancel', 'order_id': order_id}


def request(request_id, expected_version, operations):
    return {'request_id': request_id, 'expected_version': expected_version,
            'operations': operations}


class PromiseTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / 'book.db'

    def tearDown(self):
        self._tmp.cleanup()

    def init(self, seed=None):
        return init_db(self.db, copy.deepcopy(seed or base_seed()))

    def apply(self, payload):
        return apply_batch(self.db, copy.deepcopy(payload))

    def snap(self):
        return snapshot(self.db)

    def assert_fails(self, payload, message=None):
        before = self.snap()
        with self.assertRaises(ValueError):
            self.apply(payload)
        self.assertEqual(self.snap(), before)


class InitTests(PromiseTestBase):
    def test_init_snapshot_is_version_zero_without_orders(self):
        snap = self.init()
        self.assertEqual(snap['version'], 0)
        self.assertEqual(snap['orders'], [])
        self.assertEqual(set(snap['plan']), {'orders', 'ledger', 'ending_inventory'})
        # 无订单时计划仍包含原始到货
        seed = base_seed()
        seed['receipts'] = [{'id': 'R1', 'sku': 'BOLT', 'date': '2026-01-08', 'quantity': 2}]
        snap = init_db(self.tmp / 'with_receipt.db', seed)
        row = next(r for r in snap['plan']['ledger'] if r['sku'] == 'BOLT')
        self.assertEqual(row['scheduled_receipt'], 2)
        self.assertEqual(snap['plan']['ending_inventory']['BOLT'], 5)

    def test_init_rejects_existing_path_even_empty_file(self):
        empty = self.tmp / 'empty.db'
        empty.write_bytes(b'')
        with self.assertRaises(ValueError):
            init_db(empty, base_seed())
        self.assertEqual(empty.read_bytes(), b'')

    def test_failed_init_leaves_no_database(self):
        bad = base_seed()
        bad['items'] = []
        path = self.tmp / 'broken.db'
        with self.assertRaises(ValueError):
            init_db(path, bad)
        self.assertFalse(path.exists())
        with self.assertRaises(ValueError):
            snapshot(path)

    def test_seed_uses_netting_contract_with_empty_demands(self):
        bad = base_seed()
        bad['receipts'] = [{'id': 'X', 'sku': 'BOLT', 'date': '2026-01-08', 'quantity': 1},
                           {'id': 'X', 'sku': 'BOLT', 'date': '2026-01-08', 'quantity': 1}]
        with self.assertRaises(ValueError):
            self.init(bad)
        bad = base_seed()
        bad['items'] = [dict(row, sku='KIT') for row in bad['items']]
        bad['items'][1]['sku'] = 'KIT'
        with self.assertRaises(ValueError):
            self.init(bad)

    def test_apply_and_snapshot_require_initialized_db(self):
        missing = self.tmp / 'missing.db'
        with self.assertRaises(ValueError):
            apply_batch(missing, request('R', 0, [reserve('A')]))
        with self.assertRaises(ValueError):
            snapshot(missing)
        self.assertFalse(missing.exists())

    def test_input_objects_are_not_mutated(self):
        seed = base_seed()
        req = request('R', 0, [reserve('A')])
        seed_before, req_before = copy.deepcopy(seed), copy.deepcopy(req)
        self.init(seed)
        self.apply(req)
        self.assertEqual(seed, seed_before)
        self.assertEqual(req, req_before)


class AcceptanceTests(PromiseTestBase):
    def test_reserve_a_then_b_fails_with_version_unchanged(self):
        self.init()
        first = self.apply(load('request_reserve_a.json'))
        self.assertEqual(first['version'], 1)
        self.assertEqual([o['order_id'] for o in first['orders']], ['A'])
        self.assert_fails(load('request_reserve_b_fail.json'))
        self.assertEqual(self.snap()['version'], 1)

    def test_cancel_a_and_reserve_b_in_one_batch(self):
        self.init()
        self.apply(load('request_reserve_a.json'))
        snap = self.apply(load('request_cancel_a_reserve_b.json'))
        self.assertEqual(snap['version'], 2)
        self.assertEqual([(o['order_id'], o['status']) for o in snap['orders']],
                         [('A', 'cancelled'), ('B', 'active')])
        active = [o for o in snap['orders'] if o['status'] == 'active']
        self.assertEqual([o['order_id'] for o in active], ['B'])

    def test_rollback_batch_preserves_a_and_frees_identities(self):
        self.init()
        self.apply(load('request_reserve_a.json'))
        self.assert_fails(load('request_rollback.json'))
        snap = self.snap()
        self.assertEqual(snap['version'], 1)
        self.assertEqual([(o['order_id'], o['status']) for o in snap['orders']],
                         [('A', 'active')])
        # 失败不占用 request_id，也不留下前缀身份 B；修正后可重试
        fixed = request('R-ROLLBACK', 1, [cancel('A'), reserve('B')])
        snap = self.apply(fixed)
        self.assertEqual(snap['version'], 2)
        self.assertEqual([o['order_id'] for o in snap['orders']], ['A', 'B'])

    def test_reserve_b_then_cancel_a_fails_despite_feasible_end_state(self):
        self.init()
        self.apply(load('request_reserve_a.json'))
        self.assert_fails(load('request_b_then_a.json'))
        self.assertEqual([o['order_id'] for o in self.snap()['orders']], ['A'])

    def test_reserve_then_cancel_occupies_terminal_identity(self):
        self.init()
        snap = self.apply(request('R1', 0, [reserve('N'), cancel('N')]))
        self.assertEqual(snap['version'], 1)
        self.assertEqual(snap['orders'][0]['status'], 'cancelled')
        self.assert_fails(request('R2', 1, [reserve('N')]))
        self.assert_fails(request('R3', 1, [cancel('N')]))

    def test_version_increments_even_without_active_orders(self):
        self.init()
        snap = self.apply(request('R1', 0, [reserve('N'), cancel('N')]))
        self.assertEqual(snap['version'], 1)
        self.assertEqual([o for o in snap['orders'] if o['status'] == 'active'], [])

    def test_cancelled_identity_cannot_be_re_reserved(self):
        self.init()
        self.apply(load('request_reserve_a.json'))
        self.apply(load('request_cancel_a.json'))
        self.assert_fails(request('R2', 2, [reserve('A')]))

    def test_identity_limit_is_enforced(self):
        self.init()
        # 每批 100 个操作（50 对预约+撤销）产生 50 个终态身份，共 200 批
        batches = MAX_ORDERS // 50
        for batch in range(batches):
            ops = [op for i in range(50)
                   for op in (reserve(f'O{batch:03d}-{i:02d}'),
                              cancel(f'O{batch:03d}-{i:02d}'))]
            self.assertEqual(len(ops), 100)
            self.apply(request(f'RB{batch}', batch, ops))
        self.assertEqual(len(self.snap()['orders']), MAX_ORDERS)
        self.assertEqual(self.snap()['version'], batches)
        self.assert_fails(request('OVER', batches, [reserve('ONE-MORE')]))


class ReplayTests(PromiseTestBase):
    def test_successful_replay_returns_historical_snapshot_without_recompute(self):
        self.init()
        first = self.apply(load('request_reserve_a.json'))
        self.apply(load('request_cancel_a.json'))
        self.assertEqual(self.snap()['version'], 2)
        # 旧请求重放：返回预约当时的版本与计划，版本不再增加
        replay = self.apply(load('request_reserve_a.json'))
        self.assertEqual(replay, first)
        self.assertEqual(replay['version'], 1)
        self.assertEqual(self.snap()['version'], 2)
        current = self.snap()
        self.assertEqual(current['orders'][0]['status'], 'cancelled')

    def test_replay_ignores_json_key_order_but_keeps_array_order(self):
        self.init()
        normal = {'request_id': 'RK', 'expected_version': 0,
                  'operations': [reserve('A')]}
        first = self.apply(normal)
        reordered = {'operations': [{'quantity': 1, 'date': '2026-01-08',
                                     'sku': 'KIT', 'order_id': 'A', 'op': 'reserve'}],
                     'expected_version': 0, 'request_id': 'RK'}
        self.assertEqual(self.apply(reordered), first)
        self.assertEqual(self.snap()['version'], 1)

    def test_same_id_different_quantity_conflicts(self):
        self.init()
        self.apply(load('request_reserve_a.json'))
        changed = load('request_reserve_a.json')
        changed['operations'][0]['quantity'] = 2
        self.assert_fails(changed)

    def test_same_id_different_expected_version_conflicts(self):
        self.init()
        self.apply(load('request_reserve_a.json'))
        changed = load('request_reserve_a.json')
        changed['expected_version'] = 9
        self.assert_fails(changed)

    def test_replay_takes_priority_over_expected_version(self):
        self.init()
        first = self.apply(load('request_reserve_a.json'))
        # 已成功请求自身带的 expected_version=0 早已过期，重放仍成功
        self.assertEqual(self.apply(load('request_reserve_a.json')), first)

    def test_failed_batch_does_not_consume_request_id(self):
        self.init()
        self.apply(load('request_reserve_a.json'))
        self.assert_fails(load('request_reserve_b_fail.json'))
        reuse = request('R-RESERVE-B-FAIL', 1,
                        [cancel('A'), reserve('B')])
        snap = self.apply(reuse)
        self.assertEqual(snap['version'], 2)

    def test_stale_version_new_request_rejected(self):
        self.init()
        self.apply(load('request_reserve_a.json'))
        self.assert_fails(request('STALE', 0, [cancel('A')]))
        self.assertEqual(self.snap()['version'], 1)


class FeasibilityTests(PromiseTestBase):
    def test_release_equal_as_of_accepted_one_day_earlier_rejected(self):
        self.init()
        ok = self.apply(request('EQ', 0,
                                [reserve('A', date='2026-01-08', quantity=1)]))
        self.assertEqual(ok['version'], 1)
        # 需求日早于 as_of：规划范围外
        self.assert_fails(request('EARLY-DATE', 1,
                                  [reserve('C', date='2026-01-07')]))
        # 需求日等于 as_of 但 BOLT 不足：补单释放日为前一天 -> 不可兑现
        self.assert_fails(request('EARLY-RELEASE', 1,
                                  [reserve('D', date='2026-01-08', quantity=2)]))

    def test_unknown_sku_and_out_of_horizon_rejected(self):
        self.init()
        self.assert_fails(request('S', 0, [reserve('A', sku='NOPE')]))
        self.assert_fails(request('H', 0,
                                  [reserve('A', date='2036-01-09')]))

    def test_suffix_failure_leaves_no_prefix_identity(self):
        self.init()
        self.assert_fails(request('PREFIX', 0,
                                  [reserve('A'), reserve('B')]))
        self.assertEqual(self.snap()['orders'], [])
        self.assertEqual(self.snap()['version'], 0)

    def test_illegal_suffix_cancel_leaves_no_trace(self):
        self.init()
        self.apply(load('request_reserve_a.json'))
        self.assert_fails(load('request_rollback.json'))
        self.assertNotIn('B', {o['order_id'] for o in self.snap()['orders']})

    def test_every_intermediate_state_is_checked(self):
        # 单订单可兑现、双订单不可兑现：同批 reserve 两个必须在第二个操作处失败
        self.init()
        self.assert_fails(request('TWO', 0, [reserve('A'), reserve('B')]))


class SharedComponentTests(PromiseTestBase):
    def setUp(self):
        super().setUp()
        self.seed = load('seed_shared.json')
        init_db(self.db, copy.deepcopy(self.seed))

    def test_shared_aggregation_lot_slack_and_same_day_receipt(self):
        snap = self.apply(load('request_shared_demands.json'))
        orders = {(o['sku'], o['receipt_date']): o for o in snap['plan']['orders']}
        # P1 需5、批量4 -> 8（父批量余量3）；P2 需2 -> 2
        self.assertEqual(orders[('P1', '2026-02-02')]['quantity'], 8)
        self.assertEqual(orders[('P2', '2026-02-02')]['quantity'], 2)
        # 共享子件合并后毛需求 8*1+2*2=12；现货1+同日到货3=4，缺口8按批量5上取整为10
        self.assertEqual(orders[('SHARED', '2026-02-02')]['quantity'], 10)
        # SHARED 提前期1，释放 Feb1 -> RAW 毛需求 10*2=20
        self.assertEqual(orders[('RAW', '2026-02-01')]['quantity'], 20)
        for row in snap['plan']['ledger']:
            self.assertEqual(row['opening'] + row['scheduled_receipt']
                             + row['planned_receipt'] - row['gross'],
                             row['closing'])
        # 与直接调用 plan 的结果完全一致
        direct = plan({'as_of': self.seed['as_of'], 'items': self.seed['items'],
                       'receipts': self.seed['receipts'],
                       'demands': [{'id': 'O-P1', 'sku': 'P1',
                                    'date': '2026-02-02', 'quantity': 5},
                                   {'id': 'O-P2', 'sku': 'P2',
                                    'date': '2026-02-02', 'quantity': 2}]})
        self.assertEqual(snap['plan'], direct)

    def test_planned_supply_is_never_fed_back(self):
        first = self.apply(load('request_shared_demands.json'))
        # 再次查询（以及任意后续批次）都只从固定主数据出发，数字不累积
        self.assertEqual(self.snap()['plan'], first['plan'])
        follow = self.apply(request('SECOND', 1, [cancel('O-P2')]))
        direct = plan({'as_of': self.seed['as_of'], 'items': self.seed['items'],
                       'receipts': self.seed['receipts'],
                       'demands': [{'id': 'O-P1', 'sku': 'P1',
                                    'date': '2026-02-02', 'quantity': 5}]})
        self.assertEqual(follow['plan'], direct)

    def test_parent_lot_slack_covers_later_bucket(self):
        seed = copy.deepcopy(self.seed)
        seed['items'][0]['lot_multiple'] = 10  # P1 批量10
        db = self.tmp / 'slack.db'
        init_db(db, seed)
        payload = request('SLACK', 0, [
            reserve('D1', sku='P1', date='2026-02-02', quantity=6),
            reserve('D2', sku='P1', date='2026-02-03', quantity=4)])
        snap = apply_batch(db, payload)
        p1_orders = [o for o in snap['plan']['orders'] if o['sku'] == 'P1']
        # 次日桶的4件被首批10件的余量覆盖，不再补单、不再爆子件
        self.assertEqual(len(p1_orders), 1)
        self.assertEqual(p1_orders[0]['quantity'], 10)
        ledger = [r for r in snap['plan']['ledger'] if r['sku'] == 'P1']
        self.assertEqual([r['closing'] for r in ledger], [4, 0])


class ValidationTests(PromiseTestBase):
    def test_strict_fields_at_every_level(self):
        self.init()
        bad_payloads = [
            {'request_id': 'R', 'expected_version': 0},  # 缺 operations
            {'request_id': 'R', 'expected_version': 0, 'operations': [],
             'extra': 1},                                  # 未知字段
            request('R', 0, [{'op': 'reserve', 'order_id': 'A',
                              'sku': 'KIT', 'date': '2026-01-08',
                              'quantity': 1, 'extra': 0}]),
            request('R', 0, [{'op': 'frobnicate', 'order_id': 'A'}]),
            request('R', 0, [{'op': 'cancel'}]),
            request('R', 0, []),
            request('R', 0, 'not-a-list'),
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                self.assert_fails(payload)

    def test_boolean_quantity_and_version_rejected(self):
        self.init()
        self.assert_fails(request('R', 0,
                                  [reserve('A', quantity=True)]))
        self.assert_fails(request('R', True, [reserve('A')]))
        self.assert_fails(request('R', False, [reserve('A')]))
        self.assert_fails(request('R', -1, [reserve('A')]))
        self.assert_fails(request('R', 1.5, [reserve('A')]))

    def test_identifier_rules_apply_to_request_and_order_ids(self):
        self.init()
        self.assert_fails(request('', 0, [reserve('A')]))
        self.assert_fails(request('R', 0, [reserve(' A')]))
        self.assert_fails(request('R', 0, [reserve('x' * 201)]))

    def test_cancel_requires_active_order(self):
        self.init()
        self.assert_fails(request('R', 0, [cancel('GHOST')]))
        self.apply(request('R1', 0, [reserve('N'), cancel('N')]))
        self.assert_fails(request('R2', 1, [cancel('N')]))

    def test_operation_array_limited_to_100(self):
        self.init()
        self.assert_fails(request('BIG', 0, [cancel('X')] * 101))


class DeterminismTests(PromiseTestBase):
    def test_same_committed_state_produces_identical_json(self):
        self.init()
        self.apply(load('request_reserve_a.json'))
        first = json.dumps(self.snap(), sort_keys=True, ensure_ascii=False)
        second = json.dumps(self.snap(), sort_keys=True, ensure_ascii=False)
        self.assertEqual(first, second)
        self.assertNotIn('timestamp', first)

    def test_apply_response_equals_snapshot_at_commit(self):
        self.init()
        response = self.apply(load('request_reserve_a.json'))
        self.assertEqual(response, self.snap())

    def test_snapshot_orders_sorted_by_order_id(self):
        self.init()
        # Z、A 无法同时 active（BOLT 容量约束），用同批预约+撤销构造身份集合
        payload = request('R', 0, [reserve('M'), cancel('M'),
                                   reserve('A'), cancel('A'),
                                   reserve('Z'), cancel('Z')])
        snap = self.apply(payload)
        self.assertEqual([o['order_id'] for o in snap['orders']],
                         ['A', 'M', 'Z'])
        for order in snap['orders']:
            self.assertEqual(set(order),
                             {'order_id', 'sku', 'date', 'quantity', 'status'})


class CliTests(PromiseTestBase):
    def invoke(self, *args, source=None):
        cmd = [sys.executable, '-m', 'material_plan.promise_cli', *args]
        env = dict(__import__('os').environ,
                   PYTHONPATH=str(ROOT / 'src'))
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=30, env=env)

    def write(self, name, value):
        path = self.tmp / name
        if isinstance(value, (dict, list)):
            path.write_text(json.dumps(value))
        else:
            path.write_text(value)
        return path

    def test_init_apply_snapshot_cli_roundtrip(self):
        seed = self.write('seed.json', base_seed())
        r = self.invoke('init', '--db', str(self.db), '--source', str(seed))
        self.assertEqual(r.returncode, 0, r.stderr)
        init_snap = json.loads(r.stdout)
        self.assertEqual(init_snap['version'], 0)
        self.assertTrue(r.stdout.endswith('\n'))

        src = self.write('a.json', load('request_reserve_a.json'))
        r = self.invoke('apply', '--db', str(self.db), '--source', str(src))
        self.assertEqual(r.returncode, 0, r.stderr)
        first = r.stdout

        r = self.invoke('apply', '--db', str(self.db), '--source', str(src))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, first)  # 重放字节一致

        r1 = self.invoke('snapshot', '--db', str(self.db))
        r2 = self.invoke('snapshot', '--db', str(self.db))  # 新进程重开
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r1.stdout, r2.stdout)  # 跨进程一致

    def test_failure_exit_two_empty_stdout_stderr_message(self):
        self.init()
        src = self.write('bad.json', load('request_reserve_b_fail.json'))
        # 需要先把库推进到版本1
        ok_src = self.write('ok.json', load('request_reserve_a.json'))
        self.invoke('apply', '--db', str(self.db), '--source', str(ok_src))
        r = self.invoke('apply', '--db', str(self.db), '--source', str(src))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, '')
        self.assertIn('as_of', r.stderr)

    def test_malformed_json_rules_via_cli(self):
        self.init()
        for raw in ('{', '{"request_id":"X","request_id":"Y"}',
                    '{"request_id":"X","expected_version":NaN,"operations":[]}'):
            src = self.write('bad.json', raw)
            r = self.invoke('apply', '--db', str(self.db), '--source', str(src))
            self.assertEqual(r.returncode, 2, raw)
            self.assertEqual(r.stdout, '')

    def test_reopen_process_restores_committed_state(self):
        self.init()
        self.apply(load('request_reserve_a.json'))
        before = json.dumps(self.snap(), sort_keys=True)
        r = self.invoke('snapshot', '--db', str(self.db))
        self.assertEqual(r.returncode, 0, r.stderr)
        reopened = json.dumps(json.loads(r.stdout), sort_keys=True)
        self.assertEqual(reopened, before)


if __name__ == '__main__':
    unittest.main()
