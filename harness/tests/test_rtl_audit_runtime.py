"""Mechanical candidate/evidence/repair invariants, run in server temp projects."""
import json,sys,time,unittest
from pathlib import Path
from harness.tests import test_rtl_remote as fixture
from harness.runtime.rtl_worker import closure_inventory,sha


class RtlAuditRuntimeTests(unittest.TestCase):
 setUp=fixture.WorkerTests.setUp
 tearDown=fixture.WorkerTests.tearDown
 call=fixture.WorkerTests.call
 wait=fixture.WorkerTests.wait

 def write(self,path,content,scope='work'):
  target=self.out/scope/path
  return self.call(action='write',scope=scope,path=path,content=content,base_sha256=sha(target)if target.is_file()else None)

 def begin(self,**kwargs):
  if not(self.out/'work/design.sv').exists():self.write('design.sv','module design; endmodule\n')
  top='repaired'if(self.out/'work/design.sv').read_text()=='module repaired; endmodule\n'else'design'
  self.write('RTL_STRUCTURE.json',json.dumps({'version':1,'configurations':[{'id':'delivery','top':top,'sources':['design.sv']}],'blackboxes':[]}))
  return self.call(action='rtl_review_begin',**kwargs)

 def audit_job(self,review,code='import sys\nassert open(sys.argv[1]).read()\n',name='check.py'):
  self.write(name,code,review['audit_scope'])
  job=self.call(action='submit',scope=review['audit_scope'],review=review['review'],argv=['python',name,'@candidate/design.sv'],timeout_seconds=10)['job_id']
  return job,self.wait(job)

 def report(self,review,verdict,evidence):
  self.write('review.json',json.dumps({'verdict':verdict,'review_scope': review['review_scope'], 'candidate_sha256':review['candidate_sha256'],'reason':'Recorded fixture check outcome','evidence':evidence}),review['audit_scope'])

 def decide(self,review,verdict,evidence):
  self.report(review,verdict,evidence)
  if verdict=='pass':
   # These fixture modules are defined in this test (design/repaired), not
   # inferred from a project spec. The real native check is required as well.
   source=(self.out/review['candidate_scope']/'design.sv').read_text()
   top='repaired'if source=='module repaired; endmodule\n'else'design'
   structure,job=fixture.structure_report(self,review,top=top)
   path=self.out/review['audit_scope']/'review.json';data=json.loads(path.read_text());data['structure_checks']=structure;data['evidence'].append(job);path.write_text(json.dumps(data))
  return self.call(action='rtl_review_decision',review=review['review'],report_path='review.json',expected_verdict='revise'if verdict=='revise'else'final')

 def revise(self):
  review=self.begin();job,_=self.audit_job(review,'raise SystemExit(3)\n')
  self.decide(review,'revise',[job]);return review

 def test_no_review_and_complete_build_closure(self):
  self.assertEqual(self.call(action='rtl_review_status'),{'review':None})
  self.write('Makefile','all:\n\ttrue\n');self.write('data/image.mem','aabb\n');self.write('build/generated','opaque build dependency\n')
  (self.out/'work/link').symlink_to(self.out/'work/data/image.mem')
  review=self.begin();candidate=self.out/review['candidate_scope']
  self.assertEqual(closure_inventory(candidate),closure_inventory(self.out/'work'))
  for name in ('Makefile','data/image.mem','build/generated'):self.assertTrue((candidate/name).is_file())
  self.assertEqual((candidate/'link').resolve(),candidate/'data/image.mem')
  self.assertNotIn('work_inventory',review)
  self.write('data/image.mem','changed\n')
  self.assertFalse(self.call(action='rtl_review_status')['work_current'])
  self.assertEqual((candidate/'data/image.mem').read_text(),'aabb\n')

 def test_external_link_rejected_before_freeze(self):
  (self.out/'work/external').symlink_to(self.input/'spec.md')
  with self.assertRaisesRegex(ValueError,'external symlink'):self.begin()

 def test_successful_decision_is_source_bound_and_report_immutable(self):
  review=self.begin();job,result=self.audit_job(review)
  self.assertEqual(result['candidate_sha256'],review['candidate_sha256']);self.assertEqual(result['exit_code'],0)
  decision=self.decide(review,'pass',[job])
  self.assertEqual(decision['decision']['verdict'],'pass')
  self.assertEqual(decision['decision']['report_path'],review['audit_scope']+'/review.json')
  with self.assertRaisesRegex(ValueError,'already has a decision'):self.call(action='rtl_review_decision',review=review['review'],report_path='review.json',expected_verdict='final')
  with self.assertRaisesRegex(ValueError,'decided audit scope'):self.write('check.py','pass\n',review['audit_scope'])
  (self.out/decision['decision']['report_path']).write_text('{}')
  with self.assertRaisesRegex(ValueError,'report changed'):self.call(action='rtl_review_status')

 def test_failed_jobs_cannot_justify_pass_but_can_justify_repair(self):
  review=self.begin();job,result=self.audit_job(review,'raise SystemExit(7)\n')
  self.assertEqual(result['exit_code'],7)
  self.report(review,'pass',[job])
  with self.assertRaisesRegex(ValueError,'successful independent evidence'):self.call(action='rtl_review_decision',review=review['review'],report_path='review.json',expected_verdict='final')
  decision=self.decide(review,'revise',[job]);self.assertEqual(decision['decision']['verdict'],'revise')

 def test_fake_or_implementation_evidence_rejected(self):
  work_job=self.call(action='submit',argv=['python','-c','pass'],timeout_seconds=10)['job_id'];self.wait(work_job)
  review=self.begin()
  for job,pattern in [('123456','missing or unfinished'),(work_job,'independent review')]:
   self.report(review,'pass',[job])
   with self.assertRaisesRegex(ValueError,pattern):self.call(action='rtl_review_decision',review=review['review'],report_path='review.json',expected_verdict='final')

 def test_candidate_and_current_architecture_changes_rejected(self):
  review=self.begin();candidate=self.out/review['candidate_scope']/'design.sv';original=candidate.read_text();candidate.write_text('tampered')
  with self.assertRaisesRegex(ValueError,'candidate changed'):self.call(action='rtl_review_status')
  candidate.write_text(original)
  job,_=self.audit_job(review);self.report(review,'pass',[job])
  current=self.out/'architecture-current.json';value=json.loads(current.read_text());value['review_status']='changed';current.write_text(json.dumps(value))
  with self.assertRaisesRegex(ValueError,'architecture changed'):self.call(action='rtl_review_decision',review=review['review'],report_path='review.json',expected_verdict='final')

 def test_live_work_change_and_pending_jobs_block_transitions(self):
  job=self.call(action='submit',argv=['python','-c','import time; time.sleep(.4)'],timeout_seconds=10)['job_id']
  with self.assertRaisesRegex(ValueError,'unfinished jobs'):self.begin()
  self.wait(job);review=self.begin();job,_=self.audit_job(review)
  self.write('design.sv','module changed; endmodule\n');self.report(review,'pass',[job])
  with self.assertRaisesRegex(ValueError,'live work changed'):self.call(action='rtl_review_decision',review=review['review'],report_path='review.json',expected_verdict='final')

 def test_repair_and_fresh_review_reject_old_evidence(self):
  review=self.revise()
  with self.assertRaisesRegex(ValueError,'completed RTL repair'):self.call(action='rtl_review_begin',seed_review=review['review'])
  active=self.call(action='rtl_repair_begin',review=review['review']);self.assertEqual(active['repair']['status'],'active')
  self.assertEqual(self.call(action='rtl_repair_begin',review=review['review'])['repair']['status'],'active')
  self.assertIn('module design',self.call(action='read',review=review['review'],path='@candidate/design.sv')['text'])
  self.write('design.sv','module repaired; endmodule\n')
  repair_job=self.call(action='submit',review=review['review'],argv=['python','-c','assert "repaired" in open("design.sv").read()'],timeout_seconds=10)['job_id'];self.assertEqual(self.wait(repair_job)['exit_code'],0)
  ended=self.call(action='rtl_repair_end',review=review['review']);self.assertEqual(ended['repair']['status'],'completed');self.assertFalse(ended['work_current'])
  self.assertEqual(self.call(action='rtl_repair_end',review=review['review'])['repair']['status'],'completed')
  self.assertEqual(self.call(action='rtl_repair_begin',review=review['review'])['repair']['status'],'completed')
  newer=self.begin(seed_review=review['review']);self.assertNotEqual(newer['candidate_sha256'],review['candidate_sha256'])
  self.assertTrue((self.out/newer['audit_scope']/'check.py').exists())
  old_job=ended['decision']['evidence'][0];self.report(newer,'pass',[old_job])
  with self.assertRaisesRegex(ValueError,'independent review'):self.call(action='rtl_review_decision',review=newer['review'],report_path='review.json',expected_verdict='final')
  new_job,_=self.audit_job(newer);self.assertEqual(self.decide(newer,'pass',[new_job])['decision']['verdict'],'pass')

 def test_repair_job_tampering_with_tests_is_detected(self):
  review=self.revise();self.call(action='rtl_repair_begin',review=review['review'])
  audit=self.out/review['audit_scope'];candidate=self.out/review['candidate_scope'];record=self.out/'rtl/reviews'/(review['review']+'.json')
  before_audit=closure_inventory(audit);before_candidate=closure_inventory(candidate);before_record=record.read_bytes()
  script='from pathlib import Path; Path('+repr(str(self.out/review['audit_scope']/'check.py'))+').write_text("pass\\n")'
  job=self.call(action='submit',review=review['review'],argv=['python','-c',script],timeout_seconds=10)['job_id'];result=self.wait(job)
  self.assertIsInstance(result['exit_code'],int);self.assertNotEqual(result['exit_code'],0)
  self.assertIn('Read-only file system',result['stderr_tail']);self.assertNotIn('integrity_error',result);self.assertFalse(result.get('sandbox_setup_failed',False))
  self.assertEqual(closure_inventory(audit),before_audit);self.assertEqual(closure_inventory(candidate),before_candidate);self.assertEqual(record.read_bytes(),before_record)
  self.assertEqual(self.call(action='rtl_review_status')['candidate_sha256'],review['candidate_sha256'])

 def test_audit_job_tampering_with_candidate_is_detected(self):
  review=self.begin();script='from pathlib import Path; import sys; Path(sys.argv[1]).write_text("bad")\n'
  self.write('check.py',script,review['audit_scope'])
  audit=self.out/review['audit_scope'];candidate=self.out/review['candidate_scope'];record=self.out/'rtl/reviews'/(review['review']+'.json')
  before_audit=closure_inventory(audit);before_candidate=closure_inventory(candidate);before_record=record.read_bytes()
  job=self.call(action='submit',scope=review['audit_scope'],review=review['review'],argv=['python','check.py','@candidate/design.sv'],timeout_seconds=10)['job_id'];result=self.wait(job)
  self.assertIsInstance(result['exit_code'],int);self.assertNotEqual(result['exit_code'],0)
  self.assertIn('Read-only file system',result['stderr_tail']);self.assertNotIn('integrity_error',result);self.assertFalse(result.get('sandbox_setup_failed',False))
  self.assertEqual(closure_inventory(audit),before_audit);self.assertEqual(closure_inventory(candidate),before_candidate);self.assertEqual(record.read_bytes(),before_record)
  self.assertEqual(self.call(action='rtl_review_status')['candidate_sha256'],review['candidate_sha256'])

 def test_decision_action_and_report_binding_must_agree(self):
  review=self.begin();job,_=self.audit_job(review);self.report(review,'revise',[job])
  with self.assertRaisesRegex(ValueError,'selected decision action'):self.call(action='rtl_review_decision',review=review['review'],report_path='review.json',expected_verdict='final')
  path=self.out/review['audit_scope']/'review.json';data=json.loads(path.read_text());data['candidate_sha256']='0'*64;path.write_text(json.dumps(data))
  with self.assertRaisesRegex(ValueError,'stale candidate'):self.call(action='rtl_review_decision',review=review['review'],report_path='review.json',expected_verdict='revise')


if __name__=='__main__':unittest.main()
