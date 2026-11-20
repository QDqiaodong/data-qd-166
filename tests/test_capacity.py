"""有限产能制造排程集成测试。

覆盖：验收场景（分批投产与子件日期联动）、共享产线容量守恒、共享子件
同日合并取整、同日到货、容量用满/超限、跨日期拆批、过去投产、旧输入与
material-promise 兼容、无短缺不占产能以及 CLI 失败保留旧报告。
"""
import copy
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from material_plan.planning import plan
from material_plan.promise import init_db, apply_batch

FIX = ROOT / 'fixtures' / 'capacity'


def load(name):
    return json.loads((FIX / name).read_text())


def capacity_usage(result, data):
    """Rebuild per-(workcenter,date) usage from reported make batches."""
    items = {row['sku']: row for row in data['items']}
    usage = defaultdict(int)
    for order in result['orders']:
        if order['kind'] != 'make':
            continue
        item = items[order['sku']]
        if 'workcenter' in item:
            usage[(item['workcenter'], order['release_date'])] += (
                order['quantity'] * item['capacity_per_unit'])
    return usage


def capacity_limit(data):
    return {(row['workcenter'], row['date']): row['capacity']
            for row in data.get('capacities', [])}


class CapacityConservationMixin:
    def assert_capacity_conserved(self, result, data):
        limits = capacity_limit(data)
        for key, used in capacity_usage(result, data).items():
            self.assertLessEqual(used, limits.get(key, 0), key)

    def assert_orders_cover_demands(self, result, data):
        items = {row['sku']: row for row in data['items']}
        for order in result['orders']:
            if order['kind'] == 'make':
                self.assertGreaterEqual(order['release_date'], data['as_of'])
                lead = items[order['sku']]['lead_days']
                self.assertEqual(
                    date.fromisoformat(order['release_date']) + timedelta(days=lead),
                    date.fromisoformat(order['receipt_date']))
                self.assertEqual(
                    order['quantity'] % items[order['sku']]['lot_multiple'], 0)


class AcceptanceTests(CapacityConservationMixin, unittest.TestCase):
    def test_split_across_capacity_days_and_explode_at_release(self):
        data = load('acceptance.json')
        result = plan(data)
        kit = [o for o in result['orders'] if o['sku'] == 'KIT']
        self.assertEqual(sum(o['quantity'] for o in kit), 4)
        self.assertEqual({o['release_date'] for o in kit}, {'2026-01-06', '2026-01-07'})
        # 每批在各自投产日完工，均不晚于 1 月 10 日需求
        for order in kit:
            self.assertLessEqual(order['receipt_date'], '2026-01-10')
            self.assertFalse(order['past_due'])
        self.assert_capacity_conserved(result, data)
        self.assert_orders_cover_demands(result, data)
        # BOLT 需求按实际投产日计入，1 月 8 日无组件需求
        bolt = [r for r in result['ledger'] if r['sku'] == 'BOLT']
        self.assertEqual({r['date'] for r in bolt}, {'2026-01-06', '2026-01-07'})
        self.assertNotIn('2026-01-08', {r['date'] for r in bolt})
        self.assertEqual(sum(r['gross'] for r in bolt), 8)
        self.assertEqual(result['ending_inventory']['BOLT'], 0)
        # 不应再出现 1 月 8 日投产的 4 件 KIT
        self.assertNotIn(('2026-01-08', 4),
                         {(o['release_date'], o['quantity']) for o in kit})
        # 守恒
        for row in result['ledger']:
            self.assertEqual(row['opening'] + row['scheduled_receipt']
                             + row['planned_receipt'] - row['gross'], row['closing'])

    def test_all_zero_capacity_rejected(self):
        with self.assertRaises(ValueError):
            plan(load('acceptance_infeasible.json'))


class SharedWorkcenterTests(CapacityConservationMixin, unittest.TestCase):
    def test_two_skus_share_capacity_without_double_booking(self):
        data = load('shared_workcenter.json')
        result = plan(data)
        self.assert_capacity_conserved(result, data)
        self.assertEqual(sum(o['quantity'] for o in result['orders']
                             if o['sku'] == 'A'), 2)
        self.assertEqual(sum(o['quantity'] for o in result['orders']
                             if o['sku'] == 'B'), 2)
        # 每天总占用不超过 2，两天合计恰好 4
        self.assertEqual(sum(capacity_usage(result, data).values()), 4)

    def test_shared_workcenter_overflow_rejected(self):
        with self.assertRaises(ValueError):
            plan(load('shared_workcenter_infeasible.json'))


class SharedComponentTests(CapacityConservationMixin, unittest.TestCase):
    def test_component_demand_merges_on_actual_release_date(self):
        data = load('shared_component.json')
        result = plan(data)
        # 两个父项实际投产日均为 04-02，子件 S 需求合并为 3，按批量5取整为5
        s_orders = [o for o in result['orders'] if o['sku'] == 'S']
        self.assertEqual(len(s_orders), 1)
        self.assertEqual((s_orders[0]['release_date'], s_orders[0]['quantity']),
                         ('2026-04-02', 5))
        s_ledger = [r for r in result['ledger'] if r['sku'] == 'S']
        self.assertEqual([(r['date'], r['gross']) for r in s_ledger],
                         [('2026-04-02', 3)])
        self.assert_capacity_conserved(result, data)


class CapacityBoundaryTests(CapacityConservationMixin, unittest.TestCase):
    def test_same_day_receipt_covers_component_need(self):
        data = load('same_day_receipt.json')
        result = plan(data)
        self.assertFalse(any(o['sku'] == 'BOLT' for o in result['orders']))
        self.assertEqual(result['ending_inventory']['BOLT'], 0)

    def test_exact_capacity_accepted_one_unit_over_rejected(self):
        exact = plan(load('exact_capacity.json'))
        self.assert_capacity_conserved(exact, load('exact_capacity.json'))
        with self.assertRaises(ValueError):
            plan(load('capacity_overflow.json'))

    def test_cross_date_split_preserves_total_and_lot_multiple(self):
        data = load('cross_date_lot.json')
        result = plan(data)
        kit = [o for o in result['orders'] if o['sku'] == 'KIT']
        self.assertEqual(sum(o['quantity'] for o in kit), 6)
        self.assertEqual(len(kit), 3)
        for order in kit:
            self.assertEqual(order['quantity'] % 2, 0)
            self.assertLessEqual(order['receipt_date'], '2026-01-06')
        self.assert_capacity_conserved(result, data)

    def test_no_shortage_never_consumes_capacity(self):
        data = load('no_shortage.json')
        result = plan(data)
        self.assertFalse(result['orders'])
        self.assertEqual(result['ending_inventory']['KIT'], 0)

    def test_past_release_reported_not_clamped(self):
        data = load('past_due.json')
        result = plan(data)
        kit = [o for o in result['orders'] if o['sku'] == 'KIT']
        self.assertEqual(kit[0]['release_date'], '2025-12-31')
        self.assertTrue(kit[0]['past_due'])
        # 逾期批次不占用未来产能日历；空 capacities 也无需分配
        self.assertEqual(result['ending_inventory']['BOLT'], 99)


class CompatibilityTests(unittest.TestCase):
    def test_legacy_input_matches_baseline_report(self):
        data = json.loads((ROOT / 'fixtures' / 'input.json').read_text())
        result = plan(data)
        self.assertEqual([(o['sku'], o['release_date'], o['quantity'])
                          for o in result['orders']],
                         [('BOLT', '2026-01-07', 20), ('KIT', '2026-01-08', 10)])
        self.assertEqual(result['ending_inventory'], {'BOLT': 0, 'KIT': 4})

    def test_legacy_input_unchanged_when_capacities_omitted_or_empty(self):
        data = json.loads((ROOT / 'fixtures' / 'input.json').read_text())
        with_empty = dict(copy.deepcopy(data), capacities=[])
        self.assertEqual(plan(data), plan(with_empty))

    def test_promise_seed_without_capacity_still_works(self):
        seed = json.loads((ROOT / 'fixtures' / 'promise' / 'seed.json').read_text())
        with tempfile.TemporaryDirectory() as t:
            db = Path(t) / 'book.db'
            snap = init_db(db, copy.deepcopy(seed))
            applied = apply_batch(db, {
                'request_id': 'R', 'expected_version': 0,
                'operations': [{'op': 'reserve', 'order_id': 'A',
                                'sku': 'KIT', 'date': '2026-01-08',
                                'quantity': 1}]})
            self.assertEqual(snap['version'], 0)
            self.assertEqual(applied['version'], 1)


class OutputShapeTests(CapacityConservationMixin, unittest.TestCase):
    def test_make_batches_keep_order_field_shape(self):
        result = plan(load('acceptance.json'))
        for order in result['orders']:
            self.assertEqual(set(order),
                             {'sku', 'receipt_date', 'release_date',
                              'quantity', 'past_due', 'kind'})

    def test_batch_order_is_stable_and_date_driven(self):
        data = load('acceptance.json')
        first = plan(data)
        # 打乱输入行顺序不改变制造批次输出
        reordered = copy.deepcopy(data)
        reordered['items'] = list(reversed(reordered['items']))
        second = plan(reordered)
        self.assertEqual(first['orders'], second['orders'])
        releases = [o['release_date'] for o in first['orders']]
        self.assertEqual(releases, sorted(releases))


class MultiLevelTests(CapacityConservationMixin, unittest.TestCase):
    def test_constrained_children_recurse_on_actual_release(self):
        data = load('multi_level.json')
        result = plan(data)
        self.assert_capacity_conserved(result, data)
        orders = {(o['sku'], o['release_date']): o for o in result['orders']}
        # KIT 因 06-03 容量，需求 06-05 / lead2 -> 06-03；子件 SUB 需求 06-03
        self.assertEqual(orders[('KIT', '2026-06-03')]['quantity'], 2)
        # SUB 自己的 lead1 使其投产窗口为 06-02；RAW 在 SUB 投产日需求
        self.assertEqual(orders[('SUB', '2026-06-02')]['receipt_date'], '2026-06-03')
        self.assertEqual(orders[('RAW', '2026-06-02')]['quantity'], 2)
        # 每个制造批次都在自身需求日之前完工
        self.assertLessEqual(orders[('SUB', '2026-06-02')]['receipt_date'], '2026-06-03')
        self.assertLessEqual(orders[('KIT', '2026-06-03')]['receipt_date'], '2026-06-05')

    def test_infeasible_constrained_child_rejects_whole_report(self):
        with self.assertRaises(ValueError):
            plan(load('multi_level_infeasible.json'))

    def test_capacity_per_unit_scales_consumption(self):
        data = load('multi_level.json')
        data['items'][0]['capacity_per_unit'] = 2
        data['demands'][0]['quantity'] = 3
        # KIT 3 件 * 每件2 = 6 单位；06-03 容量2、06-02 容量2（且要留给 SUB）都放不下
        with self.assertRaises(ValueError):
            plan(data)
        data['capacities'] = [{'workcenter': 'line-a', 'date': '2026-06-03',
                               'capacity': 8},
                              {'workcenter': 'line-a', 'date': '2026-06-02',
                               'capacity': 8}]
        result = plan(data)
        self.assert_capacity_conserved(result, data)
        self.assertEqual(capacity_usage(result, data)[('line-a', '2026-06-03')], 6)


class ValidationTests(unittest.TestCase):
    def test_unknown_top_level_field_rejected(self):
        data = load('acceptance.json')
        data['extra'] = 1
        with self.assertRaises(ValueError):
            plan(data)
        # capacities remains optional for inputs that never declare workcenters
        data = json.loads((ROOT / 'fixtures' / 'input.json').read_text())
        self.assertTrue(plan(data)['orders'])

    def test_purchase_item_capacity_fields_are_ignored(self):
        data = load('acceptance.json')
        bolt = next(row for row in data['items'] if row['sku'] == 'BOLT')
        bolt['workcenter'] = 'line-a'
        bolt['capacity_per_unit'] = 5
        result = plan(data)
        # 采购建议 BOLT 仍不占制造产能，也不产生子件需求
        self.assertFalse(any(o['sku'] == 'BOLT' for o in result['orders']))
        self.assertEqual(capacity_usage(result, data),
                         {('line-a', '2026-01-06'): 2, ('line-a', '2026-01-07'): 2})

    def test_capacity_array_length_bounded(self):
        data = load('acceptance.json')
        data['capacities'] = [{'workcenter': 'unused', 'date': '2026-01-06',
                               'capacity': 0}] * 10001
        with self.assertRaises(ValueError):
            plan(data)

    def test_workcenter_fields_must_pair(self):
        data = load('acceptance.json')
        del data['items'][0]['capacity_per_unit']
        with self.assertRaises(ValueError):
            plan(data)
        data = load('acceptance.json')
        data['items'][0]['extra'] = 1
        with self.assertRaises(ValueError):
            plan(data)

    def test_capacity_row_validation(self):
        bad_rows = [
            {'workcenter': 'line-a', 'date': '2026-01-06'},
            {'workcenter': 'line-a', 'date': '2026-01-06', 'capacity': 1, 'x': 0},
            {'workcenter': 'line-a', 'date': '2026-01-06', 'capacity': True},
            {'workcenter': 'line-a', 'date': '2026-01-06', 'capacity': -1},
            {'workcenter': 'line-a', 'date': '2026-01-06', 'capacity': 1},
            {'workcenter': 'line-a', 'date': '2026-01-06', 'capacity': 1},
        ]
        for rows in ([bad_rows[0]], [bad_rows[1]], [bad_rows[2]],
                     [bad_rows[3]], bad_rows[4:]):
            data = load('acceptance.json')
            data['capacities'] = rows
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                plan(data)

    def test_capacity_date_outside_horizon_rejected(self):
        data = load('acceptance.json')
        data['capacities'].append({'workcenter': 'line-a',
                                   'date': '2026-01-05', 'capacity': 1})
        with self.assertRaises(ValueError):
            plan(data)

    def test_boolean_capacity_per_unit_rejected(self):
        data = load('acceptance.json')
        data['items'][0]['capacity_per_unit'] = True
        with self.assertRaises(ValueError):
            plan(data)

    def test_input_is_not_mutated(self):
        data = load('acceptance.json')
        original = copy.deepcopy(data)
        plan(data)
        self.assertEqual(data, original)


class CliTests(unittest.TestCase):
    def invoke(self, source, output):
        env = dict(__import__('os').environ, PYTHONPATH=str(ROOT / 'src'))
        return subprocess.run(
            [sys.executable, '-m', 'material_plan',
             '--source', str(source), '--output', str(output)],
            capture_output=True, text=True, timeout=30, env=env)

    def test_feasible_capacity_report_written(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / 'report.json'
            r = self.invoke(FIX / 'acceptance.json', out)
            self.assertEqual(r.returncode, 0, r.stderr)
            report = json.loads(out.read_text())
            kit = [o for o in report['orders'] if o['sku'] == 'KIT']
            self.assertEqual(sum(o['quantity'] for o in kit), 4)

    def test_infeasible_capacity_exit_two_preserves_report(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / 'report.json'
            out.write_text('old')
            r = self.invoke(FIX / 'acceptance_infeasible.json', out)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(r.stdout, '')
            self.assertTrue(r.stderr)
            self.assertEqual(out.read_text(), 'old')


if __name__ == '__main__':
    unittest.main()
