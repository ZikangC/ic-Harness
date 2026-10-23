"""Frozen architecture delivery mechanics; no model, RPC, jobs or EDA."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.runtime import rtl_worker as worker

from harness.adapters import rtl_audit as flow


class ArchitectureContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / 'output/run'
        self.base = self.out / 'snapshot/architecture'
        self.base.mkdir(parents=True)
        (self.out / 'work').mkdir()
        (self.out / 'work/payload.txt').write_bytes(b'ordinary implementation fixture\n')
        self.sources = {
            'architecture.md': ('完整草稿 Ω\r\n' + 'long-source-α' * 6000 + '\r\n').encode(),
            'SOURCE.md': '# 原生来源\r\n\r\n历史记录保持原字节。\r\n'.encode(),
            'decision.json': '{"范围":"原生决定"}\r\n'.encode(),
            'design.json': b'{"fixture":"structured design"}\n',
            'budgets.json': b'{"fixture":"calculation report"}\n',
            'review.json': b'{"fixture":"independent review"}\n',
        }
        for name, body in self.sources.items():
            (self.base / name).write_bytes(body)
        worker.save(self.out / 'architecture-current.json', {
            'path': 'snapshot/architecture', 'review_status': 'accepted_estimate_only'})

    def begin(self):
        return worker.rtl_transition(self.out, {'action': 'rtl_review_begin'})

    def context(self, **options):
        return worker.rtl_transition(self.out, {'action': 'rtl_review_status',
                                                'include_architecture_context': True, **options})

    def test_opt_in_context_is_complete_and_bound_to_frozen_candidate(self):
        review = self.begin()
        before = worker.closure_inventory(self.out)
        normal = worker.rtl_transition(self.out, {'action': 'rtl_review_status'})
        self.assertNotIn('architecture_context', normal)
        self.assertNotIn(self.sources['architecture.md'].decode(), json.dumps(normal))
        status = self.context(review=review['review'])
        value = status['architecture_context']
        self.assertEqual(value['scope'], review['architecture_scope'])
        self.assertEqual(value['candidate_sha256'], review['candidate_sha256'])
        self.assertEqual(value['identity_sha256'], status['architecture_identity_sha256'])
        self.assertEqual(worker.digest(value['identity']), value['identity_sha256'])
        self.assertTrue(value['matches_run_current'])
        self.assertEqual(set(value['files']), set(self.sources))
        for name, body in self.sources.items():
            index = value['files'][name]
            self.assertEqual(index['path'], '@run/' + review['architecture_scope'] + '/' + name)
            self.assertEqual(index['sha256'], worker.hashlib.sha256(body).hexdigest())
            self.assertEqual(index['bytes'], len(body))
            if name in ('architecture.md', 'SOURCE.md', 'decision.json'):
                self.assertEqual(value['primary'][name]['content'].encode(), body)
            else:
                self.assertNotIn('content', index)
        self.assertEqual(worker.closure_inventory(self.out), before)

    def test_later_run_current_is_not_substituted_for_frozen_baseline(self):
        review = self.begin()
        new = self.out / 'architecture/revision-1/baseline'
        new.mkdir(parents=True)
        (new / 'architecture.md').write_bytes(b'different later baseline\n')
        pointer = {'path': 'architecture/revision-1/baseline', 'review_status': 'not_run'}
        worker.save(self.out / 'architecture-current.json', pointer)
        value = self.context()['architecture_context']
        self.assertFalse(value['matches_run_current'])
        self.assertEqual(value['run_current'], pointer)
        self.assertEqual(value['identity']['current']['path'], 'snapshot/architecture')
        self.assertEqual(value['primary']['architecture.md']['content'].encode(), self.sources['architecture.md'])
        self.assertIn(review['architecture_scope'], value['primary']['architecture.md']['path'])

    def test_real_worker_response_is_accepted_and_forwarded_exactly_by_controller(self):
        review = self.begin()
        calls = []
        out = self.out
        class ReadOnlyStatus:
            def call(self, request):
                calls.append(request)
                if request['action'] != 'rtl_review_status':
                    raise AssertionError('only the controller status action is allowed')
                return worker.rtl_transition(out, request)
        text = flow._frozen_architecture(ReadOnlyStatus(), review)
        for name in ('architecture.md', 'SOURCE.md', 'decision.json'):
            self.assertEqual(text.count(self.sources[name].decode()), 1)
        self.assertEqual(calls, [{'action': 'rtl_review_status', 'review': review['review'],
                                  'include_architecture_context': True}])
        self.assertNotIn(self.sources['budgets.json'].decode(), text)

    def test_real_worker_changed_current_is_labeled_separately_in_controller_context(self):
        review = self.begin()
        later = self.out / 'architecture/revision-1/baseline'
        later.mkdir(parents=True)
        new_body = b'new live baseline content\n'
        (later / 'architecture.md').write_bytes(new_body)
        worker.save(self.out / 'architecture-current.json', {'path': 'architecture/revision-1/baseline',
                                                            'review_status': 'not_run'})
        out = self.out
        class ReadOnlyStatus:
            def call(self, request):
                return worker.rtl_transition(out, request)
        text = flow._frozen_architecture(ReadOnlyStatus(), review)
        self.assertEqual(text.count(self.sources['architecture.md'].decode()), 1)
        self.assertNotIn(new_body.decode(), text)
        # Compare delivered data fields, not the wording of an instruction.
        metadata_text = text[text.index('{'):text.index('\nRAW FROZEN FILE ')]
        metadata = json.loads(metadata_text)
        self.assertFalse(metadata['matches_run_current'])
        self.assertEqual(metadata['identity']['current']['path'], 'snapshot/architecture')
        self.assertEqual(metadata['run_current']['path'], 'architecture/revision-1/baseline')

    def test_optional_sources_absent_at_freeze_are_reported_missing(self):
        for name in ('SOURCE.md', 'decision.json'):
            (self.base / name).unlink()
        review = self.begin()
        value = self.context()['architecture_context']
        for name in ('SOURCE.md', 'decision.json'):
            self.assertNotIn(name, value['files'])
            self.assertEqual(value['primary'][name], {'status': 'missing',
                'path': '@run/' + review['architecture_scope'] + '/' + name})

    def test_declared_source_removed_or_changed_is_never_optional_absence(self):
        review = self.begin()
        target = self.out / review['architecture_scope'] / 'SOURCE.md'
        target.unlink()
        with self.assertRaises(ValueError):
            self.context()
        target.write_bytes(self.sources['SOURCE.md'] + b'changed')
        with self.assertRaises(ValueError):
            self.context()

    def test_missing_main_draft_or_non_utf8_source_is_rejected(self):
        for scenario in ('missing_main_draft', 'non_utf8'):
            with self.subTest(scenario=scenario):
                if scenario == 'missing_main_draft':
                    (self.base / 'architecture.md').unlink()
                else:
                    (self.base / 'architecture.md').write_bytes(b'\xff\x80')
                review = self.begin()
                with self.assertRaises((ValueError, UnicodeError)):
                    self.context(review=review['review'])
                # A new independently frozen fixture is used for each attempt.
                (self.base / 'architecture.md').write_bytes(self.sources['architecture.md'])

    def test_internal_primary_symlink_is_indexed_and_delivered_by_content_hash(self):
        draft = self.base / 'architecture.md'
        draft.rename(self.base / 'draft-source.txt')
        draft.symlink_to('draft-source.txt')
        review = self.begin()
        value = self.context()['architecture_context']
        self.assertEqual(value['primary']['architecture.md']['content'].encode(), self.sources['architecture.md'])
        self.assertEqual(value['files']['architecture.md']['sha256'], value['files']['draft-source.txt']['sha256'])
        self.assertEqual(value['identity']['inventory']['architecture.md']['type'], 'symlink')
        self.assertEqual((self.out / review['architecture_scope'] / 'architecture.md').resolve(),
                         (self.out / review['architecture_scope'] / 'draft-source.txt').resolve())

    def test_mutation_during_context_delivery_is_rejected(self):
        review = self.begin()
        stored = worker.get_review(self.out, review['review'])
        original = worker.architecture_identity
        def current_then_mutate(out):
            result = original(out)
            (self.out / review['architecture_scope'] / 'decision.json').write_bytes(b'changed during read\n')
            return result
        with patch.object(worker, 'architecture_identity', side_effect=current_then_mutate):
            with self.assertRaises(ValueError):
                worker.candidate_architecture_context(self.out, stored)

    def test_absent_candidate_and_non_boolean_opt_in_are_rejected(self):
        with self.assertRaises(ValueError):
            self.context()
        self.begin()
        with self.assertRaises(ValueError):
            self.context(include_architecture_context='yes')


if __name__ == '__main__':
    unittest.main()
