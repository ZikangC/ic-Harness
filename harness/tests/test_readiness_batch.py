"""Finite batch discovery uses fresh core evidence and unchanged adoption gates."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from harness.runtime import rtl_readiness as readiness
from harness.tests import test_readiness_finalization as finalization


class BatchTests(unittest.TestCase):
    setUp = finalization.FinalizationTests.setUp
    tearDown = finalization.FinalizationTests.tearDown
    call = finalization.FinalizationTests.call
    begin = finalization.FinalizationTests.begin
    candidate = finalization.FinalizationTests.candidate
    session = finalization.FinalizationTests.session
    complete = finalization.FinalizationTests.complete
    resolve = finalization.FinalizationTests.resolve
    watchdog_session = finalization.FinalizationTests.watchdog_session
    finalization = finalization.FinalizationTests.finalization

    def batch(self, campaign, verified, *, optional_b=True, equal=False, delivery='probe/delivery',
              created_utc='2026-10-22T00:00:00+00:00', comparison_updates=None):
        base = self.out / campaign['work_scope']
        target = base / delivery
        target.mkdir(parents=True, exist_ok=True)
        (target / 'finalization.json').write_text('{"kind":"readiness_finalization_v1"}\n')
        verified = dict(verified, comparison=str(target / 'comparison.json'),
                        delivery=str(target), finalization_path=str(target / 'finalization.json'),
                        finalization_identity='final-' + delivery)
        verified = finalization.bind_comparison(target, verified, created_utc=created_utc, updates=comparison_updates)
        review_path = self.out / campaign['audit_scope'] / 'review.json'
        review = json.loads(review_path.read_text())
        review['comparison_identity'] = verified['comparison_identity']
        review_path.write_text(json.dumps(review))
        plan = base / 'probe/plan.json'
        plan.write_text('{"jobs": []}\n')
        argv = ['python'] + (['-B'] if optional_b else [])
        argv += ['@skill/rtl-implementation-readiness/scripts/readiness.py', 'batch', 'probe']
        options = {'--plan': 'probe/plan.json', '--batch-id': 'measure-1', '--tool-seconds': '1'}
        for key, value in options.items():
            argv.extend([key + '=' + value] if equal else [key, value])
        queued = self.call(action='submit', scope=campaign['work_scope'], argv=argv, timeout_seconds=6)
        job = self.out / 'jobs' / queued['job_id']
        request = json.loads((job / 'request.json').read_text())
        spec = request['job_isolation']
        result = dict(exit_code=0, timed_out=False, cleanup_complete=True, container_running=False,
                      elapsed_seconds=.1, job_isolation=spec, candidate_sha256=campaign['source_sha256'],
                      isolation=dict(spec, cleanup_complete=True, container_running=False))
        (job / 'result.json').write_text(json.dumps(result))
        observed = dict(workspace=str(base / 'probe'), plan=str(plan), batch_id='measure-1',
                        tool_seconds=1, batch_identity='batch-pin', status='completed', finalization=verified)
        return queued['job_id'], observed

    def test_exact_batch_cli_and_fresh_nested_bundle_resolve(self):
        campaign, verified, _ = self.candidate()
        for optional_b, equal in ((False, False), (True, True)):
            job, observed = self.batch(campaign, verified, optional_b=optional_b, equal=equal)
            with patch.object(readiness, 'verify_batch', return_value=observed) as verify:
                result = self.resolve(campaign, finalization_job_id=job)
            self.assertTrue(result['found'])
            self.assertEqual(result['report']['workspace'], 'probe')
            self.assertEqual(result['report']['delivery'], 'probe/delivery')
            self.assertEqual(result['report_path'], 'probe/delivery/finalization.json')
            self.assertEqual(result['finalization_job_id'], job)
            verify.assert_any_call(self.out, Path(observed['workspace']), Path(observed['plan']), 'measure-1', 1)
            self.assertEqual(verify.call_count, 1 if optional_b is False else 2)
        with patch.object(readiness, 'verify_batch', return_value=observed):
            self.assertTrue(self.resolve(campaign)['found'])

    def test_submission_reserves_native_margin_without_changing_normal_limits(self):
        campaign = self.begin()
        base = self.out / campaign['work_scope']
        (base / 'probe').mkdir()
        (base / 'probe/plan.json').write_text('{"jobs": []}\n')
        argv = ['python', '@skill/rtl-implementation-readiness/scripts/readiness.py', 'batch', 'probe',
                '--plan', 'probe/plan.json', '--batch-id', 'measure-1', '--tool-seconds', '2']
        with self.assertRaisesRegex(ValueError, 'five seconds'):
            self.call(action='submit', scope=campaign['work_scope'], argv=argv, timeout_seconds=6)
        self.assertEqual(list((self.out / 'jobs').glob('*')), [])
        self.assertEqual(self.call(action='rtl_readiness_status', campaign=campaign['campaign'])['remaining_budgets']['max_jobs'], 2)

    def test_batch_id_length_matches_standalone_boundary_before_reserving_job(self):
        campaign = self.begin()
        base = self.out / campaign['work_scope']
        (base / 'probe').mkdir()
        (base / 'probe/plan.json').write_text('{"jobs": []}\n')
        argv = ['python', '@skill/rtl-implementation-readiness/scripts/readiness.py', 'batch', 'probe',
                '--plan', 'probe/plan.json', '--batch-id', 'b' * 81, '--tool-seconds', '1']
        with self.assertRaisesRegex(ValueError, 'invalid readiness batch ID'):
            self.call(action='submit', scope=campaign['work_scope'], argv=argv, timeout_seconds=6)
        self.assertEqual(list((self.out / 'jobs').glob('*')), [])
        argv[argv.index('--batch-id') + 1] = 'b' * 80
        queued = self.call(action='submit', scope=campaign['work_scope'], argv=argv, timeout_seconds=6)
        self.assertEqual(queued['status'], 'queued')

    def test_no_bundle_is_not_a_negative_report_or_model_success(self):
        campaign, verified, _ = self.candidate()
        job, observed = self.batch(campaign, verified)
        (self.out / campaign['work_scope'] / 'result.json').unlink()
        before = (self.rtl / 'block.sv').read_bytes()
        with patch.object(readiness, 'verify_batch', return_value=observed | {'finalization': None}):
            self.assertFalse(self.resolve(campaign)['found'])
            result = self.complete(campaign, finalization_job_id=job)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertFalse(result['adopted'])
        self.assertEqual(before, (self.rtl / 'block.sv').read_bytes())

    def test_invocation_and_nested_path_disagreements_are_rejected(self):
        campaign, verified, _ = self.candidate()
        job, observed = self.batch(campaign, verified)
        updates = ({'workspace': '/outside'}, {'plan': '/outside'}, {'batch_id': 'different'},
                   {'tool_seconds': 2}, {'tool_seconds': True}, {'batch_identity': ''},
                   {'finalization': observed['finalization'] | {'delivery': '/outside'}},
                   {'finalization': observed['finalization'] | {'finalization_path': '/outside'}})
        for update in updates:
            with self.subTest(update=update), patch.object(readiness, 'verify_batch', return_value=observed | update):
                self.assertFalse(self.resolve(campaign, finalization_job_id=job)['found'])
        request_path = self.out / 'jobs' / job / 'request.json'
        request = json.loads(request_path.read_text())
        for update in ({'timeout_seconds': 5}, {'cwd': str(self.out / 'work')},
                       {'argv': [request['argv'][0], '-c', 'print("batch")']},
                       {'argv': request['argv'] + ['--plan', 'probe/other.json']}):
            with self.subTest(update=update), patch.object(readiness, 'verify_batch') as verify:
                request_path.write_text(json.dumps(request | update))
                self.assertFalse(self.resolve(campaign, finalization_job_id=job)['found'])
                verify.assert_not_called()

    def test_plan_escape_alias_or_missing_file_is_rejected_before_core(self):
        campaign, verified, _ = self.candidate()
        job, observed = self.batch(campaign, verified)
        request_path = self.out / 'jobs' / job / 'request.json'
        request = json.loads(request_path.read_text())
        plan_index = request['argv'].index('--plan') + 1
        for raw in ('../outside', str(self.out / 'work/plan.json'), 'missing.json'):
            changed = copy.deepcopy(request)
            changed['argv'][plan_index] = raw
            request_path.write_text(json.dumps(changed))
            with patch.object(readiness, 'verify_batch') as verify:
                self.assertFalse(self.resolve(campaign, finalization_job_id=job)['found'])
                verify.assert_not_called()
        request_path.write_text(json.dumps(request))
        plan = Path(observed['plan'])
        plan.rename(plan.with_name('real-plan.json'))
        plan.symlink_to(plan.with_name('real-plan.json'))
        with patch.object(readiness, 'verify_batch') as verify:
            self.assertFalse(self.resolve(campaign, finalization_job_id=job)['found'])
            verify.assert_not_called()

    def test_complete_freshly_reverifies_batch_and_stale_core_evidence_preserves_rtl(self):
        campaign, verified, sessions = self.candidate()
        job, observed = self.batch(campaign, verified)
        before = (self.rtl / 'block.sv').read_bytes()
        with patch.object(readiness, 'verify_batch', return_value=observed):
            self.assertTrue(self.resolve(campaign)['found'])
        with patch.object(readiness, 'verify_batch', side_effect=ValueError('changed batch receipt')):
            result = self.complete(campaign, finalization_job_id=job, **sessions)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertEqual(before, (self.rtl / 'block.sv').read_bytes())

    def test_unclean_native_batch_is_rejected_without_running_core_verifier(self):
        campaign, verified, _ = self.candidate()
        job, _ = self.batch(campaign, verified)
        result_path = self.out / 'jobs' / job / 'result.json'
        original = json.loads(result_path.read_text())
        for update in ({'cleanup_complete': False}, {'container_running': True}, {'exit_code': 1},
                       {'timed_out': True}, {'candidate_sha256': 'different'}):
            result_path.write_text(json.dumps(original | update))
            with self.subTest(update=update), patch.object(readiness, 'verify_batch') as verify:
                self.assertFalse(self.resolve(campaign, finalization_job_id=job)['found'])
                verify.assert_not_called()

    def test_batch_and_direct_distinct_finalizations_remain_ambiguous(self):
        campaign, verified, _ = self.candidate()
        direct, one = self.finalization(campaign, verified)
        batch_job, observed = self.batch(campaign, verified, delivery='probe/second',
                                         comparison_updates={'verification_jobs': ['other-proof']})
        with patch.object(readiness, 'verify_finalization', return_value=one), \
                patch.object(readiness, 'verify_batch', return_value=observed):
            result = self.resolve(campaign, finalization_job_id=batch_job)
        self.assertFalse(result['found'])
        self.assertTrue(result['ambiguous'])

    def test_batch_relative_paths_follow_the_recorded_nested_cwd(self):
        campaign, verified, _ = self.candidate()
        job, observed = self.batch(campaign, verified)
        base = self.out / campaign['work_scope']
        (base / 'nested').mkdir()
        (base / 'probe').rename(base / 'nested/probe')
        request_path = self.out / 'jobs' / job / 'request.json'
        request = json.loads(request_path.read_text())
        request['cwd'] = str(base / 'nested')
        request_path.write_text(json.dumps(request))
        for key in ('workspace', 'plan'):
            observed[key] = observed[key].replace(str(base / 'probe'), str(base / 'nested/probe'))
        for key in ('workspace', 'delivery', 'comparison', 'finalization_path', 'candidate_tree'):
            observed['finalization'][key] = observed['finalization'][key].replace(str(base / 'probe'), str(base / 'nested/probe'))
        with patch.object(readiness, 'verify_batch', return_value=observed) as verify:
            result = self.resolve(campaign, finalization_job_id=job)
        self.assertTrue(result['found'])
        self.assertEqual(result['report']['workspace'], 'nested/probe')
        verify.assert_called_once_with(self.out, base / 'nested/probe', base / 'nested/probe/plan.json', 'measure-1', 1)

    def test_positive_batch_still_requires_completed_model_and_independent_review(self):
        campaign, verified, sessions = self.candidate()
        job, observed = self.batch(campaign, verified)
        with patch.object(readiness, 'verify_batch', return_value=observed):
            result = self.complete(campaign, finalization_job_id=job, **sessions)
        self.assertEqual(result['state'], 'adopted')
        self.assertIn('wire x = 1', (self.rtl / 'block.sv').read_text())

    def test_watchdog_positive_cannot_adopt_but_verified_negative_closes_honestly(self):
        for decision in ('candidate_for_integration', 'keep_baseline'):
            with self.subTest(decision=decision):
                if decision == 'keep_baseline':
                    self.tearDown(); self.setUp()
                campaign, verified, sessions = self.candidate()
                verified['decision'] = decision
                job, observed = self.batch(campaign, verified)
                sessions['implementation_session_id'] = self.watchdog_session(campaign)
                before = (self.rtl / 'block.sv').read_bytes()
                with patch.object(readiness, 'verify_batch', return_value=observed):
                    result = self.complete(campaign, finalization_job_id=job, **sessions)
                self.assertEqual(result['state'], 'keep_baseline' if decision == 'keep_baseline' else 'evidence_incomplete')
                self.assertFalse(result['adopted'])
                self.assertFalse(result['implementation_model_status']['completed'])
                self.assertTrue(result['implementation_model_status']['budget_exhausted'])
                self.assertEqual(before, (self.rtl / 'block.sv').read_bytes())

    def test_batch_rejected_independent_review_cannot_adopt(self):
        campaign, verified, sessions = self.candidate(accepted=False)
        job, observed = self.batch(campaign, verified)
        before = (self.rtl / 'block.sv').read_bytes()
        with patch.object(readiness, 'verify_batch', return_value=observed):
            result = self.complete(campaign, finalization_job_id=job, **sessions)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertFalse(result['adopted'])
        self.assertEqual(before, (self.rtl / 'block.sv').read_bytes())

    def test_batch_verifier_uses_exact_frozen_pure_command(self):
        campaign, _, _ = self.candidate()
        workspace = self.out / campaign['work_scope'] / 'probe'
        plan = workspace / 'plan.json'
        result = type('Result', (), {'returncode': 0, 'stdout': json.dumps({'finalization': None}), 'stderr': ''})()
        with patch.object(readiness.subprocess, 'run', return_value=result) as run:
            self.assertEqual(readiness.verify_batch(self.out, workspace, plan, 'measure-1', 1), {'finalization': None})
        argv = run.call_args.args[0]
        self.assertEqual(argv[1], '-B')
        self.assertEqual(argv[3:], ['verify-batch', str(workspace), '--plan', str(plan), '--batch-id', 'measure-1', '--tool-seconds', '1'])
        self.assertTrue(argv[2].endswith('/snapshot/harness/skills/rtl-implementation-readiness/scripts/readiness.py'))


if __name__ == '__main__':
    unittest.main()
