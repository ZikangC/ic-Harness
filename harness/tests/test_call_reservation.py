"""Reservations preserve shared quotas, including repair calls and recovery."""
import json
from pathlib import Path
import tempfile
import unittest
from harness.runtime.call_budget import CallBudget,LimitReached

class CallReservationTests(unittest.TestCase):
    def fresh(self):
        return Path(tempfile.mkdtemp()),{'max_model_calls':5,'total_active_seconds':600,'request_timeout_seconds':100,'reserved_audit_calls':2,'reserved_audit_seconds':120}

    def test_design_stops_before_spending_audit_calls(self):
        out,settings=self.fresh();budget=CallBudget(out,settings)
        for _ in range(3):budget.reserve('design')
        with self.assertRaises(LimitReached):budget.reserve('design')
        budget.reserve('audit');budget.reserve('audit')
        with self.assertRaises(LimitReached):budget.reserve('audit')
        self.assertEqual(budget.calls,5)
        self.assertEqual(json.loads((out/'limits.json').read_text())['by_purpose'],{'design':3,'audit':2})

    def test_resume_preserves_consumed_quota_and_reservation(self):
        out,settings=self.fresh();budget=CallBudget(out,settings)
        budget.reserve('audit_baseline');budget.reserve('design');budget.reserve('design')
        restored=CallBudget(out,settings)
        with self.assertRaises(LimitReached):restored.reserve('design')
        self.assertEqual(restored.calls,3)

    def test_observed_internal_turns_are_charged_even_when_reservation_would_block(self):
        out,settings=self.fresh();budget=CallBudget(out,settings)
        budget.reserve('design');budget.reserve('design');budget.reserve('design')
        budget.record_observed(2,'design')
        self.assertEqual(budget.calls,5)
        with self.assertRaises(LimitReached):budget.record_observed(2,'design')
        self.assertEqual(budget.calls,7)
        self.assertEqual(json.loads((out/'limits.json').read_text())['calls'],7)
        restored=CallBudget(out,settings)
        with self.assertRaises(LimitReached):restored.reserve('audit')

    def test_partial_or_failed_provider_output_counts_observable_turns(self):
        from harness.adapters.omp import observed_turns
        events='\n'.join(json.dumps({'type':'message_end','message':{'role':role}}) for role in ('assistant','toolResult','assistant','assistant'))
        self.assertEqual(observed_turns(events+'\n{truncated'),3)
        self.assertEqual(observed_turns(events.encode()),3)
        self.assertEqual(observed_turns(None),0)

    def test_design_request_deadline_keeps_reserved_time(self):
        out,settings=self.fresh();settings['request_timeout_seconds']=900
        budget=CallBudget(out,settings)
        self.assertLessEqual(budget.timeout('design'),480)
        self.assertGreater(budget.timeout('audit'),budget.timeout('design'))

if __name__=='__main__':unittest.main()
