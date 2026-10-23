"""Transport failures retain action context without retrying uncertain mutations."""
import json,subprocess,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from harness.adapters.rtl_remote import Remote
from harness.adapters import rtl_audit as flow


class TransportTests(unittest.TestCase):
 def setUp(self):
  self.remote=Remote({'_project_name':'fixture','remote':{'container_workspace_root':'/work','host_workspace_root':'/host/work','ssh_host':'fixture-host','container':'fixture-eda','connect_timeout_seconds':9},'eda':{'python':'/opt/eda/bin/python3'}},'fixture-run')

 def test_job_deadline_and_connection_protection_reach_server_unchanged(self):
  request={'action':'submit','argv':['python','fixture.py'],'timeout_seconds':123}
  with patch('harness.adapters.rtl_remote.subprocess.run',return_value=SimpleNamespace(stdout='{"job_id":"1","status":"queued","logs":"/work/fixture/output/run/jobs/1"}',returncode=0))as run,patch.object(self.remote,'host_call',return_value={'ok':True,'job_id':'1','status':'submitted','logs':'/host/work/fixture/output/run/jobs/1'})as launch:
   self.assertEqual(self.remote.call(request),{'ok':True,'job_id':'1','status':'submitted','logs':'/work/fixture/output/run/jobs/1'})
   launch.assert_called_once_with('launch','1')
  args,kwargs=run.call_args
  self.assertEqual(json.loads(kwargs['input']),request)
  self.assertIn('ConnectTimeout=9',args[0]);self.assertIn('ServerAliveInterval=10',args[0]);self.assertIn('ServerAliveCountMax=3',args[0])
  self.assertEqual(run.call_count,1)

 def test_init_binds_described_backend_and_frozen_helper_uses_host_mapping(self):
  isolation={'backend':'docker_readonly_job_v1','image_id':'fixture-image'}
  replies=[SimpleNamespace(stdout=json.dumps({'ok':True,'job_isolation':isolation}),returncode=0),SimpleNamespace(stdout='{"run_dir":"/work/fixture/output/fixture-run"}',returncode=0)]
  with patch('harness.adapters.rtl_remote.subprocess.run',side_effect=replies)as run:
   self.remote.call({'action':'init','resume':False})
  describe,init=run.call_args_list
  self.assertEqual(json.loads(describe.kwargs['input'])['action'],'describe')
  self.assertIn('/host/work/fixture/harness/runtime/rtl_job_host.py',describe.args[0][-1])
  self.assertEqual(json.loads(init.kwargs['input'])['job_isolation'],isolation)
  self.remote.worker='/work/fixture/output/fixture-run/snapshot/harness/runtime/rtl_worker.py'
  with patch('harness.adapters.rtl_remote.subprocess.run',return_value=SimpleNamespace(stdout='{"ok":true,"job_id":"1","status":"submitted"}',returncode=0))as run:
   self.remote.host_call('launch','1')
  self.assertIn('/host/work/fixture/output/fixture-run/snapshot/harness/runtime/rtl_job_host.py',run.call_args.args[0][-1])
  self.assertNotIn('timeout',run.call_args.kwargs)

 def test_launch_reply_loss_never_requeues_or_repeats_a_job(self):
  with patch.object(self.remote,'_worker_call',return_value={'job_id':'17','status':'queued'})as queue,patch.object(self.remote,'host_call',side_effect=ConnectionError('outcome unknown'))as launch:
   with self.assertRaises(ConnectionError):self.remote.call({'action':'submit','argv':['python','probe.py']})
  queue.assert_called_once();launch.assert_called_once_with('launch','17')

 def test_cancel_is_routed_to_host_and_status_remains_actual(self):
  with patch.object(self.remote,'host_call',return_value={'status':'cancel_requested'})as cancel,patch.object(self.remote,'_worker_call',return_value={'job_id':'17','status':'running'})as status:
   self.assertEqual(self.remote.call({'action':'cancel','job_id':'17'})['status'],'running')
  cancel.assert_called_once_with('cancel','17');status.assert_called_once_with({'action':'status','job_id':'17'})

 def test_uncertain_transport_failure_is_sanitized_and_never_retried(self):
  secret='private-transport-content'
  request={'action':'rtl_review_begin','content':secret}
  for error in [OSError(secret),subprocess.TimeoutExpired(['ssh',secret],60,output=secret,stderr=secret)]:
   with self.subTest(error=type(error).__name__):
    with patch('harness.adapters.rtl_remote.subprocess.run',side_effect=error)as run:
     with self.assertRaises(ConnectionError)as caught:self.remote.call(request)
    message=str(caught.exception)
    self.assertIn('action=rtl_review_begin',message);self.assertIn('outcome unknown',message);self.assertIn(type(error).__name__,message)
    self.assertNotIn(secret,message);self.assertTrue(caught.exception.__suppress_context__)
    self.assertEqual(run.call_count,1)

 def test_lost_reply_keeps_side_effect_and_does_not_repeat_mutation(self):
  effects=[]
  def lost_reply(*args,**kwargs):
   effects.append(json.loads(kwargs['input'])['action'])
   return SimpleNamespace(stdout='sensitive truncated reply',stderr='sensitive error',returncode=255)
  with patch('harness.adapters.rtl_remote.subprocess.run',side_effect=lost_reply)as run:
   with self.assertRaises(ConnectionError)as caught:self.remote.call({'action':'rtl_repair_end'})
  self.assertEqual(effects,['rtl_repair_end']);self.assertEqual(run.call_count,1)
  self.assertIn('action=rtl_repair_end',str(caught.exception));self.assertIn('outcome unknown',str(caught.exception));self.assertNotIn('sensitive',str(caught.exception))

 def test_interruption_propagates_without_repeating_request(self):
  with patch('harness.adapters.rtl_remote.subprocess.run',side_effect=KeyboardInterrupt)as run:
   with self.assertRaises(KeyboardInterrupt):self.remote.call({'action':'rtl_review_begin'})
  self.assertEqual(run.call_count,1)

 def test_unknown_action_data_is_not_copied_into_failure_report(self):
  secret='private-action\ncontent'
  for action in [secret,{'private':secret}]:
   with self.subTest(action_type=type(action).__name__):
    with patch('harness.adapters.rtl_remote.subprocess.run',side_effect=OSError('unavailable'))as run:
     with self.assertRaises(ConnectionError)as caught:self.remote.call({'action':action})
    self.assertIn('action=unknown',str(caught.exception));self.assertNotIn('private',str(caught.exception));self.assertEqual(run.call_count,1)

 def test_receipt_transport_failure_propagates_instead_of_becoming_missing(self):
  review={'review':'review-1','candidate_sha256':'candidate','decision':{'report_sha256':'report'}}
  for reply in [OSError('unavailable'),SimpleNamespace(stdout='truncated reply',returncode=255),SimpleNamespace(stdout='{"text":"incomplete reply"}',returncode=255)]:
   with self.subTest(reply=type(reply).__name__):
    options={'side_effect':reply}if isinstance(reply,Exception)else{'return_value':reply}
    with patch('harness.adapters.rtl_remote.subprocess.run',**options)as run:
     with self.assertRaises(ConnectionError):flow._receipt(self.remote,review,'repair')
    self.assertEqual(run.call_count,1)
  # A genuine server-side missing file remains the coordinator's missing marker.
  with patch('harness.adapters.rtl_remote.subprocess.run',return_value=SimpleNamespace(stdout='{"error":"FileNotFoundError: [Errno 2] No such file or directory: receipt"}',returncode=1))as run:
   self.assertIsNone(flow._receipt(self.remote,review,'repair'))
  self.assertEqual(run.call_count,1)
  with patch('harness.adapters.rtl_remote.subprocess.run',return_value=SimpleNamespace(stdout='{"error":"receipt integrity failure"}',returncode=1))as run:
   with self.assertRaisesRegex(RuntimeError,'integrity failure'):flow._receipt(self.remote,review,'repair')
  self.assertEqual(run.call_count,1)
  receipt={'candidate_sha256':'candidate','decision_report_sha256':'report','completed':True}
  response=json.dumps({'text':'1: '+json.dumps(receipt)})
  with patch('harness.adapters.rtl_remote.subprocess.run',return_value=SimpleNamespace(stdout=response,returncode=0))as run:
   self.assertEqual(flow._receipt(self.remote,review,'repair'),receipt)
  self.assertEqual(run.call_count,1)


if __name__=='__main__':unittest.main()
