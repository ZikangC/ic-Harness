"""Tool draining uses real server-worker lifecycles; no models or EDA needed."""
import json,shutil,signal,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from harness.adapters.rtl_remote import wait_for_jobs,PendingJobsInterrupted
from harness.runtime.rtl_worker import dispatch
from harness.tests import test_rtl_remote as fixture
from harness.stages import rtl as stage


class LocalRemote:
 def __init__(self,root,run):self.root=root;self.run=run;self.calls=[]
 def call(self,request):
  self.calls.append(request)
  return dispatch(self.root,self.run,request)


class WorkerWaitTests(unittest.TestCase):
 setUp=fixture.WorkerTests.setUp
 tearDown=fixture.WorkerTests.tearDown
 call=fixture.WorkerTests.call
 wait=fixture.WorkerTests.wait

 def test_natural_completion_and_timeout_keep_actual_result_without_new_jobs(self):
  remote=LocalRemote(self.root,self.runid)
  job=self.call(action='submit',argv=['python','-c','import time; time.sleep(.2)'],timeout_seconds=10)['job_id']
  drained=wait_for_jobs(remote,poll_seconds=.02)
  self.assertTrue(drained['waited']);self.assertEqual(drained['jobs'][0]['job_id'],job);self.assertEqual(drained['jobs'][0]['exit_code'],0)
  self.assertTrue(self.call(action='rtl_review_begin')['candidate_sha256'])
  timeout=self.call(action='submit',argv=['python','-c','import signal,sys,time; signal.signal(signal.SIGTERM,lambda *_:sys.exit(0)); time.sleep(30)'],timeout_seconds=1)['job_id']
  drained=wait_for_jobs(remote,poll_seconds=.02)
  self.assertEqual(drained['jobs'][0]['job_id'],timeout);self.assertNotEqual(drained['jobs'][0]['exit_code'],0);self.assertTrue(drained['jobs'][0]['timed_out'])
  self.assertEqual(len(list((self.out/'jobs').glob('*'))),2)
  self.assertFalse(any(q['action']in('submit','cancel','cancel_all')for q in remote.calls))
  self.assertFalse(any('stdout_tail'in result or 'source_sha256'in result for result in drained['jobs']))

 def test_interrupt_leaves_real_job_running_and_resume_only_waits(self):
  remote=LocalRemote(self.root,self.runid)
  job=self.call(action='submit',argv=['python','-c','import time; time.sleep(.4)'],timeout_seconds=10)['job_id']
  with patch('harness.adapters.rtl_remote.time.sleep',side_effect=KeyboardInterrupt):
   with self.assertRaises(PendingJobsInterrupted):wait_for_jobs(remote)
  self.assertEqual(self.call(action='status',job_id=job,brief=True)['status'],'running')
  self.assertFalse((self.out/'jobs'/job/'result.json').exists())
  self.assertEqual(wait_for_jobs(remote,poll_seconds=.02)['jobs'][0]['exit_code'],0)
  self.assertEqual(len(list((self.out/'jobs').glob('*'))),1)

 def test_pending_query_includes_old_jobs_outside_recent_twenty(self):
  jobs=self.out/'jobs'
  for n in range(25):
   p=jobs/f'{n:03d}';p.mkdir(parents=True)
   (p/'request.json').write_text(json.dumps({'scope':'work','argv':[],'cwd':str(self.out/'work')}))
   if n:(p/'result.json').write_text(json.dumps({'exit_code':0}))
  self.assertNotIn('000',[j['job_id']for j in self.call(action='jobs')['jobs']])
  self.assertEqual(self.call(action='jobs',pending_only=True),{'jobs':[{'job_id':'000','status':'running'}]})
  remote=LocalRemote(self.root,self.runid)
  with patch('harness.adapters.rtl_remote.time.sleep',side_effect=KeyboardInterrupt):
   with self.assertRaises(PendingJobsInterrupted):wait_for_jobs(remote)
  self.assertFalse((jobs/'000/result.json').exists())

 def test_cleanup_failure_blocks_transition_and_wait_without_claiming_completion(self):
  job=self.out/'jobs/1';job.mkdir(parents=True)
  (job/'request.json').write_text(json.dumps({'scope':'work','argv':[],'cwd':str(self.out/'work')}))
  (job/'result.json').write_text(json.dumps({'exit_code':0,'isolation':{'cleanup_complete':False,'container_running':True}}))
  self.assertEqual(self.call(action='status',job_id='1',brief=True)['status'],'cleanup_failed')
  self.assertEqual(self.call(action='status',job_id='1')['status'],'cleanup_failed')
  self.assertEqual(self.call(action='jobs',pending_only=True),{'jobs':[{'job_id':'1','status':'cleanup_failed'}]})
  with self.assertRaisesRegex(ValueError,'unfinished jobs'):self.call(action='rtl_review_begin')
  with patch('harness.adapters.rtl_remote.time.sleep')as sleep:
   with self.assertRaisesRegex(RuntimeError,'cleanup failed'):wait_for_jobs(LocalRemote(self.root,self.runid))
  sleep.assert_not_called()


class StageWaitInterruptTests(unittest.TestCase):
 def test_initial_context_carries_complete_primary_sources_and_fingerprints(self):
  sources={'spec.md':'要求\nfunctional acceptance\n','reference.py':'def run(x):\n return x + 1\n','support.py':'not the primary reference'}
  hashes={name:stage.digest(text.encode())for name,text in sources.items()}
  supplied=json.loads(stage.original_source_context(sources,hashes))
  self.assertEqual(set(supplied),{'spec.md','reference.py'})
  for name in supplied:
   self.assertEqual(supplied[name],{'path':'@input/'+name,'sha256':hashes[name],'total_lines':len(sources[name].splitlines()),'text':sources[name]})

 def test_controller_wait_interrupt_preserves_jobs_and_audit_phase(self):
  self.addCleanup(signal.signal,signal.SIGTERM,signal.getsignal(signal.SIGTERM))
  with tempfile.TemporaryDirectory()as directory:
   root=Path(directory);(root/'input').mkdir();(root/'input/spec.md').write_text('fixture');(root/'input/reference.py').write_text('pass\n')
   harness=root/'harness';skills=harness/'skills'
   for name in ('rtl-implementation','rtl-audit'):
    folder=skills/name;folder.mkdir(parents=True);(folder/'SKILL.md').write_text('fixture guidance')
   (skills/'manifest.json').write_text(json.dumps({'sha256':stage.inventory(skills)}))
   (root/'project.toml').write_text('[project]\nroot="."\ninput_dir="input"\n[remote]\nssh_host="unused"\n[eda]\npython="unused"\nverilator="unused"\nyosys="unused"\nopenroad="unused"\n')
   (root/'rtl.toml').write_text('')
   (root/'models.toml').write_text('[roles]\nimplementation="unused"\nreview="unused"\n[runtime]\ncommand_template=["unused","{prompt}"]\n')
   class Remote:
    def __init__(self):self.calls=[];self.state=None
    def evidence(self,*args):pass
    def call(self,request):
     self.calls.append(request)
     if request['action']=='init':return {'worker':'/server/worker','run_dir':'/server/run','capabilities':{},'reference_path':'/server/reference','input_source_paths':{}}
     if request['action']=='set_status':self.state=dict(request['value']);return {'ok':True}
     if request['action']=='jobs':return {'jobs':[{'job_id':'existing','status':'running'}]}
     raise AssertionError(request['action'])
   remote=Remote();args=SimpleNamespace(config=root/'project.toml',resume=None,run_id='wait-test',task=None,validate_inputs=False,audit_only=False)
   with patch.object(stage,'Remote',return_value=remote),patch.object(stage,'run_session',return_value={'exit_code':0,'tool_isolation_ok':True,'completed':True,'final_text':''})as invoke,patch('harness.adapters.rtl_remote.time.sleep',side_effect=KeyboardInterrupt):
    with self.assertRaises(PendingJobsInterrupted):stage.run(args)
   source_block=invoke.call_args.args[0].split('\nFROZEN ORIGINAL SOURCES:\n',1)[1].split('\nSERVER CONTEXT:\n',1)[0]
   supplied=json.loads(source_block)
   self.assertEqual(supplied['spec.md']['text'],'fixture');self.assertEqual(supplied['reference.py']['text'],'pass\n')
   self.assertEqual(remote.state['status'],'interrupted');self.assertEqual(remote.state['phase'],'audit')
   self.assertFalse(any(q['action']in('cancel','cancel_all','submit')for q in remote.calls))


if __name__=='__main__':unittest.main()
