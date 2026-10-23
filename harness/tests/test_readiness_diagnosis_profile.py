"""Read-only diagnosis boundaries through actual extension and bridge dispatch."""
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
from unittest.mock import patch
from harness.runtime import rtl_bridge
from harness.runtime.rtl_session_budget import decorate, write_budget
from harness.adapters.omp_tools import _readiness_limits
from harness.tests import test_readiness_model_profile as model_profile


class DiagnosisProfileTests(unittest.TestCase):
    def test_bridge_cannot_mutate_submit_cancel_or_publish_even_outside_schema(self):
        with tempfile.TemporaryDirectory() as name:
            context=Path(name)/'context.json'
            context.write_text(json.dumps(dict(role='readiness_diagnosis', environment={}, run_id='fixture', worker='worker', scope='work')))
            for action in ('write','replace','submit','cancel','cancel_all','handoff','evaluate','proposal','architecture_revision',
                           'rtl_request_revision','rtl_finish_review','rtl_diagnosis_complete','rtl_diagnosis_admission'):
                with self.subTest(action=action), patch.dict(os.environ,HARNESS_RTL_CONTEXT=str(context)), \
                        patch.object(rtl_bridge,'Remote') as remote, \
                        patch.object(rtl_bridge.sys,'stdin',io.StringIO(json.dumps({'action':action}))):
                    with self.assertRaisesRegex(ValueError,'read-only diagnosis'):
                        rtl_bridge.main()
                    remote.return_value.call.assert_not_called()

    def test_allowed_reads_use_trusted_scope_and_no_job_wait_loop(self):
        with tempfile.TemporaryDirectory() as name:
            context=Path(name)/'context.json'
            context.write_text(json.dumps(dict(role='readiness_diagnosis', environment={},run_id='fixture',worker='worker',scope='work')))
            for action in ('list','read','status','jobs'):
                with self.subTest(action=action), patch.dict(os.environ,HARNESS_RTL_CONTEXT=str(context)), \
                        patch.object(rtl_bridge,'Remote') as remote, patch.object(rtl_bridge,'readiness_status') as wait, \
                        patch.object(rtl_bridge.sys,'stdin',io.StringIO(json.dumps({'action':action,'scope':'other'}))), \
                        contextlib.redirect_stdout(io.StringIO()):
                    remote.return_value.call.return_value={'ok':True}
                    rtl_bridge.main()
                    self.assertEqual(remote.return_value.call.call_args.args[0]['scope'],'work')
                    remote.return_value.call.assert_called_once()
                    wait.assert_not_called()

    @unittest.skipUnless(shutil.which('node'),'Node required for actual extension registration')
    def test_extension_exposes_only_four_read_actions(self):
        extension=Path(__file__).parents[1]/'adapters/rtl_tools.js'
        script='''
const {default:extension}=await import(process.argv[1]);
let active=[];const tools=[],hooks={};
extension({registerTool:t=>tools.push(t),on:(n,f)=>hooks[n]=f,setActiveTools:async n=>{active=n},getActiveTools:()=>active});
await hooks.session_start();
const blocked=await hooks.tool_call({toolName:'bash',toolCallId:'no-shell'});
console.log(JSON.stringify({names:tools.map(t=>t.name),actions:tools[0].parameters.properties.action.enum,active,blocked}));
'''
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);context=root/'context.json';context.write_text('{"role":"readiness_diagnosis"}')
            result=subprocess.run(['node','--input-type=module','-e',script,extension.as_uri()],capture_output=True,text=True,check=True,
                                  env=dict(os.environ,HARNESS_RTL_CONTEXT=str(context),HARNESS_RTL_TOOL_RECEIPT=str(root/'receipt.json')))
            value=json.loads(result.stdout)
            self.assertEqual(value['names'],['rtl_remote']);self.assertEqual(value['active'],['rtl_remote'])
            self.assertEqual(set(value['actions']),{'list','read','status','jobs'});self.assertTrue(value['blocked']['block'])

    def test_budget_feedback_closes_with_ordinary_final_and_natural_session_can_finish(self):
        with tempfile.TemporaryDirectory() as name:
            path=Path(name)/'budget.json'
            write_budget(path,'diagnosis',dict(session_seconds=50,assistant_turns=10),9,time.monotonic())
            value=decorate(dict(role='readiness_diagnosis',session_id='diagnosis',session_budget_path=str(path)),{},'read')
            self.assertEqual(value['session_budget']['phase'],'closing')
            self.assertIn('final diagnosis JSON',value['session_budget']['closing_instruction'])
        helper=model_profile.ReadinessProfileTests()
        result=helper.run_offline_session('print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop","content":[{"type":"text","text":"{\\\"advice\\\":\\\"continue\\\"}"}]}}),flush=True)\nprint(json.dumps({"type":"agent_end"}),flush=True)\n',role='readiness_diagnosis')
        self.assertTrue(result['completed']);self.assertTrue(result['tool_isolation_ok']);self.assertFalse(result.get('handoff_complete',False))
        self.assertEqual(_readiness_limits({'role':'readiness_diagnosis','readiness_limits':{'session_seconds':30,'assistant_turns':5}}),{'session_seconds':30,'assistant_turns':5})
