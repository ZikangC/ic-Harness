"""Native calculator routing and immutable proposal checks; no model or EDA jobs."""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from harness.runtime import rtl_bridge, rtl_worker as worker
from harness.runtime import design_session
from harness.tests import test_rtl_architecture_handoff as handoff_fixture


class NativeEvaluationTests(unittest.TestCase):
    setUp = handoff_fixture.HandoffTests.setUp
    tearDown = handoff_fixture.HandoffTests.tearDown
    initialize = handoff_fixture.HandoffTests.initialize
    dump = handoff_fixture.HandoffTests.dump
    call = handoff_fixture.HandoffTests.call
    modify = handoff_fixture.HandoffTests.modify

    def candidate(self):
        revision = self.call(action='revision_begin', reason='synthetic recalculation')
        candidate = self.out / revision['candidate_scope']
        self.modify(candidate)
        self.call(action='evaluate', scope=revision['candidate_scope'])
        return revision, candidate

    def test_review_recalculation_preserves_complete_proposal_and_prior_results(self):
        revision, candidate = self.candidate()
        before = worker.closure_inventory(candidate)
        identity = self.call(action='proposal', scope=revision['audit_scope'])
        results = [self.call(action='evaluate', scope=revision['audit_scope']) for _ in range(2)]
        self.assertEqual(worker.closure_inventory(candidate), before)
        self.assertNotEqual(results[0]['full_report'], results[1]['full_report'])
        for result in results:
            self.assertTrue(result['ok'])
            self.assertEqual(result['mode'], 'independent_verification')
            self.assertEqual(result['candidate_sha256'], identity['candidate_sha256'])
            self.assertEqual(result['candidate_scope'], revision['candidate_scope'])
            for field in ('full_report', 'execution_path', 'binding_path'):
                self.assertTrue(result[field].startswith(revision['audit_scope'] + '/calculations/'))
                self.assertTrue((self.out / result[field]).is_file())
            self.assertEqual(worker.sha(self.out / result['full_report']), result['report_sha256'])
            binding = json.loads((self.out / result['binding_path']).read_text())
            self.assertEqual(binding, {
                'candidate_sha256': identity['candidate_sha256'],
                'design_file_sha256': worker.sha(candidate / 'design.json'),
                'report_sha256': result['report_sha256'],
            })
        self.assertFalse((self.out / revision['audit_scope'] / 'review.json').exists())
        with self.assertRaisesRegex(ValueError, 'installed audit skill requires an actual review'):
            self.call(action='revision_adopt', revision=revision['revision'])

    def test_changed_sidecar_still_rejects_a_previously_bound_report(self):
        revision, candidate = self.candidate()
        result = self.call(action='evaluate', scope=revision['audit_scope'])
        self.dump(self.out / revision['audit_scope'] / 'review.json', {
            'verdict': 'accept', 'design_sha256': worker.sha(candidate / 'design.json'),
            'candidate_sha256': result['candidate_sha256'], 'evidence': [],
        })
        current = (self.out / 'architecture-current.json').read_bytes()
        (candidate / 'support/schedule.txt').write_text('external post-review sidecar change')
        with self.assertRaisesRegex(ValueError, 'review is stale for candidate files'):
            self.call(action='revision_adopt', revision=revision['revision'])
        self.assertEqual((self.out / 'architecture-current.json').read_bytes(), current)

    def test_published_audit_recalculation_is_still_read_only(self):
        revision, _ = self.candidate()
        self.dump(self.out / revision['revision'] / 'handoff.json', {})
        before = worker.closure_inventory(self.out / revision['audit_scope'])
        with self.assertRaisesRegex(ValueError, 'published architecture revision is read-only'):
            self.call(action='evaluate', scope=revision['audit_scope'])
        self.assertEqual(worker.closure_inventory(self.out / revision['audit_scope']), before)

    def test_candidate_change_during_independent_calculation_is_rejected(self):
        revision, candidate = self.candidate()
        execute = design_session.execute_tool

        def changed(*args, **kwargs):
            result = execute(*args, **kwargs)
            (candidate / 'support/schedule.txt').write_text('external change during calculation')
            return result

        with patch.object(design_session, 'execute_tool', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'architecture proposal changed during independent calculation'):
                self.call(action='evaluate', scope=revision['audit_scope'])

    def test_calculation_parent_symlink_rejected_before_any_write_or_execution(self):
        revision, candidate = self.candidate()
        audit = self.out / revision['audit_scope']
        retained = audit / 'retained.txt'
        retained.write_bytes(b'prior independent evidence\n')
        alias = audit / 'calculations'
        alias.symlink_to(candidate, target_is_directory=True)
        candidate_before = worker.closure_inventory(candidate)
        audit_before = {p.name: (p.lstat().st_mode,
            os.readlink(p) if p.is_symlink() else p.read_bytes()) for p in audit.iterdir()}
        with patch.object(design_session, 'execute_tool', side_effect=AssertionError('calculator must not run')) as execute:
            with self.assertRaisesRegex(ValueError, 'independent calculation directory must not be a symlink'):
                self.call(action='evaluate', scope=revision['audit_scope'])
            execute.assert_not_called()
        self.assertEqual(worker.closure_inventory(candidate), candidate_before)
        self.assertEqual({p.name: (p.lstat().st_mode,
            os.readlink(p) if p.is_symlink() else p.read_bytes()) for p in audit.iterdir()}, audit_before)

    def test_bridge_review_evaluate_keeps_audit_scope(self):
        revision, _ = self.candidate()
        context = self.root / 'review-context.json'
        context.write_text(json.dumps({'environment': {}, 'run_id': self.runid,
            'worker': 'worker.py', 'role': 'review', 'scope': revision['audit_scope'],
            'candidate_scope': revision['candidate_scope']}))
        calls = []

        class Remote:
            def __init__(self, *args):
                pass

            def call(self, request):
                calls.append(dict(request))
                return {'ok': True}

        with patch.dict(rtl_bridge.os.environ, {'HARNESS_RTL_CONTEXT': str(context)}), \
                patch.object(rtl_bridge, 'Remote', Remote), \
                patch.object(rtl_bridge.sys, 'stdin', io.StringIO('{"action":"evaluate"}')), \
                redirect_stdout(io.StringIO()):
            rtl_bridge.main()
        self.assertEqual(calls, [{'action': 'evaluate', 'scope': revision['audit_scope']}])


if __name__ == '__main__':
    unittest.main()
