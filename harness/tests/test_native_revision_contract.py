"""Native delegate prompt/identity construction only; models and RPCs are spies."""
import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


H = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'native_revision_contract', H / 'skills/rtl-implementation/scripts/revision.py')
revision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(revision)


class RemoteSpy:
    def __init__(self, budget_ok=True, adopted=True):
        self.requests = []
        self.budget_ok = budget_ok
        self.adopted = adopted
        self.revision = {'revision': 'revision-1',
                         'candidate_scope': 'architecture/revision-1/candidate',
                         'audit_scope': 'architecture/revision-1/audit'}

    def call(self, request):
        self.requests.append(copy.deepcopy(request))
        if request['action'] == 'revision_begin':
            return copy.deepcopy(self.revision)
        if request['action'] == 'evaluate':
            return {'ok': self.budget_ok, 'synthetic_component_result': True}
        if request['action'] == 'revision_adopt':
            return {'adopted': self.adopted}
        raise AssertionError('unexpected controller action: ' + request['action'])


class NativeRevisionContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-contract-pure-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.harness = self.root / 'harness'
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        guide = self.harness / 'skills/rtl-implementation/references/architecture-revision.md'
        guide.parent.mkdir(parents=True)
        guide.write_bytes((H / 'skills/rtl-implementation/references/architecture-revision.md').read_bytes())
        self.conflict = ('OPTIONAL FULL-STAGE FIXTURE: acceptance requires check_proposal, '
                         'recompute and source-only preparation receipts; return only '
                         '<<<AUDIT_REPORT>>> final JSON instead of writing a file.\n')
        for name in ('microarchitecture-design', 'microarchitecture-audit', 'rtl-implementation'):
            folder = self.harness / 'skills' / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'SKILL.md').write_text(name + '\n' + self.conflict)
        self.task = '完整调用任务 Ω 😀\r\n逐字保留\n\nFinal line without newline.'
        self.context = {
            'harness_dir': str(self.harness),
            'capabilities': {'microarchitecture-design': True, 'microarchitecture-audit': True},
            'cache': str(self.cache), 'project_root': str(self.root),
            'remote_run_dir': '/synthetic/isolated/run',
            'tools': {'python': '/synthetic/python'}, 'models': {'fixture': 'unchanged'},
            'task': self.task,
            'settings': {'task_sha256': hashlib.sha256(self.task.encode('utf-8')).hexdigest()},
            'scope': 'work', 'role': 'implementation', 'session_id': 'fixture-parent', 'session_actor_sha256': 'a'*64,
        }
        self.request = {'action': 'architecture_revision', 'reason': 'Original request Ω',
                        'evidence': ['public-fixture-reference'],
                        'proposed_change': 'Reconcile the declared synthetic contract.'}
        self.sessions = []

    def session(self, prompt, role, models, root, context_path, cache, remote, label):
        self.sessions.append({'prompt': prompt, 'model_role': role,
                              'context': json.loads(context_path.read_text()),
                              'context_path': context_path, 'label': label,
                              'models': copy.deepcopy(models), 'root': root})
        return {'completed': True, 'tool_isolation_ok': True, 'exit_code': 0,
                'stop_reason': 'stop', 'elapsed_seconds': 0.01}

    def run_revision(self, remote=None):
        remote = remote or RemoteSpy()
        with patch.object(revision, 'run_session', side_effect=self.session):
            result = revision.run(self.request, self.context, remote)
        return result, remote

    def test_parent_actor_comes_from_controller_context_not_model_request(self):
        self.request['parent_session_id'] = 'forged-other-session'
        self.request['parent_actor_sha256'] = 'b'*64
        _, remote = self.run_revision()
        self.assertEqual(remote.requests[0]['parent_session_id'], 'fixture-parent')
        self.assertEqual(remote.requests[0]['parent_actor_sha256'], 'a'*64)

    def test_missing_parent_actor_stops_before_delegation(self):
        self.context.pop('session_id')
        remote = Mock()
        with patch.object(revision, 'run_session') as model:
            with self.assertRaisesRegex(ValueError, 'trusted implementation parent'):
                revision.run(self.request, self.context, remote)
        remote.call.assert_not_called()
        model.assert_not_called()

    def assert_exact_task(self, session):
        expected = self.context['settings']['task_sha256']
        marker = 'FROZEN DELIVERY TASK (SHA-256 ' + expected + '):\n'
        prompt = session['prompt']
        self.assertEqual(prompt.count(marker), 1)
        start = prompt.index(marker) + len(marker)
        delivered = prompt[start:start + len(self.context['task'])]
        self.assertEqual(delivered.encode('utf-8'), self.context['task'].encode('utf-8'))
        self.assertEqual(session['context']['task'], self.context['task'])
        self.assertEqual(session['context']['settings'], self.context['settings'])

    def test_complete_utf8_task_roles_scopes_and_request_survive_delegation(self):
        original_request = copy.deepcopy(self.request)
        original_context = copy.deepcopy(self.context)
        result, remote = self.run_revision()
        self.assertTrue(result['adopted'])
        self.assertEqual([s['model_role'] for s in self.sessions], ['architecture', 'review'])
        self.assertEqual([s['context']['role'] for s in self.sessions], ['architecture', 'review'])
        self.assertEqual([s['context']['scope'] for s in self.sessions],
                         [remote.revision['candidate_scope'], remote.revision['audit_scope']])
        self.assertNotEqual(self.sessions[0]['context_path'], self.sessions[1]['context_path'])
        for session in self.sessions:
            self.assert_exact_task(session)
            prompt_request = session['prompt'].split('\nREQUEST:\n', 1)[1].split('\nSERVER CONTEXT:\n', 1)[0]
            self.assertEqual(json.loads(prompt_request), original_request)
            self.assertEqual(session['context']['candidate_scope'], remote.revision['candidate_scope'])
            self.assertEqual(session['models'], original_context['models'])
        self.assertEqual(self.request, original_request)
        self.assertEqual(self.context, original_context)
        self.assertEqual(remote.requests, [dict(original_request, action='revision_begin', parent_session_id='fixture-parent', parent_actor_sha256='a'*64),
            {'action': 'evaluate', 'scope': remote.revision['candidate_scope']},
            {'action': 'revision_adopt', 'revision': 'revision-1'}])

    def test_explicit_empty_task_with_matching_identity_is_not_replaced(self):
        self.context['task'] = ''
        self.context['settings']['task_sha256'] = hashlib.sha256(b'').hexdigest()
        self.run_revision()
        self.assertEqual(len(self.sessions), 2)
        for session in self.sessions:
            self.assert_exact_task(session)

    def test_missing_nonstring_or_wrong_task_identity_fails_before_remote_and_model(self):
        invalid = [({}, 'missing'), ({'task': None}, 'null'), ({'task': 1}, 'number'),
                   ({'task': b'raw bytes'}, 'bytes'), ({'task': []}, 'list'),
                   ({'task': True}, 'boolean'), ({'task': self.task}, 'missing_settings'),
                   ({'task': self.task, 'settings': None}, 'null_settings'),
                   ({'task': self.task, 'settings': []}, 'list_settings'),
                   ({'task': self.task, 'settings': {}}, 'missing_sha'),
                   ({'task': self.task, 'settings': {'task_sha256': 7}}, 'nonstring_sha'),
                   ({'task': self.task, 'settings': {'task_sha256': '0' * 64}}, 'mismatched_sha'),
                   ({'task': '\ud800', 'settings': {'task_sha256': '0' * 64}}, 'non_utf8')]
        normalized = self.task.replace('\r\n', '\n')
        invalid.append(({'task': self.task, 'settings': {
            'task_sha256': hashlib.sha256(normalized.encode('utf-8')).hexdigest()}}, 'newline_normalized_sha'))
        for context, label in invalid:
            with self.subTest(label=label):
                # No harness/cache/scope fields are supplied: validation must be first.
                remote = Mock()
                with patch.object(revision, 'run_session') as model:
                    with self.assertRaises(ValueError):
                        revision.run(self.request, context, remote)
                remote.call.assert_not_called()
                model.assert_not_called()
                self.assertEqual(list(self.cache.iterdir()), [])

    def test_design_capability_fallback_keeps_native_role_and_complete_task(self):
        self.context['capabilities']['microarchitecture-design'] = False
        result, remote = self.run_revision(RemoteSpy(adopted=False))
        self.assertFalse(result['adopted'])
        self.assertEqual([s['model_role'] for s in self.sessions], ['implementation', 'review'])
        self.assertEqual(self.sessions[0]['context']['role'], 'architecture')
        self.assertEqual(self.sessions[0]['context']['scope'], remote.revision['candidate_scope'])
        self.assertIn('rtl-implementation\n' + self.conflict, self.sessions[0]['prompt'])
        native_start = self.sessions[0]['prompt'].index('\nAUTHORITATIVE NATIVE REVISION CONTRACT:\n')
        self.assertGreater(native_start, self.sessions[0]['prompt'].index(self.conflict))
        self.assertIn('Native design role:', self.sessions[0]['prompt'][native_start:])
        for session in self.sessions:
            self.assert_exact_task(session)
        self.assertEqual([r['action'] for r in remote.requests], ['revision_begin', 'revision_adopt'])

    def test_native_contract_follows_conflicting_optional_protocol_in_both_roles(self):
        self.run_revision()
        for session in self.sessions:
            prompt = session['prompt']
            boundary = prompt.index('\nAUTHORITATIVE NATIVE REVISION CONTRACT:\n')
            self.assertGreater(boundary, prompt.index(self.conflict))
            self.assertGreater(boundary, prompt.index('\nSERVER CONTEXT:\n'))
            native = prompt[boundary:]
            self.assertIn('Optional SKILL guidance supplies engineering methods', native)
            self.assertIn('not native tools or prerequisites here', native)
            self.assertIn('Native proposal', native)
            self.assertIn('native evaluate', native)
            self.assertIn('actual submit/status job evidence', native)
            self.assertIn('no engineering obligation is waived', native)
            if session['context']['role'] == 'review':
                self.assertIn('Persist review.json in your audit scope', native)
                self.assertIn('candidate_sha256 from proposal', native)
                self.assertIn('actual successful independent job IDs', native)
                self.assertIn('Do not edit the candidate', native)
            else:
                self.assertIn('design.json, architecture.md and CHANGES.md', native)
                self.assertIn('caller repeats the final calculation', native)

    def test_missing_optional_audit_is_not_a_fabricated_child_review(self):
        self.context['capabilities']['microarchitecture-audit'] = False
        result, remote = self.run_revision(RemoteSpy(adopted=False))
        self.assertFalse(result['adopted'])
        self.assertEqual([s['model_role'] for s in self.sessions], ['architecture'])
        self.assertEqual(result['results']['review']['status'], 'not_run')
        self.assert_exact_task(self.sessions[0])

    def test_failed_design_session_does_not_evaluate_or_adopt(self):
        remote = RemoteSpy()
        with patch.object(revision, 'run_session', return_value={
                'completed': False, 'tool_isolation_ok': True, 'exit_code': 1}) as model:
            result = revision.run(self.request, self.context, remote)
        self.assertFalse(result['adopted'])
        self.assertEqual(model.call_count, 1)
        self.assertEqual([r['action'] for r in remote.requests], ['revision_begin'])

    def test_failed_actual_budget_stops_before_review_and_adoption(self):
        result, remote = self.run_revision(RemoteSpy(budget_ok=False))
        self.assertFalse(result['adopted'])
        self.assertEqual([s['model_role'] for s in self.sessions], ['architecture'])
        self.assertEqual([r['action'] for r in remote.requests], ['revision_begin', 'evaluate'])


if __name__ == '__main__':
    unittest.main()
