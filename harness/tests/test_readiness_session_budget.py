"""Offline budget feedback and receipt-bound controlled handoff; no EDA/model."""
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from hashlib import sha256
from unittest.mock import patch

from harness.adapters.omp_tools import run_session
from harness.runtime.rtl_session_budget import decorate, write_budget


class RecordingRemote:
    def __init__(self):
        self.calls = []
        self.evidence_files = {}

    def evidence(self, name, content):
        self.evidence_files[name] = content

    def call(self, request):
        self.calls.append(dict(request))
        if request['action'] == 'rtl_session_begin':
            label = request['session_id']
            actor = dict(version=1, run_id='fixture', session_id=label, label=label,
                         role=request['role'], scope=request['scope'], model_role=request['model_role'],
                         prompt_sha256=sha256(self.evidence_files[label + '.prompt.txt'].encode()).hexdigest())
            return dict(registered=True, actor=actor, actor_path='control/sessions/' + label + '.json',
                        actor_sha256=sha256((json.dumps(actor, ensure_ascii=False, indent=2) + '\n').encode()).hexdigest())
        return {'ok': True}


class BudgetFeedbackTests(unittest.TestCase):
    def test_feedback_is_dynamic_private_and_adapts_closing_reserve(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'budget.json'
            write_budget(path, 'actor', {'assistant_turns': 80, 'session_seconds': 900}, 71, 100)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            context = {'session_id': 'actor', 'session_budget_path': str(path), 'role': 'rtl_readiness'}
            original = {'status': 'running'}
            with patch('harness.runtime.rtl_session_budget.time.monotonic', return_value=110):
                first = decorate(context, original, 'status')
            self.assertEqual(original, {'status': 'running'})
            self.assertEqual(first['session_budget']['assistant_turns_remaining'], 9)
            self.assertEqual(first['session_budget']['phase'], 'investigation')
            with patch('harness.runtime.rtl_session_budget.time.monotonic', return_value=960):
                second = decorate(context, original, 'status')
            self.assertEqual(second['session_budget']['session_seconds_remaining'], 40)
            self.assertEqual(second['session_budget']['phase'], 'closing')
            self.assertIn('handoff', second['session_budget']['closing_instruction'])
            write_budget(path, 'actor', {'assistant_turns': 3, 'session_seconds': 5}, 2, 100)
            with patch('harness.runtime.rtl_session_budget.time.monotonic', return_value=100):
                review = decorate(dict(context, role='readiness_review'), original, 'write')
            self.assertEqual(review['session_budget']['close_reserve_turns'], 1)
            self.assertEqual(review['session_budget']['close_reserve_seconds'], 1)
            self.assertEqual(review['status'], 'running')
            self.assertIn('Write any required report', review['session_budget']['closing_instruction'])
            self.assertNotIn('handoff', review['session_budget']['closing_instruction'])

    def test_missing_corrupt_and_wrong_session_sidecars_do_not_change_reply(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'budget.json'
            context = {'session_id': 'actor', 'session_budget_path': str(path)}
            reply = {'ok': True}
            for text in (None, '{', '{}', json.dumps({'version': 1, 'session_id': 'other'}),
                         json.dumps({'version': 1, 'session_id': 'actor', 'assistant_turns_used': True,
                                     'assistant_turns_limit': 80, 'session_seconds_limit': 900,
                                     'started_monotonic': 0})):
                if text is not None:
                    path.write_text(text)
                self.assertEqual(decorate(context, reply, 'read'), reply)
            self.assertEqual(decorate({}, reply, 'read'), reply)


class HandoffTests(unittest.TestCase):
    label = 'readiness-fixture'
    receipt_sha = 'a' * 64
    bundle_sha = 'b' * 64

    def events(self, *, call_id='finish', accepted=True, response_session=None, job='job-1', error=False):
        payload = {'accepted': accepted, 'session_id': response_session or self.label, 'job_id': job,
                   'receipt_sha256': self.receipt_sha, 'finalization_identity': self.bundle_sha}
        return [
            {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'toolUse', 'content': []}},
            {'type': 'tool_execution_start', 'toolName': 'rtl_remote', 'toolCallId': 'finish',
             'args': {'action': 'handoff', 'job_id': 'job-1'}},
            {'type': 'tool_execution_end', 'toolName': 'rtl_remote', 'toolCallId': call_id,
             'isError': error, 'result': {'details': payload}},
        ]

    def run_case(self, events, *, role='rtl_readiness', turns=8, status_updates=None,
                 status_delay=0, repair_skill=None, seconds=5):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            context_file = root / 'context.json'
            context_file.write_text(json.dumps({'harness_dir': str(root), 'run_id': 'fixture',
                'role': role, 'scope': 'readiness/campaign-1/work', 'campaign': 'campaign-1',
                'repair_skill': repair_skill,
                'session_budget_path': '/untrusted-inherited-path',
                'readiness_limits': {'assistant_turns': turns, 'session_seconds': seconds}}))
            outer = self
            class Remote(RecordingRemote):
                def call(self, request):
                    if request['action'] == 'rtl_readiness_handoff_status':
                        self.calls.append(dict(request))
                        if status_delay:
                            import time
                            time.sleep(status_delay)
                        answer = {'accepted': True, 'session_id': outer.label, 'job_id': 'job-1',
                                  'receipt_sha256': outer.receipt_sha, 'finalization_identity': outer.bundle_sha}
                        answer.update(status_updates or {})
                        return answer
                    return super().call(request)
            process_state = {'killed': False}
            class Process:
                pid = 424242
                def __init__(self, *args, **kwargs):
                    self.stdin = io.StringIO()
                    self.stdout = io.StringIO('\n'.join(json.dumps(e) for e in events))
                    Path(kwargs['env']['HARNESS_RTL_TOOL_RECEIPT']).write_text(json.dumps(
                        {'checks': [{'active_tools': ['rtl_remote']}], 'blocked': []}))
                def poll(self): return None
                def wait(self): return -15 if process_state['killed'] else 0
            remote = Remote()
            def kill(*args): process_state['killed'] = True
            with patch('harness.adapters.omp_tools.subprocess.Popen', Process), \
                    patch('harness.adapters.omp_tools.os.killpg', side_effect=kill):
                result = run_session('fixture only', 'implementation',
                    {'roles': {'implementation': 'offline-fixture'},
                     'runtime': {'command_template': ['offline-fixture', '{prompt}']}},
                    root, context_file, root, remote, self.label)
            context = json.loads(context_file.read_text())
            sidecar = json.loads(Path(context['session_budget_path']).read_text()) if 'session_budget_path' in context else None
            event_calls = [r['content'] for r in remote.calls if r['action'] == 'append_evidence']
            persisted_events = [json.loads(line) for chunk in event_calls for line in chunk.splitlines()]
            return result, remote, context, sidecar, persisted_events

    def test_verified_explicit_handoff_is_truthful_and_stops_without_extra_turn(self):
        result, remote, context, sidecar, events = self.run_case(self.events())
        self.assertFalse(result['completed'])
        self.assertTrue(result['handoff_complete'])
        self.assertEqual(result['termination_reason'], 'verified_handoff')
        self.assertEqual(result['exit_code'], -15)
        self.assertFalse(result['budget_exhausted'])
        self.assertEqual(result['handoff_job_id'], 'job-1')
        self.assertEqual(result['handoff_receipt_sha256'], self.receipt_sha)
        self.assertEqual(sidecar['assistant_turns_used'], 1)
        self.assertNotEqual(context['session_budget_path'], '/untrusted-inherited-path')
        native = next(r for r in remote.calls if r['action'] == 'rtl_readiness_handoff_status')
        self.assertEqual(native['actor_sha256'], context['session_actor_sha256'])
        self.assertEqual(native['campaign'], 'campaign-1')
        self.assertEqual(events[-1]['type'], 'controller_handoff')
        self.assertEqual(remote.calls[-1]['action'], 'rtl_session_end')
        recorded = json.loads(remote.evidence_files[self.label + '.result.json'])
        self.assertFalse(recorded['completed'])
        self.assertTrue(recorded['handoff_complete'])

    def test_unmatched_failed_wrong_actor_job_or_unverified_response_cannot_finish(self):
        cases = [self.events(call_id='other'), self.events(accepted=False), self.events(error=True),
                 self.events(response_session='other'), self.events(job='other')]
        for stream in cases:
            with self.subTest(stream=stream):
                result, _, _, _, events = self.run_case(stream)
                self.assertNotIn('handoff_complete', result)
                self.assertFalse(result['completed'])
                self.assertNotIn('controller_handoff', [e['type'] for e in events])
        for updates in ({'accepted': False}, {'receipt_sha256': 'c' * 64}, {'job_id': 'other'},
                        {'session_id': 'other'}, {'finalization_identity': 'c' * 64}):
            result, _, _, _, events = self.run_case(self.events(), status_updates=updates)
            self.assertNotIn('handoff_complete', result)
            self.assertNotIn('controller_handoff', [e['type'] for e in events])

    def test_review_and_repair_cannot_request_investigation_handoff(self):
        for role, repair in [('readiness_review', None), ('implementation', 'rtl-implementation-readiness')]:
            result, remote, _, _, _ = self.run_case(self.events(), role=role, repair_skill=repair)
            self.assertNotIn('handoff_complete', result)
            self.assertFalse(any(r['action'] == 'rtl_readiness_handoff_status' for r in remote.calls))

    def test_hard_turn_cap_precedes_handoff_and_retains_budget_failure(self):
        result, remote, _, sidecar, events = self.run_case(self.events(), turns=1)
        self.assertFalse(result['completed'])
        self.assertTrue(result['budget_exhausted'])
        self.assertEqual(result['budget_reason'], 'assistant_turns')
        self.assertNotIn('handoff_complete', result)
        self.assertEqual(sidecar['assistant_turns_used'], 1)
        self.assertFalse(any(r['action'] == 'rtl_readiness_handoff_status' for r in remote.calls))

    def test_wall_budget_expiry_during_native_confirmation_cannot_become_handoff(self):
        result, remote, _, _, events = self.run_case(self.events(), seconds=1, status_delay=1.05)
        self.assertTrue(any(r['action'] == 'rtl_readiness_handoff_status' for r in remote.calls))
        self.assertTrue(result['budget_exhausted'])
        self.assertEqual(result['budget_reason'], 'session_seconds')
        self.assertNotIn('handoff_complete', result)
        self.assertNotIn('controller_handoff', [e['type'] for e in events])

    def test_ordinary_session_remains_without_budget_sidecar(self):
        events = [{'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop', 'content': []}},
                  {'type': 'agent_end'}]
        result, _, context, sidecar, _ = self.run_case(events, role='architecture')
        self.assertTrue(result['completed'])
        self.assertNotIn('session_budget_path', context)
        self.assertIsNone(sidecar)
        self.assertNotIn('budget_exhausted', result)


if __name__ == '__main__':
    unittest.main()
