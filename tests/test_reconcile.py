"""采购到货核销（material_plan.reconcile）集成测试。

覆盖：验收场景（同供应商同 SKU 两行、区间重叠、认领与放行）、能区分实现的
边界（部分认领不排除余量、多批汇总才暴露的容量冲突、同日边界、供应商/SKU
不匹配、清零批次按原收货量、制造批次与改写批次不可冒充收货、单批大数量）、
证据更正与放行保证、approve 重放/冲突/版本、整份回滚、跨进程恢复与 CLI；
另以固定种子随机小实例对拍暴力枚举，验证逐行端点确为全部完整合法分配上
的精确上下界。
"""
import copy
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from material_plan import traceability, costing
from material_plan.reconcile import init_db, reconcile, approve, show
from material_plan.reconcile import _compute_bounds

FIX = ROOT / 'fixtures' / 'reconcile'


def load(name):
    return json.loads((FIX / name).read_text())


def order(line_id, supplier='S', sku='M', earliest='2026-01-01',
          latest='2026-01-31', quantity=5):
    return {'line_id': line_id, 'supplier': supplier, 'sku': sku,
            'earliest': earliest, 'latest': latest,
            'ordered_quantity': quantity}


def orders_doc(*rows):
    return {'orders': list(rows)}


def lot(lot_id, sku, produced, remaining=None, inputs=(), blocked_by=()):
    return {'lot_id': lot_id, 'sku': sku, 'produced_quantity': produced,
            'remaining_quantity': produced if remaining is None else remaining,
            'inputs': [{'lot_id': name, 'quantity': count}
                       for name, count in inputs],
            'blocked_by': list(blocked_by)}


def physical(version, lots=(), shipments=(), recalls=(), exposures=()):
    return {'version': version, 'lots': list(lots),
            'shipments': list(shipments), 'recalls': list(recalls),
            'exposures': list(exposures)}


def arrival(lot_id, supplier='S', received_on='2026-01-05', accepted=0):
    return {'lot_id': lot_id, 'supplier': supplier,
            'received_on': received_on, 'accepted_quantity': accepted}


def binding(lot_id, line_id, portion, quantity):
    return {'lot_id': lot_id, 'line_id': line_id, 'portion': portion,
            'quantity': quantity}


def evidence(version, phys, arrivals=(), bindings=()):
    return {'expected_version': version, 'physical': phys,
            'arrivals': list(arrivals), 'bindings': list(bindings)}


def approval(approval_id, version, lines):
    return {'approval_id': approval_id, 'expected_version': version,
            'lines': [{'line_id': line_id, 'quantity': quantity}
                      for line_id, quantity in lines]}


def line_row(line_id, received, accepted, released=0):
    return {'line_id': line_id, 'received_min': received[0],
            'received_max': received[1], 'accepted_min': accepted[0],
            'accepted_max': accepted[1], 'released': released,
            'releasable': accepted[0] - released}


def voucher(approval_id, expected_version, committed_version, lines):
    return {'approval_id': approval_id, 'expected_version': expected_version,
            'committed_version': committed_version,
            'lines': [{'line_id': line_id, 'quantity': quantity}
                      for line_id, quantity in sorted(lines)]}


class ReconcileTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / 'reconcile.db'

    def tearDown(self):
        self._tmp.cleanup()

    def init(self, document):
        return init_db(self.db, copy.deepcopy(document))

    def reconcile(self, payload):
        return reconcile(self.db, copy.deepcopy(payload))

    def approve(self, payload):
        return approve(self.db, copy.deepcopy(payload))

    def show(self):
        return show(self.db)

    def db_dump(self):
        conn = sqlite3.connect(self.db)
        try:
            return list(conn.iterdump())
        finally:
            conn.close()

    def assert_reconcile_fails(self, payload):
        # 失败的证据更正不得留下证据、版本或凭证的任何部分变化。
        before = self.db_dump()
        with self.assertRaises(ValueError):
            self.reconcile(payload)
        self.assertEqual(self.db_dump(), before)

    def assert_approve_fails(self, payload):
        before = self.db_dump()
        with self.assertRaises(ValueError):
            self.approve(payload)
        self.assertEqual(self.db_dump(), before)


class InitTests(ReconcileTestBase):
    def test_initial_report(self):
        report = self.init(orders_doc(order('P1'), order('P2', quantity=7)))
        self.assertEqual(report, {'version': 0, 'lines': [
            line_row('P1', (0, 0), (0, 0)), line_row('P2', (0, 0), (0, 0))],
            'approvals': []})

    def test_existing_path_rejected_even_empty_file(self):
        empty = self.tmp / 'empty.db'
        empty.write_bytes(b'')
        with self.assertRaises(ValueError):
            init_db(empty, orders_doc(order('P1')))
        self.assertEqual(empty.read_bytes(), b'')

    def test_symlink_path_rejected(self):
        target = self.tmp / 'target'
        target.write_bytes(b'x')
        link = self.tmp / 'link.db'
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            init_db(link, orders_doc(order('P1')))
        self.assertEqual(target.read_bytes(), b'x')

    def test_reinit_rejected_and_database_kept(self):
        self.init(orders_doc(order('P1')))
        with self.assertRaises(ValueError):
            init_db(self.db, orders_doc(order('P2')))
        self.assertEqual(self.show()['lines'],
                         [line_row('P1', (0, 0), (0, 0))])

    def test_other_commands_never_create_database(self):
        missing = self.tmp / 'missing.db'
        with self.assertRaises(ValueError):
            reconcile(missing, evidence(0, physical(0)))
        with self.assertRaises(ValueError):
            approve(missing, approval('A1', 0, [('P1', 1)]))
        with self.assertRaises(ValueError):
            show(missing)
        self.assertFalse(missing.exists())

    def test_other_ledger_files_are_not_reconcile_databases(self):
        trace_db = self.tmp / 'trace.db'
        traceability.init_db(trace_db, {'items': [{'sku': 'M',
                                                   'components': []}]})
        with self.assertRaises(ValueError):
            reconcile(trace_db, evidence(0, physical(0)))
        with self.assertRaises(ValueError):
            show(trace_db)
        cost_db = self.tmp / 'cost.db'
        costing.init_db(cost_db)
        with self.assertRaises(ValueError):
            reconcile(cost_db, evidence(0, physical(0)))
        with self.assertRaises(ValueError):
            show(cost_db)

    def test_str_and_path_db_are_equivalent(self):
        init_db(str(self.db), orders_doc(order('P1')))
        report = reconcile(str(self.db), evidence(0, physical(1)))
        self.assertEqual(report['version'], 1)
        self.assertEqual(show(self.db), report)

    def test_orders_structure_rules(self):
        bad = [
            {},
            {'orders': [], 'extra': 1},
            # 重复行号。
            orders_doc(order('P1'), order('P1')),
            # 未知或缺失字段。
            {'orders': [{'line_id': 'P1', 'supplier': 'S', 'sku': 'M',
                         'earliest': '2026-01-01', 'latest': '2026-01-02'}]},
            {'orders': [{'line_id': 'P1', 'supplier': 'S', 'sku': 'M',
                         'earliest': '2026-01-01', 'latest': '2026-01-02',
                         'ordered_quantity': 1, 'extra': 1}]},
            # 标识符约束。
            orders_doc(order(' P1')), orders_doc(order('')),
            orders_doc(order('P1', supplier='')), orders_doc(order('P1', sku=' ')),
            # 日期规范与窗口。
            orders_doc(order('P1', earliest='2026-1-01')),
            orders_doc(order('P1', latest='2026-02-30')),
            orders_doc(order('P1', earliest='2026-01-10',
                             latest='2026-01-01')),
            # 数量：非布尔正整数及上限。
            orders_doc(order('P1', quantity=0)),
            orders_doc(order('P1', quantity=-1)),
            orders_doc(order('P1', quantity=True)),
            orders_doc(order('P1', quantity=1.5)),
            orders_doc(order('P1', quantity=10**9 + 1)),
        ]
        for index, document in enumerate(bad):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    self.init(document)
                self.assertFalse(self.db.exists())
        # 上限内合法；同供应商同 SKU 多行、区间重叠合法。
        report = self.init(orders_doc(
            order('P1', quantity=10**9, earliest='2026-01-01',
                  latest='2026-01-05'),
            order('P2', earliest='2026-01-03', latest='2026-01-08')))
        self.assertEqual([row['line_id'] for row in report['lines']],
                         ['P1', 'P2'])


class AcceptanceTests(ReconcileTestBase):
    """可复现验收：P1 窗 1–5 日、P2 窗 3–8 日各订 5 件；F 于 2 日收 3 全
    合格，A 于 4 日收 4 其中 2 合格，B 于 6 日收 3 无合格品。无认领时 F
    只能入 P1、B 只能入 P2、A 必须各分 2 件；收货范围均 [5,5]，P1 合格
    [3,5]、P2 [0,2]。"""

    def setUp(self):
        super().setUp()
        self.init(load('orders.json'))

    def test_full_acceptance_flow(self):
        # 初始证据：收货范围 [5,5]/[5,5]，合格 [3,5]/[0,2]。
        report = self.reconcile(load('evidence_initial.json'))
        self.assertEqual(report, {'version': 1, 'lines': [
            line_row('P1', (5, 5), (3, 5)), line_row('P2', (5, 5), (0, 2))],
            'approvals': []})

        # 同一申请为 P1 放 3 且为 P2 放 1：整批失败，P1 也未放行。
        self.assert_approve_fails(load('approve_overreach.json'))
        self.assertEqual(self.show()['lines'], report['lines'])

        # P1 单独放行 3：版本 1 -> 2。
        first = self.approve(load('approve_p1.json'))
        self.assertEqual(first, voucher('AP-P1', 1, 2, [('P1', 3)]))
        self.assertEqual(self.show()['lines'], [
            line_row('P1', (5, 5), (3, 5), released=3),
            line_row('P2', (5, 5), (0, 2))])

        # 新证据认领 A 的 2 件合格品到 P2：P2 合格变为 [2,2]，可放行 2。
        claimed = self.reconcile(load('evidence_claimed.json'))
        self.assertEqual(claimed['version'], 3)
        self.assertEqual(claimed['lines'], [
            line_row('P1', (5, 5), (3, 3), released=3),
            line_row('P2', (5, 5), (2, 2))])
        self.assertEqual(claimed['approvals'], [first])

        # P2 放行这 2 件：版本 3 -> 4。
        second = self.approve(load('approve_p2.json'))
        self.assertEqual(second, voucher('AP-P2', 3, 4, [('P2', 2)]))
        self.assertEqual(self.show()['lines'], [
            line_row('P1', (5, 5), (3, 3), released=3),
            line_row('P2', (5, 5), (2, 2), released=2)])

        # 去掉认领：完整分配仍存在，但 P2 合格下限 0 < 已放行 2，须拒绝；
        # 旧证据与旧凭证保持原样。
        self.assert_reconcile_fails(load('evidence_unclaimed.json'))
        self.assertEqual(self.show()['lines'], [
            line_row('P1', (5, 5), (3, 3), released=3),
            line_row('P2', (5, 5), (2, 2), released=2)])

        # 旧凭证无论当前版本如何都按原申请重放，不重复放行、不加版本。
        self.assertEqual(self.approve(load('approve_p2.json')), second)
        self.assertEqual(self.approve(load('approve_p1.json')), first)
        self.assertEqual(self.show()['version'], 4)

    def test_endpoints_come_from_the_joint_allocation_set(self):
        # A 必须各分 2 件：若只按单批检查会误以为 A 可整批入 P1。
        report = self.reconcile(load('evidence_initial.json'))
        lines = {row['line_id']: row for row in report['lines']}
        self.assertEqual((lines['P1']['received_min'],
                          lines['P1']['received_max']), (5, 5))
        self.assertEqual((lines['P2']['received_min'],
                          lines['P2']['received_max']), (5, 5))
        # 合格端点不一定能同时达到：P1=5 与 P2=2 互斥，但各自合法。
        self.assertEqual((lines['P1']['accepted_min'],
                          lines['P1']['accepted_max']), (3, 5))
        self.assertEqual((lines['P2']['accepted_min'],
                          lines['P2']['accepted_max']), (0, 2))


class BoundsTests(ReconcileTestBase):
    """能区分实现的核对边界。"""

    def test_partial_binding_does_not_exclude_remainder(self):
        # 认领是下界：认领 1 件后同一行仍可接收该批余量。
        self.init(orders_doc(order('P1'), order('P2')))
        phys = physical(1, [lot('A', 'M', 4)])
        report = self.reconcile(evidence(
            0, phys, [arrival('A', accepted=4)],
            [binding('A', 'P1', 'accepted', 1)]))
        self.assertEqual(report['lines'], [
            line_row('P1', (1, 4), (1, 4)), line_row('P2', (0, 3), (0, 3))])

    def test_capacity_conflict_only_visible_across_lots(self):
        # 每批单独都不超额度，汇总后才暴露冲突：整份证据拒绝。
        self.init(orders_doc(order('P1', quantity=5)))
        phys = physical(1, [lot('X', 'M', 4), lot('Y', 'M', 4)])
        self.assert_reconcile_fails(evidence(
            0, phys, [arrival('X', accepted=4), arrival('Y', accepted=4)]))
        # 去掉一批则可行。
        report = self.reconcile(evidence(
            0, phys, [arrival('X', accepted=4)]))
        self.assertEqual(report['lines'], [line_row('P1', (4, 4), (4, 4))])

    def test_other_line_capacity_forces_minimum_share(self):
        # 两行各 5 件额度、共 6 件合格品：每行至少分到 1 件，
        # 不是逐行独立看起来的 [0,5]。
        self.init(orders_doc(order('P1'), order('P2')))
        phys = physical(1, [lot('X', 'M', 3), lot('Y', 'M', 3)])
        report = self.reconcile(evidence(
            0, phys, [arrival('X', accepted=3), arrival('Y', accepted=3)]))
        self.assertEqual(report['lines'], [
            line_row('P1', (1, 5), (1, 5)), line_row('P2', (1, 5), (1, 5))])

    def test_same_day_window_boundaries(self):
        self.init(orders_doc(order('P1', earliest='2026-01-05',
                                   latest='2026-01-05')))
        # 窗口唯一一天当天可收。
        phys = physical(1, [lot('X', 'M', 2)])
        report = self.reconcile(evidence(
            0, phys, [arrival('X', received_on='2026-01-05', accepted=2)]))
        self.assertEqual(report['lines'], [line_row('P1', (2, 2), (2, 2))])
        # 早一天或晚一天都落在窗外，无完整分配。
        self.assert_reconcile_fails(evidence(
            1, phys, [arrival('X', received_on='2026-01-04', accepted=2)]))
        self.assert_reconcile_fails(evidence(
            1, phys, [arrival('X', received_on='2026-01-06', accepted=2)]))
        # 最早/最晚当天均含。
        self.assertEqual(self.show()['version'], 1)

    def test_supplier_and_sku_must_match(self):
        self.init(orders_doc(order('P1', supplier='S', sku='M')))
        phys = physical(1, [lot('X', 'M', 2), lot('Y', 'N', 2)])
        # 供应商不匹配：无完整分配。
        self.assert_reconcile_fails(evidence(
            0, phys, [arrival('X', supplier='T', accepted=2)]))
        # SKU 不匹配（实物批次 SKU 与采购行不同）：无完整分配。
        self.assert_reconcile_fails(evidence(
            0, phys, [arrival('Y', supplier='S', accepted=2)]))
        # 认领不得跨供应商、跨 SKU。
        self.assert_reconcile_fails(evidence(
            0, phys, [arrival('X', supplier='T', accepted=2)],
            [binding('X', 'P1', 'accepted', 1)]))
        # 合法证据仍可提交。
        report = self.reconcile(evidence(
            0, phys, [arrival('X', supplier='S', accepted=2)]))
        self.assertEqual(report['lines'], [line_row('P1', (2, 2), (2, 2))])

    def test_binding_cannot_bypass_window_via_history(self):
        # 曾有合法认领，更正后日期落出窗口：认领不能绕开窗口。
        self.init(orders_doc(order('P1', earliest='2026-01-01',
                                   latest='2026-01-05')))
        phys = physical(1, [lot('X', 'M', 2)])
        self.reconcile(evidence(
            0, phys, [arrival('X', received_on='2026-01-05', accepted=2)],
            [binding('X', 'P1', 'accepted', 2)]))
        self.assert_reconcile_fails(evidence(
            1, phys, [arrival('X', received_on='2026-01-06', accepted=2)],
            [binding('X', 'P1', 'accepted', 2)]))

    def test_zeroed_lot_still_counts_by_original_quantity(self):
        # 批次余额清零（已领用/发货）仍按原收货量核对。
        self.init(orders_doc(order('P1')))
        phys = physical(2, [lot('X', 'M', 5, remaining=0)],
                        shipments=[{'shipment_id': 'OUT', 'customer_id': 'C',
                                    'sku': 'M', 'quantity': 5,
                                    'allocations': [{'lot_id': 'X',
                                                     'quantity': 5}]}])
        report = self.reconcile(evidence(
            0, phys, [arrival('X', accepted=5)]))
        self.assertEqual(report['lines'], [line_row('P1', (5, 5), (5, 5))])

    def test_make_lot_cannot_impersonate_receipt(self):
        self.init(orders_doc(order('P1')))
        phys = physical(1, [lot('R', 'M', 2),
                            lot('X', 'M', 2, inputs=[('R', 2)])])
        self.assert_reconcile_fails(evidence(
            0, phys, [arrival('X', accepted=2)]))
        # 收货批次本身合法。
        report = self.reconcile(evidence(0, phys, [arrival('R', accepted=2)]))
        self.assertEqual(report['lines'], [line_row('P1', (2, 2), (2, 2))])

    def test_rewritten_or_dropped_old_lot_rejected(self):
        self.init(orders_doc(order('P1')))
        phys1 = physical(1, [lot('X', 'M', 3)])
        self.reconcile(evidence(0, phys1, [arrival('X', accepted=3)]))
        # 改写原收货量。
        self.assert_reconcile_fails(evidence(
            1, physical(2, [lot('X', 'M', 4)]), [arrival('X', accepted=3)]))
        # 改写原 SKU。
        self.assert_reconcile_fails(evidence(
            1, physical(2, [lot('X', 'N', 3)]), [arrival('X', accepted=3)]))
        # 旧批次变成制造批次。
        self.assert_reconcile_fails(evidence(
            1, physical(2, [lot('X', 'M', 3, inputs=[('R', 1)]),
                            lot('R', 'M', 1)]),
            [arrival('X', accepted=3)]))
        # 曾纳入批次不再提交。
        self.assert_reconcile_fails(evidence(1, physical(2, []), []))
        # 旧证据保持原样。
        self.assertEqual(self.show()['lines'],
                         [line_row('P1', (3, 3), (3, 3))])

    def test_out_of_scope_physical_content_is_ignored(self):
        self.init(orders_doc(order('P1')))
        phys = physical(
            7, [lot('X', 'M', 2, blocked_by=['RC']),
                lot('MADE', 'M', 9, inputs=[('X', 1)]),
                lot('OTHER', 'N', 4)],
            shipments=[{'shipment_id': 'OUT', 'customer_id': 'C', 'sku': 'N',
                        'quantity': 1,
                        'allocations': [{'lot_id': 'OTHER', 'quantity': 1}]}],
            recalls=[{'recall_id': 'RC', 'lot_id': 'X', 'status': 'active'}],
            exposures=[{'recall_id': 'RC', 'shipment_id': 'OUT',
                        'customer_id': 'C', 'sku': 'N', 'lot_id': 'OTHER',
                        'quantity': 1}])
        report = self.reconcile(evidence(0, phys, [arrival('X', accepted=1)]))
        self.assertEqual(report['lines'], [line_row('P1', (2, 2), (1, 1))])

    def test_large_quantities_are_not_expanded_per_unit(self):
        self.init(orders_doc(order('P1', quantity=10**9)))
        phys = physical(1, [lot('BIG', 'M', 10**9)])
        report = self.reconcile(evidence(
            0, phys, [arrival('BIG', accepted=10**9 - 7)]))
        self.assertEqual(report['lines'], [
            line_row('P1', (10**9, 10**9), (10**9 - 7, 10**9 - 7))])

    def test_multiple_supplier_sku_groups_are_independent(self):
        self.init(orders_doc(order('P1', supplier='S', sku='M'),
                             order('P2', supplier='T', sku='N', quantity=3)))
        phys = physical(1, [lot('X', 'M', 2), lot('Y', 'N', 3)])
        report = self.reconcile(evidence(
            0, phys, [arrival('X', supplier='S', accepted=2),
                      arrival('Y', supplier='T', accepted=1)]))
        self.assertEqual(report['lines'], [
            line_row('P1', (2, 2), (2, 2)), line_row('P2', (3, 3), (1, 1))])

    def test_arrival_without_any_matching_line_is_conflict(self):
        self.init(orders_doc(order('P1', supplier='S', sku='M')))
        phys = physical(1, [lot('X', 'M', 2), lot('Z', 'M', 1)])
        self.assert_reconcile_fails(evidence(
            0, phys, [arrival('X', accepted=2),
                      arrival('Z', supplier='UNKNOWN', accepted=1)]))

    def test_empty_first_evidence_is_allowed(self):
        self.init(orders_doc(order('P1')))
        report = self.reconcile(evidence(0, physical(1)))
        self.assertEqual(report['lines'], [line_row('P1', (0, 0), (0, 0))])
        self.assertEqual(report['version'], 1)


class EvidenceStructureTests(ReconcileTestBase):
    def setUp(self):
        super().setUp()
        self.init(orders_doc(order('P1')))
        self.phys = physical(1, [lot('X', 'M', 3)])

    def test_top_level_and_row_fields_are_strict(self):
        bad = [
            {},
            evidence(0, self.phys) | {'extra': 1},
            {k: v for k, v in evidence(0, self.phys).items()
             if k != 'bindings'},
            evidence(True, self.phys),
            evidence(-1, self.phys),
            evidence(0, self.phys, [arrival('X') | {'extra': 1}]),
            evidence(0, self.phys, [arrival('X', received_on='2026-1-5')]),
            evidence(0, self.phys, [arrival('X', accepted=-1)]),
            evidence(0, self.phys, [arrival('X', accepted=True)]),
            evidence(0, self.phys, [arrival('X', accepted=4)]),
            evidence(0, self.phys, [arrival('X'), arrival('X')]),
            evidence(0, self.phys, [arrival('GHOST')]),
            evidence(0, self.phys, [arrival('X')],
                     [binding('X', 'P1', 'accepted', 1),
                      binding('X', 'P1', 'accepted', 1)]),
            evidence(0, self.phys, [arrival('X')],
                     [binding('X', 'P1', 'weird', 1)]),
            evidence(0, self.phys, [arrival('X')],
                     [binding('X', 'P1', 'accepted', 0)]),
            evidence(0, self.phys, [arrival('X')],
                     [binding('X', 'P1', 'accepted', True)]),
            evidence(0, self.phys, [arrival('X')],
                     [binding('X', 'GHOST', 'accepted', 1)]),
            evidence(0, self.phys, [arrival('X')],
                     [binding('GHOST', 'P1', 'accepted', 1)]),
            evidence(0, self.phys, [arrival('X', accepted=2)],
                     [binding('X', 'P1', 'accepted', 3)]),
            evidence(0, self.phys, [arrival('X', accepted=2)],
                     [binding('X', 'P1', 'other', 2)]),
        ]
        for index, payload in enumerate(bad):
            with self.subTest(index=index):
                self.assert_reconcile_fails(payload)

    def test_physical_structure_rules(self):
        bad_physical = [
            {},
            physical(True),
            physical(1, [lot('X', 'M', 3), lot('X', 'M', 3)]),
            physical(1, [lot('X', 'M', 0)]),
            physical(1, [lot('X', 'M', True, 1)]),
            physical(1, [lot(' X', 'M', 3)]),
            physical(1, [{'lot_id': 'X', 'sku': 'M', 'produced_quantity': 3,
                          'remaining_quantity': 3, 'inputs': []}]),
        ]
        for index, phys in enumerate(bad_physical):
            with self.subTest(index=index):
                self.assert_reconcile_fails(evidence(0, phys))

    def test_claim_sum_per_lot_portion_is_bounded(self):
        # 同一批合格品认给两行合计超该批合格量：拒绝（不能被同日他批掩盖）。
        self.init  # 数据库已有 P1；另建两行场景
        db2 = self.tmp / 'two.db'
        init_db(db2, orders_doc(order('P1'), order('P2')))
        phys = physical(1, [lot('X', 'M', 2), lot('Y', 'M', 5)])
        with self.assertRaises(ValueError):
            reconcile(db2, evidence(
                0, phys, [arrival('X', accepted=2), arrival('Y', accepted=5)],
                [binding('X', 'P1', 'accepted', 2),
                 binding('X', 'P2', 'accepted', 2)]))

    def test_claims_exceeding_ordered_quantity_rejected(self):
        phys = physical(1, [lot('X', 'M', 8), lot('Y', 'M', 8)])
        db2 = self.tmp / 'cap.db'
        init_db(db2, orders_doc(order('P1', quantity=5),
                                order('P2', quantity=20)))
        with self.assertRaises(ValueError):
            reconcile(db2, evidence(
                0, phys, [arrival('X', accepted=8), arrival('Y', accepted=8)],
                [binding('X', 'P1', 'accepted', 4),
                 binding('Y', 'P1', 'accepted', 4)]))


class CorrectionTests(ReconcileTestBase):
    def setUp(self):
        super().setUp()
        self.init(orders_doc(order('P1', supplier='S', earliest='2026-01-01',
                                   latest='2026-01-05'),
                             order('P2', supplier='T', sku='M',
                                   earliest='2026-01-01',
                                   latest='2026-01-31')))
        self.phys = physical(1, [lot('X', 'M', 4)])

    def test_supplier_date_and_acceptance_can_be_corrected(self):
        self.reconcile(evidence(
            0, self.phys, [arrival('X', supplier='S',
                                   received_on='2026-01-03', accepted=4)]))
        # 供应商更正到 T（落入 P2）、日期与验收数量也更正。
        report = self.reconcile(evidence(
            1, self.phys, [arrival('X', supplier='T',
                                   received_on='2026-01-20', accepted=1)]))
        self.assertEqual(report['lines'], [
            line_row('P1', (0, 0), (0, 0)), line_row('P2', (4, 4), (1, 1))])

    def test_correction_must_still_guarantee_released_quantities(self):
        self.reconcile(evidence(0, self.phys, [arrival('X', accepted=3)]))
        self.approve(approval('AP-1', 1, [('P1', 3)]))
        # 验收更正为 2：合格下限 2 < 已放行 3，整份拒绝，旧证据与凭证保留。
        self.assert_reconcile_fails(evidence(
            2, self.phys, [arrival('X', accepted=2)]))
        current = self.show()
        self.assertEqual(current['lines'], [
            line_row('P1', (4, 4), (3, 3), released=3),
            line_row('P2', (0, 0), (0, 0))])
        self.assertEqual(current['approvals'],
                         [voucher('AP-1', 1, 2, [('P1', 3)])])
        # 调用方核对后提交合法新证据（验收改回 3）可以成功。
        report = self.reconcile(evidence(2, self.phys,
                                         [arrival('X', accepted=3)]))
        self.assertEqual(report['version'], 3)

    def test_version_must_match_current(self):
        self.reconcile(evidence(0, self.phys, [arrival('X', accepted=1)]))
        self.assert_reconcile_fails(evidence(0, self.phys,
                                             [arrival('X', accepted=1)]))
        self.assert_reconcile_fails(evidence(2, self.phys,
                                             [arrival('X', accepted=1)]))
        report = self.reconcile(evidence(1, self.phys,
                                         [arrival('X', accepted=1)]))
        self.assertEqual(report['version'], 2)


class ApproveTests(ReconcileTestBase):
    def setUp(self):
        super().setUp()
        self.init(orders_doc(order('P1'), order('P2')))
        phys = physical(1, [lot('X', 'M', 4), lot('Y', 'M', 4)])
        # 两行各可得合格 [0,4]，收货合计 8、行额度 5：各至少 3。
        self.reconcile(evidence(
            0, phys, [arrival('X', accepted=4), arrival('Y', accepted=4)]))

    def test_structure_rules(self):
        bad = [
            {},
            approval('A1', 1, [('P1', 1)]) | {'extra': 1},
            approval('A1', True, [('P1', 1)]),
            approval('A1', -1, [('P1', 1)]),
            approval('', 1, [('P1', 1)]),
            approval('A1', 1, []),
            approval('A1', 1, [('P1', 1), ('P1', 1)]),
            approval('A1', 1, [('P1', 0)]),
            approval('A1', 1, [('P1', -2)]),
            approval('A1', 1, [('P1', True)]),
            approval('A1', 1, [('GHOST', 1)]),
            # 行内缺失或未知字段。
            {'approval_id': 'A1', 'expected_version': 1,
             'lines': [{'line_id': 'P1'}]},
            {'approval_id': 'A1', 'expected_version': 1,
             'lines': [{'line_id': 'P1', 'quantity': 1, 'extra': 2}]},
        ]
        for index, payload in enumerate(bad):
            with self.subTest(index=index):
                self.assert_approve_fails(payload)

    def test_releasable_uses_accepted_lower_bound_minus_released(self):
        # 每行合格下限 3：放行 4 失败，放行 3 成功。
        self.assert_approve_fails(approval('A1', 1, [('P1', 4)]))
        first = self.approve(approval('A1', 1, [('P1', 3)]))
        self.assertEqual(first, voucher('A1', 1, 2, [('P1', 3)]))
        # 已放行 3 后余量 0：再放 1 失败。
        self.assert_approve_fails(approval('A2', 2, [('P1', 1)]))
        # 另一行独立放行。
        second = self.approve(approval('A2', 2, [('P2', 2)]))
        self.assertEqual(second, voucher('A2', 2, 3, [('P2', 2)]))
        self.assertEqual(self.show()['lines'], [
            line_row('P1', (3, 5), (3, 5), released=3),
            line_row('P2', (3, 5), (3, 5), released=2)])

    def test_batch_is_atomic_across_lines(self):
        # 同批一行超量即整批失败，另一行也不放行。
        self.assert_approve_fails(approval('A1', 1, [('P1', 3), ('P2', 4)]))
        self.assertEqual(self.show()['lines'], [
            line_row('P1', (3, 5), (3, 5)), line_row('P2', (3, 5), (3, 5))])

    def test_replay_ignores_key_and_line_order(self):
        first = self.approve(approval('A1', 1, [('P1', 1), ('P2', 2)]))
        # 行序不同、键序不同的同一语义请求：返回原凭证，不加版本。
        replayed = self.approve({'expected_version': 1, 'lines': [
            {'quantity': 2, 'line_id': 'P2'},
            {'quantity': 1, 'line_id': 'P1'}], 'approval_id': 'A1'})
        self.assertEqual(replayed, first)
        self.assertEqual(self.show()['version'], 2)

    def test_same_id_different_content_conflicts_and_failure_frees_id(self):
        self.approve(approval('A1', 1, [('P1', 1)]))
        self.assert_approve_fails(approval('A1', 1, [('P1', 2)]))
        self.assert_approve_fails(approval('A1', 2, [('P1', 1)]))
        # 失败不占 ID：超量失败后同一 ID 可改正重试。
        self.assert_approve_fails(approval('A2', 2, [('P1', 9)]))
        retry = self.approve(approval('A2', 2, [('P1', 1)]))
        self.assertEqual(retry, voucher('A2', 2, 3, [('P1', 1)]))

    def test_replay_survives_version_changes(self):
        first = self.approve(approval('A1', 1, [('P1', 1)]))
        self.approve(approval('A2', 2, [('P2', 1)]))
        # 版本已前进：重放仍返回原凭证，不重复放行。
        self.assertEqual(self.approve(approval('A1', 1, [('P1', 1)])), first)
        current = self.show()
        self.assertEqual(current['version'], 3)
        self.assertEqual(current['approvals'], [
            voucher('A1', 1, 2, [('P1', 1)]),
            voucher('A2', 2, 3, [('P2', 1)])])

    def test_approve_does_not_shrink_allocation_set(self):
        # 放行后重导同一证据：边界不变，只是可放行量减少。
        self.approve(approval('A1', 1, [('P1', 3)]))
        phys = physical(1, [lot('X', 'M', 4), lot('Y', 'M', 4)])
        report = self.reconcile(evidence(
            2, phys, [arrival('X', accepted=4), arrival('Y', accepted=4)]))
        self.assertEqual(report['lines'], [
            line_row('P1', (3, 5), (3, 5), released=3),
            line_row('P2', (3, 5), (3, 5))])

    def test_expected_version_must_match_for_new_approval(self):
        self.assert_approve_fails(approval('A1', 0, [('P1', 1)]))
        self.assert_approve_fails(approval('A1', 2, [('P1', 1)]))


class RollbackTests(ReconcileTestBase):
    def test_failed_requests_leave_no_partial_state(self):
        self.init(orders_doc(order('P1')))
        phys = physical(1, [lot('X', 'M', 3)])
        self.reconcile(evidence(0, phys, [arrival('X', accepted=3)]))
        self.approve(approval('A1', 1, [('P1', 2)]))
        # 坏证据（验收超收货）与坏放行（超可放行量）都不留痕迹。
        self.assert_reconcile_fails(evidence(2, phys, [arrival('X', accepted=9)]))
        self.assert_approve_fails(approval('A2', 2, [('P1', 2)]))
        current = self.show()
        self.assertEqual(current['version'], 2)
        self.assertEqual(current['lines'],
                         [line_row('P1', (3, 3), (3, 3), released=2)])
        self.assertEqual(current['approvals'],
                         [voucher('A1', 1, 2, [('P1', 2)])])

    def test_api_does_not_mutate_caller_objects(self):
        document = orders_doc(order('P1'))
        before = copy.deepcopy(document)
        self.init(document)
        self.assertEqual(document, before)
        payload = evidence(0, physical(1, [lot('X', 'M', 3)]),
                           [arrival('X', accepted=3)],
                           [binding('X', 'P1', 'accepted', 1)])
        before = copy.deepcopy(payload)
        self.reconcile(payload)
        self.assertEqual(payload, before)
        request = approval('A1', 1, [('P1', 1)])
        before = copy.deepcopy(request)
        self.approve(request)
        self.assertEqual(request, before)
        failing = evidence(9, physical(1), [])
        before = copy.deepcopy(failing)
        with self.assertRaises(ValueError):
            self.reconcile(failing)
        self.assertEqual(failing, before)


class CliTests(ReconcileTestBase):
    def invoke(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        return subprocess.run([sys.executable, '-m',
                               'material_plan.reconcile_cli', *args],
                              capture_output=True, text=True, timeout=60,
                              env=env)

    def write(self, name, value):
        path = self.tmp / name
        path.write_text(value if isinstance(value, str)
                        else json.dumps(value))
        return path

    def test_full_flow_and_cross_process_recovery(self):
        result = self.invoke('init', '--db', str(self.db), '--source',
                             str(FIX / 'orders.json'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('\n'), 1)
        self.assertEqual(json.loads(result.stdout)['version'], 0)

        steps = [('reconcile', 'evidence_initial.json', 1),
                 ('approve', 'approve_p1.json', 2),
                 ('reconcile', 'evidence_claimed.json', 3),
                 ('approve', 'approve_p2.json', 4)]
        outputs = []
        for command, name, version in steps:
            source = FIX / (name if command == 'reconcile' else name)
            result = self.invoke(command, '--db', str(self.db),
                                 '--source', str(FIX / name))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count('\n'), 1)
            outputs.append(result.stdout)
        self.assertEqual(json.loads(outputs[0])['lines'], [
            line_row('P1', (5, 5), (3, 5)), line_row('P2', (5, 5), (0, 2))])
        self.assertEqual(json.loads(outputs[3]),
                         voucher('AP-P2', 3, 4, [('P2', 2)]))

        # 去掉认领的更正失败：退出 2、stdout 为空、stderr 有原因。
        result = self.invoke('reconcile', '--db', str(self.db), '--source',
                             str(FIX / 'evidence_unclaimed.json'))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)

        # 重放旧申请仍返回原凭证；新进程 show 与最后提交状态一致。
        result = self.invoke('approve', '--db', str(self.db), '--source',
                             str(FIX / 'approve_p2.json'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         voucher('AP-P2', 3, 4, [('P2', 2)]))
        shown = self.invoke('show', '--db', str(self.db))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)['version'], 4)
        again = self.invoke('show', '--db', str(self.db))
        self.assertEqual(again.stdout, shown.stdout)

    def test_failure_exit_two_empty_stdout_with_stderr(self):
        self.invoke('init', '--db', str(self.db), '--source',
                    str(FIX / 'orders.json'))
        source = self.write('bad.json', load('evidence_initial.json')
                            | {'expected_version': 9})
        result = self.invoke('reconcile', '--db', str(self.db),
                             '--source', str(source))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        result = self.invoke('approve', '--db', str(self.db),
                             '--source', str(FIX / 'approve_overreach.json'))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')

    def test_malformed_json_rules_via_cli(self):
        self.invoke('init', '--db', str(self.db), '--source',
                    str(FIX / 'orders.json'))
        for raw in ('{', '{"expected_version":0,"expected_version":1}',
                    '{"expected_version":NaN,"physical":{},'
                    '"arrivals":[],"bindings":[]}'):
            source = self.write('bad.json', raw)
            result = self.invoke('reconcile', '--db', str(self.db),
                                 '--source', str(source))
            self.assertEqual(result.returncode, 2, raw)
            self.assertEqual(result.stdout, '')

    def test_init_refuses_existing_database(self):
        self.db.write_bytes(b'')
        result = self.invoke('init', '--db', str(self.db), '--source',
                             str(FIX / 'orders.json'))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertEqual(self.db.read_bytes(), b'')

    def test_missing_database_is_not_created(self):
        for command, source in (('reconcile', FIX / 'evidence_initial.json'),
                                ('approve', FIX / 'approve_p1.json')):
            result = self.invoke(command, '--db', str(self.db),
                                 '--source', str(source))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertFalse(self.db.exists())
        result = self.invoke('show', '--db', str(self.db))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.db.exists())


class BruteForceCrossCheckTests(unittest.TestCase):
    """固定种子随机小实例：_compute_bounds 对拍暴力枚举的全部完整分配。"""

    def brute(self, lines, arrivals, bindings):
        lots = list(arrivals.values())
        lot_index = {a['lot_id']: k for k, a in enumerate(lots)}
        line_ids = list(lines)
        best = {}

        def eligible(a, lid):
            line = lines[lid]
            return a['supplier'] == line['supplier'] \
                and a['sku'] == line['sku'] \
                and line['earliest'] <= a['received_on'] <= line['latest']

        def split(total, parts):
            if parts == 0:
                return
            if parts == 1:
                yield (total,)
                return
            for first in range(total + 1):
                for rest in split(total - first, parts - 1):
                    yield (first,) + rest

        def record(assign_a, assign_o):
            totals = {}
            accepteds = {}
            for lid in line_ids:
                totals[lid] = sum(assign_a[k].get(lid, 0) + assign_o[k].get(lid, 0)
                                  for k in range(len(lots)))
                accepteds[lid] = sum(assign_a[k].get(lid, 0)
                                     for k in range(len(lots)))
                if totals[lid] > lines[lid]['ordered_quantity']:
                    return
            for b in bindings:
                source = assign_a if b['portion'] == 'accepted' else assign_o
                if source[lot_index[b['lot_id']]].get(b['line_id'], 0) \
                        < b['quantity']:
                    return
            for lid in line_ids:
                row = best.setdefault(lid, [totals[lid], totals[lid],
                                            accepteds[lid], accepteds[lid]])
                row[0] = min(row[0], totals[lid])
                row[1] = max(row[1], totals[lid])
                row[2] = min(row[2], accepteds[lid])
                row[3] = max(row[3], accepteds[lid])

        def walk(i, assign_a, assign_o):
            if i == len(lots):
                record(assign_a, assign_o)
                return
            a = lots[i]
            elig = [lid for lid in line_ids if eligible(a, lid)]
            other = a['received'] - a['accepted']
            for dist_a in split(a['accepted'], len(elig)):
                for dist_o in split(other, len(elig)):
                    assign_a[i] = dict(zip(elig, dist_a))
                    assign_o[i] = dict(zip(elig, dist_o))
                    walk(i + 1, assign_a, assign_o)
                    del assign_a[i]
                    del assign_o[i]

        walk(0, {}, {})
        if not best:
            return None
        return {lid: {'t_min': row[0], 't_max': row[1],
                      'a_min': row[2], 'a_max': row[3]}
                for lid, row in best.items()}

    def test_seeded_random_instances_match_brute_force(self):
        rng = random.Random(20261120)
        base = date(2026, 1, 1)
        checked = 0
        for _ in range(900):
            lines = {}
            for i in range(rng.randint(1, 3)):
                earliest = base + timedelta(days=rng.randint(0, 3))
                lines['P%d' % i] = {
                    'line_id': 'P%d' % i,
                    # 多数实例同供应商同 SKU 以提高可行率，仍保留不匹配组合。
                    'supplier': rng.choice('SSST'), 'sku': rng.choice('MMMN'),
                    'earliest': earliest,
                    'latest': earliest + timedelta(days=rng.randint(1, 8)),
                    'ordered_quantity': rng.randint(2, 9)}
            arrivals = {}
            for i in range(rng.randint(1, 3)):
                received = rng.randint(1, 5)
                arrivals['L%d' % i] = {
                    'lot_id': 'L%d' % i,
                    'supplier': rng.choice('SSST'), 'sku': rng.choice('MMMN'),
                    'received': received,
                    'received_on': base + timedelta(days=rng.randint(0, 8)),
                    'accepted': rng.randint(0, received)}
            bindings = []
            claimed = {}
            valid = True
            for _ in range(rng.randint(0, 2)):
                a = rng.choice(list(arrivals.values()))
                lid = rng.choice(list(lines))
                line = lines[lid]
                portion = rng.choice(['accepted', 'other'])
                pool = a['accepted'] if portion == 'accepted' \
                    else a['received'] - a['accepted']
                if a['supplier'] != line['supplier'] or a['sku'] != line['sku'] \
                        or not line['earliest'] <= a['received_on'] \
                        <= line['latest'] or pool < 1:
                    continue
                key = (a['lot_id'], portion)
                take = rng.randint(1, pool)
                claimed[key] = claimed.get(key, 0) + take
                if claimed[key] > pool:
                    valid = False
                    break
                bindings.append({'lot_id': a['lot_id'], 'line_id': lid,
                                 'portion': portion, 'quantity': take})
            if not valid:
                continue
            try:
                got = _compute_bounds(lines, arrivals, bindings)
            except ValueError:
                got = None
            want = self.brute(lines, arrivals, bindings)
            self.assertEqual(got is not None, want is not None,
                             (lines, arrivals, bindings))
            if got is not None:
                self.assertEqual(got, want, (lines, arrivals, bindings))
                checked += 1
        self.assertGreater(checked, 100)


if __name__ == '__main__':
    unittest.main()
