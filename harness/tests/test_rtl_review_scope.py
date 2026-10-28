"""Explicit acceptance boundaries, without model execution, RTL or EDA jobs."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.adapters import rtl_audit as flow
from harness.tests import test_rtl_audit_flow as flow_fixture
Remote = flow_fixture.Remote


class ScopeFlowTests(unittest.TestCase):
    setUp = flow_fixture.FlowTests.setUp

    def passing(self, remote):
        def session(prompt, role, models, root, context_path, *args):
            remote.decide('pass')
            return {'completed': True, 'tool_isolation_ok': True}
        return session

    def test_change_pass_is_candidate_and_session_bound_without_stage_release(self):
        def session(prompt, role, models, root, context_path, *args):
            self.assertEqual(json.loads(context_path.read_text())['review_scope'], 'change')
            self.assertIn('REVIEW SCOPE: change.', prompt)
            self.assertIn('"review_scope": "change"', prompt)
            return self.passing(self.remote)(prompt, role, models, root, context_path, *args)
        with patch.object(flow, 'run_session', side_effect=session):
            result = flow.review_once(self.context, self.remote, review_scope='change')
        self.assertEqual(result['review_verdict'], 'pass')
        self.assertEqual(result['review_scope'], 'change')
        self.assertFalse(result['rtl_stage_release'])
        receipt = json.loads(self.remote.files['@run/control/rtl-audit/review-1.audit.json'])
        self.assertEqual(receipt['review_scope'], 'change')
        self.assertEqual(receipt['candidate_sha256'], result['candidate_sha256'])
        with patch.object(flow, 'run_session', side_effect=AssertionError('duplicate session')):
            self.assertEqual(flow.review_once(self.context, self.remote, review_scope='change'), result)

    def test_default_candidate_still_releases_after_independent_pass(self):
        with patch.object(flow, 'run_session', side_effect=self.passing(self.remote)):
            result = flow.review_once(self.context, self.remote)
        self.assertEqual(result['review_scope'], 'candidate')
        self.assertTrue(result['rtl_stage_release'])

    def test_candidate_request_does_not_reuse_narrower_pass(self):
        with patch.object(flow, 'run_session', side_effect=self.passing(self.remote)):
            first = flow.review_once(self.context, self.remote, review_scope='change')
        old_receipt = self.remote.files['@run/control/rtl-audit/review-1.audit.json']
        with patch.object(flow, 'run_session', side_effect=self.passing(self.remote)) as invoke:
            final = flow.review_once(self.context, self.remote, review_scope='candidate')
        self.assertEqual(invoke.call_count, 1)
        self.assertNotEqual(first['review'], final['review'])
        self.assertTrue(final['rtl_stage_release'])
        self.assertEqual(self.remote.files['@run/control/rtl-audit/review-1.audit.json'], old_receipt)

    def test_scope_mismatch_does_not_displace_unfinished_review(self):
        for decided in (False, True):
            with self.subTest(decided=decided):
                remote = Remote()
                remote.call({'action': 'rtl_review_begin', 'review_scope': 'change'})
                if decided:
                    remote.decide('pass')  # Publication alone does not prove the session ended.
                before = copy.deepcopy(remote.current)
                with patch.object(flow, 'run_session', side_effect=AssertionError('new session')):
                    with self.assertRaisesRegex(ValueError, 'unfinished review'):
                        flow.review_once(self.context, remote)
                self.assertEqual(remote.current, before)
                self.assertEqual(remote.number, 1)

    def test_ended_legacy_session_without_decision_can_start_explicit_review(self):
        for completed, exit_code in ((True, None), (False, 0), (False, 7)):
            with self.subTest(completed=completed, exit_code=exit_code):
                remote = Remote()
                remote.call({'action': 'rtl_review_begin'})
                remote.current.pop('review_scope')
                path = '@run/control/rtl-audit/review-1.audit.json'
                receipt = {'completed': completed, 'tool_isolation_ok': completed,
                           'exit_code': exit_code, 'candidate_sha256': remote.current['candidate_sha256'],
                           'decision_report_sha256': None}
                remote.files[path] = json.dumps(receipt)
                with patch.object(flow, 'run_session', side_effect=self.passing(remote)) as invoke:
                    result = flow.review_once(self.context, remote)
                self.assertEqual(invoke.call_count, 1)
                self.assertEqual(result['review'], 'review-2')
                self.assertEqual(result['review_scope'], 'candidate')
                self.assertTrue(result['rtl_stage_release'])
                self.assertEqual(remote.files[path], json.dumps(receipt))

    def test_legacy_placeholder_stale_or_unknown_terminal_receipt_cannot_displace_review(self):
        for fault in ('placeholder', 'boolean-code', 'stale-candidate', 'stale-report', 'stale-scope'):
            with self.subTest(fault=fault):
                remote = Remote(); remote.call({'action': 'rtl_review_begin'})
                remote.current.pop('review_scope')
                receipt = {'completed': False, 'tool_isolation_ok': False, 'exit_code': 0,
                           'candidate_sha256': remote.current['candidate_sha256'],
                           'decision_report_sha256': None}
                if fault == 'placeholder': receipt['exit_code'] = None
                elif fault == 'boolean-code': receipt['exit_code'] = True
                elif fault == 'stale-candidate': receipt['candidate_sha256'] = 'other-candidate'
                elif fault == 'stale-report': receipt['decision_report_sha256'] = 'unbound-report'
                elif fault == 'stale-scope': receipt['review_scope'] = 'candidate'
                remote.files['@run/control/rtl-audit/review-1.audit.json'] = json.dumps(receipt)
                before = copy.deepcopy(remote.current)
                with patch.object(flow, 'run_session', side_effect=AssertionError('active review')):
                    with self.assertRaisesRegex(ValueError, 'unfinished review'):
                        flow.review_once(self.context, remote)
                self.assertEqual(remote.current, before)
                self.assertEqual(remote.number, 1)

    def test_legacy_active_repair_is_preserved_without_implicit_migration(self):
        remote = Remote(); remote.call({'action': 'rtl_review_begin'})
        remote.current.pop('review_scope')
        remote.current['repair'] = {'status': 'active'}
        before = copy.deepcopy(remote.current)
        with self.assertRaisesRegex(ValueError, 'active repair cannot be migrated'):
            flow.review_once(self.context, remote)
        self.assertEqual(remote.current, before)
        self.assertEqual(remote.number, 1)

    def test_unknown_and_inconsistent_scope_fail_conservatively(self):
        with self.assertRaisesRegex(ValueError, 'unknown RTL review_scope'):
            flow.review_once(self.context, self.remote, review_scope='regional')
        self.remote.call({'action': 'rtl_review_begin', 'review_scope': 'regional'})
        with self.assertRaisesRegex(ValueError, 'unknown RTL review_scope'):
            flow.review_once(self.context, self.remote)
        remote = Remote()
        def session(*args):
            remote.decide('pass')
            remote.current['decision']['review_scope'] = 'candidate'
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=session):
            with self.assertRaisesRegex(ValueError, 'decision review_scope'):
                flow.review_once(self.context, remote, review_scope='change')

    def test_change_scope_survives_repair_and_budgeted_resume(self):
        def session(prompt, role, models, root, context_path, *args):
            self.assertEqual(json.loads(context_path.read_text())['review_scope'], 'change')
            if role == 'review':
                self.remote.decide('revise')
            return {'completed': True, 'tool_isolation_ok': True}
        handler = flow.RepairHandler('fixture-owner', lambda *args: flow.RepairSession('fixture repair'))
        with patch.object(flow, 'run_session', side_effect=session):
            first = flow.run(self.context, self.remote, repair_handler=handler,
                             max_repairs=1, review_scope='change')
        self.assertEqual(first['status'], 'rtl_repair_budget_exhausted')
        self.assertFalse(first['rtl_stage_release'])
        self.assertEqual(first['review_scope'], 'change')
        self.assertTrue(all(req['review_scope'] == 'change' for req in self.remote.calls
                            if req['action'] == 'rtl_review_begin'))
        before = copy.deepcopy(self.remote.files)
        with patch.object(flow, 'run_session', side_effect=AssertionError('budget reset')):
            resumed = flow.run(self.context, self.remote, repair_handler=handler,
                               max_repairs=1, review_scope='change')
        self.assertEqual(first, resumed)
        self.assertEqual(before, self.remote.files)

    def test_scope_changed_receipt_cannot_be_reused(self):
        with patch.object(flow, 'run_session', side_effect=self.passing(self.remote)):
            flow.review_once(self.context, self.remote, review_scope='change')
        path = '@run/control/rtl-audit/review-1.audit.json'
        receipt = json.loads(self.remote.files[path]); receipt['review_scope'] = 'candidate'
        self.remote.files[path] = json.dumps(receipt)
        with patch.object(flow, 'run_session', side_effect=self.passing(self.remote)) as invoke:
            result = flow.review_once(self.context, self.remote, review_scope='change')
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result['review'], 'review-2')
        self.assertFalse(result['rtl_stage_release'])

    def test_legacy_verdict_is_preserved_but_new_release_requires_fresh_review(self):
        with patch.object(flow, 'run_session', side_effect=self.passing(self.remote)):
            flow.review_once(self.context, self.remote)
        self.remote.current.pop('review_scope')
        self.remote.current['decision'].pop('review_scope')
        path = '@run/control/rtl-audit/review-1.audit.json'
        receipt = json.loads(self.remote.files[path]); receipt.pop('review_scope')
        self.remote.files[path] = json.dumps(receipt)
        legacy = copy.deepcopy(self.remote.current)
        self.assertEqual(flow._decision_result(legacy)['review_verdict'], 'pass')
        self.assertFalse(flow._decision_result(legacy)['rtl_stage_release'])
        with patch.object(flow, 'run_session', side_effect=self.passing(self.remote)) as invoke:
            result = flow.review_once(self.context, self.remote)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result['review'], 'review-2')
        self.assertTrue(result['rtl_stage_release'])
        self.assertEqual(result['review_scope'], 'candidate')
        self.assertEqual(self.remote.files[path], json.dumps(receipt))


class IntegratedScopeRecoveryTests(unittest.TestCase):
    """Scope typing composes with the integrated budget/progress protections."""
    setUp = flow_fixture.FlowTests.setUp

    def prepare_change_repair(self):
        self.remote.call({'action': 'rtl_review_begin', 'review_scope': 'change'})
        self.remote.decide('revise')
        flow._save_receipt(self.remote, self.remote.current, 'audit',
                           {'completed': True, 'tool_isolation_ok': True}, 'audit-fixture')

    def test_change_scope_uses_finite_frozen_default_without_executor_session(self):
        self.prepare_change_repair()
        self.context['settings']['rtl_audit'] = {'max_repairs': 0}
        handler = flow.RepairHandler('owner', lambda *args: flow.RepairSession('repair'))
        with patch.object(flow, 'run_session', side_effect=AssertionError('zero repair budget')):
            result = flow.run(self.context, self.remote, repair_handler=handler, review_scope='change')
        self.assertEqual(result['status'], 'rtl_repair_budget_exhausted')
        self.assertEqual(result['review_scope'], 'change')
        self.assertFalse(result['rtl_stage_release'])

    def test_change_blocked_resume_waits_for_new_evidence_then_still_cannot_release(self):
        def blocked(*args):
            self.remote.decide('blocked')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=blocked):
            first = flow.review_once(self.context, self.remote, review_scope='change')
        before = copy.deepcopy(self.remote.files)
        with patch.object(flow, 'run_session', side_effect=AssertionError('empty resume')):
            self.assertEqual(flow.review_once(self.context, self.remote, review_scope='change'), first)
        self.assertEqual(self.remote.files, before)
        self.remote.current['evidence_sha256'] = 'new-objective-native-record'
        def passed(*args):
            self.remote.decide('pass')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=passed) as invoke:
            result = flow.review_once(self.context, self.remote, review_scope='change')
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result['review'], 'review-2')
        self.assertFalse(result['rtl_stage_release'])
        for path, content in before.items():
            self.assertEqual(self.remote.files[path], content)

    def test_change_repair_without_progress_stops_without_new_freeze(self):
        self.prepare_change_repair()
        self.remote.current['work_current'] = True
        handler = flow.RepairHandler('owner', lambda *args: flow.RepairSession('repair'))
        with patch.object(flow, 'run_session', return_value={
                'completed': True, 'tool_isolation_ok': True}) as invoke:
            result = flow.run(self.context, self.remote, repair_handler=handler,
                              max_repairs=1, review_scope='change')
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result['status'], 'rtl_repair_no_progress')
        self.assertEqual(result['review_scope'], 'change')
        self.assertFalse(result['rtl_stage_release'])
        with patch.object(flow, 'run_session', side_effect=AssertionError('empty repair')):
            self.assertEqual(flow.run(self.context, self.remote, repair_handler=handler,
                                      max_repairs=1, review_scope='change'), result)
        self.assertEqual(self.remote.number, 1)

    def test_completed_old_actor_can_continue_freeze_with_new_actor_without_recharging(self):
        self.prepare_change_repair()
        flow._save_receipt(self.remote, self.remote.current, 'repair',
                           {'completed': True, 'tool_isolation_ok': True}, 'old-repair',
                           'original-owner', 'old-evidence')
        self.remote.current['repair'] = {'status': 'completed'}
        self.remote.current['work_current'] = False
        self.assertTrue(flow._reserve_repair(self.remote, self.remote.current, 'original-owner', 1))
        before = copy.deepcopy(self.remote.files)
        handler = flow.RepairHandler('next-owner', lambda *args: flow.RepairSession('must not run'))
        def passed(prompt, role, *args):
            self.assertEqual(role, 'review')
            self.remote.decide('pass')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=passed) as invoke:
            result = flow.run(self.context, self.remote, repair_handler=handler,
                              max_repairs=0, review_scope='change')
        self.assertEqual(invoke.call_count, 1)
        self.assertFalse(result['rtl_stage_release'])
        self.assertEqual(result['review_scope'], 'change')
        for path, content in before.items():
            self.assertEqual(self.remote.files[path], content)
        self.assertFalse(any(q['action'] in ('rtl_repair_begin', 'rtl_repair_end')
                             for q in self.remote.calls))

    def test_callback_can_switch_next_actor_and_budget_while_preserving_change_scope(self):
        first = flow.RepairHandler('first-owner', lambda *args: flow.RepairSession('first'))
        second = flow.RepairHandler('second-owner', lambda *args: flow.RepairSession('second'))
        seen = []
        def session(prompt, role, models, root, context_path, *args):
            child = json.loads(context_path.read_text())
            self.assertEqual(child['review_scope'], 'change')
            if role == 'review':
                self.remote.decide('pass' if self.remote.number == 3 else 'revise')
            else:
                seen.append(child['repair_actor'])
                self.remote.current['work_current'] = False
            return {'completed': True, 'tool_isolation_ok': True}
        def callback():
            return {'repair_handler': second, 'max_repairs': 1}
        with patch.object(flow, 'run_session', side_effect=session):
            result = flow.run(self.context, self.remote, before_freeze=callback,
                              repair_handler=first, max_repairs=1, review_scope='change')
        self.assertEqual(seen, ['first-owner', 'second-owner'])
        self.assertEqual(result['review_verdict'], 'pass')
        self.assertFalse(result['rtl_stage_release'])
        self.assertTrue(all(q['review_scope'] == 'change' for q in self.remote.calls
                            if q['action'] == 'rtl_review_begin'))
        ledgers = [json.loads(content) for path, content in self.remote.files.items()
                   if '/repair-budget-' in path]
        self.assertEqual({ledger['repair_actor'] for ledger in ledgers}, {'first-owner', 'second-owner'})
        self.assertTrue(all(ledger['limit'] == 1 and len(ledger['rounds']) == 1 for ledger in ledgers))

    def test_architecture_request_and_response_retain_frozen_scope(self):
        self.remote.call({'action': 'rtl_review_begin', 'review_scope': 'change'})
        real_call = self.remote.call
        def changed_response(request):
            value = real_call(request)
            if request.get('include_architecture_context'):
                self.assertEqual(request['review_scope'], 'change')
                value['review_scope'] = 'candidate'
            return value
        with patch.object(self.remote, 'call', side_effect=changed_response), \
                patch.object(flow, 'run_session', side_effect=AssertionError('wrong scope actor')):
            with self.assertRaisesRegex(ValueError, 'stale review_scope binding'):
                flow.review_once(self.context, self.remote, review_scope='change')


if __name__ == '__main__':
    unittest.main()
