"""Duplicate publications coalesce only after fresh ordinary evidence validation."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from harness.runtime import rtl_readiness as readiness
from harness.tests import test_readiness_finalization as fixtures
from harness.tests import test_readiness_batch as batches


class EvidenceGroupingTests(unittest.TestCase):
    setUp = fixtures.FinalizationTests.setUp
    tearDown = fixtures.FinalizationTests.tearDown
    call = fixtures.FinalizationTests.call
    begin = fixtures.FinalizationTests.begin
    candidate = fixtures.FinalizationTests.candidate
    session = fixtures.FinalizationTests.session
    complete = fixtures.FinalizationTests.complete
    resolve = fixtures.FinalizationTests.resolve
    finalization = fixtures.FinalizationTests.finalization
    batch = batches.BatchTests.batch

    @staticmethod
    def verifier(*receipts):
        by_delivery = {item['delivery']: item for item in receipts}
        return lambda out, workspace, delivery: by_delivery[str(delivery)]

    def test_different_publication_time_and_destination_coalesce_with_exact_selected_job(self):
        campaign, verified, _ = self.candidate()
        first, one = self.finalization(campaign, verified)
        second, two = self.finalization(campaign, verified, delivery='probe/again',
                                       created_utc='2026-10-22T01:00:00+00:00')
        self.assertNotEqual(one['comparison_identity'], two['comparison_identity'])
        self.assertNotEqual(one['finalization_identity'], two['finalization_identity'])
        with patch.object(readiness, 'verify_finalization', side_effect=self.verifier(one, two)):
            result = self.resolve(campaign)
            selected_one = self.resolve(campaign, finalization_job_id=first)
            selected_two = self.resolve(campaign, finalization_job_id=second)
        self.assertTrue(result['found'])
        self.assertNotIn('ambiguous', result)
        self.assertEqual(selected_one['evidence_identity'], selected_two['evidence_identity'])
        self.assertEqual(selected_one['finalization_job_id'], first)
        self.assertEqual(selected_two['finalization_job_id'], second)
        self.assertEqual(selected_one['comparison_identity'], one['comparison_identity'])
        self.assertEqual(selected_two['comparison_identity'], two['comparison_identity'])
        self.assertEqual(selected_two['report_path'], 'probe/again/finalization.json')

    def test_batch_and_redundant_finalize_negative_close_from_real_selected_artifact(self):
        campaign, verified, sessions = self.candidate()
        verified['decision'] = 'keep_baseline'
        batch_job, batch = self.batch(campaign, verified)
        direct_job, direct = self.finalization(campaign, verified, delivery='probe/again',
                                              created_utc='2026-10-22T01:00:00+00:00')
        before = (self.rtl / 'block.sv').read_bytes()
        with patch.object(readiness, 'verify_finalization', side_effect=self.verifier(direct)), \
                patch.object(readiness, 'verify_batch', return_value=batch):
            resolved = self.resolve(campaign, finalization_job_id=batch_job)
            result = self.complete(campaign, finalization_job_id=batch_job, **sessions)
        self.assertTrue(resolved['found'])
        self.assertEqual(resolved['finalization_job_id'], batch_job)
        self.assertEqual(result['state'], 'keep_baseline')
        self.assertTrue(result['implementation_model_status']['completed'])
        self.assertFalse(result['adopted'])
        self.assertEqual(before, (self.rtl / 'block.sv').read_bytes())
        adoption = json.loads((self.out / result['adoption_path']).read_text())
        self.assertEqual(adoption['finalization_job_id'], batch_job)
        self.assertEqual(adoption['report_path'], campaign['work_scope'] + '/probe/delivery/finalization.json')

    def test_any_material_comparison_difference_remains_ambiguous(self):
        updates = [
            {'baseline_jobs': ['other-b1', 'b2', 'b3']},
            {'candidate_jobs': ['other-c1', 'c2', 'c3']},
            {'verification_jobs': ['other-proof']},
            {'metric': 'peak_rss_bytes'}, {'min_ratio': 1.3}, {'min_repeats': 4},
            {'baseline_values': [2., 2.1, 2.3]}, {'candidate_values': [1., 1.1, 1.3]},
            {'valid_verification': [{'job_id': 'q1', 'kind': 'command', 'scope': 'different stimuli'}]},
            {'invalid_verification': [{'job_id': 'q1', 'reason': 'actual proof failed'}]},
            {'ranges_separated': False}, {'limits': ['Additional proof scope restriction.']},
        ]
        for index, update in enumerate(updates):
            with self.subTest(update=update):
                if index:
                    self.tearDown(); self.setUp()
                campaign, verified, _ = self.candidate()
                first, one = self.finalization(campaign, verified)
                second, two = self.finalization(campaign, verified, delivery='probe/again', comparison_updates=update)
                with patch.object(readiness, 'verify_finalization', side_effect=self.verifier(one, two)):
                    result = self.resolve(campaign, finalization_job_id=first)
                self.assertFalse(result['found'])
                self.assertTrue(result['ambiguous'])

    def test_source_identity_manifest_allowlist_and_changes_are_not_coalesced(self):
        mutations = [
            {'variant_identity': 'other-sealed-candidate'}, {'baseline_identity': 'other-baseline'},
            {'workspace_id': 'other-workspace'},
            {'files': {'rtl/block.sv': {'sha256': 'different', 'size': 1}}},
            {'baseline_files': {'rtl/block.sv': {'sha256': 'different', 'size': 1}}},
            {'allowed_paths': ['rtl/block.sv']}, {'changes': ['rtl/defs.svh']},
            {'decision': 'keep_baseline'},
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                if index:
                    self.tearDown(); self.setUp()
                campaign, verified, _ = self.candidate()
                first, one = self.finalization(campaign, verified)
                second, two = self.finalization(campaign, verified | mutation, delivery='probe/again')
                with patch.object(readiness, 'verify_finalization', side_effect=self.verifier(one, two)):
                    result = self.resolve(campaign, finalization_job_id=first)
                self.assertFalse(result['found'])
                self.assertTrue(result['ambiguous'])

    def test_same_comparison_from_a_different_workspace_remains_ambiguous(self):
        campaign, verified, _ = self.candidate()
        first, one = self.finalization(campaign, verified)
        second, two = self.finalization(campaign, verified, delivery='probe/again')
        base = self.out / campaign['work_scope']
        other = base / 'other'
        other.mkdir()
        (base / 'probe/again').rename(other / 'delivery')
        two.update(workspace=str(other), delivery=str(other / 'delivery'),
                   comparison=str(other / 'delivery/comparison.json'),
                   finalization_path=str(other / 'delivery/finalization.json'))
        request_path = self.out / 'jobs' / second / 'request.json'
        request = json.loads(request_path.read_text())
        request['argv'][request['argv'].index('finalize') + 1] = 'other'
        request['argv'][-1] = 'other/delivery'
        request_path.write_text(json.dumps(request))
        with patch.object(readiness, 'verify_finalization', side_effect=self.verifier(one, two)):
            result = self.resolve(campaign)
        self.assertFalse(result['found'])
        self.assertTrue(result['ambiguous'])

    def test_selected_corrupt_publication_cannot_fall_back_to_equivalent_other_job(self):
        campaign, verified, sessions = self.candidate()
        first, one = self.finalization(campaign, verified)
        second, two = self.finalization(campaign, verified, delivery='probe/again',
                                       created_utc='2026-10-22T01:00:00+00:00')
        Path(one['comparison']).write_text('{}\n')
        before = (self.rtl / 'block.sv').read_bytes()
        with patch.object(readiness, 'verify_finalization', side_effect=self.verifier(one, two)):
            self.assertFalse(self.resolve(campaign, finalization_job_id=first)['found'])
            self.assertTrue(self.resolve(campaign, finalization_job_id=second)['found'])
            result = self.complete(campaign, finalization_job_id=first, **sessions)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertFalse(result['adopted'])
        self.assertEqual(before, (self.rtl / 'block.sv').read_bytes())

    def test_comparison_must_exist_match_fresh_verifier_and_retain_source_binding(self):
        campaign, verified, _ = self.candidate()
        job, verified = self.finalization(campaign, verified)
        comparison = Path(verified['comparison'])
        original = comparison.read_text()
        altered = json.loads(original)
        altered['created_utc'] = 'changed without new identity'
        for content in ('{}\n', '[]\n', '{broken json', json.dumps(altered)):
            with self.subTest(content=content):
                comparison.write_text(content)
                with patch.object(readiness, 'verify_finalization', return_value=verified):
                    self.assertFalse(self.resolve(campaign, finalization_job_id=job)['found'])
        comparison.unlink()
        with patch.object(readiness, 'verify_finalization', return_value=verified):
            self.assertFalse(self.resolve(campaign, finalization_job_id=job)['found'])
        comparison.write_text(original)
        for mutation in ({'comparison_identity': 'unbound'}, {'baseline_identity': 'unbound'}):
            with patch.object(readiness, 'verify_finalization', return_value=verified | mutation):
                self.assertFalse(self.resolve(campaign, finalization_job_id=job)['found'])

    def test_unicode_proof_reason_and_export_paths_use_standalone_canonical_digest(self):
        campaign, verified, _ = self.candidate()
        verified['decision'] = 'keep_baseline'
        updates = {'invalid_verification': [{'job_id': 'q1', 'reason': '证明未完成：/实验/叶子 Ω'}]}
        first, one = self.finalization(campaign, verified, delivery='probe/终态一', comparison_updates=updates)
        second, two = self.finalization(campaign, verified, delivery='probe/终态二', comparison_updates=updates,
                                       created_utc='2026-10-22T01:00:00+00:00')
        with patch.object(readiness, 'verify_finalization', side_effect=self.verifier(one, two)):
            result = self.resolve(campaign, finalization_job_id=second)
        self.assertTrue(result['found'])
        self.assertEqual(result['report_path'], 'probe/终态二/finalization.json')

    def test_positive_duplicate_cannot_reuse_review_for_other_comparison_identity(self):
        campaign, verified, sessions = self.candidate()
        first, one = self.finalization(campaign, verified)
        second, two = self.finalization(campaign, verified, delivery='probe/again',
                                       created_utc='2026-10-22T01:00:00+00:00')
        # Fixture review is deliberately bound to the second ordinary identity.
        before = (self.rtl / 'block.sv').read_bytes()
        with patch.object(readiness, 'verify_finalization', side_effect=self.verifier(one, two)):
            result = self.complete(campaign, finalization_job_id=first, **sessions)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertFalse(result['adopted'])
        self.assertEqual(before, (self.rtl / 'block.sv').read_bytes())


if __name__ == '__main__':
    unittest.main()
