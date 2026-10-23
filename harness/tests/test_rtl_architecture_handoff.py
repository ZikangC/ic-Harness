"""Native architecture revision transport using real calculator and supervisor jobs.

Only the two model sessions are replaced. Run this module on the server; every
calculation, independent probe and temporary project stays there.
"""
import hashlib, importlib.util, json, os, shutil, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_architecture import fixture
from harness.checks.architecture import calculate
from harness.runtime.rtl_worker import dispatch, inventory, closure_inventory, sha
from harness.runtime import rtl_worker as worker
from harness.tests import test_rtl_remote as remote_fixture

H=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('native_revision',H/'skills/rtl-implementation/scripts/revision.py')
revision=importlib.util.module_from_spec(spec);spec.loader.exec_module(revision)


class HandoffTests(unittest.TestCase):
 def setUp(self):
  self.missing_job_environment=[name for name in remote_fixture.JOB_ENVIRONMENT if not os.environ.get(name)];self.job_environment=not self.missing_job_environment;self.owned_jobs=[]
  self.eda_python=os.environ.get('HARNESS_TEST_EDA_PYTHON','/opt/eda/bin/python3')if self.job_environment else sys.executable
  evidence=os.environ.get('ARCHITECTURE_HANDOFF_EVIDENCE_DIR')
  if evidence:Path(evidence).mkdir(parents=True,exist_ok=True)
  self.keep_evidence=bool(evidence);self.tmp=tempfile.TemporaryDirectory(prefix=self._testMethodName+'-',dir=evidence or os.environ.get('HARNESS_TEST_PROJECT_DIR'));self.root=Path(self.tmp.name).resolve()
  if self.keep_evidence:self.tmp._finalizer.detach()
  self.runid='handoff';self.document,self.sources=fixture()
  shutil.copytree(H,self.root/'harness',ignore=shutil.ignore_patterns('__pycache__','._*','.DS_Store'))
  (self.root/'input').mkdir();baseline=self.root/'result/architecture';(baseline/'input').mkdir(parents=True)
  for name,text in self.sources.items():
   (self.root/'input'/name).write_text(text);(baseline/'input'/name).write_text(text)
  self.dump(baseline/'design.json',self.document);self.dump(baseline/'budgets.json',calculate(self.document,self.sources))
  self.dump(baseline/'review.json',{'verdict':'accept','origin':'historical-original-review'})
  self.dump(baseline/'decision.json',{'deliverable':True,'origin':'historical-original-decision'})
  (baseline/'architecture.md').write_text(self.document['architecture_md']);(baseline/'SOURCE.md').write_text('Original handoff, not a revision review.')
  self.initialize()

 def tearDown(self):
  # Explicit evidence retention still drains and checks only this fixture's jobs.
  if self.keep_evidence:
   with patch.object(self.tmp,'cleanup',return_value=None):remote_fixture.WorkerTests.tearDown(self)
  else:remote_fixture.WorkerTests.tearDown(self)

 def dump(self,path,value):path.write_text(json.dumps(value,indent=2)+'\n')
 call=remote_fixture.WorkerTests.call
 def initialize(self):
  task='Continue the synthetic native architecture revision with original requirements and independent evidence.\n'
  settings={'tools':{'python':self.eda_python},'task_sha256':hashlib.sha256(task.encode('utf-8')).hexdigest()}
  isolation=remote_fixture.host_call(self,'describe')['job_isolation']if self.job_environment else{'backend':'docker_readonly_job_v1','uid':os.getuid(),'gid':os.getgid(),'metadata_fixture_only':True}
  init=self.call(action='init',resume=False,input_sha256={n:sha(self.root/'input'/n)for n in self.sources},harness_sha256=inventory(self.root/'harness'),settings=settings,models={'roles':{}},job_isolation=isolation)
  self.out=Path(init['run_dir']);self.cache=self.root/'cache';self.cache.mkdir(exist_ok=True)
  self.context={'harness_dir':str(self.root/'harness'),'capabilities':init['capabilities'],'cache':str(self.cache),'project_root':str(self.root),'remote_run_dir':str(self.out),'tools':init['tools'],'models':{},'settings':settings,'task':task}
  parent=remote_fixture.synthetic_parent_binding(self)
  self.context.update(session_id=parent['parent_session_id'],session_actor_sha256=parent['parent_actor_sha256'])
  owner=self
  class Remote:
   def call(self,request):return owner.call(**request)
  self.remote=Remote()

 def wait(self,job):
  status=remote_fixture.WorkerTests.wait(self,job)
  self.assertEqual(status.get('exit_code'),0,status);self.assertFalse(status.get('timed_out'));self.assertFalse(status.get('error'));return status

 def modify(self,candidate,count=3,overhead=20,selected='small'):
  document=json.loads((candidate/'design.json').read_text());document['selected']=selected
  document['candidates'][0]['macros'][0]['count']=str(count)
  document['candidates'][0]['cost_groups'][0]['overhead_cycles']=str(overhead)
  self.dump(candidate/'design.json',document);(candidate/'architecture.md').write_text('Revised resource count and full-task cost, with original requirements retained.')
  (candidate/'CHANGES.md').write_text('Resource replication and scheduling changed; see the freshly calculated report.')
  (candidate/'support').mkdir(exist_ok=True);(candidate/'support/schedule.txt').write_text('Retained schedule evidence sidecar.\n')
  return document

 def accept(self,rev):
  scope=rev['audit_scope'];candidate=self.out/rev['candidate_scope'];identity=self.call(action='proposal',scope=scope)
  # Independent arithmetic over this synthetic fixture, not calculator output
  # accepted as its own oracle. The task has 80 operations plus 20+20 serial cycles.
  script=('import json\nfrom pathlib import Path\np=Path('+repr(str(candidate))+')\n'
   'd=json.loads((p/"design.json").read_text());r=json.loads((p/"budgets.json").read_text())\n'
   'c=next(x for x in d["candidates"] if x["id"]==d["selected"]);m=c["macros"][0]\n'
   'bits=int(m["count"])*int(m["depth"])*int(m["width"])\n'
   'v=r["candidates"][d["selected"]];assert v["macro_inventory_bits"]==bits\n'
   'expected=80+20+20+int(c["cost_groups"][0]["overhead_cycles"])\n'
   'assert v["workloads"]["primary"]["task_cycles"]==expected\n'
   'assert v["workloads"]["primary"]["items_per_second"]==80000000*12/expected\n'
   'print("INDEPENDENT_DECLARED_RESOURCE_AND_TASK_COST_OK")\n')
  self.call(action='write',scope=scope,path='probe.py',base_sha256=None,content=script)
  job=self.call(action='submit',scope=scope,argv=['python','probe.py'],timeout_seconds=10)['job_id'];self.wait(job)
  self.dump(self.out/scope/'review.json',{'verdict':'accept','design_sha256':sha(candidate/'design.json'),'candidate_sha256':identity['candidate_sha256'],'reason':'Fresh declared capacity and task-cost arithmetic independently executed.','evidence':[job],'conditions':['Synthetic component mechanism test; not RTL validation.']})
  return job

 def run_revision(self,**changes):
  def session(prompt,role,models,root,context_path,cache,remote,label):
   context=json.loads(context_path.read_text());rev={'candidate_scope':context['candidate_scope'],'audit_scope':context['scope']}
   if role=='architecture':self.modify(self.out/context['scope'],**changes)
   else:self.accept(rev)
   return {'completed':True,'tool_isolation_ok':True,'exit_code':0,'elapsed_seconds':.01}
  with patch.object(revision,'run_session',side_effect=session):
   return revision.run({'reason':'Synthetic resource/cost revision','evidence':[],'proposed_change':'Update declared resource replication and full-task cost.'},self.context,self.remote)

 def test_real_recalculation_full_return_and_next_candidate_uses_native_packet(self):
  original=closure_inventory(self.out/'snapshot/architecture');result=self.run_revision()
  self.assertTrue(result['adopted']);budget=result['results']['budget'];self.assertIn('selected_estimates',budget);est=budget['selected_estimates']
  self.assertEqual(est['macro_inventory_bits'],384);self.assertEqual(est['area_total'],{'lower_mm2':2.375,'upper_mm2':2.375})
  self.assertEqual(est['workloads']['primary']['task_cycles'],140);self.assertAlmostEqual(est['workloads']['primary']['items_per_second'],80000000*12/140)
  self.assertEqual(sha(self.out/budget['full_report']),budget['report_sha256'])
  execution=json.loads((self.out/result['revision']['candidate_scope']/'budget.execution.json').read_text())
  self.assertEqual(execution['exit_code'],0);self.assertEqual(execution['script_sha256'],sha(self.out/'snapshot/harness/skills/microarchitecture-design/scripts/evaluate.py'))
  candidate=self.out/result['revision']['candidate_scope'];proposal=closure_inventory(candidate);current=result['current'];baseline=self.out/current['path']
  self.assertEqual(current['review_status'],'accept');self.assertTrue(current['approved_for_implementation']);self.assertTrue(baseline.name=='baseline')
  self.assertEqual(json.loads((baseline/'review.json').read_text())['candidate_sha256'],current['candidate_sha256'])
  self.assertTrue(json.loads((baseline/'decision.json').read_text())['deliverable']);self.assertNotIn('historical-original', (baseline/'review.json').read_text())
  self.assertEqual(self.call(action='read',path='@architecture/budgets.json')['sha256'],sha(baseline/'budgets.json'))
  next_review=self.call(action='rtl_review_begin');meta=json.loads((self.out/next_review['inventory_path']).read_text())
  self.assertEqual(meta['architecture_identity']['current'],current);self.assertEqual(meta['architecture_identity']['inventory'],closure_inventory(baseline))
  self.assertEqual(closure_inventory(candidate),proposal);self.assertEqual(closure_inventory(self.out/'snapshot/architecture'),original)
  self.assertEqual(sha(self.out/result['budget']['full_report']),result['budget']['report_sha256'])
  self.assertEqual(result['artifacts']['decision.json'],sha(baseline/'decision.json'))

 def test_private_review_recalculation_real_job_and_native_adoption_share_identity(self):
  if not self.job_environment:self.skipTest('real isolated audit job requires server Docker environment')
  rev=self.call(action='revision_begin',reason='private independent calculator evidence')
  candidate=self.out/rev['candidate_scope'];self.modify(candidate)
  self.call(action='evaluate',scope=rev['candidate_scope']);before=closure_inventory(candidate)
  verified=self.call(action='evaluate',scope=rev['audit_scope']);cid=verified['candidate_sha256']
  self.assertEqual(closure_inventory(candidate),before)
  self.assertTrue(verified['full_report'].startswith(rev['audit_scope']+'/calculations/'))
  job=self.accept(rev)
  for name in ('request.json','result.json'):
   self.assertEqual(json.loads((self.out/'jobs'/job/name).read_text())['candidate_sha256'],cid)
  self.assertEqual(json.loads((self.out/rev['audit_scope']/'review.json').read_text())['candidate_sha256'],cid)
  adopted=self.call(action='revision_adopt',revision=rev['revision'])
  self.assertTrue(adopted['adopted']);self.assertTrue(adopted['current']['approved_for_implementation'])
  self.assertEqual(adopted['current']['candidate_sha256'],cid)
  self.assertEqual(closure_inventory(candidate),before)
  self.assertEqual(adopted['artifacts']['decision.json'],sha(Path(adopted['path'])/'decision.json'))
  # The OMP/model protocol is mocked; every page below is an actual worker
  # response from the adopted immutable nine-file packet, not child evidence.
  current=adopted['current'];parent=self.context['session_id'];events=[];sequence=0
  self.assertEqual(current['parent_session_id'],parent)
  self.assertEqual(current['parent_actor_sha256'],self.context['session_actor_sha256'])
  self.assertEqual(len(current['readback_required_files']),9)
  expected={}
  for item in current['readback_required_files']:
   name='@run/'+item['resolved_run_path'];cursor=None
   expected[str(Path(item['resolved_run_path']).relative_to(current['path']))]={'sha256':item['sha256'],'bytes':item['bytes']}
   while True:
    request={'action':'read','path':name,'lines':40}
    if cursor is not None:request['cursor']=cursor
    page=self.call(**request);sequence+=1;tool_id='component-parent-read-'+str(sequence)
    self.assertEqual(page['resolved_run_path'],item['resolved_run_path'])
    self.assertEqual(page['sha256'],item['sha256'])
    events.extend([{'type':'tool_execution_start','toolCallId':tool_id,'toolName':'rtl_remote','args':request},
                   {'type':'tool_execution_end','toolCallId':tool_id,'toolName':'rtl_remote','isError':False,
                    'result':{'content':[{'type':'text','text':json.dumps(page,ensure_ascii=False)}],
                              'details':{'mocked_OMP_protocol_not_read_evidence':True},'isError':False}}])
    cursor=page['next_cursor']
    if cursor is None:
     self.assertTrue(page['eof']);break
  raw=''.join(json.dumps(event,ensure_ascii=False)+'\n'for event in events)
  for suffix,content in [('events.jsonl',raw),
                         ('isolation.json',json.dumps({'checks':[{'active_tools':['rtl_remote','rtl_revise_architecture']}],'blocked':[]})),
                         ('result.json',json.dumps({'role':'implementation','completed':True,'tool_isolation_ok':True,
                                                   'exit_code':0,'stop_reason':'stop','model_execution':'mocked OMP component; no model invocation'}))]:
   self.call(action='put_evidence',path='control/'+parent+'.'+suffix,content=content)
  ended=self.call(action='rtl_session_end',session_id=parent)
  receipt=json.loads((self.out/ended['readback_path']).read_text())
  self.assertTrue(receipt['witness']['complete'])
  self.assertEqual(receipt['actor_sha256'],self.context['session_actor_sha256'])
  self.assertEqual(receipt['packet']['baseline_sha256'],current['baseline_sha256'])
  self.assertEqual(receipt['packet']['handoff_sha256'],current['handoff_sha256'])
  self.assertEqual(receipt['witness']['events_sha256'],hashlib.sha256(raw.encode()).hexdigest())
  fresh=self.call(action='rtl_review_begin')
  self.assertTrue(fresh['parent_readback']['complete'])
  self.assertTrue(fresh['parent_readback']['session_ended'])
  self.assertEqual(fresh['parent_readback']['parent_session_id'],parent)
  self.assertEqual(fresh['parent_readback']['baseline_sha256'],current['baseline_sha256'])
  # Exactly one additional real isolated host-launch job checks this fresh RTL
  # candidate's architecture copy. No artificial jobs/1 metadata is installed.
  script=('import hashlib,json\nfrom pathlib import Path\n'
          'root=Path('+repr(str(self.out/fresh['architecture_scope']))+')\n'
          'expected=json.loads('+repr(json.dumps(expected,sort_keys=True))+')\n'
          'for name,item in expected.items():\n'
          ' p=(root/name).resolve();p.relative_to(root.resolve());raw=p.read_bytes()\n'
          ' assert len(raw)==item["bytes"] and hashlib.sha256(raw).hexdigest()==item["sha256"]\n'
          'print("FROZEN_ARCHITECTURE_READBACK_PACKET_BOUND")\n')
  self.call(action='write',scope=fresh['audit_scope'],path='readback_probe.py',base_sha256=None,content=script)
  rtl_job=self.call(action='submit',scope=fresh['audit_scope'],argv=['python','readback_probe.py'],timeout_seconds=10)['job_id']
  checked=self.wait(rtl_job);self.assertNotEqual(rtl_job,job)
  self.assertTrue(checked['cleanup_complete']);self.assertFalse(checked['container_running'])
  request=json.loads((self.out/'jobs'/rtl_job/'request.json').read_text())
  self.assertEqual(request['scope'],fresh['audit_scope']);self.assertEqual(request['candidate_sha256'],fresh['candidate_sha256'])
  report=self.out/fresh['audit_scope']/'readback-final-review.json'
  self.dump(report,{'verdict':'pass','candidate_sha256':fresh['candidate_sha256'],'evidence':[rtl_job],
                    'reason':'Synthetic parent-readback and final release binding component only; no RTL functional or performance acceptance claim.'})
  final=self.call(action='rtl_review_decision',review=fresh['review'],report_path=str(report.relative_to(self.out)),expected_verdict='final')
  self.assertEqual(final['decision']['verdict'],'pass');self.assertTrue(final['parent_readback']['complete'])
  self.assertEqual(closure_inventory(candidate),before)

 def test_sidecar_changed_after_review_cannot_be_adopted(self):
  before=(self.out/'architecture-current.json').read_bytes();rev=self.call(action='revision_begin',reason='sidecar')
  candidate=self.out/rev['candidate_scope'];self.modify(candidate);self.call(action='evaluate',scope=rev['candidate_scope']);self.accept(rev)
  (candidate/'support/schedule.txt').write_text('Changed after independent review.\n')
  with self.assertRaisesRegex(ValueError,'stale'):self.call(action='revision_adopt',revision=rev['revision'])
  self.assertEqual((self.out/'architecture-current.json').read_bytes(),before);self.assertFalse((candidate.parent/'baseline').exists())

 def publication_race(self,kind):
  before=(self.out/'architecture-current.json').read_bytes();rev=self.call(action='revision_begin',reason='publication identity race')
  candidate=self.out/rev['candidate_scope'];self.modify(candidate);self.call(action='evaluate',scope=rev['candidate_scope']);self.accept(rev)
  original_copy=worker.copy_closure;triggered=[]
  def copying(source,destination):
   if source==candidate:
    triggered.append(True)
    if kind=='candidate':(candidate/'support/schedule.txt').write_text('Stable mutation between validation and copy.\n')
    copied=original_copy(source,destination)
    if kind=='review':
     p=self.out/rev['audit_scope']/'review.json';data=json.loads(p.read_text());data['verdict']='revise';self.dump(p,data)
    if kind=='parent':
     p=self.out/'snapshot/architecture/architecture.md';p.chmod((p.stat().st_mode&0o777)^0o100)
    return copied
   return original_copy(source,destination)
  with patch.object(worker,'copy_closure',side_effect=copying):
   with self.assertRaisesRegex(ValueError,'changed'):self.call(action='revision_adopt',revision=rev['revision'])
  self.assertTrue(triggered);self.assertEqual((self.out/'architecture-current.json').read_bytes(),before);self.assertFalse((candidate.parent/'baseline').exists())

 def test_validated_candidate_matches_actual_published_copy(self):self.publication_race('candidate')
 def test_validated_review_matches_native_review_copy(self):self.publication_race('review')
 def test_parent_mode_change_during_publication_keeps_current_pointer(self):self.publication_race('parent')

 def test_native_record_internal_links_cannot_overwrite_proposal_targets(self):
  rev=self.call(action='revision_begin',reason='native record links');candidate=self.out/rev['candidate_scope'];self.modify(candidate)
  for name,target in (('review.json','design.json'),('decision.json','architecture.md'),('SOURCE.md','design.json')):
   p=candidate/name
   if p.exists():p.unlink()
   p.symlink_to(target)
  self.call(action='evaluate',scope=rev['candidate_scope']);self.accept(rev);original=closure_inventory(candidate)
  design=(candidate/'design.json').read_bytes();prose=(candidate/'architecture.md').read_bytes()
  result=self.call(action='revision_adopt',revision=rev['revision']);baseline=Path(result['path'])
  self.assertEqual((baseline/'design.json').read_bytes(),design);self.assertEqual((baseline/'architecture.md').read_bytes(),prose);self.assertEqual(closure_inventory(candidate),original)
  for name in ('review.json','decision.json','SOURCE.md'):self.assertFalse((baseline/name).is_symlink(),name)
  self.assertTrue(json.loads((baseline/'decision.json').read_text())['deliverable'])

 def busy_scope(self,role):
  before=(self.out/'architecture-current.json').read_bytes();rev=self.call(action='revision_begin',reason='active registered job');candidate=self.out/rev['candidate_scope'];self.modify(candidate)
  scope=rev[role+'_scope'];release=self.root/'release'
  script='from pathlib import Path\nimport time\np=Path('+repr(str(release))+')\nfor _ in range(500):\n if p.exists():break\n time.sleep(.01)\nelse:raise RuntimeError("test did not release its own job")\n'
  self.call(action='write',scope=scope,path='wait.py',base_sha256=None,content=script)
  self.call(action='evaluate',scope=rev['candidate_scope']);self.accept(rev)
  job=self.call(action='submit',scope=scope,argv=['python','wait.py'],timeout_seconds=10)['job_id']
  try:
   with self.assertRaisesRegex(ValueError,'unfinished jobs'):self.call(action='revision_adopt',revision=rev['revision'])
   self.assertEqual((self.out/'architecture-current.json').read_bytes(),before)
  finally:release.touch();self.wait(job)

 def test_candidate_jobs_must_finish_before_adoption(self):self.busy_scope('candidate')
 def test_audit_jobs_must_finish_before_adoption(self):self.busy_scope('audit')

 def test_consecutive_revision_has_no_inherited_approval_or_binding(self):
  first=self.run_revision();self.assertIn('current',first);parent=self.out/first['current']['path'];original=closure_inventory(parent)
  rev=self.call(action='revision_begin',reason='second revision');candidate=self.out/rev['candidate_scope']
  for name in ('review.json','decision.json','budgets.json','budget_binding.json'):self.assertFalse((candidate/name).exists(),name)
  self.modify(candidate,count=4,overhead=30);fresh=self.call(action='evaluate',scope=rev['candidate_scope']);self.assertEqual(fresh['selected_estimates']['macro_inventory_bits'],512)
  self.accept(rev);second=self.call(action='revision_adopt',revision=rev['revision'])
  self.assertTrue(second['adopted']);self.assertNotEqual(first['current']['candidate_sha256'],second['current']['candidate_sha256']);self.assertEqual(closure_inventory(parent),original)
  self.assertEqual(json.loads((Path(second['path'])/'review.json').read_text())['candidate_sha256'],second['current']['candidate_sha256'])

 def test_parent_internal_link_identity_cannot_change_with_equal_file_bytes(self):
  first=self.call(action='revision_begin',reason='parent link identity');candidate=self.out/first['candidate_scope'];self.modify(candidate)
  side=candidate/'support';(side/'equal.txt').write_bytes((side/'schedule.txt').read_bytes());(side/'alias.txt').symlink_to('schedule.txt')
  self.call(action='evaluate',scope=first['candidate_scope']);self.accept(first);adopted=self.call(action='revision_adopt',revision=first['revision']);parent=Path(adopted['path'])
  rev=self.call(action='revision_begin',reason='next revision');self.modify(self.out/rev['candidate_scope']);self.call(action='evaluate',scope=rev['candidate_scope']);self.accept(rev)
  before=(self.out/'architecture-current.json').read_bytes();files=inventory(parent);link=parent/'support/alias.txt';link.unlink();link.symlink_to('equal.txt');self.assertEqual(inventory(parent),files)
  with self.assertRaisesRegex(ValueError,'baseline changed'):self.call(action='revision_adopt',revision=rev['revision'])
  self.assertEqual((self.out/'architecture-current.json').read_bytes(),before)

 def test_published_proposal_and_audit_scopes_are_read_only(self):
  first=self.run_revision();rev=first['revision'];before={role:closure_inventory(self.out/rev[role+'_scope'])for role in ('candidate','audit')};jobs=list((self.out/'jobs').iterdir())
  for role in ('candidate','audit'):
   scope=rev[role+'_scope']
   for action,extra in (('write',{'path':'new.txt','base_sha256':None,'content':'changed'}),('replace',{'path':'SOURCE.md','base_sha256':None,'old':'x','content':'y'}),('evaluate',{}),('submit',{'argv':['python','-c','pass'],'timeout_seconds':10})):
    with self.subTest(role=role,action=action):
     error=None
     try:
      result=self.call(action=action,scope=scope,**extra)
      if action=='submit':self.wait(result['job_id'])
     except Exception as exception:error=exception
     self.assertIsInstance(error,ValueError);self.assertIn('published architecture revision is read-only',str(error))
   self.assertTrue(self.call(action='list',scope=scope)['files']);self.assertTrue(self.call(action='proposal',scope=scope)['candidate_sha256'])
   self.assertEqual(closure_inventory(self.out/scope),before[role])
  self.assertEqual(list((self.out/'jobs').iterdir()),jobs)
  next_revision=self.call(action='revision_begin',reason='fresh scope remains writable')
  self.call(action='write',scope=next_revision['candidate_scope'],path='fresh.txt',base_sha256=None,content='Fresh revision scope.\n')

 def guarded_work_job(self,target):
  adopted=self.run_revision();rev=adopted['revision'];scope=rev[target+'_scope']if target in ('candidate','audit')else adopted['current']['path'];relative={'candidate':'support/schedule.txt','audit':'probe.py','baseline':'architecture.md'}[target];path=self.out/scope/relative
  protected=[rev['candidate_scope'],rev['audit_scope'],adopted['current']['path']];before={name:closure_inventory(self.out/name)for name in protected};current=(self.out/'architecture-current.json').read_bytes();handoff=(self.out/rev['candidate_scope']).parent/'handoff.json';handoff_bytes=handoff.read_bytes()
  content='from pathlib import Path\np=Path('+repr(str(path))+')\np.write_bytes(p.read_bytes()+b"\\nchanged by synthetic test job\\n")\n'
  self.call(action='write',path='touch_old.py',base_sha256=None,content=content)
  job=self.call(action='submit',argv=['python','touch_old.py'],timeout_seconds=10)['job_id']
  status=remote_fixture.WorkerTests.wait(self,job)
  self.assertIsInstance(status.get('exit_code'),int);self.assertNotEqual(status.get('exit_code'),0,status)
  self.assertIn('Read-only file system',status['stderr_tail']);self.assertNotIn('integrity_error',status);self.assertFalse(status.get('sandbox_setup_failed',False))
  self.assertEqual({name:closure_inventory(self.out/name)for name in protected},before);self.assertEqual((self.out/'architecture-current.json').read_bytes(),current);self.assertEqual(handoff.read_bytes(),handoff_bytes)
  request=json.loads((self.out/'jobs'/job/'request.json').read_text());paths={g['path']for g in request['integrity_guards']}
  self.assertTrue({rev['candidate_scope'],rev['audit_scope'],adopted['current']['path']}<=paths)
  self.assertTrue(worker.integrity_guards(self.out))

 def test_work_job_detects_changed_historical_proposal(self):self.guarded_work_job('candidate')
 def test_work_job_detects_changed_historical_audit(self):self.guarded_work_job('audit')
 def test_work_job_detects_changed_historical_baseline(self):self.guarded_work_job('baseline')

 def test_failed_budget_and_interrupted_delegate_keep_previous_baseline(self):
  before=(self.out/'architecture-current.json').read_bytes();rev=self.call(action='revision_begin',reason='hard violation')
  self.modify(self.out/rev['candidate_scope'],selected='large');self.call(action='evaluate',scope=rev['candidate_scope'])
  with self.assertRaisesRegex(ValueError,'hard budget'):self.call(action='revision_adopt',revision=rev['revision'])
  self.assertEqual((self.out/'architecture-current.json').read_bytes(),before)
  with patch.object(revision,'run_session',return_value={'completed':False,'tool_isolation_ok':True,'exit_code':1}):
   result=revision.run({'reason':'interrupted'},self.context,self.remote)
  self.assertFalse(result['adopted']);self.assertEqual((self.out/'architecture-current.json').read_bytes(),before)

 def test_invalid_declarations_and_keyboard_interrupt_never_publish(self):
  before=(self.out/'architecture-current.json').read_bytes()
  def invalid(prompt,role,models,root,context_path,cache,remote,label):
   self.assertEqual(role,'architecture');context=json.loads(context_path.read_text());candidate=self.out/context['scope'];document=self.modify(candidate)
   document['candidates'][0]['macros'][0]['width']='7';self.dump(candidate/'design.json',document)
   return {'completed':True,'tool_isolation_ok':True,'exit_code':0}
  with patch.object(revision,'run_session',side_effect=invalid):result=revision.run({'reason':'invalid declaration'},self.context,self.remote)
  self.assertFalse(result['adopted']);self.assertIn('ok',result['results']['budget']);self.assertFalse(result['results']['budget']['ok']);self.assertTrue(result['results']['budget']['errors'])
  self.assertEqual((self.out/'architecture-current.json').read_bytes(),before)
  with patch.object(revision,'run_session',side_effect=KeyboardInterrupt):
   with self.assertRaises(KeyboardInterrupt):revision.run({'reason':'interrupted'},self.context,self.remote)
  self.assertEqual((self.out/'architecture-current.json').read_bytes(),before)

 def test_optional_review_missing_allows_exploration_but_cannot_pass(self):
  (self.root/'harness/skills/microarchitecture-audit/SKILL.md').unlink();self.runid='without-audit';self.initialize()
  result=self.run_revision();self.assertTrue(result['adopted']);self.assertEqual(result['review_status'],'not_run')
  self.assertIn('current',result);self.assertFalse(result['current']['approved_for_implementation']);baseline=Path(result['path']);self.assertFalse(json.loads((baseline/'decision.json').read_text())['deliverable'])
  review=self.call(action='rtl_review_begin');scope=review['audit_scope']
  self.call(action='write',scope=scope,path='probe.py',base_sha256=None,content='assert 2+2==4\n')
  job=self.call(action='submit',scope=scope,argv=['python','probe.py'],timeout_seconds=10)['job_id'];self.wait(job)
  report=self.out/scope/'report.json';data={'verdict':'pass','candidate_sha256':review['candidate_sha256'],'reason':'Synthetic functional evidence does not supply missing architecture approval.','evidence':[job]};self.dump(report,data)
  with self.assertRaisesRegex(ValueError,'architecture revision lacks required approval'):self.call(action='rtl_review_decision',review=review['review'],report_path='report.json',expected_verdict='final')
  self.assertIsNone(self.call(action='rtl_review_status')['decision'])
  data['verdict']='insufficient_evidence';self.dump(report,data)
  self.assertEqual(self.call(action='rtl_review_decision',review=review['review'],report_path='report.json',expected_verdict='final')['decision']['verdict'],'insufficient_evidence')

 def test_initial_estimate_baseline_still_allows_independent_rtl_pass(self):
  review=self.call(action='rtl_review_begin');scope=review['audit_scope']
  self.call(action='write',scope=scope,path='probe.py',base_sha256=None,content='assert 2+2==4\n')
  job=self.call(action='submit',scope=scope,argv=['python','probe.py'],timeout_seconds=10)['job_id'];self.wait(job)
  self.dump(self.out/scope/'report.json',{'verdict':'pass','candidate_sha256':review['candidate_sha256'],'reason':'Actual independent synthetic component evidence.','evidence':[job]})
  self.assertEqual(self.call(action='rtl_review_decision',report_path='report.json',expected_verdict='final')['decision']['verdict'],'pass')

if __name__=='__main__':unittest.main()
