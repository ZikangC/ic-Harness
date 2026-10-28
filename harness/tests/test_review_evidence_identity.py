"""Compact resume-change observations never authorize RTL acceptance."""
import unittest

from harness.runtime import rtl_worker as worker
from harness.tests import test_rtl_structure_gate as fixtures


class ReviewEvidenceIdentityTests(unittest.TestCase):
    setUp = fixtures.StructureGateTests.setUp
    call = fixtures.StructureGateTests.call

    def status(self):
        return self.call(action='rtl_review_status', review=self.review['review'])

    def add_metadata(self, name='1', result=False):
        job = self.out / 'jobs' / name
        worker.save(job / 'request.json', {'scope': 'work', 'candidate_sha256': 'another-candidate'})
        if result:
            worker.save(job / 'result.json', {'exit_code': 0, 'candidate_sha256': 'another-candidate'})
        return job

    def test_empty_and_unchanged_metadata_have_stable_compact_identity(self):
        first = self.status()
        self.assertEqual(first['evidence_sha256'], worker.digest({}))
        self.assertEqual(self.status()['evidence_sha256'], first['evidence_sha256'])
        self.assertNotIn('evidence_inventory', first)

    def test_request_result_addition_and_exact_bytes_change_identity(self):
        first = self.status()['evidence_sha256']
        job = self.add_metadata()
        request_only = self.status()['evidence_sha256']
        self.assertNotEqual(request_only, first)
        worker.save(job / 'result.json', {'exit_code': 1})
        result_added = self.status()['evidence_sha256']
        self.assertNotEqual(result_added, request_only)
        (job / 'result.json').write_text('{"exit_code":1}\n')
        self.assertNotEqual(self.status()['evidence_sha256'], result_added)

    def test_logs_do_not_change_metadata_identity(self):
        job = self.add_metadata(result=True)
        first = self.status()['evidence_sha256']
        (job / 'stdout.log').write_text('new large result not read by status observation')
        (job / 'structure-check.json').write_text('{}')
        self.assertEqual(self.status()['evidence_sha256'], first)

    def test_observation_does_not_override_blocked_or_candidate_binding(self):
        report = self.audit / 'review.json'
        worker.save(report, {'verdict': 'blocked', 'review_scope': self.review['review_scope'], 'candidate_sha256': self.review['candidate_sha256'],
                             'reason': 'Real independent evidence unavailable.', 'evidence': []})
        decided = self.call(action='rtl_review_decision', review=self.review['review'],
                            report_path='review.json', expected_verdict='blocked')
        self.add_metadata(result=True)
        observed = self.status()
        self.assertNotEqual(observed['evidence_sha256'], decided['evidence_sha256'])
        self.assertEqual(observed['decision'], decided['decision'])
        self.assertEqual(observed['candidate_sha256'], decided['candidate_sha256'])
        self.assertTrue(observed['work_current'])
        self.assertTrue(observed['architecture_current'])

    def test_foreign_success_metadata_does_not_supply_release_evidence(self):
        self.add_metadata(result=True)
        self.assertIsNone(self.status()['decision'])
        worker.save(self.audit / 'review.json', {'verdict': 'pass',
                    'review_scope': self.review['review_scope'], 'candidate_sha256': self.review['candidate_sha256'], 'reason': 'Cannot reuse foreign job.',
                    'evidence': ['1']})
        with self.assertRaisesRegex(ValueError, 'not bound'):
            self.call(action='rtl_review_decision', review=self.review['review'],
                      report_path='review.json', expected_verdict='pass')


if __name__ == '__main__':
    unittest.main()
