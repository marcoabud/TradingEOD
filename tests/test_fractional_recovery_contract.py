"""Replay the real Nov 2 paper incident; only provider IDs are aliased."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import unittest
from src.core_recovery import reconcile_intent
from src.safety import SafetyError


class FractionalContractTests(unittest.TestCase):
    def setUp(self):
        self.case=json.loads((Path(__file__).parent/'fixtures/fractional_recovery_2026-11-02.json').read_text())

    def reconcile(self):
        c=self.case
        return reconcile_intent(c['intent'],c['orders'],c['fills'],c['positions'],date.fromisoformat(c['through_day']))

    def test_actual_integer_and_fractional_legs_reconcile_to_full_orders(self):
        result=self.reconcile()
        self.assertEqual(result['status'],'reconciled')
        self.assertEqual(result['fill_count'],4)
        self.assertTrue(all(float(v)==0 for v in result['core_qty'].values()))

    def test_activity_after_snapshot_still_blocks(self):
        self.case['fills'][1]['transaction_time']='2026-10-30T19:45:13.255141Z'
        with self.assertRaisesRegex(SafetyError,'timestamp outside'):
            self.reconcile()

    def test_activity_before_submission_still_blocks(self):
        self.case['fills'][0]['transaction_time']='2026-10-30T19:45:12.329981Z'
        with self.assertRaisesRegex(SafetyError,'timestamp outside'):
            self.reconcile()

    def test_missing_or_duplicate_fractional_leg_cannot_pass(self):
        original=deepcopy(self.case)
        for change in ('missing','duplicate','quantity','identity','position'):
            self.case=deepcopy(original)
            if change=='missing': self.case['fills'].pop(1)
            elif change=='duplicate': self.case['fills'].append(deepcopy(self.case['fills'][1]))
            elif change=='quantity': self.case['fills'][1]['qty']='0.8413216581'
            elif change=='identity': self.case['fills'][1]['order_id']='foreign-order'
            else: self.case['positions']['SPY']={'symbol':'SPY','qty':'0.01'}
            with self.subTest(change=change),self.assertRaises(SafetyError):
                self.reconcile()

    def test_unfilled_order_with_partial_activity_still_blocks(self):
        self.case['orders'][0]['status']='partially_filled'
        with self.assertRaisesRegex(SafetyError,'partially filled'):
            self.reconcile()

    def test_update_cannot_precede_fill_or_extend_into_future(self):
        for stamp in ('2026-10-30T19:45:19.109427Z','2026-11-04T19:45:19Z'):
            self.case['orders'][0]['updated_at']=stamp
            with self.subTest(stamp=stamp),self.assertRaisesRegex(SafetyError,'update timestamp'):
                self.reconcile()
