"""批次可用窗口净算（lot window）集成测试。

覆盖：发货但未放行/已过期的到货不得掩盖缺料、放行与失效边界、FEFO 消耗、
跨日期余额、共享子件合并、组件释放日、旧输入兼容以及失败输入。
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from material_plan.planning import plan

FIX = ROOT / 'fixtures' / 'lots'


def load(name):
    return json.loads((FIX / name).read_text())


def rows(result, sku):
    return [row for row in result['ledger'] if row['sku'] == sku]


class ConservationMixin:
    def assert_conserved(self, result):
        for row in result['ledger']:
            expired = row.get('expired', 0)
            self.assertEqual(
                row['opening'] + row['scheduled_receipt']
                + row['planned_receipt'] - row['gross'] - expired,
                row['closing'], row)


class AcceptanceTests(ConservationMixin, unittest.TestCase):
    def test_expired_receipt_cannot_mask_component_shortage(self):
        data = load('acceptance.json')
        result = plan(data)
        self.assert_conserved(result)
        # KIT 1 件需求 Jan10、lead 2 -> Jan8 投产，BOLT 需 3 个于 Jan8。
        kit = next(o for o in result['orders'] if o['sku'] == 'KIT')
        self.assertEqual((kit['release_date'], kit['receipt_date'], kit['quantity']),
                         ('2026-01-08', '2026-01-10', 1))
        # 收货日上的旧到货在 Jan7 失效，不能覆盖 Jan8 的 3 个需求：必须补 3。
        bolt = next(o for o in result['orders'] if o['sku'] == 'BOLT')
        self.assertEqual((bolt['release_date'], bolt['quantity'], bolt['kind']),
                         ('2026-01-08', 3, 'buy'))
        bolt_ledger = {row['date']: row for row in rows(result, 'BOLT')}
        self.assertEqual(bolt_ledger['2026-01-06']['scheduled_receipt'], 3)
        self.assertEqual(bolt_ledger['2026-01-06']['closing'], 3)
        self.assertEqual(bolt_ledger['2026-01-08']['expired'], 3)
        self.assertEqual(bolt_ledger['2026-01-08']['planned_receipt'], 3)
        self.assertEqual(bolt_ledger['2026-01-08']['closing'], 0)
        self.assertEqual(result['ending_inventory']['BOLT'], 0)

    def test_partial_consumption_then_expiry_of_remainder(self):
        data = load('acceptance_partial_use.json')
        result = plan(data)
        self.assert_conserved(result)
        bolt = {row['date']: row for row in rows(result, 'BOLT')}
        # Jan7 独立需求 2 个由该批次覆盖。
        self.assertEqual(bolt['2026-01-07']['gross'], 2)
        self.assertEqual(bolt['2026-01-07']['lot_consumption'],
                         [{'lot_id': 'L-OLD', 'quantity': 2}])
        self.assertEqual(bolt['2026-01-07']['closing'], 1)
        # 剩余 1 个在 Jan8 作废，不结转到 Jan8 的组件需求。
        self.assertEqual(bolt['2026-01-08']['expired'], 1)
        self.assertEqual(bolt['2026-01-08']['lot_consumption'], [])
        self.assertEqual(bolt['2026-01-08']['planned_receipt'], 3)
        # 过期作废进入守恒：3 收 + 3 补 = 2 耗 + 3 过期 + 1? 逐行校验已覆盖。
        self.assertEqual(result['ending_inventory']['BOLT'], 0)


class WindowBoundaryTests(ConservationMixin, unittest.TestCase):
    def test_future_release_and_expiry_day_boundaries(self):
        data = load('window_boundaries.json')
        result = plan(data)
        self.assert_conserved(result)
        w = {row['date']: row for row in rows(result, 'W')}
        # Mar1 放行的 L-EARLY 在 Mar4 可用；Mar5 才放行的 L-LATE 不早于 Mar5。
        self.assertEqual(w['2026-03-04']['lot_consumption'],
                         [{'lot_id': 'L-EARLY', 'quantity': 1}])
        # Mar5 放行当天即可消费；同日失效的 L-EARLY 在 Mar5 仍可用。
        self.assertEqual(w['2026-03-05']['scheduled_receipt'], 5)
        self.assertEqual(w['2026-03-05']['lot_consumption'],
                         [{'lot_id': 'L-EARLY', 'quantity': 1},
                          {'lot_id': 'L-LATE', 'quantity': 2}])
        self.assertEqual(w['2026-03-05']['expired'], 0)
        # Mar10 为 L-LATE 的 expires_on，当天仍可消费。
        self.assertEqual(w['2026-03-10']['lot_consumption'],
                         [{'lot_id': 'L-LATE', 'quantity': 2}])
        # Mar11 已过期：余量 1 作废，不得消费，缺 1 需补。
        self.assertEqual(w['2026-03-11']['expired'], 1)
        self.assertEqual(w['2026-03-11']['lot_consumption'], [])
        self.assertEqual(w['2026-03-11']['planned_receipt'], 1)

    def test_unreleased_quantity_does_not_cover_earlier_demand(self):
        data = {
            'as_of': '2026-01-01',
            'items': [{'sku': 'P', 'lead_days': 0, 'lot_multiple': 1,
                       'on_hand': 5, 'components': [],
                       'inventory_lots': [{'lot_id': 'A', 'quantity': 5,
                                           'available_from': '2026-01-05',
                                           'expires_on': '2026-01-20'}]}],
            'demands': [{'id': 'D1', 'sku': 'P', 'date': '2026-01-04',
                         'quantity': 2}],
            'receipts': [],
        }
        result = plan(data)
        self.assert_conserved(result)
        # 未放行批次不能覆盖 Jan4 需求。
        self.assertEqual(next(o for o in result['orders']
                              if o['sku'] == 'P')['release_date'], '2026-01-04')
        day4 = {row['date']: row for row in rows(result, 'P')}['2026-01-04']
        self.assertEqual(day4['lot_consumption'], [])
        self.assertEqual(day4['planned_receipt'], 2)


class FefoTests(ConservationMixin, unittest.TestCase):
    def test_earliest_expiry_then_lot_id_and_no_double_use(self):
        result = plan(load('fefo_cross_date.json'))
        self.assert_conserved(result)
        p = {row['date']: row for row in rows(result, 'PART')}
        # L-A/L-B 同日失效，按 lot_id 升序先耗尽 L-A。
        self.assertEqual(p['2026-04-03']['lot_consumption'],
                         [{'lot_id': 'L-A', 'quantity': 4},
                          {'lot_id': 'L-B', 'quantity': 2}])
        # 后续日期继续消耗 L-B 剩余 3、再到 L-C 2（不重复消耗）。
        self.assertEqual(p['2026-04-08']['lot_consumption'],
                         [{'lot_id': 'L-B', 'quantity': 3},
                          {'lot_id': 'L-C', 'quantity': 2}])
        self.assertEqual(p['2026-04-15']['lot_consumption'],
                         [{'lot_id': 'L-C', 'quantity': 3}])
        self.assertEqual(p['2026-04-15']['planned_receipt'], 1)
        consumed = [(item['lot_id'], item['quantity'])
                    for row in p.values() for item in row['lot_consumption']]
        totals = {}
        for lot_id, quantity in consumed:
            totals[lot_id] = totals.get(lot_id, 0) + quantity
        self.assertEqual(totals, {'L-A': 4, 'L-B': 5, 'L-C': 5})

    def test_single_lot_drawn_down_across_dates(self):
        data = {
            'as_of': '2026-01-01',
            'items': [{'sku': 'P', 'lead_days': 0, 'lot_multiple': 1,
                       'on_hand': 10, 'components': [],
                       'inventory_lots': [{'lot_id': 'A', 'quantity': 10,
                                           'available_from': '2026-01-01',
                                           'expires_on': '2026-01-20'}]}],
            'demands': [{'id': 'D1', 'sku': 'P', 'date': '2026-01-05',
                         'quantity': 3},
                        {'id': 'D2', 'sku': 'P', 'date': '2026-01-06',
                         'quantity': 4},
                        {'id': 'D3', 'sku': 'P', 'date': '2026-01-07',
                         'quantity': 3}],
            'receipts': [],
        }
        result = plan(data)
        self.assert_conserved(result)
        p = rows(result, 'P')
        self.assertEqual([row['lot_consumption'] for row in p],
                         [[{'lot_id': 'A', 'quantity': 3}],
                          [{'lot_id': 'A', 'quantity': 4}],
                          [{'lot_id': 'A', 'quantity': 3}]])
        self.assertEqual(p[-1]['closing'], 0)
        self.assertFalse(result['orders'])


class LedgerShapeTests(ConservationMixin, unittest.TestCase):
    def test_batched_rows_expose_expired_and_consumption(self):
        result = plan(load('fefo_cross_date.json'))
        for row in rows(result, 'PART'):
            self.assertEqual(set(row), {'sku', 'date', 'opening', 'gross',
                                        'scheduled_receipt', 'planned_receipt',
                                        'closing', 'expired', 'lot_consumption'})
            for entry in row['lot_consumption']:
                self.assertEqual(set(entry), {'lot_id', 'quantity'})
                self.assertIs(type(entry['quantity']), int)

    def test_release_only_and_expiry_only_days_are_reported(self):
        result = plan(load('window_boundaries.json'))
        w = {row['date']: row for row in rows(result, 'W')}
        # Mar5 放行量即使被消费也需保留账本行。
        self.assertIn('2026-03-05', w)
        self.assertEqual(w['2026-03-05']['gross'], 3)

    def test_expiry_only_day_is_reported(self):
        data = {
            'as_of': '2026-01-01',
            'items': [{'sku': 'P', 'lead_days': 0, 'lot_multiple': 1,
                       'on_hand': 6, 'components': [],
                       'inventory_lots': [{'lot_id': 'A', 'quantity': 6,
                                           'available_from': '2026-01-01',
                                           'expires_on': '2026-01-10'}]}],
            'demands': [{'id': 'D1', 'sku': 'P', 'date': '2026-01-03',
                         'quantity': 2}],
            'receipts': [],
        }
        result = plan(data)
        self.assert_conserved(result)
        p = {row['date']: row for row in rows(result, 'P')}
        # 仅发生作废的 Jan11 也输出账本行。
        self.assertEqual(p['2026-01-11']['gross'], 0)
        self.assertEqual(p['2026-01-11']['expired'], 4)
        self.assertEqual(result['ending_inventory']['P'], 0)

    def test_ending_inventory_excludes_expired_remainder(self):
        result = plan(load('acceptance_partial_use.json'))
        self.assertEqual(result['ending_inventory']['BOLT'], 0)


class ComponentTests(ConservationMixin, unittest.TestCase):
    def test_shared_component_demands_merge_before_lot_rounding(self):
        data = load('shared_component.json')
        result = plan(data)
        self.assert_conserved(result)
        # 两个父项同日各需 2 -> 子件 S 合并毛需求 4；现货 2（批次）后补 5。
        s = rows(result, 'S')
        self.assertEqual([(row['date'], row['gross']) for row in s],
                         [('2026-05-05', 4)])
        self.assertEqual(s[0]['lot_consumption'],
                         [{'lot_id': 'S-LOT', 'quantity': 2}])
        self.assertEqual(s[0]['planned_receipt'], 5)
        orders = {(o['sku'], o['release_date']): o for o in result['orders']}
        self.assertEqual(orders[('S', '2026-05-05')]['quantity'], 5)

    def test_component_demand_forms_on_planned_parent_release(self):
        result = plan(load('acceptance.json'))
        kit = next(o for o in result['orders'] if o['sku'] == 'KIT')
        bolt = {row['date']: row for row in rows(result, 'BOLT')}
        # 父项 KIT 在 Jan8 投产，BOLT 毛需求应在 Jan8 而非需求日 Jan10。
        self.assertEqual(kit['release_date'], '2026-01-08')
        self.assertIn('2026-01-08', bolt)
        self.assertEqual(bolt['2026-01-08']['gross'], 3)
        self.assertNotIn('2026-01-10', bolt)


class CompatibilityTests(unittest.TestCase):
    def test_legacy_input_is_unchanged(self):
        data = json.loads((ROOT / 'fixtures' / 'input.json').read_text())
        result = plan(data)
        self.assertEqual([(o['sku'], o['release_date'], o['quantity'])
                          for o in result['orders']],
                         [('BOLT', '2026-01-07', 20), ('KIT', '2026-01-08', 10)])
        self.assertEqual(result['ending_inventory'], {'BOLT': 0, 'KIT': 4})
        for row in result['ledger']:
            self.assertNotIn('expired', row)
            self.assertNotIn('lot_consumption', row)

    def test_non_batched_sku_rows_keep_legacy_shape(self):
        data = load('acceptance.json')
        no_field = copy.deepcopy(data)
        for item in no_field['items']:
            item.pop('inventory_lots', None)
        # 到货声明窗口时无论是否给物料空 inventory_lots 都进入批次模式。
        self.assertEqual(plan(data), plan(no_field))
        # KIT 本身没有批次声明，其账本行保持 v1 字段。
        for row in rows(plan(data), 'KIT'):
            self.assertNotIn('expired', row)
            self.assertNotIn('lot_consumption', row)

    def test_planned_supply_is_not_fed_back_as_physical_stock(self):
        data = load('acceptance.json')
        first = plan(data)
        second = plan(copy.deepcopy(data))
        self.assertEqual(first, second)
        # 采购建议仍是建议：再次计划不改变 BOLT 的到货/库存输入。
        self.assertEqual(len([o for o in second['orders']
                              if o['sku'] == 'BOLT']), 1)

    def test_input_is_not_mutated(self):
        data = load('fefo_cross_date.json')
        original = copy.deepcopy(data)
        plan(data)
        self.assertEqual(data, original)


class MixedSupplyTests(ConservationMixin, unittest.TestCase):
    def test_legacy_receipt_stays_unlimited_while_lot_is_fefo(self):
        data = {
            'as_of': '2026-01-01',
            'items': [{'sku': 'P', 'lead_days': 0, 'lot_multiple': 1,
                       'on_hand': 5, 'components': [],
                       'inventory_lots': [{'lot_id': 'A', 'quantity': 5,
                                           'available_from': '2026-01-01',
                                           'expires_on': '2026-01-03'}]}],
            'demands': [{'id': 'D1', 'sku': 'P', 'date': '2026-01-02',
                         'quantity': 4},
                        {'id': 'D2', 'sku': 'P', 'date': '2026-01-06',
                         'quantity': 4}],
            'receipts': [{'id': 'R', 'sku': 'P', 'date': '2026-01-02',
                          'quantity': 2}],
        }
        result = plan(data)
        self.assert_conserved(result)
        p = {row['date']: row for row in rows(result, 'P')}
        # 1/2 先耗尽批次 4，旧到货 2 为无限期可用。
        self.assertEqual(p['2026-01-02']['lot_consumption'],
                         [{'lot_id': 'A', 'quantity': 4}])
        self.assertEqual(p['2026-01-02']['closing'], 3)
        # 1/4 剩余 1 个批次作废，旧到货余额 2 保留。
        self.assertEqual(p['2026-01-04']['expired'], 1)
        self.assertEqual(p['2026-01-04']['closing'], 2)
        # 1/6 需求 4 由旧到货余额 2 覆盖后仍缺 2，需补单。
        self.assertEqual(p['2026-01-06']['lot_consumption'], [])
        self.assertEqual(p['2026-01-06']['planned_receipt'], 2)

    def test_lot_expired_before_as_of_is_voided_on_first_bucket(self):
        data = {
            'as_of': '2026-01-05',
            'items': [{'sku': 'P', 'lead_days': 0, 'lot_multiple': 1,
                       'on_hand': 4, 'components': [],
                       'inventory_lots': [{'lot_id': 'OLD', 'quantity': 4,
                                           'available_from': '2025-12-01',
                                           'expires_on': '2026-01-04'}]}],
            'demands': [{'id': 'D', 'sku': 'P', 'date': '2026-01-05',
                         'quantity': 4}],
            'receipts': [],
        }
        result = plan(data)
        self.assert_conserved(result)
        p = rows(result, 'P')[0]
        self.assertEqual((p['date'], p['expired'], p['planned_receipt'],
                          p['lot_consumption']),
                         ('2026-01-05', 4, 4, []))


class ValidationTests(unittest.TestCase):
    def base(self):
        return {
            'as_of': '2026-01-01',
            'items': [{'sku': 'P', 'lead_days': 0, 'lot_multiple': 1,
                       'on_hand': 5, 'components': [],
                       'inventory_lots': [{'lot_id': 'A', 'quantity': 5,
                                           'available_from': '2026-01-01',
                                           'expires_on': '2026-01-10'}]}],
            'demands': [], 'receipts': [],
        }

    def test_lot_quantity_must_sum_to_on_hand(self):
        data = self.base()
        data['items'][0]['inventory_lots'][0]['quantity'] = 4
        with self.assertRaisesRegex(ValueError, 'sum'):
            plan(data)

    def test_lot_fields_are_exact(self):
        for mutation in ('extra', 'missing'):
            data = self.base()
            lot = data['items'][0]['inventory_lots'][0]
            if mutation == 'extra':
                lot['x'] = 1
            else:
                del lot['expires_on']
            with self.subTest(mutation=mutation), \
                    self.assertRaises(ValueError):
                plan(data)

    def test_lot_validation_rules(self):
        cases = [
            ('quantity', 0),
            ('quantity', True),
            ('quantity', 1.5),
            ('available_from', '2026-1-1'),
            ('expires_on', 'not-a-date'),
            ('expires_on', '2025-12-31'),
        ]
        for field, value in cases:
            data = self.base()
            data['items'][0]['inventory_lots'][0][field] = value
            with self.subTest(field=field, value=value), \
                    self.assertRaises(ValueError):
                plan(data)

    def test_lot_id_unique_across_input(self):
        data = self.base()
        data['items'].append({'sku': 'Q', 'lead_days': 0, 'lot_multiple': 1,
                              'on_hand': 2, 'components': [],
                              'inventory_lots': [{'lot_id': 'A', 'quantity': 2,
                                                  'available_from': '2026-01-01',
                                                  'expires_on': '2026-01-10'}]})
        with self.assertRaisesRegex(ValueError, 'duplicate lot_id'):
            plan(data)
        data = self.base()
        data['receipts'].append({'id': 'R', 'sku': 'P', 'date': '2026-01-02',
                                 'quantity': 1, 'lot_id': 'A',
                                 'available_from': '2026-01-02',
                                 'expires_on': '2026-01-10'})
        with self.assertRaisesRegex(ValueError, 'duplicate lot_id'):
            plan(data)

    def test_receipt_lot_fields_must_appear_together(self):
        data = self.base()
        data['receipts'].append({'id': 'R', 'sku': 'P', 'date': '2026-01-02',
                                 'quantity': 1, 'lot_id': 'B'})
        with self.assertRaises(ValueError):
            plan(data)

    def test_receipt_lot_release_not_before_receipt_date(self):
        data = self.base()
        data['receipts'].append({'id': 'R', 'sku': 'P', 'date': '2026-01-05',
                                 'quantity': 1, 'lot_id': 'B',
                                 'available_from': '2026-01-04',
                                 'expires_on': '2026-01-09'})
        with self.assertRaises(ValueError):
            plan(data)

    def test_receipt_lot_expiry_not_before_release(self):
        data = self.base()
        data['receipts'].append({'id': 'R', 'sku': 'P', 'date': '2026-01-05',
                                 'quantity': 1, 'lot_id': 'B',
                                 'available_from': '2026-01-06',
                                 'expires_on': '2026-01-05'})
        with self.assertRaises(ValueError):
            plan(data)

    def test_unknown_item_field_still_rejected(self):
        data = self.base()
        data['items'][0]['lot'] = []
        with self.assertRaises(ValueError):
            plan(data)


class CliTests(unittest.TestCase):
    def invoke(self, source, output):
        env = dict(__import__('os').environ, PYTHONPATH=str(ROOT / 'src'))
        return subprocess.run(
            [sys.executable, '-m', 'material_plan',
             '--source', str(source), '--output', str(output)],
            capture_output=True, text=True, timeout=30, env=env)

    def test_lot_report_written_with_expiry_evidence(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / 'report.json'
            r = self.invoke(FIX / 'acceptance.json', out)
            self.assertEqual(r.returncode, 0, r.stderr)
            report = json.loads(out.read_text())
            bolt = [row for row in report['ledger'] if row['sku'] == 'BOLT']
            self.assertEqual(sum(row['expired'] for row in bolt), 3)
            self.assertEqual(sum(o['quantity'] for o in report['orders']
                                 if o['sku'] == 'BOLT'), 3)

    def test_invalid_lot_input_exits_two_and_preserves_report(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / 'report.json'
            out.write_text('old')
            bad = Path(t) / 'bad.json'
            data = load('acceptance.json')
            data['items'][1]['inventory_lots'] = [
                {'lot_id': 'X', 'quantity': 9,
                 'available_from': '2026-01-01', 'expires_on': '2026-01-02'}]
            data['items'][1]['on_hand'] = 1
            bad.write_text(json.dumps(data))
            r = self.invoke(bad, out)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(r.stdout, '')
            self.assertTrue(r.stderr)
            self.assertEqual(out.read_text(), 'old')


if __name__ == '__main__':
    unittest.main()
