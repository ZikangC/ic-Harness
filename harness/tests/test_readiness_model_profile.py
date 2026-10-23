"""Offline role boundaries and watchdogs; no provider or remote execution."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from hashlib import sha256
from unittest.mock import patch

from harness.adapters.omp_tools import run_session, _readiness_limits
from harness.runtime import rtl_bridge


class RecordingRemote:
    def __init__(self):
        self.calls = []
        self.evidence_files = {}

    def evidence(self, name, content):
        self.evidence_files[name] = content

    def call(self, request):
        self.calls.append(request.copy())
        if request['action'] == 'rtl_session_begin':
            label = request['session_id']
            actor = dict(version=1, run_id='fixture', session_id=label, label=label,
                         role=request['role'], scope=request['scope'], model_role=request['model_role'],
                         prompt_sha256=sha256(self.evidence_files[label + '.prompt.txt'].encode()).hexdigest())
            actor.update({key: request[key] for key in ('diagnosis_observation_id','diagnosis_fingerprint') if key in request})
            return dict(registered=True, actor=actor, actor_path='control/sessions/' + label + '.json',
                        actor_sha256=sha256((json.dumps(actor, ensure_ascii=False, indent=2) + '\n').encode()).hexdigest())
        return {'ok': True}


class ReadinessProfileTests(unittest.TestCase):
    def test_bridge_rejects_all_native_and_architecture_actions_for_both_roles(self):
        with tempfile.TemporaryDirectory() as name:
            context = Path(name) / 'context.json'
            for profile in ({'role': 'rtl_readiness'}, {'role': 'readiness_review'},
                            {'role': 'implementation', 'repair_skill': 'rtl-implementation-readiness'}):
                role = profile['role']
                context.write_text(json.dumps(dict(profile, environment={}, run_id='fixture', worker='worker',
                                                  scope='readiness/campaign-1/work')))
                for action in ('evaluate', 'proposal', 'architecture_revision', 'rtl_request_revision',
                               'rtl_finish_review', 'rtl_readiness_complete', 'rtl_readiness_begin', 'rtl_readiness_resolve', 'cancel_all'):
                    with self.subTest(role=role, action=action), patch.dict(os.environ, HARNESS_RTL_CONTEXT=str(context)), \
                            patch.object(rtl_bridge, 'Remote') as remote, \
                            patch.object(rtl_bridge.sys, 'stdin', io.StringIO(json.dumps({'action': action}))):
                        with self.assertRaisesRegex(ValueError, 'only scoped remote'):
                            rtl_bridge.main()
                        remote.return_value.call.assert_not_called()

    def test_bridge_overrides_caller_scope_for_allowed_file_and_job_actions(self):
        with tempfile.TemporaryDirectory() as name:
            context = Path(name) / 'context.json'
            scope = 'readiness/campaign-1/audit'
            context.write_text(json.dumps(dict(role='readiness_review', environment={}, run_id='fixture',
                                              worker='worker', scope=scope)))
            for action in ('list', 'read', 'write', 'replace', 'submit', 'status', 'cancel', 'jobs'):
                with self.subTest(action=action), patch.dict(os.environ, HARNESS_RTL_CONTEXT=str(context)), \
                        patch.object(rtl_bridge, 'Remote') as remote, \
                        patch.object(rtl_bridge.sys, 'stdin', io.StringIO(json.dumps({'action': action, 'scope': 'work'}))), \
                        contextlib.redirect_stdout(io.StringIO()):
                    remote.return_value.call.return_value = {'ok': True}
                    rtl_bridge.main()
                    self.assertEqual(remote.return_value.call.call_args.args[0]['scope'], scope)

    def test_readiness_repair_bridge_preserves_trusted_work_scope_and_review_binding(self):
        with tempfile.TemporaryDirectory() as name:
            context = Path(name) / 'context.json'
            context.write_text(json.dumps(dict(role='implementation', repair_skill='rtl-implementation-readiness',
                                               environment={}, run_id='fixture', worker='worker',
                                               scope='work', review='review-3')))
            with patch.dict(os.environ, HARNESS_RTL_CONTEXT=str(context)), \
                    patch.object(rtl_bridge, 'Remote') as remote, \
                    patch.object(rtl_bridge.sys, 'stdin', io.StringIO(json.dumps(
                        {'action': 'write', 'scope': 'audit/review-99', 'review': 'review-99',
                         'path': 'rtl/top.sv', 'content': 'synthetic source'}))), \
                    contextlib.redirect_stdout(io.StringIO()):
                remote.return_value.call.return_value = {'ok': True}
                rtl_bridge.main()
            self.assertEqual(remote.return_value.call.call_args.args[0]['scope'], 'work')
            self.assertEqual(remote.return_value.call.call_args.args[0]['review'], 'review-3')

    @unittest.skipUnless(shutil.which('node'), 'Node required for actual extension registration')
    def test_extension_registers_only_remote_and_restricts_its_action_schema(self):
        extension = Path(__file__).parents[1] / 'adapters/rtl_tools.js'
        script = '''
import fs from 'node:fs';
const extension=await import(process.argv[1]);
const tools=[],hooks={};let active=[];
const pi={registerTool:t=>tools.push(t),on:(n,f)=>hooks[n]=f,
 setActiveTools:async n=>{active=n},getActiveTools:()=>active};
extension.default(pi);await hooks.session_start();
const blocked=await hooks.tool_call({toolName:'rtl_revise_architecture',toolCallId:'blocked'});
console.log(JSON.stringify({names:tools.map(t=>t.name),actions:tools[0].parameters.properties.action.enum,active,blocked}));
'''
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for profile, expected in [({'role': 'rtl_readiness'}, ['rtl_remote']),
                                      ({'role': 'readiness_review'}, ['rtl_remote']),
                                      ({'role': 'implementation', 'repair_skill': 'rtl-implementation-readiness'}, ['rtl_remote']),
                                      ({'role': 'implementation'}, ['rtl_remote', 'rtl_revise_architecture']),
                                      ({'role': 'rtl_audit'}, ['rtl_remote', 'rtl_request_revision', 'rtl_finish_review'])]:
                role = profile['role']
                context = root / 'context.json'
                context.write_text(json.dumps(profile))
                result = subprocess.run(['node', '--input-type=module', '-e', script, extension.as_uri()],
                                        env=dict(os.environ, HARNESS_RTL_CONTEXT=str(context),
                                                 HARNESS_RTL_TOOL_RECEIPT=str(root / 'receipt.json')),
                                        text=True, capture_output=True, check=True)
                observed = json.loads(result.stdout)
                self.assertEqual(observed['names'], expected)
                self.assertEqual(observed['active'], expected)
                if expected == ['rtl_remote']:
                    self.assertTrue(observed['blocked']['block'])
                    self.assertNotIn('evaluate', observed['actions'])
                    self.assertNotIn('proposal', observed['actions'])
                    self.assertEqual('handoff' in observed['actions'], role == 'rtl_readiness')
                else:
                    self.assertIn('evaluate', observed['actions'])

    def run_offline_session(self, body, *, seconds=5, turns=3, role='rtl_readiness', repair_skill=None):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            executable = root / 'event-fixture'
            executable.write_text('#!/usr/bin/env python3\nimport json,os,sys,time\nfrom pathlib import Path\n'
                                  'sys.stdin.read()\n'
                                  'Path(os.environ["HARNESS_RTL_TOOL_RECEIPT"]).write_text(json.dumps({"checks":[{"active_tools":["rtl_remote"]}],"blocked":[]}))\n'
                                  + body)
            executable.chmod(0o700)
            context = root / 'context.json'
            context.write_text(json.dumps(dict(harness_dir=str(root), run_id='fixture', role=role,
                                               scope='work' if repair_skill else 'readiness/campaign-1/work',
                                               repair_skill=repair_skill, readiness_limits=dict(session_seconds=seconds, assistant_turns=turns))))
            if role == 'readiness_diagnosis':
                value=json.loads(context.read_text());value.update(diagnosis_observation_id='observation-fixture',diagnosis_fingerprint='fixture');context.write_text(json.dumps(value))
            remote = RecordingRemote()
            result = run_session('fixture only', 'implementation',
                                 {'roles': {'implementation': 'offline-fixture'},
                                  'runtime': {'command_template': [str(executable), '{prompt}']}},
                                 root, context, root, remote, 'readiness-fixture')
            self.assertEqual(remote.calls[-1]['action'], 'rtl_session_end')
            self.assertIn('readiness-fixture.result.json', remote.evidence_files)
            return result

    def test_readiness_role_completes_with_remote_only_isolation(self):
        result = self.run_offline_session('print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop","content":[]}}),flush=True)\nprint(json.dumps({"type":"agent_end"}),flush=True)\n')
        self.assertTrue(result['completed'])
        self.assertTrue(result['tool_isolation_ok'])
        self.assertFalse(result['budget_exhausted'])

    def test_readiness_repair_uses_existing_implementation_role_with_restricted_profile(self):
        result = self.run_offline_session(
            'print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop","content":[]}}),flush=True)\nprint(json.dumps({"type":"agent_end"}),flush=True)\n',
            role='implementation', repair_skill='rtl-implementation-readiness')
        self.assertTrue(result['completed'])
        self.assertTrue(result['tool_isolation_ok'])
        self.assertEqual(result['role'], 'implementation')
        self.assertEqual(result['readiness_limits'], {'session_seconds': 5, 'assistant_turns': 3})

    def test_readiness_repair_silent_stream_has_same_watchdog(self):
        result = self.run_offline_session('time.sleep(30)\n', seconds=1,
                                          role='implementation', repair_skill='rtl-implementation-readiness')
        self.assertFalse(result['completed'])
        self.assertEqual(result['budget_reason'], 'session_seconds')
        self.assertTrue(result['tool_isolation_ok'])

    def test_readiness_repair_turn_budget_applies_to_implementation_role(self):
        result = self.run_offline_session(
            'for n in range(20):\n print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"toolUse","content":[]}}),flush=True)\n time.sleep(.05)\ntime.sleep(30)\n',
            turns=2, role='implementation', repair_skill='rtl-implementation-readiness')
        self.assertEqual(result['assistant_turns'], 2)
        self.assertEqual(result['budget_reason'], 'assistant_turns')
        self.assertFalse(result['completed'])

    def test_readiness_repair_marker_uses_investigation_limits_and_requires_budget(self):
        context = {'role': 'implementation', 'repair_skill': 'rtl-implementation-readiness'}
        with self.assertRaises(ValueError):
            _readiness_limits(context)
        context['settings'] = {'readiness': {'session_seconds': 11, 'assistant_turns': 5,
                                             'review_seconds': 19, 'review_turns': 7}}
        self.assertEqual(_readiness_limits(context), {'session_seconds': 11, 'assistant_turns': 5})

    def test_silent_stream_is_stopped_by_session_watchdog(self):
        started = time.monotonic()
        result = self.run_offline_session('time.sleep(30)\n', seconds=1)
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(result['completed'])
        self.assertEqual(result['budget_reason'], 'session_seconds')
        self.assertTrue(result['tool_isolation_ok'])

    def test_turn_cap_stops_before_next_model_turn_and_records_failure(self):
        result = self.run_offline_session('for n in range(20):\n print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"toolUse","content":[]}}),flush=True)\n time.sleep(.05)\ntime.sleep(30)\n', turns=2)
        self.assertEqual(result['assistant_turns'], 2)
        self.assertEqual(result['budget_reason'], 'assistant_turns')
        self.assertFalse(result['completed'])

    def test_only_readiness_roles_require_positive_watchdog_settings(self):
        self.assertIsNone(_readiness_limits({'role': 'implementation', 'readiness_limits': {'session_seconds': -1}}))
        for role in ('rtl_readiness', 'readiness_review'):
            with self.assertRaises(ValueError):
                _readiness_limits({'role': role})
        self.assertEqual(_readiness_limits({'role': 'readiness_review', 'settings': {'readiness': {'review_seconds': 9, 'review_turns': 4}}}),
                         {'session_seconds': 9, 'assistant_turns': 4})


if __name__ == '__main__':
    unittest.main()
