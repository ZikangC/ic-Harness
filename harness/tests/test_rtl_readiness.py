"""Offline native campaign, scope, provenance and adoption boundary tests."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from harness.runtime import rtl_readiness as readiness
from harness.runtime.rtl_worker import dispatch, inventory, sha
from harness.tests import test_rtl_remote as fixture


class ReadinessTests(unittest.TestCase):
    tearDown = fixture.WorkerTests.tearDown

    def setUp(self):
        fixture.WorkerTests.setUp(self)
        self.rtl = self.out / 'work/rtl'
        self.rtl.mkdir()
        (self.rtl / 'block.sv').write_text('module block; wire x = 0; endmodule\n')
        (self.rtl / 'defs.svh').write_text('`define VALUE 0\n')
        (self.out / 'work/tests').mkdir()
        (self.out / 'work/tests/equivalence.py').write_text('baseline test sentinel\n')
        script = self.out / 'snapshot/harness/skills/rtl-implementation-readiness/scripts/readiness.py'
        script.parent.mkdir(parents=True)
        script.write_text('# synthetic verifier; adoption tests inject a structured core receipt\n')
        manifest = json.loads((self.out / 'manifest.json').read_text())
        manifest['harness_sha256'] = inventory(self.out / 'snapshot/harness')
        manifest['settings']['readiness'] = dict(enabled=True, max_campaigns=1,
                                                max_jobs=2, max_job_seconds=6, tool_seconds=10)
        (self.out / 'manifest.json').write_text(json.dumps(manifest))

    def call(self, **request):
        return dispatch(self.root, self.runid, request)

    def begin(self):
        return self.call(action='rtl_readiness_begin')

    def session(self, campaign, review=False, completed=True, tools=None):
        name = 'readiness-review' if review else 'readiness-implementation'
        role = 'readiness_review' if review else 'rtl_readiness'
        scope = campaign['audit_scope'] if review else campaign['work_scope']
        model = 'review' if review else 'implementation'
        self.call(action='put_evidence', path='control/' + name + '.prompt.txt', content='Explicit synthetic native session fixture')
        self.call(action='rtl_session_begin', session_id=name, role=role, scope=scope, model_role=model)
        result = dict(role=model, completed=completed, exit_code=0, stop_reason='stop', tool_isolation_ok=True)
        isolation = dict(blocked=False, checks=[{'active_tools': tools or ['rtl_remote']}])
        for suffix, value in [('result.json', json.dumps(result)), ('isolation.json', json.dumps(isolation)), ('events.jsonl', '')]:
            self.call(action='put_evidence', path='control/' + name + '.' + suffix, content=value)
        self.call(action='rtl_session_end', session_id=name)
        return name

    def candidate(self, accepted=True):
        campaign = self.begin()
        work = self.out / campaign['work_scope']
        workspace = work / 'probe'
        tree = workspace / 'delivery/candidate'
        (tree / 'rtl').mkdir(parents=True)
        (tree / 'rtl/block.sv').write_text('module block; wire x = 1; endmodule\n')
        # An exported unrelated asset must never be copied to implementation.
        (tree / 'tests').mkdir()
        (tree / 'tests/equivalence.py').write_text('changed test should never be adopted\n')
        source = readiness.source_inventory(self.out / 'work')
        files = dict(source)
        files['rtl/block.sv'] = dict(sha256=sha(tree / 'rtl/block.sv'), size=(tree / 'rtl/block.sv').stat().st_size)
        verified = dict(decision='candidate_for_integration', comparison_identity='comparison-pin',
                        baseline_identity='baseline-pin', variant_identity='variant-pin', workspace_id='workspace-pin',
                        workspace=str(workspace), delivery=str(tree.parent), candidate_tree=str(tree),
                        files=files, baseline_files=source, allowed_paths=['rtl/block.sv', 'rtl/defs.svh'],
                        changes=['rtl/block.sv'])
        (work / 'result.json').write_text(json.dumps(dict(decision='candidate_for_integration', reason='Explicit local evidence',
            workspace='probe', comparison='probe/comparison.json', delivery='probe/delivery')))
        review = {key: verified[key] for key in ('comparison_identity', 'baseline_identity', 'variant_identity')}
        review.update(verdict='accept' if accepted else 'revise', reason='Synthetic independent review')
        (self.out / campaign['audit_scope'] / 'review.json').write_text(json.dumps(review))
        sessions = dict(implementation_session_id=self.session(campaign), review_session_id=self.session(campaign, review=True))
        return campaign, verified, sessions

    def complete(self, campaign, **extra):
        return self.call(action='rtl_readiness_complete', campaign=campaign['campaign'], report_path='result.json', **extra)

    def test_small_source_fingerprint_and_reuse_ignore_build_output_changes(self):
        campaign = self.begin()
        (self.out / 'work/build.log').write_text('new unrelated synthesis log\n')
        (self.out / 'work/tests/equivalence.py').write_text('unrelated asset variation')
        repeated = self.begin()
        self.assertTrue(repeated['reused'])
        self.assertEqual(repeated['campaign'], campaign['campaign'])
        self.assertEqual(set(readiness.load(self.out)['source_files']), {'rtl/block.sv', 'rtl/defs.svh'})

    def test_default_disabled_and_missing_optional_skill_do_not_create_campaign(self):
        manifest = json.loads((self.out / 'manifest.json').read_text())
        manifest['settings'].pop('readiness')
        (self.out / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'disabled'):
            self.begin()
        self.assertFalse((self.out / 'readiness').exists())
        # Module-level unavailable-skill branch is an optional integration skip.
        manifest['settings']['readiness'] = {'enabled': True}
        (self.out / 'manifest.json').write_text(json.dumps(manifest))
        script = self.out / 'snapshot/harness/skills/rtl-implementation-readiness/scripts/readiness.py'
        script.unlink()
        self.assertEqual(readiness.begin(self.out, {}, lambda _: None)['state'], 'skill_unavailable')

    def test_budget_reservations_persist_across_status_and_count_both_roles(self):
        campaign = self.begin()
        one = self.call(action='submit', scope=campaign['work_scope'], argv=['python', '-c', 'pass'], timeout_seconds=6)
        status = self.call(action='rtl_readiness_status', campaign=campaign['campaign'])
        self.assertEqual(status['remaining_budgets']['tool_seconds'], 4)
        self.assertEqual(status['remaining_budgets']['max_jobs'], 1)
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            self.call(action='submit', scope=campaign['audit_scope'], argv=['python', '-c', 'pass'], timeout_seconds=6)
        (self.out / 'jobs' / one['job_id'] / 'result.json').write_text(json.dumps({'elapsed_seconds': 2, 'exit_code': 0}))
        two = self.call(action='submit', scope=campaign['audit_scope'], argv=['python', '-c', 'pass'], timeout_seconds=6)
        request = json.loads((self.out / 'jobs' / two['job_id'] / 'request.json').read_text())
        self.assertEqual(request['readiness_budget']['source_sha256'], campaign['source_sha256'])
        self.assertEqual(request['source_sha256'], readiness.load(self.out)['source_files'])
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            self.call(action='submit', scope=campaign['work_scope'], argv=['python', '-c', 'pass'], timeout_seconds=1)

    def test_per_job_limit_and_variant_limit_prevent_native_queue(self):
        campaign = self.begin()
        with self.assertRaisesRegex(ValueError, 'per-job'):
            self.call(action='submit', scope=campaign['work_scope'], argv=['python', '-c', 'pass'], timeout_seconds=7)
        workspace = self.out / campaign['work_scope'] / 'probe'
        for name in ('baseline', 'candidate-1', 'candidate-2'):
            (workspace / 'variants' / name).mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'variant budget'):
            self.call(action='submit', scope=campaign['work_scope'], argv=['python', '@skill/rtl-implementation-readiness/scripts/readiness.py',
                'variant', str(workspace), 'candidate-3'], timeout_seconds=1)
        self.assertEqual(list((self.out / 'jobs').glob('*')), [])

    def test_missing_report_is_terminal_preserving_baseline(self):
        campaign = self.begin()
        before = (self.rtl / 'block.sv').read_bytes()
        result = self.complete(campaign)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertEqual(before, (self.rtl / 'block.sv').read_bytes())
        self.assertTrue(self.begin()['reused'])

    def test_relative_variant_workspace_uses_the_recorded_job_cwd(self):
        campaign = self.begin()
        workspace = self.out / campaign['work_scope'] / 'nested/probe'
        (workspace / 'variants/baseline').mkdir(parents=True)
        queued = self.call(action='submit', scope=campaign['work_scope'], cwd='nested',
            argv=['python', '@skill/rtl-implementation-readiness/scripts/readiness.py', 'variant', 'probe', 'candidate-1'],
            timeout_seconds=1)
        self.assertEqual(queued['status'], 'queued')

    def test_changed_rtl_only_adopts_after_fresh_core_verify_and_real_actor_receipts(self):
        campaign, verified, sessions = self.candidate()
        asset = (self.out / 'work/tests/equivalence.py').read_bytes()
        header = (self.rtl / 'defs.svh').read_bytes()
        with mock.patch.object(readiness, 'verify_core', return_value=verified) as verify:
            result = self.complete(campaign, **sessions)
        verify.assert_called_once()
        self.assertEqual(result['state'], 'adopted')
        self.assertIn('wire x = 1', (self.rtl / 'block.sv').read_text())
        self.assertEqual(asset, (self.out / 'work/tests/equivalence.py').read_bytes())
        self.assertEqual(header, (self.rtl / 'defs.svh').read_bytes())
        receipt = json.loads((self.out / result['adoption_path']).read_text())
        self.assertEqual(set(receipt['changed_files']), {'rtl/block.sv'})
        self.assertTrue(self.begin()['reused'])

    def test_probe_only_rtl_and_asset_allowlist_cannot_be_adopted(self):
        for bad_name in ('rtl/probe_only.sv', 'tests/equivalence.py'):
            with self.subTest(path=bad_name):
                # Each independent fixture must have a fresh active campaign.
                self.tearDown(); self.setUp()
                campaign, verified, sessions = self.candidate()
                before = (self.rtl / 'block.sv').read_bytes()
                verified['allowed_paths'].append(bad_name)
                with mock.patch.object(readiness, 'verify_core', return_value=verified):
                    result = self.complete(campaign, **sessions)
                self.assertEqual(result['state'], 'evidence_incomplete')
                self.assertEqual(before, (self.rtl / 'block.sv').read_bytes())

    def test_stale_review_source_id_or_nonaccepted_review_preserve_baseline(self):
        campaign, verified, sessions = self.candidate()
        before = (self.rtl / 'block.sv').read_bytes()
        verified['baseline_identity'] = 'different-source'
        with mock.patch.object(readiness, 'verify_core', return_value=verified):
            self.assertEqual(self.complete(campaign, **sessions)['state'], 'evidence_incomplete')
        self.assertEqual(before, (self.rtl / 'block.sv').read_bytes())

    def test_nonaccepted_engineering_review_and_failed_verifier_preserve_baseline(self):
        campaign, verified, sessions = self.candidate(accepted=False)
        before = (self.rtl / 'block.sv').read_bytes()
        with mock.patch.object(readiness, 'verify_core', return_value=verified):
            result = self.complete(campaign, **sessions)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertEqual(before, (self.rtl / 'block.sv').read_bytes())

    def test_forged_or_incomplete_actor_cannot_authorize_adoption(self):
        campaign, verified, sessions = self.candidate()
        sessions['implementation_session_id'] = 'nonexistent-actor'
        with mock.patch.object(readiness, 'verify_core', return_value=verified) as verify:
            self.assertEqual(self.complete(campaign, **sessions)['state'], 'evidence_incomplete')
        verify.assert_not_called()

    def test_incomplete_or_expanded_tools_actor_is_recorded_but_cannot_adopt(self):
        campaign = self.begin()
        session = self.session(campaign, completed=False, tools=['rtl_remote', 'rtl_revise_architecture'])
        with self.assertRaisesRegex(ValueError, 'incomplete or not isolated'):
            readiness._require_actor(self.out, session, 'rtl_readiness', campaign['work_scope'])

    def test_scope_role_paths_and_native_action_boundary(self):
        campaign = self.begin()
        for action in ('evaluate', 'proposal', 'revision_begin'):
            with self.assertRaisesRegex(ValueError, 'unsupported readiness'):
                self.call(action=action, scope=campaign['audit_scope'])
        for name in ('work/rtl/block.sv', campaign['work_scope'] + '/result.json', '../work/result.json'):
            with self.assertRaises(ValueError):
                self.call(action='write', scope=campaign['audit_scope'], path=name, content='bad', base_sha256=None)
        self.call(action='put_evidence', path='control/wrong-role.prompt.txt', content='fixture')
        with self.assertRaisesRegex(ValueError, 'role differs'):
            self.call(action='rtl_session_begin', session_id='wrong-role', role='readiness_review',
                      scope=campaign['work_scope'], model_role='review')

    def test_baseline_drift_terminates_without_applying_variant(self):
        campaign, verified, sessions = self.candidate()
        (self.rtl / 'defs.svh').write_text('external baseline mutation')
        with mock.patch.object(readiness, 'verify_core', return_value=verified) as verify:
            result = self.complete(campaign, **sessions)
        self.assertEqual(result['state'], 'evidence_incomplete')
        verify.assert_not_called()
        self.assertIn('wire x = 0', (self.rtl / 'block.sv').read_text())

    def test_terminal_campaign_is_read_only(self):
        campaign = self.begin()
        self.complete(campaign)
        with self.assertRaisesRegex(ValueError, 'terminal readiness'):
            self.call(action='write', scope=campaign['work_scope'], path='x.json', content='{}', base_sha256=None)


if __name__ == '__main__':
    unittest.main()
