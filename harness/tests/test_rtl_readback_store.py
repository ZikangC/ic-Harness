"""Local synthetic control/file fixtures; no OMP, Docker, EDA or project RTL."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from harness.runtime import rtl_worker as worker
from harness.runtime import rtl_readback_store as store


class ReadbackStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rtl-readback-store-', dir=os.environ.get('HARNESS_TEST_PROJECT_DIR'))
        self.root = Path(self.tmp.name).resolve(); self.out = self.root / 'output/run-fixture'
        for name in ('snapshot/harness', 'snapshot/input', 'snapshot/input-physical', 'snapshot/architecture', 'work', 'control'):
            (self.out / name).mkdir(parents=True, exist_ok=True)
        (self.out / 'snapshot/architecture/architecture.md').write_text('Original accepted estimate.\n')
        self.dump(self.out / 'snapshot/architecture/design.json', {'requirements': {'original': True}})
        skill = self.out / 'snapshot/harness/skills/microarchitecture-audit/SKILL.md'; skill.parent.mkdir(parents=True); skill.write_text('Synthetic optional capability.\n')
        self.dump(self.out / 'manifest.json', {'input_sha256': {}, 'physical_input_sha256': {},
                                             'harness_sha256': worker.inventory(self.out / 'snapshot/harness'),
                                             'architecture_sha256': worker.inventory(self.out / 'snapshot/architecture')})
        self.dump(self.out / 'architecture-current.json', {'path': 'snapshot/architecture', 'review_status': 'accepted_estimate_only'})
        self.parent = 'parent-fixture-1'; self.register(self.parent)

    def tearDown(self):
        self.tmp.cleanup()

    def dump(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value, ensure_ascii=False) + '\n')

    def call(self, **request):
        return worker.dispatch(self.root, self.out.name, request)

    def register(self, label, role='implementation', scope='work', model_role='implementation'):
        (self.out / 'control' / (label + '.prompt.txt')).write_text('Trusted synthetic parent task.\n')
        return self.call(action='rtl_session_begin', session_id=label, role=role, scope=scope, model_role=model_role)

    def adopt(self, parent=None, missing=None):
        parent = parent or self.parent; revision = self.out / 'architecture/revision-1'; candidate = revision / 'candidate'; candidate.mkdir(parents=True)
        bodies = {'architecture.md': 'Architectural commitments.\n' + '长行🙂' * 3500,
                  'design.json': json.dumps({'requirements': {'original': True}, 'selected': 'generic'}),
                  'CHANGES.md': 'The reviewed architectural changes.\n', 'budgets.json': json.dumps({'ok': True}),
                  'review.json': '{}\n', 'decision.json': '{}\n', 'SOURCE.md': 'Proposal provenance.\n',
                  'budget_binding.json': '{}\n', 'budget.execution.json': '{"executed": true}\n'}
        for name, body in bodies.items():
            if name != missing:
                (candidate / name).write_text(body)
        report = revision / 'audit/review.json'; self.dump(report, {'verdict': 'accept', 'reason': 'Synthetic engineering review record.'})
        identity = worker.architecture_identity(self.out)
        origin = {'baseline_path': str(self.out / identity['current']['path']), 'baseline_sha256': worker.inventory(self.out / identity['current']['path']),
                  'baseline_identity': identity, 'parent_session_id': parent, 'parent_actor_sha256': worker.sha(self.out / 'control/sessions' / (parent + '.json'))}
        result = worker.publish_architecture_baseline(self.out, revision, candidate, worker.proposal_identity(candidate)['candidate_sha256'], 'accept', origin, worker.sha(report))
        keys = ('path', 'review_status', 'native_review_status', 'approved_for_implementation', 'baseline_sha256', 'candidate_path', 'candidate_sha256', 'handoff_path', 'handoff_sha256', 'scope', 'parent_session_id', 'parent_actor_sha256', 'readback_required_files')
        current = {key: result[key] for key in keys}; current['design_sha256'] = worker.sha(Path(result['path']) / 'design.json') if Path(result['path']).is_absolute() else worker.sha(self.out / result['path'] / 'design.json')
        self.dump(self.out / 'architecture-current.json', current)
        return current

    def events(self, current, mode='full', scope='work'):
        events = []; number = 0
        for item in current['readback_required_files']:
            name = '@run/' + item['resolved_run_path']; cursor = None
            while True:
                page = self.call(action='read', scope=scope, path=name, lines=1, **({'cursor': cursor} if cursor else {}))
                number += 1; ident = 'read-' + str(number)
                include = mode != 'endonly' or page['eof']
                if mode == 'wrongversion' and item['resolved_run_path'].endswith('/design.json'):
                    page['sha256'] = 'f' * 64
                if include:
                    events += [{'type': 'tool_execution_start', 'toolName': 'rtl_remote', 'toolCallId': ident,
                                'args': {'action': 'read', 'path': name, **({'cursor': cursor} if cursor else {})}},
                               {'type': 'tool_execution_end', 'toolName': 'rtl_remote', 'toolCallId': ident, 'isError': False,
                                'result': {'content': [{'type': 'text', 'text': json.dumps(page, ensure_ascii=False)}], 'details': {'not_evidence': True}}}]
                cursor = page['next_cursor']
                if cursor is None:
                    break
        return events

    def finish(self, events, label=None, completed=True):
        label = label or self.parent
        self.dump(self.out / 'control' / (label + '.result.json'), {'role': 'implementation', 'completed': completed,
                                                                  'tool_isolation_ok': True, 'exit_code': 0, 'stop_reason': 'stop'})
        self.dump(self.out / 'control' / (label + '.isolation.json'), {'checks': [{'active_tools': ['rtl_remote', 'rtl_revise_architecture']}], 'blocked': []})
        (self.out / 'control' / (label + '.events.jsonl')).write_bytes(''.join(json.dumps(event, ensure_ascii=False) + '\n' for event in events).encode())
        return self.call(action='rtl_session_end', session_id=label, completed=True, events_sha256='0' * 64)

    def decide(self, expected=True):
        review = self.call(action='rtl_review_begin'); report_path = self.out / review['audit_scope'] / 'report.json'
        # Synthetic terminal protocol metadata only; no hardware execution claim.
        job = self.out / 'jobs/1'
        self.dump(job / 'metadata-fixture-only.json', {'schema': 'synthetic-protocol-metadata-only/v1', 'not_launched': True, 'no_host_launch_or_container': True})
        self.dump(job / 'request.json', {'metadata_fixture_only': True, 'scope': review['audit_scope'], 'candidate_sha256': review['candidate_sha256']})
        self.dump(job / 'result.json', {'metadata_fixture_only': True, 'candidate_sha256': review['candidate_sha256'], 'exit_code': 0, 'timed_out': False})
        self.dump(report_path, {'verdict': 'pass', 'candidate_sha256': review['candidate_sha256'], 'reason': 'Synthetic release-guard binding check only.', 'evidence': ['1']})
        if expected:
            return self.call(action='rtl_review_decision', review=review['review'], report_path=str(report_path.relative_to(self.out)), expected_verdict='final')
        with self.assertRaisesRegex(ValueError, 'complete original parent readback'):
            self.call(action='rtl_review_decision', review=review['review'], report_path=str(report_path.relative_to(self.out)), expected_verdict='final')

    def test_actual_worker_pages_bind_complete_parent_receipt_and_allow_release_guard(self):
        current = self.adopt(); original = (self.out / 'architecture-current.json').read_bytes(); baseline = worker.closure_inventory(self.out / current['path'])
        self.assertFalse(worker.architecture_identity(self.out)['parent_readback']['complete'])
        events = self.events(current); self.assertGreater(len(events), 2 * len(store.REQUIRED_FILES))
        ended = self.finish(events); self.assertTrue(ended['completed'])
        identity = worker.architecture_identity(self.out)
        self.assertTrue(identity['parent_readback']['complete'])
        receipt = json.loads((self.out / ended['readback_path']).read_text())
        self.assertTrue(receipt['witness']['complete']); self.assertEqual(len(receipt['witness']['files']), 9)
        self.assertNotIn('长行', json.dumps(receipt, ensure_ascii=False))
        self.assertEqual(original, (self.out / 'architecture-current.json').read_bytes())
        self.assertEqual(worker.closure_inventory(self.out / current['path']), baseline)
        self.assertEqual(self.decide()['decision']['verdict'], 'pass')

    def test_end_only_pages_cannot_release(self):
        current = self.adopt(); self.finish(self.events(current, 'endonly'))
        self.assertFalse(worker.architecture_identity(self.out)['parent_readback']['complete']); self.decide(False)

    def test_missing_reads_cannot_release(self):
        self.adopt(); self.finish([])
        self.assertFalse(worker.architecture_identity(self.out)['parent_readback']['complete']); self.decide(False)

    def test_wrong_version_cannot_release(self):
        current = self.adopt(); self.finish(self.events(current, 'wrongversion'))
        self.assertFalse(worker.architecture_identity(self.out)['parent_readback']['complete']); self.decide(False)

    def test_uncompleted_actual_result_cannot_be_overridden_by_end_request(self):
        current = self.adopt(); ended = self.finish(self.events(current), completed=False)
        self.assertFalse(ended['completed']); self.assertFalse(worker.architecture_identity(self.out)['parent_readback']['complete']); self.decide(False)

    def test_child_with_implementation_model_role_cannot_replace_parent(self):
        current = self.adopt(); child = 'child-fallback'; self.register(child, 'architecture', 'architecture/revision-1/candidate')
        ended = self.finish(self.events(current, scope='architecture/revision-1/candidate'), label=child)
        self.assertIsNone(ended['readback_path']); self.assertFalse(worker.architecture_identity(self.out)['parent_readback']['complete'])
        self.finish([]); self.decide(False)

    def test_missing_or_child_or_ended_parent_cannot_begin_revision(self):
        for value in (None, '../escape', 'missing'):
            with self.subTest(parent=value), self.assertRaises(ValueError):
                self.call(action='revision_begin', parent_session_id=value, reason='Synthetic request.')
        child = 'child'; self.register(child, 'architecture', 'architecture/revision-1/candidate')
        with self.assertRaisesRegex(ValueError, 'actual implementation parent'):
            self.call(action='revision_begin', parent_session_id=child, parent_actor_sha256=worker.sha(self.out / 'control/sessions' / (child + '.json')), reason='Synthetic request.')
        self.finish([])
        with self.assertRaisesRegex(ValueError, 'has ended'):
            self.call(action='revision_begin', parent_session_id=self.parent, parent_actor_sha256=worker.sha(self.out / 'control/sessions' / (self.parent + '.json')), reason='Synthetic request.')

    def test_publication_fails_before_baseline_if_main_packet_file_missing(self):
        with self.assertRaisesRegex(ValueError, 'missing required file: CHANGES.md'):
            self.adopt(missing='CHANGES.md')
        self.assertFalse((self.out / 'architecture/revision-1/baseline').exists())
        self.assertNotIn('parent_session_id', json.loads((self.out / 'architecture-current.json').read_text()))

    def test_old_snapshot_has_no_new_obligation(self):
        self.assertNotIn('parent_readback', worker.architecture_identity(self.out))
        self.assertEqual(self.decide()['decision']['verdict'], 'pass')

    def test_actor_end_receipt_and_original_sources_are_immutable_or_checked(self):
        current = self.adopt(); ended = self.finish(self.events(current)); again = self.call(action='rtl_session_end', session_id=self.parent)
        self.assertEqual(ended, again)
        for relative in (ended['readback_path'], 'control/sessions/' + self.parent + '.json',
                         'control/sessions/' + self.parent + '.end.json',
                         'control/' + self.parent + '.result.json', 'control/' + self.parent + '.events.jsonl',
                         'control/' + self.parent + '.isolation.json', current['handoff_path']):
            path = self.out / relative; original = path.read_bytes()
            with self.subTest(relative=relative):
                path.write_bytes(original + b' ')
                with self.assertRaises(ValueError):
                    worker.architecture_identity(self.out)
                path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.call(action='rtl_session_begin', session_id=self.parent, role='implementation', scope='work')
        with self.assertRaisesRegex(ValueError, 'controller-owned'):
            self.call(action='put_evidence', path='control/sessions/' + self.parent + '.json', content='{}')
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.call(action='append_evidence', path='control/' + self.parent + '.events.jsonl', content='{}\n')

    def test_read_returns_canonical_packet_locator(self):
        current = self.adopt(); page = self.call(action='read', path='@architecture/design.json')
        self.assertEqual(page['resolved_run_path'], current['path'] + '/design.json')

    def test_active_actor_original_pin_rejects_role_rewrite_and_parent_pin_is_required(self):
        actor_path = self.out / 'control/sessions' / (self.parent + '.json'); original = actor_path.read_bytes()
        original_sha = hashlib.sha256(original).hexdigest()
        with self.assertRaisesRegex(ValueError, 'fingerprint changed'):
            self.call(action='revision_begin', parent_session_id=self.parent, reason='Missing trusted actor pin.')
        actor = json.loads(original); actor['model_role'] = 'different-model-role'
        actor_path.write_bytes(store._encoded(actor))
        with self.assertRaisesRegex(ValueError, 'fingerprint changed'):
            self.call(action='revision_begin', parent_session_id=self.parent, parent_actor_sha256=original_sha, reason='Changed active actor.')
        actor_path.write_bytes(original)
        revision = self.call(action='revision_begin', parent_session_id=self.parent, parent_actor_sha256=original_sha, reason='Correct original actor.')
        request = json.loads((self.out / revision['revision'] / 'request.json').read_text())
        self.assertEqual(request['parent_actor_sha256'], original_sha)

    def test_registered_source_protection_prevents_accidental_restart_overwrite(self):
        prompt = 'control/' + self.parent + '.prompt.txt'
        for action in ('put_evidence', 'append_evidence'):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'prompt is immutable'):
                self.call(action=action, path=prompt, content='replacement')
        events = 'control/' + self.parent + '.events.jsonl'
        self.call(action='put_evidence', path=events, content='')
        self.call(action='append_evidence', path=events, content='{"type":"message_end"}\n')
        with self.assertRaisesRegex(ValueError, 'cannot be overwritten'):
            self.call(action='put_evidence', path=events, content='')
        with self.assertRaisesRegex(ValueError, 'only active session events'):
            self.call(action='append_evidence', path='control/' + self.parent + '.result.json', content='{}')


if __name__ == '__main__':
    unittest.main()
