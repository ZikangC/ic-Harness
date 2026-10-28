"""Real worker queue metadata enforces readiness repair budgets; nothing launches."""
import json
import unittest

from harness.runtime import rtl_worker as worker
from harness.tests import test_rtl_remote as fixtures


class ReadinessRepairBudgetTests(unittest.TestCase):
    tearDown = fixtures.WorkerTests.tearDown

    def setUp(self):
        fixtures.WorkerTests.setUp(self)
        (self.out / 'work/rtl').mkdir()
        (self.out / 'work/rtl/top.sv').write_text('module top; endmodule\n')
        self.freeze_limits(max_jobs=2, max_job_seconds=6, tool_seconds=10)

    def call(self, **request):
        # Deliberately bypass the fixture's host launcher. submit persists only
        # a queued request, and owned_jobs stays empty throughout these tests.
        return worker.dispatch(self.root, self.runid, request)

    def freeze_limits(self, **limits):
        manifest = json.loads((self.out / 'manifest.json').read_text())
        manifest['settings']['readiness'] = {'enabled': True, 'rtl_review': True, **limits}
        worker.save(self.out / 'manifest.json', manifest)
        self.req['settings'] = manifest['settings']

    def active_repair(self, *, actor='rtl-implementation-readiness'):
        review = self.call(action='rtl_review_begin')
        scope = review['audit_scope']
        report = self.out / scope / 'review.json'
        report.write_text(json.dumps({'verdict': 'revise', 'review_scope': review['review_scope'], 'candidate_sha256': review['candidate_sha256'],
                                      'reason': 'Synthetic queue budget fixture, no hardware claim.', 'evidence': []}))
        review = self.call(action='rtl_review_decision', review=review['review'],
                           report_path='review.json', expected_verdict='revise')
        self.call(action='rtl_repair_begin', review=review['review'])
        worker.save(self.out / 'control/rtl-audit' / (review['review'] + '.repair.json'), {
            'repair_actor': actor, 'candidate_sha256': review['candidate_sha256'],
            'decision_report_sha256': review['decision']['report_sha256'],
            'completed': False, 'tool_isolation_ok': False})
        return review

    def submit(self, **options):
        answer = self.call(action='submit', scope='work', argv=['python', '-c', 'pass'], **options)
        self.assertEqual(answer['status'], 'queued')
        path = self.out / 'jobs' / answer['job_id']
        self.assertEqual({p.name for p in path.iterdir()}, {'request.json'})
        self.assertEqual(self.owned_jobs, [])
        return answer['job_id'], json.loads((path / 'request.json').read_text())

    def queued(self):
        return sorted((self.out / 'jobs').glob('*/request.json'))

    def test_work_scope_cannot_use_old_six_hundred_second_default_explicitly(self):
        self.active_repair()
        with self.assertRaisesRegex(ValueError, 'timeout|per-job'):
            self.submit(timeout_seconds=600)
        self.assertEqual(self.queued(), [])

    def test_default_timeout_uses_frozen_repair_ceiling_and_records_binding(self):
        review = self.active_repair()
        job, request = self.submit()
        self.assertEqual(request['timeout_seconds'], 6)
        self.assertEqual(request['scope'], 'work')
        self.assertEqual(request['repair_review'], review['review'])
        self.assertIn('repair_budget', request)
        budget = request['repair_budget']
        self.assertIn('rtl-implementation-readiness', json.dumps(budget))
        self.assertIn(review['candidate_sha256'], json.dumps(budget))
        self.assertIn(review['decision']['report_sha256'], json.dumps(budget))

    def test_invalid_or_excessive_timeouts_create_no_queue_records(self):
        self.active_repair()
        for timeout in (True, False, '1', 1.5, None, 0, -1, 7):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.submit(timeout_seconds=timeout)
            self.assertEqual(self.queued(), [])

    def test_frozen_defaults_apply_when_optional_limit_fields_are_absent(self):
        from harness.runtime.rtl_readiness import DEFAULTS
        self.freeze_limits()
        self.active_repair()
        job, request = self.submit()
        self.assertEqual(request['timeout_seconds'], DEFAULTS['max_job_seconds'])
        self.assertIn('repair_budget', request)

    def test_job_count_is_shared_across_all_work_commands_in_same_repair(self):
        self.freeze_limits(max_jobs=2, max_job_seconds=6, tool_seconds=100)
        self.active_repair()
        self.submit(timeout_seconds=1)
        self.submit(timeout_seconds=1)
        with self.assertRaisesRegex(ValueError, 'budget exhausted|job.*budget'):
            self.submit(timeout_seconds=1)
        self.assertEqual(len(self.queued()), 2)

    def test_total_reserved_seconds_prevent_another_job_before_launch(self):
        self.freeze_limits(max_jobs=10, max_job_seconds=6, tool_seconds=10)
        self.active_repair()
        self.submit(timeout_seconds=6)
        self.submit(timeout_seconds=4)
        with self.assertRaisesRegex(ValueError, 'budget exhausted|tool.*budget'):
            self.submit(timeout_seconds=1)
        self.assertEqual(len(self.queued()), 2)

    def test_terminal_metadata_does_not_refund_reserved_repair_budget(self):
        self.freeze_limits(max_jobs=10, max_job_seconds=6, tool_seconds=10)
        self.active_repair()
        job, request = self.submit(timeout_seconds=6)
        # Synthetic terminal metadata tests accounting only; no process ran.
        worker.save(self.out / 'jobs' / job / 'result.json', {'elapsed_seconds': 0.1, 'exit_code': 0})
        with self.assertRaisesRegex(ValueError, 'budget exhausted|tool.*budget'):
            self.submit(timeout_seconds=6)
        self.submit(timeout_seconds=4)
        self.assertEqual(len(self.queued()), 2)

    def test_resume_uses_persistent_queue_records_without_resetting_limits(self):
        self.freeze_limits(max_jobs=1, max_job_seconds=6, tool_seconds=10)
        review = self.active_repair()
        job, request = self.submit(timeout_seconds=1)
        worker.save(self.out / 'jobs' / job / 'result.json', {'elapsed_seconds': 0, 'exit_code': 0})
        self.call(**dict(self.req, action='init', resume=True))
        self.call(action='rtl_repair_begin', review=review['review'])
        with self.assertRaisesRegex(ValueError, 'budget exhausted|job.*budget'):
            self.submit(timeout_seconds=1)
        self.assertEqual(len(self.queued()), 1)

    def test_stale_controller_candidate_or_report_cannot_bypass_budget(self):
        review = self.active_repair()
        path = self.out / 'control/rtl-audit' / (review['review'] + '.repair.json')
        original = json.loads(path.read_text())
        for field in ('candidate_sha256', 'decision_report_sha256'):
            changed = dict(original, **{field: 'unrelated-source-or-report'})
            worker.save(path, changed)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'stale controller'):
                self.submit(timeout_seconds=1)
            self.assertEqual(self.queued(), [])
        worker.save(path, original)
        self.submit(timeout_seconds=1)

    def test_request_actor_fields_cannot_opt_out_of_controller_selected_policy(self):
        self.active_repair()
        with self.assertRaisesRegex(ValueError, 'timeout|per-job'):
            self.submit(timeout_seconds=600, repair_actor='rtl-implementation',
                        repair_skill='rtl-implementation', readiness_budget={})
        self.assertEqual(self.queued(), [])

    def test_existing_queued_budget_binding_must_remain_source_bound(self):
        self.active_repair()
        job, request = self.submit(timeout_seconds=1)
        request['repair_budget']['candidate_sha256'] = 'another-candidate'
        worker.save(self.out / 'jobs' / job / 'request.json', request)
        with self.assertRaisesRegex(ValueError, 'stale budget binding'):
            self.submit(timeout_seconds=1)
        self.assertEqual(len(self.queued()), 1)

    def test_plain_implementation_work_keeps_existing_timeout_behavior(self):
        job, request = self.submit()
        self.assertEqual(request['timeout_seconds'], 600)
        self.assertNotIn('repair_budget', request)

    def test_implementation_owned_repair_is_not_given_readiness_limits(self):
        self.active_repair(actor='rtl-implementation')
        job, request = self.submit(timeout_seconds=600)
        self.assertEqual(request['timeout_seconds'], 600)
        self.assertNotIn('repair_budget', request)


if __name__ == '__main__':
    unittest.main()
