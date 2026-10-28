"""Review-scope integration keeps full native candidate acceptance mandatory.

All jobs/receipts are explicit offline metadata fixtures; no EDA/host runs.
"""
import copy
import json
import unittest

from harness.adapters import rtl_audit
from harness.runtime import rtl_worker as worker
from harness.tests import test_rtl_structure_gate as fixtures


class ReviewScopeNativeGateTests(unittest.TestCase):
    setUp = fixtures.StructureGateTests.setUp
    call = fixtures.StructureGateTests.call
    references = fixtures.StructureGateTests.references
    enqueue = fixtures.StructureGateTests.enqueue
    receipts = fixtures.StructureGateTests.receipts
    complete = fixtures.StructureGateTests.complete

    def choose_scope(self, scope):
        self.review = self.call(action='rtl_review_begin', review_scope=scope)
        self.metadata = json.loads((self.out / self.review['inventory_path']).read_text())
        self.audit = self.out / self.review['audit_scope']

    def ordinary_evidence(self):
        queued = self.call(action='submit', scope=self.review['audit_scope'],
                           argv=['python', '-c', 'pass'], timeout_seconds=5)
        self.receipts(queued['job_id'])
        return queued['job_id']

    def report(self, jobs, **extra):
        value = {'verdict': 'pass', 'candidate_sha256': self.review['candidate_sha256'],
                 'review_scope': self.review['review_scope'], 'reason': 'Offline scope/gate contract.',
                 'evidence': jobs}
        value.update(extra)
        worker.save(self.audit / 'scope-report.json', value)
        return value

    def decide(self):
        return self.call(action='rtl_review_decision', review=self.review['review'],
                         report_path='scope-report.json', expected_verdict='final')

    def native_pass(self):
        queued = self.enqueue();self.complete(queued)
        value = fixtures.StructureGateTests.report(self, [queued])
        value['review_scope'] = self.review['review_scope']
        worker.save(self.audit / 'scope-report.json', value)
        return self.decide(), queued

    def test_candidate_pass_cannot_use_plain_success_or_missing_native_contract(self):
        self.assertEqual(self.review['review_scope'], 'candidate')
        job = self.ordinary_evidence();self.report([job])
        with self.assertRaisesRegex(ValueError, 'source-bound native'):
            self.decide()
        self.assertIsNone(self.call(action='rtl_review_status')['decision'])

    def test_candidate_native_pass_still_revalidates_gate_and_raw_identity(self):
        accepted, queued = self.native_pass()
        self.assertIn('structure_checks', accepted['decision'])
        self.assertTrue(rtl_audit._decision_result(accepted)['rtl_stage_release'])
        raw = self.out / 'jobs' / queued['job_id'] / 'stdout.log'
        raw.write_text(raw.read_text() + '\n')
        with self.assertRaises(ValueError):
            self.call(action='rtl_review_status')

    def test_change_pass_keeps_claimed_structure_unvalidated_and_cannot_release(self):
        self.choose_scope('change');job = self.ordinary_evidence()
        original = self.report([job], structure_checks={'version': 1, 'unverified_claim': 'native passed'})
        accepted = self.decide()
        self.assertEqual(accepted['decision']['review_scope'], 'change')
        self.assertNotIn('structure_checks', accepted['decision'])
        self.assertFalse(rtl_audit._decision_result(accepted)['rtl_stage_release'])
        self.assertEqual(json.loads((self.audit / 'scope-report.json').read_text()), original)

    def test_change_report_cannot_be_upgraded_in_place_or_reused_by_candidate_review(self):
        self.choose_scope('change');job = self.ordinary_evidence()
        self.report([job], review_scope='candidate')
        with self.assertRaisesRegex(ValueError, 'report review_scope'):
            self.decide()
        self.report([job]);accepted = self.decide();original_report = (self.audit / 'scope-report.json').read_bytes()
        original_candidate = accepted['candidate_sha256']
        self.choose_scope('candidate')
        self.assertEqual(self.review['candidate_sha256'], original_candidate)
        worker.save(self.audit / 'scope-report.json', json.loads(original_report))
        with self.assertRaisesRegex(ValueError, 'report review_scope'):
            self.decide()
        self.report([job])
        with self.assertRaisesRegex(ValueError, 'not bound'):
            self.decide()

    def test_scope_relabel_or_removed_verified_gate_cannot_recover_as_candidate_pass(self):
        self.choose_scope('change');job = self.ordinary_evidence();self.report([job]);self.decide()
        path = self.out / self.review['inventory_path']
        original = json.loads(path.read_text());relabeled = copy.deepcopy(original)
        relabeled['review_scope'] = relabeled['decision']['review_scope'] = 'candidate'
        worker.save(path, relabeled)
        with self.assertRaisesRegex(ValueError, 'report review_scope'):
            self.call(action='rtl_review_status')
        worker.save(path, original)
        self.choose_scope('candidate');accepted, _ = self.native_pass()
        path = self.out / self.review['inventory_path'];record = json.loads(path.read_text())
        record['decision'].pop('structure_checks');worker.save(path, record)
        with self.assertRaisesRegex(ValueError, 'decided RTL structure evidence changed'):
            self.call(action='rtl_review_status')

    def test_change_repair_seed_inherits_scope_and_cannot_upgrade_to_candidate(self):
        self.choose_scope('change');self.report([], verdict='revise')
        self.call(action='rtl_review_decision', review=self.review['review'],
                  report_path='scope-report.json', expected_verdict='revise')
        self.call(action='rtl_repair_begin', review=self.review['review'])
        self.call(action='write', path='design.sv', base_sha256=worker.sha(self.work / 'design.sv'),
                  content='module design(input wire a, output wire y); assign y=~a; endmodule\n')
        self.call(action='rtl_repair_end', review=self.review['review'])
        with self.assertRaisesRegex(ValueError, 'scope cannot change'):
            self.call(action='rtl_review_begin', seed_review=self.review['review'], review_scope='candidate')
        after = self.call(action='rtl_review_begin', seed_review=self.review['review'])
        self.assertEqual(after['review_scope'], 'change')
        self.assertNotEqual(after['candidate_sha256'], self.review['candidate_sha256'])


if __name__ == '__main__':
    unittest.main()
