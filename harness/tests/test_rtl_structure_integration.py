"""Actual isolated host jobs/Yosys, run only in the server fixture environment.

No OMP model is invoked and no project RTL is used. These cases qualify the
native structure evidence path, not autonomous RTL/functional/PPA acceptance.
"""
import json
from pathlib import Path
import unittest

from harness.runtime import rtl_worker as worker
from harness.tests import test_rtl_remote as fixture


class StructureIntegrationTests(unittest.TestCase):
    setUp = fixture.WorkerTests.setUp
    tearDown = fixture.WorkerTests.tearDown
    call = fixture.WorkerTests.call
    wait = fixture.WorkerTests.wait

    def begin(self, name):
        source = Path(__file__).parent / 'fixtures/rtl_structure' / name
        self.call(action='write', path='design.sv', base_sha256=None, content=source.read_text())
        self.call(action='write', path='RTL_STRUCTURE.json', base_sha256=None, content=json.dumps({'version': 1, 'configurations': [{'id': 'delivery', 'top': 'top', 'sources': ['design.sv']}], 'blackboxes': []}))
        return self.call(action='rtl_review_begin')

    def report(self, review, verdict, evidence, structure=None):
        data = {'verdict': verdict, 'review_scope': review['review_scope'], 'candidate_sha256': review['candidate_sha256'],
                'reason': 'Actual isolated native structure mechanism component; no project acceptance claim.',
                'evidence': evidence}
        if structure is not None:
            data['structure_checks'] = structure
        self.call(action='write', scope=review['audit_scope'], path='review.json',
                  base_sha256=None, content=json.dumps(data))
        return data

    def decide(self, review, expected='final'):
        return self.call(action='rtl_review_decision', review=review['review'],
                         report_path='review.json', expected_verdict=expected)

    def test_real_clean_native_check_allows_bound_pass(self):
        review = self.begin('clean.sv')
        structure, job = fixture.structure_report(self, review)
        self.report(review, 'pass', [job], structure)
        result = self.decide(review)
        self.assertEqual(result['decision']['verdict'], 'pass')
        proof = result['decision']['structure_checks']
        self.assertEqual(proof['scope'], 'pre-mapping_structure_only')
        self.assertEqual(proof['checks'][0]['job_id'], job)
        raw = json.loads((self.out / 'jobs' / job / 'stdout.log').read_text())
        self.assertTrue(raw['source_stable']); self.assertTrue(raw['tool']['binary_stable'])
        self.assertEqual(raw['status'], 'passed')

    def test_real_partial_used_vector_cannot_pass_and_can_request_repair(self):
        review = self.begin('partial_used.sv')
        check = self.call(action='structure_check', scope=review['audit_scope'], review=review['review'],
                          configuration={'top': 'top', 'sources': ['design.sv']}, timeout_seconds=30)
        failed = self.wait(check['job_id'])
        self.assertNotEqual(failed['exit_code'], 0)
        raw = json.loads((self.out / 'jobs' / check['job_id'] / 'stdout.log').read_text())
        self.assertEqual(raw['status'], 'findings'); self.assertTrue(raw['diagnostics']['undriven'])
        probe = self.call(action='submit', scope=review['audit_scope'], argv=['python', '-c',
                         'print("Independent component mechanism evidence only")'], timeout_seconds=10)
        self.assertEqual(self.wait(probe['job_id'])['exit_code'], 0)
        references = [
            {'path': '@input/spec.md', 'sha256': worker.sha(self.out / 'snapshot/input/spec.md')},
            {'path': '@candidate/RTL_STRUCTURE.json', 'sha256': worker.sha(self.out / review['candidate_scope'] / 'RTL_STRUCTURE.json')},
            {'path': '@run/' + review['architecture_scope'] + '/architecture.md',
             'sha256': worker.sha(self.out / review['architecture_scope'] / 'architecture.md')}]
        structure = {'version': 1, 'required_configurations': [
            {'job_id': check['job_id'], 'configuration_sha256': check['configuration_sha256']}],
            'coverage': {'claim': 'full_design', 'reason': 'This synthetic fixture has one explicit top and configuration.',
                         'source_refs': references}, 'waivers': []}
        evidence = [check['job_id'], probe['job_id']]
        data = self.report(review, 'pass', evidence, structure)
        with self.assertRaisesRegex(ValueError, 'unresolved used-undriven'):
            self.decide(review)
        data['verdict'] = 'revise'
        path = self.out / review['audit_scope'] / 'review.json'
        self.call(action='write', scope=review['audit_scope'], path='review.json',
                  base_sha256=worker.sha(path), content=json.dumps(data))
        self.assertEqual(self.decide(review, 'revise')['decision']['verdict'], 'revise')

    def test_real_ordinary_successful_submit_cannot_impersonate_structure_check(self):
        review = self.begin('clean.sv')
        job = self.call(action='submit', scope=review['audit_scope'], argv=['python', '-c',
                        'print("structure passed")'], timeout_seconds=10)['job_id']
        self.assertEqual(self.wait(job)['exit_code'], 0)
        refs = [
            {'path': '@input/spec.md', 'sha256': worker.sha(self.out / 'snapshot/input/spec.md')},
            {'path': '@candidate/RTL_STRUCTURE.json', 'sha256': worker.sha(self.out / review['candidate_scope'] / 'RTL_STRUCTURE.json')},
            {'path': '@run/' + review['architecture_scope'] + '/architecture.md',
             'sha256': worker.sha(self.out / review['architecture_scope'] / 'architecture.md')}]
        structure = {'version': 1, 'required_configurations': [{'job_id': job, 'configuration_sha256': '0' * 64}],
                     'coverage': {'claim': 'full_design', 'reason': 'Untrusted claim cannot substitute for native execution.',
                                  'source_refs': refs}, 'waivers': []}
        self.report(review, 'pass', [job], structure)
        with self.assertRaisesRegex(ValueError, 'ordinary submit job'):
            self.decide(review)
        self.assertIsNone(self.call(action='rtl_review_status')['decision'])


if __name__ == '__main__':
    unittest.main()
