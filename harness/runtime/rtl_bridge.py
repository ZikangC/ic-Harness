#!/usr/bin/env python3
"""OMP tool dispatch, with stage-scoped workspaces and optional skill delegation."""
import importlib.util,json,os,sys,signal,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from harness.adapters.rtl_remote import Remote
from harness.runtime.rtl_session_budget import decorate

def readiness_status(remote,request):
 """Coalesce successful pending observations of the same outer job for up to 15s.

 Existing Remote transport behavior is unchanged. A transport error propagates;
 this never retries an uncertain call, submits work or observes an inner PID.
 """
 deadline=time.monotonic()+15
 result=remote.call(dict(request))
 while result.get('status')in ('queued','submitted','running'):
  remaining=deadline-time.monotonic()
  if remaining<=0:break
  time.sleep(min(.5,remaining))
  if time.monotonic()>=deadline:break
  result=remote.call(dict(request))
 return result

def main():
 context=json.loads(Path(os.environ['HARNESS_RTL_CONTEXT']).read_text());req=json.load(sys.stdin);remote=Remote(context['environment'],context['run_id'],context['worker']);action=req['action']
 diagnosis=context['role']=='readiness_diagnosis'
 readiness=context['role']in ('rtl_readiness','readiness_review','readiness_diagnosis')or context.get('repair_skill')=='rtl-implementation-readiness'
 if diagnosis and action not in ('list','read','status','jobs'):
  raise ValueError('read-only diagnosis permits only list/read/status/jobs')
 if readiness and action not in ('list','read','write','replace','submit','status','cancel','jobs','handoff'):
  raise ValueError('readiness sessions expose only scoped remote file and job actions')
 if action=='handoff':
  if context['role']!='rtl_readiness':raise ValueError('handoff requires readiness investigation role')
  if set(req)!={'action','job_id'}:raise ValueError('handoff requires only the explicit finalization job_id')
  result=remote.call({'action':'rtl_readiness_handoff','session_id':context['session_id'],
                      'actor_sha256':context['session_actor_sha256'],'campaign':context['campaign'],
                      'job_id':req['job_id']})
 elif action in ('rtl_request_revision','rtl_finish_review'):
  if context['role']!='rtl_audit':raise ValueError('RTL audit decision requires audit role')
  result=remote.call({'action':'rtl_review_decision','review':context['review'],'report_path':req['report_path'],'expected_verdict':'revise'if action=='rtl_request_revision'else'final'})
 elif action=='architecture_revision':
  if context['role']!='implementation':raise ValueError('nested architecture delegation is unavailable')
  script=Path(context['harness_dir'])/'skills/rtl-implementation/scripts/revision.py';spec=importlib.util.spec_from_file_location('rtl_revision',script);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);result=module.run(req,context,remote)
 else:
  if action not in ('list','read','write','replace','submit','status','cancel','jobs','evaluate','proposal','structure_check'):raise ValueError('unsupported model workspace action')
  if action=='structure_check'and context['role']not in ('implementation','rtl_audit'):raise ValueError('RTL structural checks require an implementation or RTL audit role')
  if context['role']=='rtl_audit'and action in ('evaluate','proposal'):raise ValueError('RTL review does not edit/evaluate architecture candidates')
  req['scope']=context['scope']
  if context['role']=='rtl_readiness' and action=='submit':
   req['session_id']=context['session_id'];req['session_actor_sha256']=context['session_actor_sha256']
  if context.get('review'):req['review']=context['review']
  if action=='evaluate'and context.get('candidate_scope')and context['role']!='review':req['scope']=context['candidate_scope']
  if context['role']=='review'and action in ('write','replace')and req['path'].startswith('../'):raise ValueError('review cannot modify candidate')
  result=readiness_status(remote,req)if readiness and not diagnosis and action=='status'else remote.call(req)
 print(json.dumps(decorate(context,result,action) if readiness else result,ensure_ascii=False))
if __name__=='__main__':
 signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
 try:main()
 except Exception as e:print(json.dumps({'error':type(e).__name__+': '+str(e)}));raise SystemExit(1)
