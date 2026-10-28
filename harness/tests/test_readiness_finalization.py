"""Native finalization discovery and adoption gates; offline synthetic receipts."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from harness.runtime import rtl_readiness as readiness
from harness.tests import test_rtl_readiness as legacy


def comparison_fixture(verified, *, created_utc='2026-10-22T00:00:00+00:00', updates=None):
    """Ordinary core comparison shape; native tests still mock the core proof."""
    decision = verified['decision']
    reasons = {'candidate_for_integration': 'local_benefit_with_scoped_functional_evidence',
               'keep_baseline': 'provided_functional_verification_invalid_or_failed',
               'evidence_incomplete': 'functional_verification_missing'}
    report = {'api_version': 2, 'created_utc': created_utc,
              **{key: verified[key] for key in ('workspace_id', 'baseline_identity', 'variant_identity')},
              'variant': 'candidate', 'decision': decision, 'reason': reasons[decision],
              'metric': 'elapsed_seconds', 'baseline_jobs': ['b1', 'b2', 'b3'],
              'candidate_jobs': ['c1', 'c2', 'c3'],
              'verification_jobs': ['q1'] if decision != 'evidence_incomplete' else [],
              'valid_verification': [{'job_id': 'q1', 'kind': 'command', 'scope': 'regression'}]
                                    if decision == 'candidate_for_integration' else [],
              'invalid_verification': [{'job_id': 'q1', 'reason': 'Experiment is not successful and intact: q1'}]
                                      if decision == 'keep_baseline' else [],
              'baseline_values': [2., 2.1, 2.2], 'candidate_values': [1., 1.1, 1.2],
              'median_ratio': 2.1 / 1.1, 'min_ratio': 1.2, 'min_repeats': 3, 'ranges_separated': True,
              'limits': ['Only this synthetic fixture scope was exercised.']}
    report.update(updates or {})
    report['identity'] = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(',', ':'),
                                                 ensure_ascii=False).encode()).hexdigest()
    return report


def bind_comparison(target, verified, **kwargs):
    report = comparison_fixture(verified, **kwargs)
    (target / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False) + '\n')
    return dict(verified, comparison_identity=report['identity'], reason=report['reason'])


class FinalizationTests(unittest.TestCase):
    setUp = legacy.ReadinessTests.setUp
    tearDown = legacy.ReadinessTests.tearDown
    call = legacy.ReadinessTests.call
    begin = legacy.ReadinessTests.begin
    session = legacy.ReadinessTests.session
    candidate = legacy.ReadinessTests.candidate
    complete = legacy.ReadinessTests.complete

    def finalization(self, campaign, verified, *, delivery='probe/delivery', optional_b=True, equal=False,
                     created_utc='2026-10-22T00:00:00+00:00', comparison_updates=None, health=None):
        base = self.out / campaign['work_scope']
        target = base / delivery
        target.mkdir(parents=True, exist_ok=True)
        (target / 'finalization.json').write_text('{"kind":"readiness_finalization_v1"}\n')
        verified = dict(verified, comparison=str(target / 'comparison.json'),
                        delivery=str(target), finalization_path=str(target / 'finalization.json'),
                        finalization_identity='final-' + delivery)
        if health is not None:
            path = Path(health)
            path = path if path.is_absolute() else base / path
            reference = {'path': str(path), 'sha256': readiness.sha(path)}
            verified['health_report'] = reference
            comparison_updates = dict(comparison_updates or {}, health_report=reference)
        verified = bind_comparison(target, verified, created_utc=created_utc, updates=comparison_updates)
        review_path = self.out / campaign['audit_scope'] / 'review.json'
        review = json.loads(review_path.read_text())
        review['comparison_identity'] = verified['comparison_identity']
        review_path.write_text(json.dumps(review))
        argv = ['python'] + (['-B'] if optional_b else [])
        argv += ['@skill/rtl-implementation-readiness/scripts/readiness.py', 'finalize', 'probe',
                 '--variant', 'candidate']
        report = json.loads((target / 'comparison.json').read_text())
        for option, field in (('--baseline-job', 'baseline_jobs'), ('--candidate-job', 'candidate_jobs'),
                              ('--verification-job', 'verification_jobs')):
            for item in report[field]:
                argv += [option, item]
        argv += ['--out=' + delivery] if equal else ['--out', delivery]
        if health is not None:
            argv += ['--health=' + str(health)] if equal else ['--health', str(health)]
        queued = self.call(action='submit', scope=campaign['work_scope'], argv=argv, timeout_seconds=1)
        job = self.out / 'jobs' / queued['job_id']
        request = json.loads((job / 'request.json').read_text())
        spec = request['job_isolation']
        result = dict(exit_code=0, timed_out=False, cleanup_complete=True, container_running=False,
                      elapsed_seconds=.1, job_isolation=spec, candidate_sha256=campaign['source_sha256'],
                      isolation=dict(spec, cleanup_complete=True, container_running=False))
        (job / 'result.json').write_text(json.dumps(result))
        return queued['job_id'], verified

    def resolve(self, campaign, **request):
        return self.call(action='rtl_readiness_resolve', campaign=campaign['campaign'], **request)

    def watchdog_session(self, campaign):
        name = 'watchdog-implementation'
        self.call(action='put_evidence', path='control/' + name + '.prompt.txt', content='synthetic bounded session')
        self.call(action='rtl_session_begin', session_id=name, role='rtl_readiness',
                  scope=campaign['work_scope'], model_role='implementation')
        result = dict(role='implementation', completed=False, exit_code=143, stop_reason='toolUse',
                      tool_isolation_ok=True, budget_exhausted=True, budget_reason='assistant_turns', assistant_turns=80)
        for suffix, data in [('result.json', result), ('isolation.json', {'blocked': [], 'checks': [{'active_tools': ['rtl_remote']}]}),
                             ('events.jsonl', '')]:
            self.call(action='put_evidence', path='control/' + name + '.' + suffix,
                      content=data if isinstance(data, str) else json.dumps(data))
        self.call(action='rtl_session_end', session_id=name)
        return name

    def test_exact_frozen_helper_and_both_out_syntaxes_resolve_normalized_paths(self):
        campaign, verified, _ = self.candidate()
        for index, (optional_b, equal) in enumerate(((False, False), (True, True))):
            job, verified = self.finalization(campaign, verified, optional_b=optional_b, equal=equal)
            with patch.object(readiness, 'verify_finalization', return_value=verified):
                result = self.resolve(campaign, finalization_job_id=job)
            self.assertTrue(result['found'])
            self.assertEqual(result['report']['workspace'], 'probe')
            self.assertEqual(result['report']['delivery'], 'probe/delivery')
            self.assertEqual(result['report_path'], 'probe/delivery/finalization.json')
            self.assertNotIn('_verification', result)
        with patch.object(readiness, 'verify_finalization', return_value=verified):
            self.assertTrue(self.resolve(campaign)['found'])  # Same identity deduplicates.

    def test_lifecycle_and_source_forgery_never_count_as_finalization(self):
        campaign, verified, _ = self.candidate()
        job_id, verified = self.finalization(campaign, verified)
        job = self.out / 'jobs' / job_id
        original_request = json.loads((job / 'request.json').read_text())
        original_result = json.loads((job / 'result.json').read_text())
        for update in ({'exit_code': 2}, {'cleanup_complete': False}, {'container_running': True},
                       {'timed_out': True}, {'error': 'failed'}, {'candidate_sha256': 'wrong'},
                       {'isolation': {}}, {'job_isolation': {}}):
            with self.subTest(update=update), patch.object(readiness, 'verify_finalization') as verify:
                (job / 'result.json').write_text(json.dumps(original_result | update))
                self.assertFalse(self.resolve(campaign, finalization_job_id=job_id)['found'])
                verify.assert_not_called()
        (job / 'result.json').write_text(json.dumps(original_result))
        for update in ({'scope': campaign['audit_scope']}, {'cwd': str(self.out / 'work')},
                       {'candidate_sha256': 'wrong'}, {'readiness_budget': {}},
                       {'argv': [original_request['argv'][0], '-c', 'print("finalize")']},
                       {'argv': original_request['argv'] + ['--out', 'probe/other']}):
            with self.subTest(update=update), patch.object(readiness, 'verify_finalization') as verify:
                (job / 'request.json').write_text(json.dumps(original_request | update))
                self.assertFalse(self.resolve(campaign, finalization_job_id=job_id)['found'])
                verify.assert_not_called()
        (job / 'request.json').write_text(json.dumps(original_request))

    def test_health_option_both_syntaxes_resolves_and_binds_fresh_report(self):
        campaign, verified, _ = self.candidate()
        base = self.out / campaign['work_scope']
        health = base / 'probe/health.json'
        health.write_text('{"fixture":"health semantic verification belongs to the frozen helper"}\n')
        for equal in (False, True):
            job, checked = self.finalization(campaign, verified, health='probe/health.json', equal=equal)
            with patch.object(readiness, 'verify_finalization', return_value=checked) as verify:
                self.assertTrue(self.resolve(campaign, finalization_job_id=job)['found'])
            verify.assert_called_with(self.out, base / 'probe', base / 'probe/delivery')
            for reference in (None, checked['health_report'] | {'sha256': 'wrong'},
                              checked['health_report'] | {'path': str(base / 'probe/other.json')}):
                with self.subTest(reference=reference), patch.object(readiness, 'verify_finalization',
                        return_value=checked | {'health_report': reference}):
                    result = self.resolve(campaign, finalization_job_id=job)
                self.assertFalse(result['found'])
                self.assertIn('health report differs from its native command', result['reason'])

    def test_health_option_rejects_unsafe_missing_duplicate_and_unknown_paths_before_verification(self):
        campaign, verified, _ = self.candidate()
        base = self.out / campaign['work_scope']
        health = base / 'probe/health.json'
        health.write_text('{}\n')
        alias = health.with_name('health-alias.json')
        alias.symlink_to(health)
        job, checked = self.finalization(campaign, verified)
        request_path = self.out / 'jobs' / job / 'request.json'
        original = json.loads(request_path.read_text())
        cases = [(['--health', raw]) for raw in ('../escape', str(self.out / 'work/outside'),
                  'health-outside-workspace.json', 'probe/missing.json', 'probe', 'probe/health-alias.json')]
        (base / 'health-outside-workspace.json').write_text('{}\n')
        cases += [['--health', 'probe/health.json', '--health=probe/health.json'],
                  ['--health'], ['--unknown-health', 'probe/health.json']]
        for options in cases:
            with self.subTest(options=options), patch.object(readiness, 'verify_finalization') as verify:
                request_path.write_text(json.dumps(original | {'argv': original['argv'] + options}))
                self.assertFalse(self.resolve(campaign, finalization_job_id=job)['found'])
                verify.assert_not_called()

    def test_comparison_health_reference_must_match_fresh_verification(self):
        campaign, verified, _ = self.candidate()
        base = self.out / campaign['work_scope']
        (base / 'probe/health.json').write_text('{}\n')
        job, verified = self.finalization(campaign, verified, health='probe/health.json')
        target = base / 'probe/delivery'
        changed = bind_comparison(target, verified, updates={'health_report':
                                  verified['health_report'] | {'sha256': 'different'}})
        with patch.object(readiness, 'verify_finalization', return_value=changed):
            result = self.resolve(campaign, finalization_job_id=job)
        self.assertFalse(result['found'])
        self.assertIn('comparison differs from verified health report', result['reason'])

    def test_real_frozen_helper_consumes_health_and_rejects_report_tampering(self):
        """Real standalone finalization/verification; only bounded Python commands, no EDA."""
        campaign = self.begin()
        base = self.out / campaign['work_scope']
        scripts = self.out / 'snapshot/harness/skills/rtl-implementation-readiness/scripts'
        shutil.copytree(Path(__file__).resolve().parents[1] /
                        'skills/rtl-implementation-readiness/scripts', scripts, dirs_exist_ok=True)
        manifest = json.loads((self.out / 'manifest.json').read_text())
        manifest['settings']['tools']['python'] = sys.executable
        manifest['harness_sha256'] = legacy.inventory(self.out / 'snapshot/harness')
        (self.out / 'manifest.json').write_text(json.dumps(manifest))
        script = scripts / 'readiness.py'

        def cli(*args):
            result = subprocess.run([sys.executable, '-B', str(script), *map(str, args)],
                                    cwd=base, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        cli('init', '--project', self.out / 'work', '--workspace', 'probe', '--top', 'block',
            '--source', 'rtl/block.sv', '--allow-edit', 'rtl/block.sv')
        (base / 'overlay/rtl').mkdir(parents=True)
        (base / 'overlay/rtl/block.sv').write_text('module block; wire x = 1; endmodule\n')
        cli('variant', 'probe', 'candidate', '--overlay', 'overlay')
        (base / 'measure.json').write_text(json.dumps({'kind': 'command', 'purpose': 'measurement',
            'argv': [sys.executable, '-c', 'print("bounded fixture measurement; no RTL tool")'],
            'timeout_seconds': 5, 'memory_mib': 256}))
        for variant, job in (('baseline', 'b1'), ('candidate', 'c1')):
            cli('run', 'probe', '--variant', variant, '--config', 'measure.json', '--job-id', job, '--foreground')
        cli('health', 'probe', '--variant', 'candidate', '--out', 'probe/health.json')
        argv = ['python', '-B', '@skill/rtl-implementation-readiness/scripts/readiness.py', 'finalize',
                'probe', '--variant', 'candidate', '--baseline-job', 'b1', '--candidate-job', 'c1',
                '--min-repeats', '1', '--health', 'probe/health.json', '--out', 'probe/delivery']
        cli(*argv[3:])
        queued = self.call(action='submit', scope=campaign['work_scope'], argv=argv, timeout_seconds=1)
        job = self.out / 'jobs' / queued['job_id']
        spec = json.loads((job / 'request.json').read_text())['job_isolation']
        (job / 'result.json').write_text(json.dumps(dict(exit_code=0, timed_out=False,
            cleanup_complete=True, container_running=False, elapsed_seconds=.1, job_isolation=spec,
            candidate_sha256=campaign['source_sha256'], isolation=dict(spec, cleanup_complete=True, container_running=False))))
        result = self.resolve(campaign, finalization_job_id=queued['job_id'])
        self.assertTrue(result['found'], result)
        self.assertEqual(result['report']['decision'], 'evidence_incomplete')
        # No functional proof was supplied: valid structural plumbing must not
        # promote this ordinary incomplete comparison to an accepted candidate.
        health = base / 'probe/health.json'
        changed = json.loads(health.read_text())
        changed['variant_identity'] = 'forged'
        changed['identity'] = hashlib.sha256(json.dumps(
            {key: value for key, value in changed.items() if key != 'identity'},
            sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        health.write_text(json.dumps(changed))
        # A self-consistent JSON digest still cannot replace the source-bound
        # report: the real helper recomputes health from the sealed workspace.
        forged_argv = argv[3:-1] + ['probe/forged-delivery']
        rejected = subprocess.run([sys.executable, '-B', str(script), *forged_argv],
                                  cwd=base, capture_output=True, text=True, timeout=15)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn('Structural health report is stale or its evidence changed', rejected.stderr)
        self.assertFalse((base / 'probe/forged-delivery').exists())
        result = self.resolve(campaign, finalization_job_id=queued['job_id'])
        self.assertFalse(result['found'])
        self.assertIn('standalone finalization verify failed', result['reason'])

    def test_path_escape_alias_and_verifier_path_disagreement_are_rejected(self):
        campaign, verified, _ = self.candidate()
        job, verified = self.finalization(campaign, verified)
        request_path = self.out / 'jobs' / job / 'request.json'
        original = json.loads(request_path.read_text())
        for raw in ('../escape', str(self.out / 'work/outside')):
            changed = copy.deepcopy(original)
            changed['argv'][-1] = raw
            request_path.write_text(json.dumps(changed))
            with patch.object(readiness, 'verify_finalization') as verify:
                self.assertFalse(self.resolve(campaign, finalization_job_id=job)['found'])
                verify.assert_not_called()
        request_path.write_text(json.dumps(original))
        with patch.object(readiness, 'verify_finalization', return_value=verified | {'finalization_path': '/outside/file.json'}):
            self.assertFalse(self.resolve(campaign, finalization_job_id=job)['found'])
        target = self.out / campaign['work_scope'] / 'probe/delivery'
        target.rename(target.with_name('real-delivery'))
        target.symlink_to(target.with_name('real-delivery'), target_is_directory=True)
        with patch.object(readiness, 'verify_finalization') as verify:
            self.assertFalse(self.resolve(campaign, finalization_job_id=job)['found'])
            verify.assert_not_called()

    def test_distinct_verified_bundles_are_ambiguous(self):
        campaign, verified, _ = self.candidate()
        first, one = self.finalization(campaign, verified)
        second, two = self.finalization(campaign, verified, delivery='probe/second',
                                       comparison_updates={'baseline_jobs': ['other1', 'other2', 'other3']})
        with patch.object(readiness, 'verify_finalization', side_effect=[one, two]):
            result = self.resolve(campaign)
        self.assertFalse(result['found'])
        self.assertTrue(result['ambiguous'])
        with patch.object(readiness, 'verify_finalization', side_effect=[one, two]):
            self.assertTrue(self.resolve(campaign, finalization_job_id=first)['ambiguous'])

    def test_relative_workspace_and_output_use_recorded_nested_cwd(self):
        campaign, verified, _ = self.candidate()
        job, verified = self.finalization(campaign, verified)
        base = self.out / campaign['work_scope']
        (base / 'nested').mkdir()
        (base / 'probe').rename(base / 'nested/probe')
        request_path = self.out / 'jobs' / job / 'request.json'
        request = json.loads(request_path.read_text())
        request['cwd'] = str(base / 'nested')
        request_path.write_text(json.dumps(request))
        for key in ('workspace', 'delivery', 'comparison', 'finalization_path', 'candidate_tree'):
            verified[key] = verified[key].replace(str(base / 'probe'), str(base / 'nested/probe'))
        with patch.object(readiness, 'verify_finalization', return_value=verified) as verify:
            result = self.resolve(campaign, finalization_job_id=job)
        verify.assert_called_once_with(self.out, base / 'nested/probe', base / 'nested/probe/delivery')
        self.assertEqual(result['report']['workspace'], 'nested/probe')

    def test_discovery_never_recursively_scans_workspace_sources(self):
        campaign, verified, _ = self.candidate()
        job, verified = self.finalization(campaign, verified)
        with patch.object(readiness, 'verify_finalization', return_value=verified), \
                patch.object(Path, 'rglob', side_effect=AssertionError('recursive discovery unavailable')):
            self.assertTrue(readiness.resolve(self.out, {'campaign': campaign['campaign']})['found'])

    def test_verifier_is_exact_frozen_readonly_command(self):
        campaign, verified, _ = self.candidate()
        base = self.out / campaign['work_scope']
        workspace = base / 'probe'
        result = type('Result', (), {'returncode': 0, 'stdout': json.dumps({'decision': 'keep_baseline'}), 'stderr': ''})()
        with patch.object(readiness.subprocess, 'run', return_value=result) as run:
            self.assertEqual(readiness.verify_finalization(self.out, workspace, workspace / 'delivery'),
                             {'decision': 'keep_baseline'})
        argv = run.call_args.args[0]
        self.assertEqual(argv[1], '-B')
        self.assertEqual(argv[3:], ['verify-finalization', str(workspace), '--delivery', str(workspace / 'delivery')])
        self.assertTrue(argv[2].endswith('/snapshot/harness/skills/rtl-implementation-readiness/scripts/readiness.py'))

    def test_complete_freshly_reverifies_selected_job_and_rejects_corruption(self):
        campaign, verified, sessions = self.candidate()
        job, verified = self.finalization(campaign, verified)
        before = (self.rtl / 'block.sv').read_bytes()
        with patch.object(readiness, 'verify_finalization', return_value=verified):
            self.assertTrue(self.resolve(campaign)['found'])
        with patch.object(readiness, 'verify_finalization', side_effect=ValueError('artifact changed since discovery')):
            result = self.complete(campaign, finalization_job_id=job, **sessions)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertEqual((self.rtl / 'block.sv').read_bytes(), before)

    def test_negative_bundle_closes_after_watchdog_but_records_actual_termination(self):
        campaign, verified, _ = self.candidate()
        verified['decision'] = 'keep_baseline'
        job, verified = self.finalization(campaign, verified)
        session = self.watchdog_session(campaign)
        before = (self.rtl / 'block.sv').read_bytes()
        (self.out / campaign['work_scope'] / 'result.json').unlink()
        with patch.object(readiness, 'verify_finalization', return_value=verified):
            result = self.complete(campaign, finalization_job_id=job, implementation_session_id=session)
        self.assertEqual(result['state'], 'keep_baseline')
        self.assertFalse(result['adopted'])
        self.assertFalse(result['implementation_model_status']['completed'])
        self.assertTrue(result['implementation_model_status']['budget_exhausted'])
        self.assertEqual((self.rtl / 'block.sv').read_bytes(), before)
        receipt = json.loads((self.out / result['adoption_path']).read_text())
        self.assertEqual(receipt['finalization_job_id'], job)
        self.assertEqual(receipt['report_path'], campaign['work_scope'] + '/probe/delivery/finalization.json')

    def test_positive_bundle_still_needs_completed_actor_and_accepting_review(self):
        campaign, verified, sessions = self.candidate()
        job, verified = self.finalization(campaign, verified)
        with patch.object(readiness, 'verify_finalization', return_value=verified):
            result = self.complete(campaign, finalization_job_id=job, **sessions)
        self.assertEqual(result['state'], 'adopted')
        self.assertIn('wire x = 1', (self.rtl / 'block.sv').read_text())

    def test_positive_bundle_cannot_promote_watchdog_actor(self):
        campaign, verified, sessions = self.candidate()
        job, verified = self.finalization(campaign, verified)
        sessions['implementation_session_id'] = self.watchdog_session(campaign)
        with patch.object(readiness, 'verify_finalization', return_value=verified):
            result = self.complete(campaign, finalization_job_id=job, **sessions)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertFalse(result['adopted'])

    def test_positive_bundle_rejected_review_cannot_adopt(self):
        campaign, verified, sessions = self.candidate(accepted=False)
        job, verified = self.finalization(campaign, verified)
        with patch.object(readiness, 'verify_finalization', return_value=verified):
            result = self.complete(campaign, finalization_job_id=job, **sessions)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertFalse(result['adopted'])


if __name__ == '__main__':
    unittest.main()
