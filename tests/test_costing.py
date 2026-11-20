"""离线材料成本月末关账（material_plan.costing）集成测试。

覆盖：三个月验收场景、历史分摊顺序、多来源与多层耗尽批次、零金额分配、
实物基线核对、成本更新规则、期间连续性、整期回滚、跨进程恢复与 CLI。
"""
import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from material_plan import traceability
from material_plan.costing import init_db, close_period, show_period

FIX = ROOT / 'fixtures' / 'cost'


def load(name):
    return json.loads((FIX / name).read_text())


def lot(lot_id, sku, produced, remaining, inputs=(), blocked_by=()):
    return {'lot_id': lot_id, 'sku': sku, 'produced_quantity': produced,
            'remaining_quantity': remaining,
            'inputs': [{'lot_id': name, 'quantity': count}
                       for name, count in inputs],
            'blocked_by': list(blocked_by)}


def shipment(shipment_id, customer_id, sku, quantity, allocations):
    return {'shipment_id': shipment_id, 'customer_id': customer_id,
            'sku': sku, 'quantity': quantity,
            'allocations': [{'lot_id': name, 'quantity': count}
                            for name, count in allocations]}


def physical(version, lots=(), shipments=(), recalls=(), exposures=()):
    return {'version': version, 'lots': list(lots),
            'shipments': list(shipments), 'recalls': list(recalls),
            'exposures': list(exposures)}


def update(lot_id, total_cents, source_ref='INV-1'):
    return {'lot_id': lot_id, 'total_cents': total_cents,
            'source_ref': source_ref}


def close(period, phys, updates=()):
    return {'period': period, 'physical': phys,
            'cost_updates': list(updates)}


def cogs_row(category, shipment_id, lot_id, amount):
    return {'category': category, 'shipment_id': shipment_id,
            'lot_id': lot_id, 'amount_cents': amount}


def inventory_row(lot_id, sku, quantity, amount):
    return {'lot_id': lot_id, 'sku': sku, 'quantity': quantity,
            'amount_cents': amount}


def totals(opening, purchases, cogs, ending):
    return {'opening_cents': opening, 'purchases_cents': purchases,
            'cogs_cents': cogs, 'ending_cents': ending}


class CostTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / 'cost.db'

    def tearDown(self):
        self._tmp.cleanup()

    def init(self):
        return init_db(self.db)

    def close(self, payload):
        return close_period(self.db, copy.deepcopy(payload))

    def show(self, period):
        return show_period(self.db, period)

    def db_dump(self):
        conn = sqlite3.connect(self.db)
        try:
            return list(conn.iterdump())
        finally:
            conn.close()

    def assert_fails(self, payload):
        # 关账错误不得留下期间、成本更新、实物基线或账单的部分变化。
        before = self.db_dump()
        with self.assertRaises(ValueError):
            self.close(payload)
        self.assertEqual(self.db_dump(), before)


class InitTests(CostTestBase):
    def test_initial_status(self):
        self.assertEqual(self.init(), {'closed_periods': []})

    def test_existing_path_rejected_even_empty_file(self):
        empty = self.tmp / 'empty.db'
        empty.write_bytes(b'')
        with self.assertRaises(ValueError):
            init_db(empty)
        self.assertEqual(empty.read_bytes(), b'')

    def test_symlink_path_rejected(self):
        target = self.tmp / 'target'
        target.write_bytes(b'x')
        link = self.tmp / 'link.db'
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            init_db(link)
        self.assertEqual(target.read_bytes(), b'x')

    def test_reinit_rejected_and_ledger_kept(self):
        self.init()
        self.close(close('2026-01', physical(1)))
        with self.assertRaises(ValueError):
            init_db(self.db)
        self.assertEqual(self.show('2026-01')['period'], '2026-01')

    def test_close_and_show_never_create_database(self):
        missing = self.tmp / 'missing.db'
        with self.assertRaises(ValueError):
            close_period(missing, close('2026-01', physical(0)))
        with self.assertRaises(ValueError):
            show_period(missing, '2026-01')
        self.assertFalse(missing.exists())

    def test_other_ledger_file_is_not_a_cost_ledger(self):
        trace_db = self.tmp / 'trace.db'
        traceability.init_db(trace_db, {'items': [{'sku': 'RAW',
                                                   'components': []}]})
        with self.assertRaises(ValueError):
            close_period(trace_db, close('2026-01', physical(0)))
        with self.assertRaises(ValueError):
            show_period(trace_db, '2026-01')

    def test_str_and_path_db_are_equivalent(self):
        init_db(str(self.db))
        bill = close_period(str(self.db), close('2026-01', physical(0)))
        self.assertEqual(show_period(self.db, '2026-01'), bill)


class AcceptanceTests(CostTestBase):
    """可复现验收：首月 R/S 各剩 1 件价值 34/33，O1 销售成本 33；次月
    R 改价 104，期初 67、采购净增 4、O1 调整 +1、期末 70；第三月 R 改
    97 并把余料发至 A/O2，A/O2 销售成本 33/32，O1 补 -2，期末 0。"""

    def setUp(self):
        super().setUp()
        self.init()
        # 实物由 material-trace 记录：验证 snapshot 输出可直接作为 close 输入。
        self.trace_db = self.tmp / 'trace.db'
        traceability.init_db(self.trace_db, {'items': [
            {'sku': 'RAW', 'components': []},
            {'sku': 'SUB',
             'components': [{'sku': 'RAW', 'quantity': 1}]}]})
        self.phys1 = traceability.apply_batch(self.trace_db, {
            'request_id': 'M1', 'expected_version': 0,
            'operations': [
                {'op': 'receive', 'lot_id': 'R', 'sku': 'RAW',
                 'quantity': 3},
                {'op': 'make', 'lot_id': 'S', 'sku': 'SUB', 'quantity': 2,
                 'inputs': [{'lot_id': 'R', 'quantity': 2}]},
                {'op': 'ship', 'shipment_id': 'O1', 'customer_id': 'C1',
                 'sku': 'SUB', 'quantity': 1,
                 'allocations': [{'lot_id': 'S', 'quantity': 1}]}]})

    def test_three_month_acceptance(self):
        bill1 = self.close(close('2026-01', self.phys1,
                                 [update('R', 100, 'INV-R-1')]))
        self.assertEqual(bill1, {
            'period': '2026-01', 'physical_version': 1,
            'cost_updates': [{'lot_id': 'R', 'total_cents': 100,
                              'source_ref': 'INV-R-1'}],
            'inventory': [inventory_row('R', 'RAW', 1, 34),
                          inventory_row('S', 'SUB', 1, 33)],
            'cogs': [cogs_row('sale', 'O1', 'S', 33)],
            'totals': totals(0, 100, 33, 67)})

        # 次月实物不变（同版本同事实），仅更正 R 成本为 104。
        bill2 = self.close(close('2026-02', self.phys1,
                                 [update('R', 104, 'INV-R-2')]))
        self.assertEqual(bill2, {
            'period': '2026-02', 'physical_version': 1,
            'cost_updates': [{'lot_id': 'R', 'total_cents': 104,
                              'source_ref': 'INV-R-2'}],
            'inventory': [inventory_row('R', 'RAW', 1, 35),
                          inventory_row('S', 'SUB', 1, 35)],
            'cogs': [cogs_row('adjustment', 'O1', 'S', 1)],
            'totals': totals(67, 4, 1, 70)})

        # 第三月 R 改价 97，R 余料发至 A、S 余料发至 O2。
        phys2 = traceability.apply_batch(self.trace_db, {
            'request_id': 'M3', 'expected_version': 1,
            'operations': [
                {'op': 'ship', 'shipment_id': 'A', 'customer_id': 'C2',
                 'sku': 'RAW', 'quantity': 1,
                 'allocations': [{'lot_id': 'R', 'quantity': 1}]},
                {'op': 'ship', 'shipment_id': 'O2', 'customer_id': 'C1',
                 'sku': 'SUB', 'quantity': 1,
                 'allocations': [{'lot_id': 'S', 'quantity': 1}]}]})
        bill3 = self.close(close('2026-03', phys2,
                                 [update('R', 97, 'INV-R-3')]))
        self.assertEqual(bill3, {
            'period': '2026-03', 'physical_version': 2,
            'cost_updates': [{'lot_id': 'R', 'total_cents': 97,
                              'source_ref': 'INV-R-3'}],
            'inventory': [],
            'cogs': [cogs_row('sale', 'A', 'R', 33),
                     cogs_row('adjustment', 'O1', 'S', -2),
                     cogs_row('sale', 'O2', 'S', 32)],
            'totals': totals(70, -7, 63, 0)})

        # 已冻结账单保持原值，不随后续改价重出。
        self.assertEqual(self.show('2026-01'), bill1)
        self.assertEqual(self.show('2026-02'), bill2)
        self.assertEqual(self.show('2026-03'), bill3)

    def test_fixture_files_reproduce_the_same_bills(self):
        bills = [self.close(load('close_2026_01.json')),
                 self.close(load('close_2026_02.json')),
                 self.close(load('close_2026_03.json'))]
        self.assertEqual([bill['totals'] for bill in bills],
                         [totals(0, 100, 33, 67), totals(67, 4, 1, 70),
                          totals(70, -7, 63, 0)])
        self.assertEqual(bills[2]['cogs'],
                         [cogs_row('sale', 'A', 'R', 33),
                          cogs_row('adjustment', 'O1', 'S', -2),
                          cogs_row('sale', 'O2', 'S', 32)])

    def test_array_order_does_not_change_facts(self):
        phys = copy.deepcopy(self.phys1)
        phys['lots'].reverse()
        bill = self.close(close('2026-01', phys, [update('R', 100)]))
        self.assertEqual(bill['inventory'],
                         [inventory_row('R', 'RAW', 1, 34),
                          inventory_row('S', 'SUB', 1, 33)])
        # 次月以另一种数组排列提交同一版本同一事实：允许。
        scrambled = copy.deepcopy(self.phys1)
        scrambled['shipments'].reverse()
        bill2 = self.close(close('2026-02', scrambled))
        self.assertEqual(bill2['totals'], totals(67, 0, 0, 67))


class AllocationOrderTests(CostTestBase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_shipment_order_is_historical_not_alphabetical(self):
        # 同一批次先发 Z，隔月再发字典序更早的 A：A 不得挤到 Z 前面。
        phys1 = physical(1, lots=[lot('P', 'RAW', 3, 2)],
                         shipments=[shipment('Z', 'C', 'RAW', 1, [('P', 1)])])
        bill1 = self.close(close('2026-01', phys1, [update('P', 5)]))
        self.assertEqual(bill1['cogs'], [cogs_row('sale', 'Z', 'P', 1)])
        self.assertEqual(bill1['inventory'],
                         [inventory_row('P', 'RAW', 2, 4)])
        phys2 = physical(2, lots=[lot('P', 'RAW', 3, 1)],
                         shipments=[shipment('Z', 'C', 'RAW', 1, [('P', 1)]),
                                    shipment('A', 'C', 'RAW', 1, [('P', 1)])])
        bill2 = self.close(close('2026-02', phys2))
        # 若误按发货号全局重排，A 会得 1 而 Z 补 1；正确结果 A 得第 2 段 2。
        self.assertEqual(bill2['cogs'], [cogs_row('sale', 'A', 'P', 2)])
        self.assertEqual(bill2['inventory'],
                         [inventory_row('P', 'RAW', 1, 2)])
        self.assertEqual(bill2['totals'], totals(4, 0, 2, 2))

    def test_same_month_make_sorts_before_shipment(self):
        phys1 = physical(1, lots=[lot('Q', 'RAW', 3, 1),
                                  lot('M', 'SUB', 1, 1, inputs=[('Q', 1)])],
                         shipments=[shipment('Z', 'C', 'RAW', 1, [('Q', 1)])])
        bill = self.close(close('2026-01', phys1, [update('Q', 5)]))
        # 同月先领料（M 得 1）后发货（Z 得第 2 段 2）。
        self.assertEqual(bill['cogs'], [cogs_row('sale', 'Z', 'Q', 2)])
        self.assertEqual(bill['inventory'],
                         [inventory_row('M', 'SUB', 1, 1),
                          inventory_row('Q', 'RAW', 1, 2)])
        self.assertEqual(bill['totals'], totals(0, 5, 2, 3))

    def test_same_month_makes_sort_by_consuming_lot_id(self):
        phys1 = physical(1, lots=[lot('Q', 'RAW', 3, 1),
                                  lot('B', 'SUB', 1, 1, inputs=[('Q', 1)]),
                                  lot('A', 'SUB', 1, 1, inputs=[('Q', 1)])])
        bill = self.close(close('2026-01', phys1, [update('Q', 5)]))
        # 同月领料按领入批次升序：A 得 1，B 得 2。
        amounts = {row['lot_id']: row['amount_cents']
                   for row in bill['inventory']}
        self.assertEqual(amounts, {'Q': 2, 'A': 1, 'B': 2})
        self.assertEqual(bill['totals'], totals(0, 5, 0, 5))


class ValuationTests(CostTestBase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_multi_source_make_merges_then_allocates_by_own_output(self):
        phys1 = physical(1, lots=[lot('R1', 'RAW', 2, 1),
                                  lot('R2', 'RAW', 2, 1),
                                  lot('M', 'SUB', 2, 1,
                                      inputs=[('R1', 1), ('R2', 1)])],
                         shipments=[shipment('OUT', 'C', 'SUB', 1,
                                             [('M', 1)])])
        bill = self.close(close('2026-01', phys1,
                                [update('R1', 10), update('R2', 21)]))
        # R1 转入 5、R2 转入 10，汇合后 M 值 15 按自身产量 2 分摊：OUT 得 7。
        self.assertEqual(bill['cogs'], [cogs_row('sale', 'OUT', 'M', 7)])
        self.assertEqual(bill['inventory'],
                         [inventory_row('M', 'SUB', 1, 8),
                          inventory_row('R1', 'RAW', 1, 5),
                          inventory_row('R2', 'RAW', 1, 11)])
        self.assertEqual(bill['totals'], totals(0, 31, 7, 24))

    def test_multi_level_depleted_lots_and_depleted_reprice(self):
        phys1 = physical(1, lots=[lot('R', 'RAW', 4, 0),
                                  lot('S', 'SUB', 2, 0, inputs=[('R', 4)]),
                                  lot('K', 'KIT', 2, 1, inputs=[('S', 2)])],
                         shipments=[shipment('OUT', 'C', 'KIT', 1,
                                             [('K', 1)])])
        bill1 = self.close(close('2026-01', phys1, [update('R', 40)]))
        # 中间层 S 余额为 0 仍完整传导：R→S 40、S→K 40、K 发 1 得 20。
        self.assertEqual(bill1['cogs'], [cogs_row('sale', 'OUT', 'K', 20)])
        self.assertEqual(bill1['inventory'],
                         [inventory_row('K', 'KIT', 1, 20)])
        self.assertEqual(bill1['totals'], totals(0, 40, 20, 20))
        # 已用尽的收货批次仍可改价：差额沿链路传导到已发货与在库。
        bill2 = self.close(close('2026-02', phys1, [update('R', 80, 'INV-2')]))
        self.assertEqual(bill2['cogs'],
                         [cogs_row('adjustment', 'OUT', 'K', 20)])
        self.assertEqual(bill2['inventory'],
                         [inventory_row('K', 'KIT', 1, 40)])
        self.assertEqual(bill2['totals'], totals(20, 40, 20, 40))

    def test_zero_cost_allocation_is_listed(self):
        phys1 = physical(1, lots=[lot('Z', 'RAW', 2, 1)],
                         shipments=[shipment('OUT', 'C', 'RAW', 1,
                                             [('Z', 1)])])
        bill = self.close(close('2026-01', phys1, [update('Z', 0)]))
        # 首次入账分配即便为零也列 sale。
        self.assertEqual(bill['cogs'], [cogs_row('sale', 'OUT', 'Z', 0)])
        self.assertEqual(bill['inventory'],
                         [inventory_row('Z', 'RAW', 1, 0)])
        self.assertEqual(bill['totals'], totals(0, 0, 0, 0))

    def test_old_receipt_lot_keeps_cost_without_update(self):
        phys1 = physical(1, lots=[lot('R', 'RAW', 2, 1)],
                         shipments=[shipment('OUT', 'C', 'RAW', 1,
                                             [('R', 1)])])
        self.close(close('2026-01', phys1, [update('R', 100)]))
        bill2 = self.close(close('2026-02', phys1))
        self.assertEqual(bill2['cogs'], [])
        self.assertEqual(bill2['inventory'],
                         [inventory_row('R', 'RAW', 1, 50)])
        self.assertEqual(bill2['totals'], totals(50, 0, 0, 50))

    def test_large_quantities_are_not_expanded_per_unit(self):
        phys1 = physical(1, lots=[lot('R', 'RAW', 10**9, 10**9 - 1)],
                         shipments=[shipment('OUT', 'C', 'RAW', 1,
                                             [('R', 1)])])
        bill = self.close(close('2026-01', phys1, [update('R', 10**12)]))
        self.assertEqual(bill['cogs'], [cogs_row('sale', 'OUT', 'R', 1000)])
        self.assertEqual(bill['inventory'],
                         [inventory_row('R', 'RAW', 10**9 - 1,
                                        10**12 - 1000)])
        self.assertEqual(bill['totals'],
                         totals(0, 10**12, 1000, 10**12 - 1000))

    def test_quality_fields_are_ignored_for_valuation_and_comparison(self):
        phys1 = physical(1, lots=[lot('R', 'RAW', 2, 1)],
                         shipments=[shipment('OUT', 'C', 'RAW', 1,
                                             [('R', 1)])])
        self.close(close('2026-01', phys1, [update('R', 10)]))
        # 同版本、实物事实相同，仅质量状态不同：允许且不影响估值。
        phys2 = physical(
            1, lots=[lot('R', 'RAW', 2, 1, blocked_by=['X'])],
            shipments=[shipment('OUT', 'C', 'RAW', 1, [('R', 1)])],
            recalls=[{'recall_id': 'X', 'lot_id': 'R', 'status': 'active'}],
            exposures=[{'recall_id': 'X', 'shipment_id': 'OUT',
                        'customer_id': 'C', 'sku': 'RAW', 'lot_id': 'R',
                        'quantity': 1}])
        bill2 = self.close(close('2026-02', phys2))
        self.assertEqual(bill2['cogs'], [])
        self.assertEqual(bill2['inventory'],
                         [inventory_row('R', 'RAW', 1, 5)])
        self.assertEqual(bill2['totals'], totals(5, 0, 0, 5))


class PhysicalValidationTests(CostTestBase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_first_close_physical_validation(self):
        cases = [
            # 发货分配之和不等于发货数量。
            physical(1, lots=[lot('R', 'RAW', 2, 1)],
                     shipments=[shipment('O', 'C', 'RAW', 2, [('R', 1)])]),
            # 分配批次 SKU 与发货不符。
            physical(1, lots=[lot('R', 'RAW', 1, 0), lot('S', 'SUB', 1, 1)],
                     shipments=[shipment('O', 'C', 'SUB', 1, [('R', 1)])]),
            # 未知领料来源 / 未知分配批次。
            physical(1, lots=[lot('M', 'SUB', 1, 1, inputs=[('GHOST', 1)])]),
            physical(1, lots=[lot('R', 'RAW', 1, 1)],
                     shipments=[shipment('O', 'C', 'RAW', 1, [('GHOST', 1)])]),
            # 领料谱系环（含自环）。
            physical(1, lots=[lot('X', 'SUB', 1, 0, inputs=[('Y', 1)]),
                              lot('Y', 'SUB', 1, 0, inputs=[('X', 1)])]),
            physical(1, lots=[lot('X', 'SUB', 1, 0, inputs=[('X', 1)])]),
            # 产出量不等于余额与全部消耗之和。
            physical(1, lots=[lot('R', 'RAW', 3, 3)],
                     shipments=[shipment('O', 'C', 'RAW', 1, [('R', 1)])]),
            physical(1, lots=[lot('R', 'RAW', 3, 4)]),
            # 重复身份。
            physical(1, lots=[lot('R', 'RAW', 1, 1), lot('R', 'RAW', 1, 1)]),
            physical(1, lots=[lot('R', 'RAW', 2, 0)],
                     shipments=[shipment('O', 'C', 'RAW', 1, [('R', 1)]),
                                shipment('O', 'C', 'RAW', 1, [('R', 1)])]),
            # 单批次内重复领料 / 重复分配。
            physical(1, lots=[lot('R', 'RAW', 2, 0),
                              {'lot_id': 'M', 'sku': 'SUB',
                               'produced_quantity': 1,
                               'remaining_quantity': 1,
                               'inputs': [{'lot_id': 'R', 'quantity': 1},
                                          {'lot_id': 'R', 'quantity': 1}],
                               'blocked_by': []}]),
            physical(1, lots=[lot('R', 'RAW', 2, 0)],
                     shipments=[{'shipment_id': 'O', 'customer_id': 'C',
                                 'sku': 'RAW', 'quantity': 2,
                                 'allocations': [
                                     {'lot_id': 'R', 'quantity': 1},
                                     {'lot_id': 'R', 'quantity': 1}]}]),
        ]
        for index, phys in enumerate(cases):
            with self.subTest(index=index):
                self.assert_fails(close('2026-01', phys))

    def test_structure_and_scalar_rules(self):
        bad = [
            {},
            {'period': '2026-01', 'physical': physical(0)},
            {'period': '2026-01', 'physical': physical(0), 'cost_updates': [],
             'extra': 1},
            close('2026-01', {'version': 0, 'lots': [], 'shipments': [],
                              'recalls': []}),
            close('2026-01', physical(True)),
            close('2026-01', physical(-1)),
            close('2026-01', physical(1, lots=[lot('R', 'RAW', True, 1)])),
            close('2026-01', physical(1, lots=[lot('R', 'RAW', 0, 0)])),
            close('2026-01', physical(1, lots=[lot('R', 'RAW', 1, -1)])),
            close('2026-01', physical(1, lots=[lot(' R', 'RAW', 1, 1)])),
            close('2026-01', physical(1, lots=[lot('R', 'RAW', 1, 1)]),
                  [update('R', 1.5)]),
            close('2026-01', physical(1, lots=[lot('R', 'RAW', 1, 1)]),
                  [{'lot_id': 'R', 'total_cents': 1}]),
            close('2026-01', physical(1, lots=[lot('R', 'RAW', 1, 1)]),
                  [{'lot_id': 'R', 'total_cents': 1, 'source_ref': 'I',
                    'extra': 1}]),
            close('2026-01', physical(1, lots=[lot('R', 'RAW', 1, 1)]),
                  [update('R', 1, '')]),
        ]
        for index, payload in enumerate(bad):
            with self.subTest(index=index):
                self.assert_fails(payload)

    def test_version_cannot_regress(self):
        self.close(close('2026-01', physical(2)))
        self.assert_fails(close('2026-02', physical(1)))
        self.assert_fails(close('2026-02', physical(0)))

    def test_same_version_requires_identical_facts(self):
        phys1 = physical(1, lots=[lot('R', 'RAW', 3, 1),
                                  lot('S', 'SUB', 2, 1, inputs=[('R', 2)])],
                         shipments=[shipment('O1', 'C', 'SUB', 1,
                                             [('S', 1)])])
        self.close(close('2026-01', phys1, [update('R', 100)]))
        # 同版本但改写批次产出量。
        changed = physical(1, lots=[lot('R', 'RAW', 3, 1),
                                    lot('S', 'SUB', 1, 1, inputs=[('R', 2)])],
                           shipments=[shipment('O1', 'C', 'SUB', 1,
                                               [('S', 1)])])
        self.assert_fails(close('2026-02', changed, [update('R', 100)]))
        # 同版本但多出批次。
        extra = physical(1, lots=[lot('R', 'RAW', 3, 1),
                                  lot('S', 'SUB', 2, 1, inputs=[('R', 2)]),
                                  lot('X', 'RAW', 1, 1)],
                         shipments=[shipment('O1', 'C', 'SUB', 1,
                                             [('S', 1)])])
        self.assert_fails(close('2026-02', extra, [update('R', 100),
                                                   update('X', 1)]))
        # 同版本同事实（仅成本更正）成功。
        bill = self.close(close('2026-02', phys1, [update('R', 104)]))
        self.assertEqual(bill['totals'], totals(67, 4, 1, 70))

    def test_history_cannot_be_rewritten_when_version_advances(self):
        phys1 = physical(1, lots=[lot('R', 'RAW', 3, 1),
                                  lot('S', 'SUB', 2, 1, inputs=[('R', 2)])],
                         shipments=[shipment('O1', 'C', 'SUB', 1,
                                             [('S', 1)])])
        self.close(close('2026-01', phys1, [update('R', 100)]))
        rewrites = [
            # 旧批次消失。
            physical(2, lots=[lot('R', 'RAW', 3, 1)]),
            # 旧批次产出量被改写（局部仍守恒）。
            physical(2, lots=[lot('R', 'RAW', 4, 2),
                              lot('S', 'SUB', 2, 1, inputs=[('R', 2)])],
                     shipments=[shipment('O1', 'C', 'SUB', 1, [('S', 1)])]),
            # 旧批次领料谱系被改写。
            physical(2, lots=[lot('R', 'RAW', 3, 2),
                              lot('S', 'SUB', 2, 1, inputs=[('R', 1)])],
                     shipments=[shipment('O1', 'C', 'SUB', 1, [('S', 1)])]),
            # 旧发货消失。
            physical(2, lots=[lot('R', 'RAW', 3, 1),
                              lot('S', 'SUB', 2, 2, inputs=[('R', 2)])]),
            # 旧发货客户被改写。
            physical(2, lots=[lot('R', 'RAW', 3, 1),
                              lot('S', 'SUB', 2, 1, inputs=[('R', 2)])],
                     shipments=[shipment('O1', 'C9', 'SUB', 1, [('S', 1)])]),
            # 旧发货分配被改写。
            physical(2, lots=[lot('R', 'RAW', 3, 1),
                              lot('S', 'SUB', 2, 1, inputs=[('R', 2)])],
                     shipments=[shipment('O1', 'C', 'SUB', 1, [('R', 1)])]),
        ]
        for index, phys in enumerate(rewrites):
            with self.subTest(index=index):
                self.assert_fails(close('2026-02', phys))
        # 全部拒绝后，正确的次月仍可关账。
        phys2 = physical(2, lots=[lot('R', 'RAW', 3, 0),
                                  lot('S', 'SUB', 2, 1, inputs=[('R', 2)])],
                         shipments=[shipment('O1', 'C', 'SUB', 1, [('S', 1)]),
                                    shipment('A', 'C', 'RAW', 1, [('R', 1)])])
        bill = self.close(close('2026-02', phys2))
        self.assertEqual(bill['cogs'], [cogs_row('sale', 'A', 'R', 34)])
        self.assertEqual(bill['inventory'],
                         [inventory_row('S', 'SUB', 1, 33)])
        self.assertEqual(bill['totals'], totals(67, 0, 34, 33))


class CostUpdateTests(CostTestBase):
    def setUp(self):
        super().setUp()
        self.init()
        self.phys = physical(1, lots=[lot('R', 'RAW', 2, 1),
                                      lot('M', 'SUB', 1, 1,
                                          inputs=[('R', 1)])])

    def test_new_receipt_lot_requires_cost(self):
        self.assert_fails(close('2026-01', self.phys))
        # 允许明确为 0。
        bill = self.close(close('2026-01', self.phys, [update('R', 0)]))
        self.assertEqual(bill['totals'], totals(0, 0, 0, 0))

    def test_update_must_reference_a_receipt_lot_in_snapshot(self):
        self.assert_fails(close('2026-01', self.phys,
                                [update('R', 1), update('M', 1)]))
        self.assert_fails(close('2026-01', self.phys,
                                [update('R', 1), update('GHOST', 1)]))

    def test_duplicate_update_rejected(self):
        self.assert_fails(close('2026-01', self.phys,
                                [update('R', 1), update('R', 2, 'INV-2')]))

    def test_total_cents_bounds(self):
        for bad in (-1, True, 10**12 + 1, 1.5, '100'):
            with self.subTest(bad=bad):
                self.assert_fails(close('2026-01', self.phys,
                                        [update('R', bad)]))
        bill = self.close(close('2026-01', self.phys,
                                [update('R', 10**12)]))
        amounts = {row['lot_id']: row['amount_cents']
                   for row in bill['inventory']}
        self.assertEqual(amounts, {'R': 10**12 // 2, 'M': 10**12 // 2})

    def test_depleted_receipt_lot_can_be_repriced(self):
        phys = physical(1, lots=[lot('R', 'RAW', 1, 0)],
                        shipments=[shipment('OUT', 'C', 'RAW', 1,
                                            [('R', 1)])])
        self.close(close('2026-01', phys, [update('R', 10)]))
        bill2 = self.close(close('2026-02', phys, [update('R', 25, 'INV-2')]))
        self.assertEqual(bill2['cogs'],
                         [cogs_row('adjustment', 'OUT', 'R', 15)])
        self.assertEqual(bill2['totals'], totals(0, 15, 15, 0))


class PeriodTests(CostTestBase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_period_format(self):
        for bad in ('2026-1', '2026-13', '2026-00', '202601', '2026-01-01',
                    'abcd-ef', '0000-01', '2026-1 ', 202601, True, ''):
            with self.subTest(bad=bad):
                self.assert_fails(close(bad, physical(1)))

    def test_first_period_may_be_any_valid_month_then_consecutive(self):
        self.close(close('2026-12', physical(1)))
        # 跨年连续关账。
        self.close(close('2027-01', physical(1)))
        self.assertEqual(self.show('2027-01')['period'], '2027-01')

    def test_periods_must_be_consecutive_and_unique(self):
        self.close(close('2026-03', physical(1)))
        for bad in ('2026-03', '2026-02', '2026-05', '2025-04'):
            with self.subTest(bad=bad):
                self.assert_fails(close(bad, physical(1)))
        self.close(close('2026-04', physical(1)))
        self.assert_fails(close('2026-04', physical(1)))

    def test_show_rejects_unclosed_or_malformed_period(self):
        self.close(close('2026-01', physical(1)))
        with self.assertRaises(ValueError):
            self.show('2026-02')
        with self.assertRaises(ValueError):
            self.show('2026-1')


class RollbackTests(CostTestBase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_failed_close_leaves_no_partial_state(self):
        phys1 = physical(1, lots=[lot('R', 'RAW', 3, 1),
                                  lot('S', 'SUB', 2, 1, inputs=[('R', 2)])],
                         shipments=[shipment('O1', 'C', 'SUB', 1,
                                             [('S', 1)])])
        # 成本更新引用未知批次：整期回滚，期间、基线与账单均无残留。
        self.assert_fails(close('2026-01', phys1, [update('GHOST', 1)]))
        bill = self.close(close('2026-01', phys1, [update('R', 100)]))
        self.assertEqual(self.show('2026-01'), bill)
        # 次月先提交改写历史的坏关账，再提交正确关账。
        bad2 = physical(2, lots=[lot('R', 'RAW', 4, 2),
                                 lot('S', 'SUB', 2, 1, inputs=[('R', 2)])],
                        shipments=[shipment('O1', 'C', 'SUB', 1, [('S', 1)])])
        self.assert_fails(close('2026-02', bad2, [update('R', 104)]))
        good2 = physical(2, lots=[lot('R', 'RAW', 3, 0),
                                  lot('S', 'SUB', 2, 1, inputs=[('R', 2)])],
                         shipments=[shipment('O1', 'C', 'SUB', 1, [('S', 1)]),
                                    shipment('A', 'C', 'RAW', 1, [('R', 1)])])
        bill2 = self.close(close('2026-02', good2, [update('R', 104)]))
        # 首月账单不受失败关账影响。
        self.assertEqual(self.show('2026-01'), bill)
        self.assertEqual(bill2['cogs'], [cogs_row('sale', 'A', 'R', 35),
                                         cogs_row('adjustment', 'O1', 'S', 1)])
        self.assertEqual(bill2['totals'], totals(67, 4, 36, 35))

    def test_close_does_not_mutate_input(self):
        payload = close('2026-01',
                        physical(1, lots=[lot('R', 'RAW', 2, 1)],
                                 shipments=[shipment('O', 'C', 'RAW', 1,
                                                     [('R', 1)])]),
                        [update('R', 10)])
        before = copy.deepcopy(payload)
        self.close(payload)
        self.assertEqual(payload, before)
        failing = close('2026-02', physical(2), [update('GHOST', 1)])
        before = copy.deepcopy(failing)
        with self.assertRaises(ValueError):
            self.close(failing)
        self.assertEqual(failing, before)


class CliTests(CostTestBase):
    def invoke(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        return subprocess.run([sys.executable, '-m',
                               'material_plan.costing_cli', *args],
                              capture_output=True, text=True, timeout=60,
                              env=env)

    def write(self, name, value):
        path = self.tmp / name
        path.write_text(value if isinstance(value, str)
                        else json.dumps(value))
        return path

    def test_init_close_show_roundtrip(self):
        result = self.invoke('init', '--db', str(self.db))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'closed_periods': []})
        self.assertTrue(result.stdout.endswith('\n'))
        self.assertEqual(result.stdout.count('\n'), 1)

        outputs = []
        for name in ('close_2026_01', 'close_2026_02', 'close_2026_03'):
            source = self.write(name + '.json', load(name + '.json'))
            result = self.invoke('close', '--db', str(self.db),
                                 '--source', str(source))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count('\n'), 1)
            outputs.append(result.stdout)
        self.assertEqual(json.loads(outputs[2])['totals'],
                         totals(70, -7, 63, 0))

        # show 取回首次关账时的冻结账单，与当时输出逐字节一致。
        shown = self.invoke('show', '--db', str(self.db),
                            '--period', '2026-01')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(shown.stdout, outputs[0])

    def test_failure_exit_two_empty_stdout_with_stderr(self):
        self.invoke('init', '--db', str(self.db))
        source = self.write('bad.json', close('2026-01', physical(1),
                                              [update('GHOST', 1)]))
        result = self.invoke('close', '--db', str(self.db),
                             '--source', str(source))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        shown = self.invoke('show', '--db', str(self.db),
                            '--period', '2026-01')
        self.assertEqual(shown.returncode, 2)
        self.assertEqual(shown.stdout, '')

    def test_malformed_json_rules_via_cli(self):
        self.invoke('init', '--db', str(self.db))
        for raw in ('{', '{"period":"X","period":"Y"}',
                    '{"period":"2026-01","physical":NaN,"cost_updates":[]}'):
            source = self.write('bad.json', raw)
            result = self.invoke('close', '--db', str(self.db),
                                 '--source', str(source))
            self.assertEqual(result.returncode, 2, raw)
            self.assertEqual(result.stdout, '')

    def test_init_refuses_existing_database(self):
        self.db.write_bytes(b'')
        result = self.invoke('init', '--db', str(self.db))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertEqual(self.db.read_bytes(), b'')

    def test_missing_database_is_not_created(self):
        source = self.write('close.json', load('close_2026_01.json'))
        result = self.invoke('close', '--db', str(self.db),
                             '--source', str(source))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertFalse(self.db.exists())
        shown = self.invoke('show', '--db', str(self.db), '--period', '2026-01')
        self.assertEqual(shown.returncode, 2)
        self.assertFalse(self.db.exists())

    def test_cross_process_recovery(self):
        self.invoke('init', '--db', str(self.db))
        for name in ('close_2026_01', 'close_2026_02'):
            source = self.write(name + '.json', load(name + '.json'))
            self.invoke('close', '--db', str(self.db), '--source', str(source))
        first = self.invoke('show', '--db', str(self.db),
                            '--period', '2026-02')
        second = self.invoke('show', '--db', str(self.db),
                             '--period', '2026-02')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(json.loads(first.stdout)['totals'],
                         totals(67, 4, 1, 70))


if __name__ == '__main__':
    unittest.main()
