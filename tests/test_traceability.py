"""实物批次谱系与召回出库闸（material_plan.traceability）集成测试。

覆盖：验收场景、召回沿实际用料传播与多路径去重、出库闸、历史暴露、
关闭单项召回、重放、批次回滚、身份上限、主数据校验、跨进程恢复与 CLI。
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from material_plan import traceability
from material_plan.traceability import (MAX_IDENTITIES, apply_batch, init_db,
                                        snapshot)

FIX = ROOT / 'fixtures' / 'trace'


def load(name):
    return json.loads((FIX / name).read_text())


def master(*items):
    return {'items': [{'sku': sku, 'components': components}
                      for sku, components in items]}


def comp(sku, quantity):
    return {'sku': sku, 'quantity': quantity}


def receive(lot_id, sku, quantity):
    return {'op': 'receive', 'lot_id': lot_id, 'sku': sku, 'quantity': quantity}


def make(lot_id, sku, quantity, inputs):
    return {'op': 'make', 'lot_id': lot_id, 'sku': sku, 'quantity': quantity,
            'inputs': [{'lot_id': name, 'quantity': count}
                       for name, count in inputs]}


def ship(shipment_id, customer_id, sku, quantity, allocations):
    return {'op': 'ship', 'shipment_id': shipment_id,
            'customer_id': customer_id, 'sku': sku, 'quantity': quantity,
            'allocations': [{'lot_id': name, 'quantity': count}
                            for name, count in allocations]}


def recall(recall_id, lot_id):
    return {'op': 'recall', 'recall_id': recall_id, 'lot_id': lot_id}


def close_recall(recall_id):
    return {'op': 'close_recall', 'recall_id': recall_id}


def request(request_id, expected_version, operations):
    return {'request_id': request_id, 'expected_version': expected_version,
            'operations': operations}


class TraceTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / 'trace.db'

    def tearDown(self):
        self._tmp.cleanup()

    def init(self, master_data=None):
        return init_db(self.db, copy.deepcopy(master_data or load('master.json')))

    def apply(self, payload):
        return apply_batch(self.db, copy.deepcopy(payload))

    def snap(self):
        return snapshot(self.db)

    def assert_fails(self, payload):
        before = self.snap()
        with self.assertRaises(ValueError):
            self.apply(payload)
        self.assertEqual(self.snap(), before)

    def lots(self):
        return {row['lot_id']: row for row in self.snap()['lots']}


class InitTests(TraceTestBase):
    def test_initial_snapshot_is_empty_version_zero(self):
        snap = self.init()
        self.assertEqual(snap, {'version': 0, 'lots': [], 'shipments': [],
                                'recalls': [], 'exposures': []})

    def test_existing_path_rejected_even_empty_file(self):
        empty = self.tmp / 'empty.db'
        empty.write_bytes(b'')
        with self.assertRaises(ValueError):
            init_db(empty, load('master.json'))
        self.assertEqual(empty.read_bytes(), b'')

    def test_symlink_path_rejected(self):
        target = self.tmp / 'target'
        target.write_bytes(b'x')
        link = self.tmp / 'link.db'
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            init_db(link, load('master.json'))
        self.assertEqual(target.read_bytes(), b'x')

    def test_failed_init_leaves_no_database(self):
        path = self.tmp / 'broken.db'
        with self.assertRaises(ValueError):
            init_db(path, {'items': []})
        self.assertFalse(path.exists())
        with self.assertRaises(ValueError):
            snapshot(path)

    def test_apply_and_snapshot_never_create_database(self):
        missing = self.tmp / 'missing.db'
        with self.assertRaises(ValueError):
            apply_batch(missing, request('R', 0, [receive('L', 'RAW', 1)]))
        with self.assertRaises(ValueError):
            snapshot(missing)
        self.assertFalse(missing.exists())

    def test_str_and_path_db_are_equivalent(self):
        init_db(str(self.db), load('master.json'))
        self.assertEqual(snapshot(str(self.db)), snapshot(self.db))
        result = apply_batch(str(self.db), request(
            'R', 0, [receive('L', 'RAW', 3)]))
        self.assertEqual(result['version'], 1)
        self.assertEqual(snapshot(self.db)['lots'][0]['remaining_quantity'], 3)

    def test_master_must_be_exactly_items(self):
        for bad in ({}, {'items': [], 'extra': 1}, {'items': load('master.json')['items'], 'x': 0}):
            path = self.tmp / ('bad%d.db' % id(bad))
            with self.assertRaises(ValueError):
                init_db(path, bad)
            self.assertFalse(path.exists())

    def test_master_structure_and_bom_rules(self):
        cycle = master(('A', [comp('B', 1)]), ('B', [comp('A', 1)]))
        self_cycle = master(('A', [comp('A', 1)]))
        unknown = master(('A', [comp('GHOST', 1)]))
        duplicate_sku = master(('A', []), ('A', []))
        duplicate_component = master(('A', [comp('B', 1), comp('B', 2)]), ('B', []))
        zero_quantity = master(('A', [comp('B', 0)]), ('B', []))
        bool_quantity = master(('A', [comp('B', True)]), ('B', []))
        unknown_item_field = {'items': [{'sku': 'A', 'components': [],
                                         'lead_days': 1}]}
        missing_component_field = {'items': [{'sku': 'A',
                                              'components': [{'sku': 'A'}]}]}
        for index, bad in enumerate((cycle, self_cycle, unknown, duplicate_sku,
                                     duplicate_component, zero_quantity,
                                     bool_quantity, unknown_item_field,
                                     missing_component_field)):
            with self.subTest(index=index):
                path = self.tmp / ('bad%d.db' % index)
                with self.assertRaises(ValueError):
                    init_db(path, bad)
                self.assertFalse(path.exists())

    def test_master_shared_component_is_allowed(self):
        data = master(('P1', [comp('S', 1)]), ('P2', [comp('S', 2)]), ('S', []))
        snap = self.init(data)
        self.assertEqual(snap['version'], 0)

    def test_input_objects_are_not_mutated(self):
        master_data = load('master.json')
        build = load('request_physical_build.json')
        master_before = copy.deepcopy(master_data)
        build_before = copy.deepcopy(build)
        self.init(master_data)
        self.apply(build)
        self.assertEqual(master_data, master_before)
        self.assertEqual(build, build_before)


class AcceptanceTests(TraceTestBase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_full_scenario(self):
        first = self.apply(load('request_physical_build.json'))
        self.assertEqual(first['version'], 1)
        lots = {row['lot_id']: row for row in first['lots']}
        self.assertEqual({name: row['remaining_quantity'] for name, row in lots.items()},
                         {'R': 0, 'S1': 1, 'S2': 1, 'K': 0})
        self.assertEqual(lots['S1']['inputs'], [{'lot_id': 'R', 'quantity': 4}])
        self.assertEqual(lots['S2']['inputs'], [{'lot_id': 'R', 'quantity': 2}])
        self.assertEqual(lots['K']['inputs'], [{'lot_id': 'S1', 'quantity': 1}])
        self.assertEqual(first['shipments'], [{
            'shipment_id': 'OUT1', 'customer_id': 'C1', 'sku': 'KIT',
            'quantity': 1, 'allocations': [{'lot_id': 'K', 'quantity': 1}]}])
        self.assertEqual(first['exposures'], [])

        recalled = self.apply(load('request_recall_x.json'))
        self.assertEqual(recalled['version'], 2)
        blocked = {row['lot_id']: row['blocked_by'] for row in recalled['lots']}
        self.assertEqual(blocked, {'R': ['X'], 'S1': ['X'], 'S2': ['X'],
                                   'K': ['X']})
        self.assertEqual(recalled['exposures'], [{
            'recall_id': 'X', 'shipment_id': 'OUT1', 'customer_id': 'C1',
            'sku': 'KIT', 'lot_id': 'K', 'quantity': 1}])
        # 根与末端余额均为 0 也不截断传播或客户暴露。
        self.assertEqual(recalled['recalls'],
                         [{'recall_id': 'X', 'lot_id': 'R', 'status': 'active'}])

        clean = self.apply(load('request_receive_clean.json'))
        self.assertEqual(clean['version'], 3)
        self.assertEqual(self.lots()['CLEAN']['blocked_by'], [])

    def test_blocked_make_then_close_is_rejected_at_the_make(self):
        self.apply(load('request_physical_build.json'))
        self.apply(load('request_recall_x.json'))
        self.apply(load('request_receive_clean.json'))
        self.assert_fails(load('request_blocked_make_then_close.json'))
        snap = self.snap()
        self.assertEqual(snap['version'], 3)
        self.assertEqual({row['lot_id'] for row in snap['lots']},
                         {'R', 'S1', 'S2', 'K', 'CLEAN'})
        self.assertEqual({row['lot_id']: row['remaining_quantity']
                          for row in snap['lots']},
                         {'R': 0, 'S1': 1, 'S2': 1, 'K': 0, 'CLEAN': 1})

    def test_second_recall_survives_closing_the_first(self):
        self.apply(load('request_physical_build.json'))
        self.apply(load('request_recall_x.json'))
        self.apply(load('request_receive_clean.json'))
        self.apply(load('request_recall_y.json'))
        closed = self.apply(load('request_close_x.json'))
        blocked = {row['lot_id']: row['blocked_by'] for row in closed['lots']}
        self.assertEqual(blocked, {'CLEAN': [], 'K': ['Y'], 'R': [], 'S1': ['Y'],
                                   'S2': []})
        self.assertEqual(closed['exposures'], [{
            'recall_id': 'Y', 'shipment_id': 'OUT1', 'customer_id': 'C1',
            'sku': 'KIT', 'lot_id': 'K', 'quantity': 1}])
        self.assertEqual([(row['recall_id'], row['status'])
                          for row in closed['recalls']],
                         [('X', 'closed'), ('Y', 'active')])

        resumed = self.apply(load('request_resume_shipping.json'))
        self.assertEqual(resumed['version'], 6)
        self.assertEqual(self.lots()['S2']['remaining_quantity'], 0)
        self.assertEqual(self.lots()['K3']['remaining_quantity'], 0)
        self.assertEqual([(row['recall_id'], row['shipment_id'], row['lot_id'])
                          for row in resumed['exposures']],
                         [('Y', 'OUT1', 'K')])

        replay = self.apply(load('request_physical_build.json'))
        self.assertEqual(replay['version'], 1)
        self.assertEqual(replay['shipments'][0]['shipment_id'], 'OUT1')
        current = self.snap()
        self.assertEqual(current['version'], 6)
        self.assertEqual({row['lot_id'] for row in current['lots']},
                         {'R', 'S1', 'S2', 'K', 'CLEAN', 'K3'})
        self.assertEqual([(row['recall_id'], row['status'])
                          for row in current['recalls']],
                         [('X', 'closed'), ('Y', 'active')])


class PropagationTests(TraceTestBase):
    def test_diamond_paths_are_deduplicated(self):
        self.init(master(('RAW', []), ('A', [comp('RAW', 1)]),
                         ('B', [comp('RAW', 1)]),
                         ('TOP', [comp('A', 1), comp('B', 1)])))
        self.apply(request('BUILD', 0, [
            receive('R', 'RAW', 2),
            make('A1', 'A', 1, [('R', 1)]),
            make('B1', 'B', 1, [('R', 1)]),
            make('T', 'TOP', 1, [('A1', 1), ('B1', 1)]),
            ship('OUT', 'C', 'TOP', 1, [('T', 1)])]))
        snap = self.apply(request('RX', 1, [recall('X', 'R')]))
        top = next(row for row in snap['lots'] if row['lot_id'] == 'T')
        self.assertEqual(top['blocked_by'], ['X'])
        self.assertEqual(len(snap['exposures']), 1)
        self.assertEqual(snap['exposures'][0]['lot_id'], 'T')

        # 再指向另一祖先，两条路径同根同批仍只输出一个 recall 行。
        snap = self.apply(request('RA', 2, [recall('Z', 'A1')]))
        top = next(row for row in snap['lots'] if row['lot_id'] == 'T')
        self.assertEqual(top['blocked_by'], ['X', 'Z'])
        self.assertEqual([(row['recall_id'], row['lot_id'])
                          for row in snap['exposures']],
                         [('X', 'T'), ('Z', 'T')])

    def test_multiple_sources_of_same_sku_make_one_product(self):
        self.init(master(('RAW', []), ('SUB', [comp('RAW', 2)]),
                         ('KIT', [comp('SUB', 2)])))
        snap = self.apply(request('BUILD', 0, [
            receive('R1', 'RAW', 4),
            receive('R2', 'RAW', 4),
            make('S1', 'SUB', 1, [('R1', 2)]),
            make('S2', 'SUB', 1, [('R2', 2)]),
            make('K', 'KIT', 1, [('S1', 1), ('S2', 1)]),
            ship('OUT', 'C', 'KIT', 1, [('K', 1)])]))
        kit = next(row for row in snap['lots'] if row['lot_id'] == 'K')
        self.assertEqual(kit['inputs'], [{'lot_id': 'S1', 'quantity': 1},
                                         {'lot_id': 'S2', 'quantity': 1}])
        snap = self.apply(request('RX', 1, [recall('X', 'R2')]))
        blocked = {row['lot_id']: row['blocked_by'] for row in snap['lots']}
        self.assertEqual(blocked['S1'], [])
        self.assertEqual(blocked['S2'], ['X'])
        self.assertEqual(blocked['K'], ['X'])  # 整批受影响，不按投入比例拆分
        self.assertEqual([(row['recall_id'], row['shipment_id'], row['lot_id'],
                           row['quantity']) for row in snap['exposures']],
                         [('X', 'OUT', 'K', 1)])

    def test_new_make_uses_recall_state_at_that_operation(self):
        self.init(master(('RAW', []), ('SUB', [comp('RAW', 2)])))
        self.apply(request('BUILD', 0, [receive('R', 'RAW', 4)]))
        # 先 recall 后 close：make 时仍 active，整批失败。
        self.assert_fails(request('BLOCK', 1, [
            recall('X', 'R'), make('S', 'SUB', 2, [('R', 4)]),
            close_recall('X')]))
        # 先 close 再 make：该操作时已无 active 召回，成功。
        snap = self.apply(request('FREE', 1, [
            recall('Y', 'R'), close_recall('Y'),
            make('S', 'SUB', 2, [('R', 4)])]))
        self.assertEqual(snap['version'], 2)
        self.assertEqual(snap['exposures'], [])
        self.assertEqual(self.lots()['S']['remaining_quantity'], 2)
        # 但先 make 后 close 不可行：前面的 make 已经非法。
        self.assert_fails(request('LATE', 2, [
            recall('Z', 'R'), make('S2', 'SUB', 1, []), close_recall('Z')]))

    def test_recall_after_shipment_exposes_history_long_after(self):
        self.init(master(('RAW', []), ('SUB', [comp('RAW', 2)])))
        self.apply(request('B1', 0, [
            receive('R', 'RAW', 4), make('S', 'SUB', 2, [('R', 4)]),
            ship('OUT', 'C9', 'SUB', 2, [('S', 2)])]))
        # 发货时无召回，合法；之后开启召回须列出历史发货。
        snap = self.apply(request('B2', 1, [recall('X', 'R')]))
        self.assertEqual(snap['exposures'], [{
            'recall_id': 'X', 'shipment_id': 'OUT', 'customer_id': 'C9',
            'sku': 'SUB', 'lot_id': 'S', 'quantity': 2}])
        # 尚未发货的余额不进入 exposures；此处余额为 0。
        self.assertEqual(self.lots()['S']['remaining_quantity'], 0)

    def test_unsent_balance_is_not_exposed(self):
        self.init(master(('RAW', []), ('SUB', [comp('RAW', 2)])))
        self.apply(request('B1', 0, [
            receive('R', 'RAW', 6), make('S1', 'SUB', 1, [('R', 2)]),
            make('S2', 'SUB', 2, [('R', 4)]), ship('OUT', 'C', 'SUB', 1, [('S1', 1)])]))
        snap = self.apply(request('B2', 1, [recall('X', 'R')]))
        self.assertEqual([(row['shipment_id'], row['lot_id'])
                          for row in snap['exposures']], [('OUT', 'S1')])
        self.assertEqual(self.lots()['S2']['remaining_quantity'], 2)

    def test_clean_same_sku_lot_can_ship_while_recalled_lot_cannot(self):
        self.init(master(('RAW', [])))
        self.apply(request('B1', 0, [
            receive('GOOD', 'RAW', 1), receive('BAD', 'RAW', 1),
            recall('X', 'BAD')]))
        snap = self.apply(request('B2', 1, [ship('OUT', 'C', 'RAW', 1, [('GOOD', 1)])]))
        self.assertEqual(snap['shipments'][0]['allocations'],
                         [{'lot_id': 'GOOD', 'quantity': 1}])
        self.assert_fails(request('B3', 2,
                                  [ship('OUT2', 'C', 'RAW', 1, [('BAD', 1)])]))
        self.assert_fails(request('B4', 2,
                                  [ship('OUT3', 'C', 'RAW', 1, [('CLEAN', 1)])]))

    def test_external_receive_same_sku_is_not_affected(self):
        self.init(master(('RAW', [])))
        self.apply(request('B1', 0, [receive('R1', 'RAW', 1), recall('X', 'R1')]))
        snap = self.apply(request('B2', 1, [receive('R2', 'RAW', 5)]))
        self.assertEqual(self.lots()['R2']['blocked_by'], [])
        snap = self.apply(request('B3', 2, [ship('OUT', 'C', 'RAW', 1, [('R2', 1)])]))
        self.assertEqual(snap['shipments'][0]['allocations'],
                         [{'lot_id': 'R2', 'quantity': 1}])

    def test_multiple_allocations_produce_one_exposure_row_each(self):
        self.init(master(('RAW', [])))
        self.apply(request('B1', 0, [
            receive('R1', 'RAW', 1), receive('R2', 'RAW', 2),
            ship('OUT', 'C', 'RAW', 3, [('R1', 1), ('R2', 2)]),
            recall('X', 'R1'), recall('Y', 'R2')]))
        snap = self.snap()
        self.assertEqual(
            [(row['recall_id'], row['lot_id'], row['quantity'])
             for row in snap['exposures']],
            [('X', 'R1', 1), ('Y', 'R2', 2)])


class BusinessRuleTests(TraceTestBase):
    def setUp(self):
        super().setUp()
        self.init(master(('RAW', []), ('SUB', [comp('RAW', 2)]),
                         ('KIT', [comp('SUB', 1)])))

    def test_receive_can_create_manufactured_sku_lot(self):
        snap = self.apply(request('B', 0, [receive('BOUGHT-KIT', 'KIT', 3)]))
        row = snap['lots'][0]
        self.assertEqual((row['sku'], row['produced_quantity'],
                          row['remaining_quantity'], row['inputs']),
                         ('KIT', 3, 3, []))
        # 外购件不推断上游来源，也不参与配方核对。
        snap = self.apply(request('S', 1,
                                  [ship('OUT', 'C', 'KIT', 1, [('BOUGHT-KIT', 1)])]))
        self.assertEqual(snap['shipments'][0]['allocations'],
                         [{'lot_id': 'BOUGHT-KIT', 'quantity': 1}])

    def test_make_requires_exact_recipe_aggregated_by_sku(self):
        self.apply(request('B', 0, [
            receive('R1', 'RAW', 4), receive('R2', 'RAW', 4)]))
        # 同 SKU 多来源汇总：2 件 SUB 需 4 个 RAW，可拆在两个批次上。
        snap = self.apply(request('OK', 1, [
            make('S', 'SUB', 2, [('R1', 1), ('R2', 3)])]))
        self.assertEqual(snap['lots'][-1]['inputs'],
                         [{'lot_id': 'R1', 'quantity': 1},
                          {'lot_id': 'R2', 'quantity': 3}])
        before = self.snap()
        # 缺料、多投、夹入非配方物料都拒绝。
        for index, lines in enumerate(([('R1', 3)], [('R1', 5)],
                                       [('R1', 4), ('S', 1)])):
            with self.subTest(index=index):
                self.assert_fails(request('BAD%d' % index, 2,
                                          [make('X', 'SUB', 2, lines)]))
        self.assertEqual(self.snap(), before)
        # SUB 不可用 RAW 以外的已存在批次。
        self.assert_fails(request('WRONG', 2, [make('X', 'SUB', 1, [('S', 1)])]))

    def test_make_quantity_multiples_and_units(self):
        self.apply(request('B', 0, [receive('R', 'RAW', 10)]))
        # 3 件 SUB 需 6 个 RAW；配方计量不要求跨 SKU 数量相等。
        snap = self.apply(request('M', 1, [make('S', 'SUB', 3, [('R', 6)])]))
        self.assertEqual(self.lots()['S']['produced_quantity'], 3)
        self.assertEqual(self.lots()['R']['remaining_quantity'], 4)
        self.assert_fails(request('BAD', 2, [make('S2', 'SUB', 2, [('R', 3)])]))

    def test_make_rejects_raw_componentless_sku(self):
        self.apply(request('B', 0, [receive('R', 'RAW', 2)]))
        self.assert_fails(request('M', 1, [make('R2', 'RAW', 1, [('R', 2)])]))

    def test_make_rejects_self_reference_and_future_lot(self):
        self.apply(request('B', 0, [receive('R', 'RAW', 4)]))
        self.assert_fails(request('SELF', 1, [make('S', 'SUB', 1, [('S', 2)])]))
        self.assert_fails(request('FUTURE', 1,
                                  [make('S', 'SUB', 2, [('R', 4), ('LATER', 1)]),
                                   receive('LATER', 'RAW', 1)]))
        self.assertEqual(self.snap()['version'], 1)

    def test_insufficient_balance_rolls_back_whole_batch(self):
        self.apply(request('B', 0, [receive('R', 'RAW', 3)]))
        self.assert_fails(request('M', 1, [
            receive('R2', 'RAW', 1),
            make('S', 'SUB', 2, [('R', 4)])]))
        self.assertEqual(self.snap()['version'], 1)
        self.assertEqual(set(self.lots()), {'R'})

    def test_ship_requires_explicit_allocations_and_exact_sum(self):
        self.apply(request('B', 0, [
            receive('R1', 'RAW', 2), receive('R2', 'RAW', 3)]))
        snap = self.apply(request('S', 1, [
            ship('OUT', 'C', 'RAW', 3, [('R1', 1), ('R2', 2)])]))
        self.assertEqual(snap['shipments'][0]['allocations'],
                         [{'lot_id': 'R1', 'quantity': 1},
                          {'lot_id': 'R2', 'quantity': 2}])
        self.assertEqual(self.lots()['R1']['remaining_quantity'], 1)
        for index, (quantity, lines) in enumerate(((4, [('R1', 1), ('R2', 2)]),
                                                   (2, [('R1', 1)]),
                                                   (1, [('R1', 2)]))):
            with self.subTest(index=index):
                self.assert_fails(request('BAD%d' % index, 2,
                                          [ship('O%d' % index, 'C', 'RAW',
                                                quantity, lines)]))
        self.assert_fails(request('SKU', 2,
                                  [ship('OS', 'C', 'SUB', 1, [('R1', 1)])]))
        self.assertEqual([row['shipment_id'] for row in self.snap()['shipments']],
                         ['OUT'])

    def test_ship_does_not_switch_to_clean_lot_automatically(self):
        self.apply(request('B', 0, [
            receive('BAD', 'RAW', 2), receive('GOOD', 'RAW', 2),
            recall('X', 'BAD')]))
        # 明确给出受召回批次：拒绝，绝不静默改用 GOOD。
        self.assert_fails(request('S', 2,
                                  [ship('OUT', 'C', 'RAW', 1, [('BAD', 1)])]))
        self.assertEqual(self.lots()['GOOD']['remaining_quantity'], 2)

    def test_unavailable_allocation_rolls_back_valid_prefix(self):
        self.apply(request('B', 0, [receive('R', 'RAW', 5)]))
        self.assert_fails(request('S', 1, [
            receive('R2', 'RAW', 1),
            ship('OUT', 'C', 'RAW', 2, [('R', 1), ('R2', 1)]),
            ship('BAD', 'C', 'RAW', 1, [('MISSING', 1)])]))
        snap = self.snap()
        self.assertEqual(snap['version'], 1)
        self.assertEqual({row['lot_id']: row['remaining_quantity']
                          for row in snap['lots']}, {'R': 5})
        self.assertEqual(snap['shipments'], [])

    def test_shipment_and_recall_identities_are_terminal(self):
        self.apply(request('B', 0, [
            receive('R', 'RAW', 4), recall('X', 'R'), close_recall('X')]))
        self.assert_fails(request('REOPEN', 1, [close_recall('X')]))
        self.assert_fails(request('REOPEN2', 1, [recall('X', 'R')]))
        self.assert_fails(request('GHOST', 1, [close_recall('NOPE')]))

    def test_multiple_recalls_may_target_the_same_root(self):
        self.apply(request('B', 0, [receive('R', 'RAW', 1), recall('X', 'R')]))
        snap = self.apply(request('B2', 1, [recall('Y', 'R')]))
        self.assertEqual(self.lots()['R']['blocked_by'], ['X', 'Y'])
        snap = self.apply(request('B3', 2, [close_recall('X')]))
        self.assertEqual(self.lots()['R']['blocked_by'], ['Y'])
        self.assertEqual([(row['recall_id'], row['status'])
                          for row in snap['recalls']],
                         [('X', 'closed'), ('Y', 'active')])

    def test_recall_requires_existing_lot(self):
        self.assert_fails(request('R', 0, [recall('X', 'UNBORN')]))
        self.assertEqual(self.snap()['recalls'], [])

    def test_cleared_balance_keeps_identity_and_lineage(self):
        self.apply(request('B', 0, [
            receive('R', 'RAW', 2), make('S', 'SUB', 1, [('R', 2)]),
            ship('OUT', 'C', 'SUB', 1, [('S', 1)])]))
        lots = self.lots()
        self.assertEqual(lots['R']['remaining_quantity'], 0)
        self.assertEqual(lots['S']['remaining_quantity'], 0)
        self.assertEqual(lots['S']['inputs'], [{'lot_id': 'R', 'quantity': 2}])
        self.assert_fails(request('REUSE', 1, [receive('S', 'RAW', 1)]))


class CommitTests(TraceTestBase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_version_conflict_and_stale_replay(self):
        first = self.apply(load('request_physical_build.json'))
        self.assert_fails(request('STALE', 0, [receive('X', 'RAW', 1)]))
        # 合法结构的成功重放优先于版本检查。
        self.assertEqual(self.apply(load('request_physical_build.json')), first)
        self.assertEqual(self.snap()['version'], 1)

    def test_same_request_id_different_payload_conflicts(self):
        self.apply(load('request_physical_build.json'))
        changed = load('request_physical_build.json')
        changed['operations'][0]['quantity'] = 7
        self.assert_fails(changed)
        # 仅 JSON 键序不同仍是同一请求。
        reordered = json.loads(json.dumps(load('request_physical_build.json')))
        self.assertEqual(self.apply(reordered)['version'], 1)

    def test_array_order_matters_for_replay_equality(self):
        self.apply(request('B', 0, [receive('R1', 'RAW', 1),
                                    receive('R2', 'RAW', 1)]))
        swapped = request('B', 0, [receive('R2', 'RAW', 1),
                                   receive('R1', 'RAW', 1)])
        self.assert_fails(swapped)

    def test_failed_request_does_not_consume_request_id(self):
        self.assert_fails(request('R', 0, [receive('R', 'RAW', 1),
                                           make('S', 'SUB', 1, [('R', 1)])]))
        snap = self.apply(request('R', 0, [receive('R', 'RAW', 1)]))
        self.assertEqual(snap['version'], 1)

    def test_snapshot_is_always_current_not_historical(self):
        self.apply(load('request_physical_build.json'))
        self.apply(load('request_recall_x.json'))
        current = self.snap()
        self.assertEqual(current['version'], 2)
        self.assertEqual(current['exposures'][0]['recall_id'], 'X')
        # 重放首次提交不改变当前状态。
        self.apply(load('request_physical_build.json'))
        self.assertEqual(self.snap(), current)

    def test_committed_state_serializes_identically(self):
        self.apply(load('request_physical_build.json'))
        first = json.dumps(self.snap(), sort_keys=True, ensure_ascii=False)
        second = json.dumps(self.snap(), sort_keys=True, ensure_ascii=False)
        self.assertEqual(first, second)

    def test_snapshot_ordering(self):
        self.apply(request('B', 0, [
            receive('Z', 'RAW', 1), receive('A', 'RAW', 1),
            ship('S2', 'C2', 'RAW', 1, [('A', 1)]),
            ship('S1', 'C1', 'RAW', 1, [('Z', 1)]),
            recall('R2', 'A'), recall('R1', 'Z')]))
        snap = self.snap()
        self.assertEqual([row['lot_id'] for row in snap['lots']], ['A', 'Z'])
        self.assertEqual([row['shipment_id'] for row in snap['shipments']],
                         ['S1', 'S2'])
        self.assertEqual([row['recall_id'] for row in snap['recalls']],
                         ['R1', 'R2'])
        self.assertEqual([(row['recall_id'], row['shipment_id'])
                          for row in snap['exposures']],
                         [('R1', 'S1'), ('R2', 'S2')])
        for row in snap['lots']:
            self.assertEqual(set(row), {'lot_id', 'sku', 'produced_quantity',
                                        'remaining_quantity', 'inputs',
                                        'blocked_by'})
        for row in snap['shipments']:
            self.assertEqual(set(row), {'shipment_id', 'customer_id', 'sku',
                                        'quantity', 'allocations'})
        for row in snap['exposures']:
            self.assertEqual(set(row), {'recall_id', 'shipment_id',
                                        'customer_id', 'sku', 'lot_id',
                                        'quantity'})


class ValidationTests(TraceTestBase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_request_field_validation(self):
        bad = [
            {'request_id': 'R', 'expected_version': 0},
            {'request_id': 'R', 'expected_version': 0, 'operations': [],
             'extra': 1},
            request('', 0, [receive('L', 'RAW', 1)]),
            request('R', True, [receive('L', 'RAW', 1)]),
            request('R', -1, [receive('L', 'RAW', 1)]),
            request('R', 1.5, [receive('L', 'RAW', 1)]),
            request('R', 0, []),
            request('R', 0, 'not-a-list'),
            request('R', 0, [{'op': 'receive', 'lot_id': 'L', 'sku': 'RAW',
                              'quantity': True}]),
            request('R', 0, [{'op': 'receive', 'lot_id': ' L', 'sku': 'RAW',
                              'quantity': 1}]),
            request('R', 0, [{'op': 'frobnicate'}]),
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                self.assert_fails(payload)

    def test_each_operation_rejects_unknown_and_missing_fields(self):
        cases = [
            ({'op': 'receive', 'lot_id': 'L', 'sku': 'RAW', 'quantity': 1,
              'extra': 1}),
            ({'op': 'receive', 'lot_id': 'L', 'sku': 'RAW'}),
            ({'op': 'make', 'lot_id': 'L', 'sku': 'SUB', 'quantity': 1,
              'inputs': []}),
            ({'op': 'make', 'lot_id': 'L', 'sku': 'SUB', 'quantity': 1,
              'inputs': [{'lot_id': 'R', 'quantity': 1, 'extra': 1}]}),
            ({'op': 'make', 'lot_id': 'L', 'sku': 'SUB', 'quantity': 1,
              'inputs': [{'lot_id': 'R', 'quantity': 1},
                         {'lot_id': 'R', 'quantity': 1}]}),
            ({'op': 'ship', 'shipment_id': 'S', 'customer_id': 'C',
              'sku': 'RAW', 'quantity': 1, 'allocations': [], 'extra': 0}),
            ({'op': 'ship', 'shipment_id': 'S', 'customer_id': 'C',
              'sku': 'RAW', 'quantity': 1,
              'allocations': [{'lot_id': 'A', 'quantity': 1},
                              {'lot_id': 'A', 'quantity': 1}]}),
            ({'op': 'recall', 'recall_id': 'X', 'lot_id': 'L', 'extra': 0}),
            ({'op': 'close_recall', 'recall_id': 'X', 'lot_id': 'L'}),
        ]
        for operation in cases:
            with self.subTest(operation=operation):
                self.assert_fails(request('R', 0, [operation]))

    def test_bounded_arrays(self):
        too_many_ops = [receive('L%d' % i, 'RAW', 1) for i in range(101)]
        self.assert_fails(request('OPS', 0, too_many_ops))
        too_many_inputs = [{'lot_id': 'L%d' % i, 'quantity': 1}
                           for i in range(101)]
        self.assert_fails(request('INPUTS', 0,
                                  [{'op': 'make', 'lot_id': 'X', 'sku': 'SUB',
                                    'quantity': 1,
                                    'inputs': too_many_inputs}]))
        too_many_allocations = [{'lot_id': 'L%d' % i, 'quantity': 1}
                                for i in range(101)]
        self.assert_fails(request('ALLOC', 0,
                                  [{'op': 'ship', 'shipment_id': 'S',
                                    'customer_id': 'C', 'sku': 'RAW',
                                    'quantity': 1,
                                    'allocations': too_many_allocations}]))

    def test_unknown_sku_rejected(self):
        self.assert_fails(request('R', 0, [receive('L', 'GHOST', 1)]))

    def test_input_objects_are_not_mutated_by_failure(self):
        payload = request('R', 0, [receive('L', 'RAW', 1),
                                   ship('S', 'C', 'RAW', 1, [('NOPE', 1)])])
        before = copy.deepcopy(payload)
        with self.assertRaises(ValueError):
            self.apply(payload)
        self.assertEqual(payload, before)


class LimitTests(TraceTestBase):
    def test_identity_limits(self):
        self.init(master(('RAW', []), ('SUB', [comp('RAW', 2)])))
        original = traceability.MAX_IDENTITIES
        traceability.MAX_IDENTITIES = 3
        try:
            self.apply(request('B1', 0, [
                receive('R1', 'RAW', 1), receive('R2', 'RAW', 1),
                receive('R3', 'RAW', 1)]))
            self.assert_fails(request('B2', 1, [receive('R4', 'RAW', 1)]))
            # 批次身份上限独立计数：3 个批次后无法再完工或收货。
            snap = self.snap()
            self.assertEqual(snap['version'], 1)
        finally:
            traceability.MAX_IDENTITIES = original

        # 发货身份上限。
        self.db.unlink()
        self.init(master(('RAW', [])))
        traceability.MAX_IDENTITIES = 2
        try:
            self.apply(request('S1', 0, [
                receive('R', 'RAW', 2), ship('A', 'C', 'RAW', 1, [('R', 1)])]))
            self.apply(request('S2', 1, [ship('B', 'C', 'RAW', 1, [('R', 1)])]))
            self.assert_fails(request('S3', 2, [ship('D', 'C', 'RAW', 1,
                                                     [('R', 1)])]))
        finally:
            traceability.MAX_IDENTITIES = original

        # 召回身份上限（含已关闭）。
        self.db.unlink()
        self.init(master(('RAW', [])))
        traceability.MAX_IDENTITIES = 2
        try:
            self.apply(request('L', 0, [receive('R', 'RAW', 1)]))
            self.apply(request('R1', 1, [recall('X', 'R')]))
            self.apply(request('R2', 2, [recall('Y', 'R'), close_recall('Y')]))
            self.assert_fails(request('R3', 3, [recall('Z', 'R')]))
            self.assertEqual([(row['recall_id'], row['status'])
                              for row in self.snap()['recalls']],
                             [('X', 'active'), ('Y', 'closed')])
        finally:
            traceability.MAX_IDENTITIES = original

    def test_full_identity_limit_is_reached_exactly(self):
        self.init(master(('RAW', [])))
        for batch in range(MAX_IDENTITIES // 100):
            operations = [receive('L%05d' % (batch * 100 + i), 'RAW', 1)
                          for i in range(100)]
            self.apply(request('B%03d' % batch, batch, operations))
        self.assertEqual(len(self.snap()['lots']), MAX_IDENTITIES)
        self.assert_fails(request('OVER', MAX_IDENTITIES // 100,
                                  [receive('ONE-MORE', 'RAW', 1)]))


class CliTests(TraceTestBase):
    def invoke(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        return subprocess.run([sys.executable, '-m',
                               'material_plan.traceability_cli', *args],
                              capture_output=True, text=True, timeout=60,
                              env=env)

    def write(self, name, value):
        path = self.tmp / name
        path.write_text(value if isinstance(value, str)
                        else json.dumps(value))
        return path

    def test_init_apply_snapshot_roundtrip_and_replay(self):
        master_path = self.write('master.json', load('master.json'))
        result = self.invoke('init', '--db', str(self.db),
                             '--source', str(master_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.endswith('\n'))
        self.assertEqual(result.stdout.count('\n'), 1)
        self.assertEqual(json.loads(result.stdout)['version'], 0)

        build = self.write('build.json', load('request_physical_build.json'))
        first = self.invoke('apply', '--db', str(self.db),
                            '--source', str(build))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.count('\n'), 1)
        replay = self.invoke('apply', '--db', str(self.db),
                             '--source', str(build))
        self.assertEqual(replay.stdout, first.stdout)

        one = self.invoke('snapshot', '--db', str(self.db))
        two = self.invoke('snapshot', '--db', str(self.db))
        self.assertEqual(one.returncode, 0)
        self.assertEqual(one.stdout, two.stdout)
        self.assertEqual(json.loads(one.stdout),
                         json.loads(first.stdout))

    def test_failure_exit_two_empty_stdout_with_stderr(self):
        master_path = self.write('master.json', load('master.json'))
        self.invoke('init', '--db', str(self.db), '--source', str(master_path))
        build = self.write('build.json', load('request_physical_build.json'))
        self.invoke('apply', '--db', str(self.db), '--source', str(build))
        recall_x = self.write('x.json', load('request_recall_x.json'))
        self.invoke('apply', '--db', str(self.db), '--source', str(recall_x))
        bad = self.write('bad.json', request(
            'BAD-MAKE', 2, [make('K2', 'KIT', 1, [('S2', 1)]),
                            close_recall('X')]))
        result = self.invoke('apply', '--db', str(self.db), '--source', str(bad))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('recall', result.stderr)

    def test_malformed_json_rules_via_cli(self):
        master_path = self.write('master.json', load('master.json'))
        self.invoke('init', '--db', str(self.db), '--source', str(master_path))
        for raw in ('{', '{"request_id":"X","request_id":"Y"}',
                    '{"request_id":"X","expected_version":NaN,'
                    '"operations":[]}'):
            source = self.write('bad.json', raw)
            result = self.invoke('apply', '--db', str(self.db),
                                 '--source', str(source))
            self.assertEqual(result.returncode, 2, raw)
            self.assertEqual(result.stdout, '')

    def test_init_refuses_existing_database(self):
        master_path = self.write('master.json', load('master.json'))
        self.db.write_bytes(b'')
        result = self.invoke('init', '--db', str(self.db),
                             '--source', str(master_path))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertEqual(self.db.read_bytes(), b'')

    def test_missing_database_is_not_created(self):
        build = self.write('build.json', load('request_physical_build.json'))
        result = self.invoke('apply', '--db', str(self.db),
                             '--source', str(build))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertFalse(self.db.exists())

    def test_cross_process_recovery(self):
        master_path = self.write('master.json', load('master.json'))
        self.invoke('init', '--db', str(self.db), '--source', str(master_path))
        build = self.write('build.json', load('request_physical_build.json'))
        self.invoke('apply', '--db', str(self.db), '--source', str(build))
        recall_x = self.write('x.json', load('request_recall_x.json'))
        self.invoke('apply', '--db', str(self.db), '--source', str(recall_x))
        before = json.dumps(self.snap(), sort_keys=True)
        result = self.invoke('snapshot', '--db', str(self.db))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.dumps(json.loads(result.stdout), sort_keys=True),
                         before)


if __name__ == '__main__':
    unittest.main()
