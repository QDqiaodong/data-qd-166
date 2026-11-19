import copy
import json
from pathlib import Path
import unittest
from material_plan.planning import plan

ROOT = Path(__file__).resolve().parents[1]
def sample():
    return json.loads((ROOT/'fixtures/input.json').read_text())

class PlanningTests(unittest.TestCase):
    def test_expected_orders_and_conservation(self):
        result=plan(sample())
        orders={r['sku']:r for r in result['orders']}
        self.assertEqual((orders['KIT']['quantity'],orders['KIT']['release_date']),(10,'2026-01-08'))
        self.assertEqual((orders['BOLT']['quantity'],orders['BOLT']['release_date']),(20,'2026-01-07'))
        self.assertEqual(result['ending_inventory'],{'BOLT':0,'KIT':4})
        for row in result['ledger']:
            self.assertEqual(row['opening']+row['scheduled_receipt']+row['planned_receipt']-row['gross'],row['closing'])
    def test_finished_stock_does_not_explode(self):
        data=sample(); data['items'][0]['on_hand']=6
        self.assertFalse(any(o['sku']=='KIT' for o in plan(data)['orders']))
        self.assertFalse(plan(data)['orders'])
    def test_late_receipt_does_not_cover_earlier_need(self):
        data=sample();data['receipts'][0]['date']='2026-01-09'
        self.assertEqual(next(o for o in plan(data)['orders'] if o['sku']=='BOLT')['quantity'],30)
    def test_shared_components_aggregate_before_lot_rounding(self):
        data=sample();data['items'][0]['components']=[{'sku':'SUB','quantity':1},{'sku':'BOLT','quantity':1}]
        data['items'].append({'sku':'SUB','lead_days':0,'lot_multiple':1,'on_hand':0,'components':[{'sku':'BOLT','quantity':1}]})
        self.assertEqual(sum(o['quantity'] for o in plan(data)['orders'] if o['sku']=='BOLT'),10)
    def test_cycle_rejected(self):
        data=sample();data['items'][1]['components']=[{'sku':'KIT','quantity':1}]
        with self.assertRaisesRegex(ValueError,'cycle'):plan(data)
    def test_unknown_component_rejected(self):
        data=sample();data['items'][0]['components'][0]['sku']='MISSING'
        with self.assertRaises(ValueError):plan(data)
    def test_duplicate_demand_rejected(self):
        data=sample();data['demands']*=2
        with self.assertRaises(ValueError):plan(data)
    def test_past_due_is_not_clamped(self):
        data=sample();data['demands'][0]['date']='2026-01-01';data['receipts']=[]
        self.assertTrue(all(o['past_due'] for o in plan(data)['orders']))
        self.assertIn('2025-12-30',{o['release_date'] for o in plan(data)['orders']})
    def test_bad_quantities_rejected(self):
        for quantity in (True,0,-1,1.2):
            data=sample();data['demands'][0]['quantity']=quantity
            with self.subTest(quantity=quantity),self.assertRaises(ValueError):plan(data)
    def test_input_is_not_mutated(self):
        data=sample();original=copy.deepcopy(data);plan(data);self.assertEqual(data,original)
