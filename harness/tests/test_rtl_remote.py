"""Observable backend invariants; all jobs in an isolated server temporary project."""
import hashlib,json,os,subprocess,sys,tempfile,time,unittest,shutil
from pathlib import Path
from harness.runtime.rtl_worker import dispatch,inventory

JOB_ENVIRONMENT=('HARNESS_TEST_PROJECT_DIR','HARNESS_TEST_HOST_WORKSPACE_ROOT','HARNESS_TEST_CONTAINER_WORKSPACE_ROOT','HARNESS_TEST_CONTROL_CONTAINER')

def host_payload(test,action,job_id=None,run_id=None):
 run_id=run_id or test.runid
 container_workspace=Path(os.environ['HARNESS_TEST_CONTAINER_WORKSPACE_ROOT'])
 host_project=Path(os.environ['HARNESS_TEST_HOST_WORKSPACE_ROOT'])/test.root.relative_to(container_workspace)
 value={'action':action,'control_container':os.environ['HARNESS_TEST_CONTROL_CONTAINER'],
        'host_project_root':str(host_project),'container_project_root':str(test.root),
        'host_workspace_root':os.environ['HARNESS_TEST_HOST_WORKSPACE_ROOT'],
        'container_workspace_root':str(container_workspace),
        'worker':str(test.root/'output'/run_id/'snapshot/harness/runtime/rtl_worker.py'),
        'eda_python':test.eda_python,'run_id':run_id}
 if job_id is not None:value['job_id']=job_id
 return value

def host_call(test,action,job_id=None,run_id=None):
 if not test.job_environment:test.skipTest('actual jobs require server Docker fixture environment: '+', '.join(test.missing_job_environment))
 run_id=run_id or test.runid
 payload=host_payload(test,action,job_id,run_id)
 helper=(test.root/'harness/runtime/rtl_job_host.py'if action=='describe'else Path(payload['host_project_root'])/'output'/run_id/'snapshot/harness/runtime/rtl_job_host.py')
 result=subprocess.run([sys.executable,'-B',str(helper)],input=json.dumps(payload).encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
 if result.returncode:
  raise AssertionError('host '+action+' failed: '+result.stdout.decode(errors='replace')[-2000:]+result.stderr.decode(errors='replace')[-2000:])
 response=json.loads(result.stdout)
 if response.get('ok')is not True:raise AssertionError('host '+action+' did not succeed: '+repr(response))
 return response

def synthetic_parent_binding(test):
 """Explicit test actor, pinned once from its original registration response."""
 key=(str(test.root),test.runid)
 if not hasattr(test,'synthetic_parents'):test.synthetic_parents={}
 if key not in test.synthetic_parents:
  label='fixture-parent-'+test.runid
  dispatch(test.root,test.runid,{'action':'put_evidence','path':'control/'+label+'.prompt.txt','content':'Synthetic component parent; no model invocation.\n'})
  actor=dispatch(test.root,test.runid,{'action':'rtl_session_begin','session_id':label,'role':'implementation','scope':'work','model_role':'implementation'})
  test.synthetic_parents[key]={'parent_session_id':label,'parent_actor_sha256':actor['actor_sha256']}
 return dict(test.synthetic_parents[key])

class WorkerTests(unittest.TestCase):
 def setUp(self):
  self.missing_job_environment=[name for name in JOB_ENVIRONMENT if not os.environ.get(name)];self.job_environment=not self.missing_job_environment;self.owned_jobs=[]
  self.tmp=tempfile.TemporaryDirectory(prefix='rtl-worker-test-',dir=os.environ.get('HARNESS_TEST_PROJECT_DIR'));self.root=Path(self.tmp.name).resolve();self.runid='rtl-test';h=self.root/'harness';(h/'runtime').mkdir(parents=True);(h/'runtime/__init__.py').touch();(h/'__init__.py').touch()
  for name in ('input_bundle.py','rtl_worker.py','rtl_job_host.py','rtl_readback.py','rtl_readback_store.py','rtl_readiness.py'):shutil.copyfile(Path(__file__).parents[1]/'runtime'/name,h/'runtime'/name)
  self.eda_python=os.environ.get('HARNESS_TEST_EDA_PYTHON','/opt/eda/bin/python3')if self.job_environment else sys.executable
  self.input=self.root/'input';self.input.mkdir();(self.input/'spec.md').write_text('Test specification');(self.input/'reference.py').write_text('x=1\n')
  b=self.root/'result/architecture';(b/'input').mkdir(parents=True)
  for n in ('architecture.md','SOURCE.md'):(b/n).write_text('Test frozen baseline')
  for n,data in [('design.json',{'requirements':{'x':1}}),('budgets.json',{'ok':True}),('review.json',{}),('decision.json',{'deliverable':True})]:(b/n).write_text(json.dumps(data))
  for n in ('spec.md','reference.py'):shutil.copyfile(self.input/n,b/'input'/n)
  isolation=host_call(self,'describe')['job_isolation']if self.job_environment else{'backend':'docker_readonly_job_v1','uid':os.getuid(),'gid':os.getgid(),'metadata_fixture_only':True}
  self.req={'action':'init','resume':False,'input_sha256':{n:hashlib.sha256((self.input/n).read_bytes()).hexdigest()for n in ('spec.md','reference.py')},'harness_sha256':inventory(h),'settings':{'tools':{'python':self.eda_python}},'models':{'roles':{}},'job_isolation':isolation}
  dispatch(self.root,self.runid,self.req);self.out=self.root/'output'/self.runid
 def tearDown(self):
  errors=[]
  pending=[(run_id,job_id)for run_id,job_id in self.owned_jobs if not(self.root/'output'/run_id/'jobs'/job_id/'result.json').is_file()]
  for run_id,job_id in pending:
   try:host_call(self,'cancel',job_id,run_id)
   except Exception as error:errors.append(str(error))
  deadline=time.monotonic()+60
  while any(not(self.root/'output'/run_id/'jobs'/job_id/'result.json').is_file()for run_id,job_id in pending)and time.monotonic()<deadline:time.sleep(.1)
  for run_id,job_id in self.owned_jobs:
   result=self.root/'output'/run_id/'jobs'/job_id/'result.json'
   if not result.is_file():errors.append('owned job still pending: '+run_id+'/'+job_id);continue
   value=json.loads(result.read_text())
   if value.get('cleanup_complete')is not True or value.get('container_running')is not False:errors.append('owned job cleanup not confirmed: '+run_id+'/'+job_id)
  if errors:
   self.tmp._finalizer.detach()
   self.fail('fixture retained at '+str(self.root)+'; '+ '; '.join(errors))
  if os.environ.get('HARNESS_TEST_PRESERVE_FIXTURES')=='1':self.tmp._finalizer.detach()
  else:self.tmp.cleanup()
 def call(self,**req):
  if req.get('action')=='revision_begin'and'parent_session_id'not in req:req.update(synthetic_parent_binding(self))
  if req.get('action')=='submit':
   if not self.job_environment:self.skipTest('actual jobs require server Docker fixture environment: '+', '.join(self.missing_job_environment))
   value=dispatch(self.root,self.runid,req)
   self.assertEqual(value['status'],'queued');self.owned_jobs.append((self.runid,value['job_id']))
   host_call(self,'launch',value['job_id']);return value
  if req.get('action')=='cancel':
   self.assertIn((self.runid,req['job_id']),self.owned_jobs)
   return host_call(self,'cancel',req['job_id'])
  return dispatch(self.root,self.runid,req)
 def wait(self,j):
  deadline=time.monotonic()+60
  while time.monotonic()<deadline:
   r=self.call(action='status',job_id=j)
   if r['status']=='completed':return r
   time.sleep(.1)
  self.fail('job did not complete')
 def test_versions_and_paths(self):
  a=self.call(action='write',path='a.py',content='print(1)\n',base_sha256=None)
  with self.assertRaises(ValueError):self.call(action='write',path='a.py',content='bad',base_sha256=None)
  self.call(action='replace',path='a.py',old='print(1)',content='print(2)',base_sha256=a['sha256'])
  self.assertIn('print(2)',self.call(action='read',path='a.py')['text'])
  with self.assertRaises(ValueError):self.call(action='write',path='../input/spec.md',content='bad',base_sha256=None)
  (self.out/'work/link').symlink_to(self.input/'spec.md')
  with self.assertRaises(ValueError):self.call(action='write',path='link',content='bad',base_sha256=None)
 def test_async_exit_and_evidence(self):
  self.call(action='write',path='fail.py',content='import sys\nprint("actual failure")\nsys.exit(7)\n',base_sha256=None)
  j=self.call(action='submit',argv=['python','fail.py'],timeout_seconds=10)['job_id'];r=self.wait(j)
  self.assertEqual(r['exit_code'],7);self.assertIn('actual failure',r['stdout_tail']);self.assertIn('fail.py',r['source_sha256'])
 def test_identity_and_unplugged_revision(self):
  self.assertTrue(dispatch(self.root,self.runid,dict(self.req,resume=True))['run_dir'])
  rev=self.call(action='revision_begin',reason='test',evidence=[],proposed_change='local detail')
  # Optional skills are absent; this fixture has no fresh complete main packet.
  # Do not fabricate calculator/review evidence merely to preserve old adoption.
  with self.assertRaisesRegex(ValueError,'missing required file'):self.call(action='revision_adopt',revision=rev['revision'])
  self.assertEqual(json.loads((self.out/'architecture-current.json').read_text())['path'],'snapshot/architecture')
  (self.out/'snapshot/input/spec.md').write_text('changed')
  with self.assertRaises(ValueError):self.call(action='list')
 def test_timeout_and_cancel(self):
  self.call(action='write',path='sleep.py',content='import time\nfrom pathlib import Path\nPath("started").write_text("ready")\ntime.sleep(30)\n',base_sha256=None)
  j=self.call(action='submit',argv=['python','sleep.py'],timeout_seconds=1)['job_id'];r=self.wait(j);self.assertTrue(r['timed_out']);self.assertNotEqual(r['exit_code'],0)
  if(self.out/'work/started').exists():(self.out/'work/started').unlink()
  j=self.call(action='submit',argv=['python','sleep.py'],timeout_seconds=30)['job_id']
  deadline=time.monotonic()+30
  while not(self.out/'work/started').is_file()and time.monotonic()<deadline:time.sleep(.1)
  self.assertTrue((self.out/'work/started').is_file(),'cancel test target did not start')
  self.call(action='cancel',job_id=j);cancelled=self.wait(j);self.assertTrue(cancelled['cancelled']);self.assertNotEqual(cancelled['exit_code'],0)

class RevisionBindingTests(WorkerTests):
 def test_requirements_cannot_change_without_design_skill(self):
  rev=self.call(action='revision_begin',reason='test',evidence=[],proposed_change='test')
  p=self.out/rev['candidate_scope']/'design.json';p.write_text(json.dumps({'requirements':{'x':2}}))
  with self.assertRaisesRegex(ValueError,'original requirements'):self.call(action='revision_adopt',revision=rev['revision'])
 def test_review_requires_cited_current_execution(self):
  skill=self.root/'harness/skills/microarchitecture-audit/SKILL.md';skill.parent.mkdir(parents=True);skill.write_text('test review capability')
  self.runid='rtl-audit-test';dispatch(self.root,self.runid,dict(self.req,harness_sha256=inventory(self.root/'harness')));self.out=self.root/'output'/self.runid
  rev=self.call(action='revision_begin',reason='test',evidence=[],proposed_change='test');scope=rev['audit_scope'];candidate=self.out/rev['candidate_scope'];cid=self.call(action='proposal',scope=scope)
  self.call(action='write',scope=scope,path='probe.py',content='assert 2+2==4\n',base_sha256=None)
  job=self.call(action='submit',scope=scope,argv=['python','probe.py'],timeout_seconds=10)['job_id'];self.assertEqual(self.wait(job)['exit_code'],0)
  review={'verdict':'accept','design_sha256':__import__('hashlib').sha256((candidate/'design.json').read_bytes()).hexdigest(),'candidate_sha256':cid['candidate_sha256'],'evidence':[]}
  rp=self.out/scope/'review.json';rp.write_text(json.dumps(review))
  with self.assertRaisesRegex(ValueError,'independent evidence'):self.call(action='revision_adopt',revision=rev['revision'])
  review['evidence']=['nonexistent'];rp.write_text(json.dumps(review))
  with self.assertRaisesRegex(ValueError,'independent evidence'):self.call(action='revision_adopt',revision=rev['revision'])
  self.call(action='write',scope=scope,path='timeout.py',content='import signal,time,sys\nsignal.signal(signal.SIGTERM,lambda *_:sys.exit(0))\ntime.sleep(30)\n',base_sha256=None)
  timeout_job=self.call(action='submit',scope=scope,argv=['python','timeout.py'],timeout_seconds=1)['job_id'];timed=self.wait(timeout_job);self.assertNotEqual(timed['exit_code'],0);self.assertTrue(timed['timed_out'])
  # Docker timeout termination is nonzero; a timed-out receipt cannot authorize acceptance.
  review['evidence']=[timeout_job];rp.write_text(json.dumps(review))
  with self.assertRaisesRegex(ValueError,'independent evidence'):self.call(action='revision_adopt',revision=rev['revision'])
  review['evidence']=[job];rp.write_text(json.dumps(review));original=(candidate/'architecture.md').read_text();(candidate/'architecture.md').write_text('changed after review')
  with self.assertRaisesRegex(ValueError,'stale'):self.call(action='revision_adopt',revision=rev['revision'])
  (candidate/'architecture.md').write_text(original)
  # The cited-job binding is now valid, but this metadata fixture still lacks
  # the separately required fresh budget/main-packet files.
  with self.assertRaisesRegex(ValueError,'missing required file'):self.call(action='revision_adopt',revision=rev['revision'])
 def test_noncanonical_reference_and_support(self):
  from harness.runtime.input_bundle import load_input_bundle
  (self.input/'docs').mkdir();(self.input/'models').mkdir();(self.input/'docs/spec.md').write_text('dependency closure test');(self.input/'models/model.py').write_text('from helper import helper\nassert helper==2\nprint(\"PHYSICAL_ALIAS_OK\")\n');(self.input/'models/helper.py').write_text('helper=2\n')
  (self.input/'sources.json').write_text(json.dumps({'schema_version':1,'spec':'docs/spec.md','reference':'models/model.py','support':['models/helper.py']}))
  sources,files,check=load_input_bundle(self.input);b=self.root/'result/architecture'
  for name,data in files.items():p=b/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
  self.runid='rtl-support-test';req=dict(self.req,input_sha256={n:hashlib.sha256(t.encode()).hexdigest()for n,t in sources.items()},input_bundle_sha256={n[6:]:hashlib.sha256(t).hexdigest()for n,t in files.items()},input_source_paths=check['source_paths'])
  result=dispatch(self.root,self.runid,req);self.out=self.root/'output'/self.runid
  self.assertTrue(self.call(action='list',path='@input/')['files'])
  job=self.call(action='submit',argv=['python','@input/reference.py'],timeout_seconds=10)['job_id'];result_job=self.wait(job);self.assertEqual(result_job['exit_code'],0);self.assertIn('PHYSICAL_ALIAS_OK',result_job['stdout_tail'])
  self.assertTrue(Path(result['reference_path']).exists());self.assertIn('helper=2',self.call(action='read',path='@input/support/models/helper.py')['text'])
  self.assertTrue(dispatch(self.root,self.runid,dict(req,resume=True))['run_dir'])

if __name__=='__main__':unittest.main()
