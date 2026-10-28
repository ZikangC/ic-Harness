"""Fresh scoped imports never mutate or rename the historical candidate."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.runtime.rtl_readiness_seed import seed


class SeedImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'top.sv').write_text('module top; endmodule\n')
        (self.source / 'large-result.bin').write_bytes(b'result not imported')
        self.out = self.root / 'run'
        (self.out / 'work').mkdir(parents=True)
        self.req = {'source_work': str(self.source), 'include_paths': ['top.sv'],
                    'provenance': {'candidate_sha256': 'historical-id',
                                   'selected_sha256': {'top.sv': hashlib.sha256((self.source / 'top.sv').read_bytes()).hexdigest()}}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_selected_import_and_repeat_identity(self):
        receipt = seed(self.out, self.req)
        self.assertEqual(set(receipt['selected_files']), {'top.sv'})
        self.assertEqual((self.out / 'work/top.sv').read_bytes(), (self.source / 'top.sv').read_bytes())
        self.assertFalse((self.out / 'work/large-result.bin').exists())
        self.assertEqual(seed(self.out, self.req), receipt)
        self.assertEqual(seed(self.out, {'validate_only': True}), receipt)
        changed = dict(self.req, provenance={})
        with self.assertRaises(ValueError):
            seed(self.out, changed)

    def test_wrong_source_identity_copies_nothing(self):
        self.req['provenance']['selected_sha256']['top.sv'] = 'wrong'
        with self.assertRaises(ValueError):
            seed(self.out, self.req)
        self.assertEqual(list((self.out / 'work').iterdir()), [])

    def test_escape_link_and_existing_work_are_rejected(self):
        for paths in [['../else.sv'], ['/etc/passwd']]:
            with self.assertRaises(ValueError):
                seed(self.out, dict(self.req, include_paths=paths, provenance={}))
        (self.source / 'link.sv').symlink_to(self.source / 'top.sv')
        with self.assertRaises(ValueError):
            seed(self.out, dict(self.req, include_paths=['link.sv'], provenance={}))
        (self.out / 'work/user.sv').write_text('preserve')
        with self.assertRaises(ValueError):
            seed(self.out, self.req)
        self.assertEqual((self.out / 'work/user.sv').read_text(), 'preserve')

    def test_interrupted_copy_cannot_publish_partial_seed(self):
        with patch('harness.runtime.rtl_readiness_seed.shutil.copy2', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                seed(self.out, self.req)
        self.assertEqual(list((self.out / 'work').iterdir()), [])
        self.assertFalse((self.out / 'control/readiness-seed.json').exists())
        with self.assertRaises(FileNotFoundError):
            seed(self.out, {'validate_only': True})
        seed(self.out, self.req)
        (self.out / 'work/top.sv').write_text('partial')
        with self.assertRaises(ValueError):
            seed(self.out, {'validate_only': True})


class FormalRepairSeedImport(unittest.TestCase):
    """Real candidate/decision/repair transitions, without model or tool jobs."""
    setUp = SeedImport.setUp
    tearDown = SeedImport.tearDown

    def formal_seed(self):
        from harness.runtime import rtl_worker as worker
        (self.source / 'rtl').mkdir()
        (self.source / 'assets').mkdir()
        (self.source / 'rtl/top.sv').write_text('module top; wire original; endmodule\n')
        (self.source / 'assets/weights.mem').write_text('1234\n')
        self.req = {'source_work': str(self.source),
                    'include_paths': ['rtl/top.sv', 'assets/weights.mem'],
                    'provenance': {'candidate_sha256': 'historical-id'}}
        receipt = seed(self.out, self.req)
        worker.save(self.out / 'manifest.json', {'settings': {'readiness': {'rtl_review': True}}})
        baseline = self.out / 'snapshot/architecture'
        baseline.mkdir(parents=True)
        (baseline / 'architecture.md').write_text('unchanged frozen architecture\n')
        worker.save(self.out / 'architecture-current.json', {'path': 'snapshot/architecture'})
        return receipt

    def transition(self, action, **options):
        from harness.runtime.rtl_worker import rtl_transition
        return rtl_transition(self.out, {'action': action, **options})

    def start_repair(self):
        self.formal_seed()
        review = self.transition('rtl_review_begin')
        report = self.out / review['audit_scope'] / 'review.json'
        report.write_text(json.dumps({'verdict': 'revise', 'review_scope': review['review_scope'], 'candidate_sha256': review['candidate_sha256'],
                                      'reason': 'Synthetic review finding; no hardware pass claimed.', 'evidence': []}))
        review = self.transition('rtl_review_decision', review=review['review'],
                                 report_path='review.json', expected_verdict='revise')
        self.transition('rtl_repair_begin', review=review['review'])
        actor = {'repair_actor': 'rtl-implementation-readiness',
                 'candidate_sha256': review['candidate_sha256'],
                 'decision_report_sha256': review['decision']['report_sha256'],
                 'completed': False, 'tool_isolation_ok': False}
        path = self.out / 'control/rtl-audit' / (review['review'] + '.repair.json')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(actor))
        return review, path

    def test_active_completed_and_next_audit_resume_accept_recorded_rtl_repair(self):
        review, actor = self.start_repair()
        original_receipt = (self.out / 'control/readiness-seed.json').read_bytes()
        (self.out / 'work/rtl/top.sv').write_text('module top; wire repaired; endmodule\n')
        seed(self.out, {'validate_only': True})
        self.transition('rtl_repair_end', review=review['review'])
        seed(self.out, {'validate_only': True})
        fresh = self.transition('rtl_review_begin', seed_review=review['review'])
        self.assertNotEqual(fresh['candidate_sha256'], review['candidate_sha256'])
        seed(self.out, {'validate_only': True})
        self.assertEqual((self.out / 'control/readiness-seed.json').read_bytes(), original_receipt)
        (self.out / 'work/rtl/top.sv').write_text('unrecorded change after freeze\n')
        with self.assertRaisesRegex(ValueError, 'outside the recorded formal'):
            seed(self.out, {'validate_only': True})

    def test_active_repair_never_allows_selected_non_rtl_asset_changes(self):
        review, actor = self.start_repair()
        (self.out / 'work/rtl/top.sv').write_text('module top; wire repaired; endmodule\n')
        (self.out / 'work/assets/weights.mem').write_text('changed asset\n')
        with self.assertRaisesRegex(ValueError, 'assets/weights.mem'):
            seed(self.out, {'validate_only': True})
        self.transition('rtl_repair_end', review=review['review'])
        with self.assertRaisesRegex(ValueError, 'assets/weights.mem'):
            seed(self.out, {'validate_only': True})

    def test_missing_or_stale_actor_cannot_authorize_changed_live_rtl(self):
        review, path = self.start_repair()
        actor = json.loads(path.read_text())
        (self.out / 'work/rtl/top.sv').write_text('module changed; endmodule\n')
        for fault in ('missing', 'repair_actor', 'candidate_sha256', 'decision_report_sha256'):
            with self.subTest(fault=fault):
                if fault == 'missing':
                    path.unlink(missing_ok=True)
                else:
                    value = dict(actor)
                    value[fault] = 'unrelated-identity'
                    path.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, 'imported seed or adopted source changed'):
                    seed(self.out, {'validate_only': True})
        path.write_text(json.dumps(actor))
        seed(self.out, {'validate_only': True})

    def test_live_rtl_change_without_active_repair_or_opt_in_is_rejected(self):
        self.formal_seed()
        self.transition('rtl_review_begin')
        target = self.out / 'work/rtl/top.sv'
        target.write_text('module changed; endmodule\n')
        with self.assertRaisesRegex(ValueError, 'outside the recorded formal'):
            seed(self.out, {'validate_only': True})
        manifest = self.out / 'manifest.json'
        manifest.write_text(json.dumps({'settings': {'readiness': {'rtl_review': False}}}))
        with self.assertRaisesRegex(ValueError, 'imported seed or adopted source changed'):
            seed(self.out, {'validate_only': True})

    def test_frozen_candidate_tampering_is_detected_during_active_repair_resume(self):
        review, actor = self.start_repair()
        (self.out / 'work/rtl/top.sv').write_text('module repaired; endmodule\n')
        (self.out / review['candidate_scope'] / 'rtl/top.sv').write_text('tampered frozen input\n')
        with self.assertRaisesRegex(ValueError, 'frozen RTL candidate changed'):
            seed(self.out, {'validate_only': True})

    def test_formal_history_must_descend_from_imported_or_adopted_closure(self):
        self.formal_seed()
        (self.out / 'work/rtl/top.sv').write_text('unrelated source before first review\n')
        self.transition('rtl_review_begin')
        with self.assertRaisesRegex(ValueError, 'does not descend from the imported seed'):
            seed(self.out, {'validate_only': True})

    def test_frozen_audit_report_tampering_is_detected_on_resume(self):
        review, actor = self.start_repair()
        (self.out / review['decision']['report_path']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'decided RTL audit evidence changed'):
            seed(self.out, {'validate_only': True})

    def test_adopted_source_pins_remain_valid_when_first_formal_review_starts(self):
        self.formal_seed()
        target = self.out / 'work/rtl/top.sv'
        target.write_text('module top; wire adopted; endmodule\n')
        adoption = self.out / 'readiness/campaign-1.adoption.json'
        adoption.parent.mkdir()
        adoption.write_text(json.dumps({'adopted': True, 'changed_files': {
            'rtl/top.sv': hashlib.sha256(target.read_bytes()).hexdigest()}}))
        self.transition('rtl_review_begin')
        seed(self.out, {'validate_only': True})


if __name__ == '__main__':
    unittest.main()
