"""Optional hook, role isolation and bounded resume routing without model/EDA."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.adapters import readiness


class Remote:
    def __init__(self, *, reused=False, decision='keep_baseline', state='active'):
        self.calls = []
        self.decision = decision
        self.campaign = {'campaign': 'campaign-1', 'work_scope': 'readiness/campaign-1/work',
                         'audit_scope': 'readiness/campaign-1/audit', 'baseline_path': 'work',
                         'state': state, 'source_sha256': 'baseline', 'reused': reused,
                         'budgets': {}, 'remaining_budgets': {}}

    def call(self, request):
        self.calls.append(request)
        action = request['action']
        if action == 'jobs':
            return {'jobs': []}
        if action == 'rtl_readiness_begin':
            return self.campaign.copy()
        if action == 'rtl_readiness_resolve':
            return {'found': False}
        if action == 'read':
            if self.decision == 'missing':
                raise RuntimeError('missing report')
            return {'content': json.dumps({'decision': self.decision}), 'eof': True}
        if action == 'rtl_readiness_complete':
            return {'state': 'completed', 'decision': self.decision}
        raise AssertionError(request)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.skill = self.path / 'skills/rtl-implementation-readiness/SKILL.md'
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text('standalone skill fixture')
        self.context = {'harness_dir': str(self.path), 'project_root': str(self.path),
                        'remote_run_dir': '/server/isolated run',
                        'cache': str(self.path), 'models': {}, 'settings': {'readiness': {
                            'enabled': True, 'session_seconds': 900, 'assistant_turns': 80,
                            'review_seconds': 600, 'review_turns': 50}},
                        'session_id': 'parent', 'session_actor_sha256': 'parent-fingerprint',
                        'role': 'implementation', 'scope': 'work', 'review': 'old-review',
                        'candidate_scope': 'old-candidate'}

    def test_disabled_or_absent_skill_has_no_remote_or_model_effect(self):
        for absent in (False, True):
            with self.subTest(absent=absent):
                self.context['settings']['readiness']['enabled'] = absent
                if absent:
                    self.skill.unlink()
                remote = Remote()
                with patch.object(readiness, 'run_session') as invoke:
                    result = readiness.run(self.context, remote)
                self.assertEqual(result['status'], 'rtl_readiness_skipped')
                self.assertFalse(result['adopted'])
                self.assertEqual(remote.calls, [])
                invoke.assert_not_called()

    def test_existing_active_or_terminal_attempt_never_restarts_model(self):
        for state in ('active', 'evidence_incomplete', 'completed', 'keep_baseline'):
            remote = Remote(reused=True, state=state)
            with patch.object(readiness, 'run_session') as invoke:
                result = readiness.run(self.context, remote)
            self.assertEqual(result['status'], 'rtl_readiness_reused')
            invoke.assert_not_called()
            self.assertEqual([q['action'] for q in remote.calls], ['jobs', 'rtl_readiness_begin'])

    def test_candidate_gets_fresh_scoped_roles_and_native_gate_receives_actor_ids(self):
        remote = Remote(decision='candidate_for_integration')
        children, prompts, labels = [], [], []
        def invoke(prompt, model_role, models, root, context_path, cache, actual_remote, label):
            child = json.loads(context_path.read_text())
            children.append((model_role, child))
            prompts.append(prompt)
            labels.append(label)
            self.assertIs(actual_remote, remote)
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(readiness, 'run_session', side_effect=invoke):
            result = readiness.run(self.context, remote)
        self.assertEqual([role for role, _ in children], ['implementation', 'review'])
        self.assertEqual([c['role'] for _, c in children], ['rtl_readiness', 'readiness_review'])
        self.assertEqual([c['scope'] for _, c in children],
                         ['readiness/campaign-1/work', 'readiness/campaign-1/audit'])
        self.assertEqual([c['readiness_limits'] for _, c in children],
                         [{'session_seconds': 900, 'assistant_turns': 80},
                          {'session_seconds': 600, 'assistant_turns': 50}])
        for _, child in children:
            self.assertEqual(child['baseline_project_root'], '/server/isolated run/work')
            self.assertNotIn('session_id', child)
            self.assertNotIn('session_actor_sha256', child)
            self.assertNotIn('review', child)
            self.assertNotIn('candidate_scope', child)
        self.assertIn('run --foreground', prompts[0])
        self.assertIn('Poll the OUTER job', prompts[0])
        self.assertIn('Every submit, including inspection', prompts[0])
        self.assertIn('A staging project within campaign work is also allowed', prompts[0])
        self.assertIn('Always explicitly repeat --allow-edit only for actual original RTL files', prompts[0])
        self.assertIn('New wrappers may be frozen --source inputs', prompts[0])
        self.assertIn('reserve a functional check, three baseline measurements and three candidate measurements', prompts[0])
        self.assertIn('small fraction of remaining campaign tool time after those reserves', prompts[0])
        self.assertIn('max_job_seconds is a ceiling, not the pilot target', prompts[0])
        self.assertIn('narrow independent multiplicity and/or the source/dependency closure once', prompts[0])
        self.assertIn('Confirm an affordable representative baseline pilot before sealing an optimization variant', prompts[0])
        self.assertIn('batch W --plan P --batch-id ID --tool-seconds S', prompts[0])
        self.assertIn('native timeout at least S+5', prompts[0])
        self.assertIn('Preplan the final group to include the functional check and optional finalize', prompts[0])
        self.assertIn('no per-inner status/read loop', prompts[0])
        completion = remote.calls[-1]
        self.assertEqual(completion['implementation_session_id'], labels[0])
        self.assertEqual(completion['review_session_id'], labels[1])
        self.assertEqual(completion['review_path'], 'review.json')
        self.assertFalse(result['adopted'])  # Controller response is authoritative.

    def test_verified_atomic_bundle_is_handed_to_review_without_model_json_copy(self):
        remote = Remote(decision='candidate_for_integration')
        ordinary = remote.call
        resolved = {'found': True, 'finalization_job_id': '123', 'report_path': 'probe/final/finalization.json',
                    'report': {'decision': 'candidate_for_integration', 'reason': 'local evidence',
                               'workspace': 'probe', 'comparison': 'probe/final/comparison.json', 'delivery': 'probe/final'}}
        def call(request):
            if request['action'] == 'rtl_readiness_resolve':
                remote.calls.append(request)
                return resolved
            return ordinary(request)
        remote.call = call
        prompts = []
        with patch.object(readiness, 'run_session', side_effect=lambda prompt, *a: prompts.append(prompt) or
                          {'completed': True, 'tool_isolation_ok': True}):
            readiness.run(self.context, remote)
        self.assertEqual(len(prompts), 2)
        self.assertIn('finalize W --variant V', prompts[0])
        self.assertIn('no separate result.json', prompts[0])
        self.assertIn('verify-finalization W --delivery D', prompts[1])
        self.assertIn('controller has freshly verified this exact native finalization', prompts[1])
        self.assertIn('only if it resolves a concrete question and the remaining job/time budget permits', prompts[1])
        self.assertIn('probe/final/finalization.json', prompts[1])
        self.assertNotIn('/result.json and its', prompts[1])
        self.assertFalse(any(r['action'] == 'read' for r in remote.calls))
        self.assertEqual(remote.calls[-1]['finalization_job_id'], '123')

    def test_negative_atomic_bundle_survives_watchdog_without_claiming_model_completion(self):
        remote = Remote()
        ordinary = remote.call
        def call(request):
            if request['action'] == 'rtl_readiness_resolve':
                remote.calls.append(request)
                return {'found': True, 'finalization_job_id': '321',
                        'report': {'decision': 'keep_baseline', 'reason': 'no measured benefit'}}
            return ordinary(request)
        remote.call = call
        with patch.object(readiness, 'run_session', return_value={'completed': False, 'tool_isolation_ok': True,
                          'budget_exhausted': True, 'budget_reason': 'assistant_turns'}) as invoke:
            result = readiness.run(self.context, remote)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(remote.calls[-1]['finalization_job_id'], '321')
        self.assertFalse(result['implementation_session_status']['completed'])
        self.assertTrue(result['implementation_session_status']['budget_exhausted'])

    def test_baseline_root_contract_keeps_original_relative_rtl_paths(self):
        remote = Remote()
        value = json.loads(readiness._context_text(self.context, remote.campaign))
        self.assertEqual(value['baseline_project_root'], '/server/isolated run/work')
        self.assertIn('rtl/... without a work/ prefix', value['source_path_base'])
        self.assertIn('campaign staging root preserving original paths/SHA', value['source_path_base'])
        self.assertEqual(value['campaign']['work_scope'], 'readiness/campaign-1/work')

    def test_receipt_path_alone_cannot_be_reported_as_adoption(self):
        remote = Remote(reused=True)
        remote.campaign.update(adoption_path='readiness/campaign-1.adoption.json', adopted=False)
        result = readiness.run(self.context, remote)
        self.assertFalse(result['adopted'])

    def test_failed_or_budget_limited_model_cannot_start_candidate_review(self):
        for answer in ({'completed': False, 'tool_isolation_ok': True},
                       {'completed': True, 'tool_isolation_ok': False},
                       {'completed': True, 'tool_isolation_ok': True, 'budget_exhausted': True}):
            remote = Remote(decision='candidate_for_integration')
            with patch.object(readiness, 'run_session', return_value=answer) as invoke:
                readiness.run(self.context, remote)
            self.assertEqual(invoke.call_count, 1)
            self.assertNotIn('review_session_id', remote.calls[-1])
            self.assertEqual(remote.calls[-1]['action'], 'rtl_readiness_complete')
            self.assertFalse(any(q['action'] == 'read' for q in remote.calls))

    def test_missing_report_is_left_for_native_incomplete_gate_without_retry(self):
        remote = Remote(decision='missing')
        with patch.object(readiness, 'run_session', return_value={
                'completed': True, 'tool_isolation_ok': True}) as invoke:
            readiness.run(self.context, remote)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(remote.calls[-1]['action'], 'rtl_readiness_complete')

    def test_repair_requires_explicit_opt_in(self):
        remote = Remote()
        with patch.object(readiness, 'run_session') as invoke:
            result = readiness.run(self.context, remote, reason='repair')
        self.assertEqual(result['status'], 'rtl_readiness_skipped')
        self.assertEqual(remote.calls, [])
        invoke.assert_not_called()

    def test_exact_content_pages_preserve_unicode_and_boundaries(self):
        pages = [{'content': '{"decision":\n"candidate_for_', 'eof': False,
                  'next_cursor': 'part-two', 'sha256': 'same-source'},
                 {'content': 'integration", "reason":"原始字符 Ω"}',
                  'eof': True, 'sha256': 'same-source'}]
        calls = []
        class Paginated:
            def call(self, request):
                calls.append(request)
                return pages.pop(0)
        result = readiness._json_file(Paginated(), 'readiness/campaign-1/work', 'result.json')
        self.assertEqual(result, {'decision': 'candidate_for_integration', 'reason': '原始字符 Ω'})
        self.assertEqual(calls[1]['cursor'], 'part-two')
        self.assertNotIn('start', calls[1])

    def test_lossy_numbered_pagination_and_source_drift_are_rejected(self):
        cases = [
            [{'text': '1: {"decision":', 'eof': False, 'next_cursor': 'next'}],
            [{'content': '{"decision":', 'eof': False, 'next_cursor': 'next', 'sha256': 'first'},
             {'content': '"keep_baseline"}', 'eof': True, 'sha256': 'changed'}]]
        for pages in cases:
            class Paginated:
                def call(self, request):
                    return pages.pop(0)
            with self.assertRaises(ValueError):
                readiness._json_file(Paginated(), 'readiness/campaign-1/work', 'result.json')


if __name__ == '__main__':
    unittest.main()
