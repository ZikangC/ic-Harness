"""Controller progress and interrupted round recovery; no provider or EDA jobs."""
import copy
import json
import unittest
from unittest.mock import patch

from harness.adapters import rtl_audit as audit
from harness.adapters.rtl_repair import implementation_repair
from harness.tests import test_rtl_audit_flow as fixtures


class RecoveryTests(unittest.TestCase):
    setUp = fixtures.FlowTests.setUp

    def test_non_rtl_failure_resume_needs_new_objective_progress(self):
        for verdict in ('blocked', 'insufficient_evidence'):
            with self.subTest(verdict=verdict):
                remote = fixtures.Remote()
                def session(*args):
                    remote.decide(verdict)
                    return {'completed': True, 'tool_isolation_ok': True}
                with patch.object(audit, 'run_session', side_effect=session):
                    first = audit.run(self.context, remote, repair_handler=implementation_repair)
                files, current = copy.deepcopy(remote.files), copy.deepcopy(remote.current)
                with patch.object(audit, 'run_session', side_effect=AssertionError('empty rerun')):
                    resumed = audit.run(self.context, remote, repair_handler=implementation_repair)
                self.assertEqual(first, resumed)
                self.assertEqual(files, remote.files)
                self.assertEqual(current, remote.current)
                self.assertFalse(any(q['action'].startswith('rtl_repair') for q in remote.calls))

    def test_completed_repair_without_changes_or_jobs_stops_and_retains_issues(self):
        fixtures.FlowTests.prepare_repair(self, self.remote)
        self.remote.current['work_current'] = True
        report = copy.deepcopy(self.remote.current['decision'])
        with patch.object(audit, 'run_session', return_value={'completed': True, 'tool_isolation_ok': True}) as run:
            result = audit.run(self.context, self.remote, repair_handler=implementation_repair, max_repairs=1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result['status'], 'rtl_repair_no_progress')
        self.assertFalse(result['rtl_stage_release'])
        self.assertEqual(self.remote.current['decision'], report)
        with patch.object(audit, 'run_session', side_effect=AssertionError('empty rerun')):
            self.assertEqual(audit.run(self.context, self.remote,
                                      repair_handler=implementation_repair, max_repairs=1), result)
        self.assertEqual(self.remote.number, 1)

    def test_keyboard_interrupt_reserves_round_and_resume_does_not_reset_it(self):
        fixtures.FlowTests.prepare_repair(self, self.remote)
        def interrupted(*args):
            raise KeyboardInterrupt()
        with patch.object(audit, 'run_session', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                audit.run(self.context, self.remote, repair_handler=implementation_repair, max_repairs=1)
        ledger_path = next(path for path in self.remote.files if '/repair-budget-' in path)
        before = self.remote.files[ledger_path]
        roles = []
        def recovered(prompt, role, *args):
            roles.append(role)
            if role == 'implementation':
                self.remote.current['work_current'] = False
            else:
                self.remote.decide('revise')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(audit, 'run_session', side_effect=recovered):
            result = audit.run(self.context, self.remote, repair_handler=implementation_repair, max_repairs=1)
        self.assertEqual(roles, ['implementation', 'review'])
        self.assertEqual(result['status'], 'rtl_repair_budget_exhausted')
        self.assertFalse(result['rtl_stage_release'])
        self.assertEqual(self.remote.files[ledger_path], before)
        self.assertEqual(set(json.loads(before)['rounds']), {'review-1'})
        with patch.object(audit, 'run_session', side_effect=AssertionError('budget reset')):
            self.assertEqual(audit.run(self.context, self.remote,
                                      repair_handler=implementation_repair, max_repairs=1), result)

    def test_omitted_limit_uses_finite_frozen_setting(self):
        fixtures.FlowTests.prepare_repair(self, self.remote)
        self.context['settings']['rtl_audit'] = {'max_repairs': 0}
        with patch.object(audit, 'run_session', side_effect=AssertionError('zero budget')):
            result = audit.run(self.context, self.remote, repair_handler=implementation_repair)
        self.assertEqual(result['status'], 'rtl_repair_budget_exhausted')
        self.assertFalse(result['rtl_stage_release'])

    def test_completed_receipt_cannot_reuse_pass_after_work_changes(self):
        def passed(*args):
            self.remote.decide('pass')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(audit, 'run_session', side_effect=passed):
            first = audit.review_once(self.context, self.remote)
        self.remote.current['work_current'] = False
        def blocked(*args):
            self.remote.decide('insufficient_evidence')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(audit, 'run_session', side_effect=blocked) as run:
            result = audit.review_once(self.context, self.remote)
        self.assertEqual(run.call_count, 1)
        self.assertNotEqual(first['review'], result['review'])
        self.assertFalse(result['rtl_stage_release'])


if __name__ == '__main__':
    unittest.main()
