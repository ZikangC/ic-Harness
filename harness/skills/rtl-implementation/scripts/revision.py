"""Scoped design/review delegation using optional installed architecture skills."""
import json,re,time
from hashlib import sha256
from pathlib import Path
from harness.adapters.omp_tools import run_session

def _delivery_task(context):
 task=context.get('task');settings=context.get('settings')
 expected=settings.get('task_sha256')if isinstance(settings,dict)else None
 if not isinstance(task,str):raise ValueError('frozen delivery task missing or fingerprint mismatch')
 try:body=task.encode('utf-8')
 except UnicodeError as error:raise ValueError('frozen delivery task is not valid UTF-8')from error
 if sha256(body).hexdigest()!=expected:raise ValueError('frozen delivery task missing or fingerprint mismatch')
 return '\nFROZEN DELIVERY TASK (SHA-256 '+expected+'):\n'+task+'\n'

def _native_contract(role):
 common=(
  '\nAUTHORITATIVE NATIVE REVISION CONTRACT:\n'
  'This is the scoped native RTL architecture-revision mode. Optional SKILL guidance supplies engineering methods; '
  'this native contract selects the operational tools, evidence and persisted deliverables, including when optional guidance describes another stage. '
  'Original requirements and the complete frozen caller task still govern the engineering judgment; no engineering obligation is waived. '
  'Use the registered rtl_remote workspace actions. Native proposal supplies the complete candidate fingerprint; native evaluate executes the frozen calculator. '
  'Use actual submit/status job evidence in the supplied role scope. Full architecture-stage check_proposal, recompute, source-only preparation receipts '
  'and AUDIT_REPORT/final-JSON transport are not native tools or prerequisites here. Final prose or stage-only report frames do not publish native files. '
  'Do not request nested architecture delegation or extend tool permissions.\n')
 if role=='architecture':
  return common+(
   'Native design role: edit the candidate design.json, architecture.md and CHANGES.md consistently in the supplied candidate scope. '
   'Run evaluate after design changes, inspect its actual result and update affected commitments and prose. '
   'The caller repeats the final calculation before a fresh independent review and eligible versioned adoption; do not claim that your session completion is review or adoption.\n')
 if role=='review':
  return common+(
   'Native independent review role: the supplied audit scope is separate from the candidate. Do not edit the candidate. '
   'Review evaluate recalculates into private audit evidence and returns its report, execution and binding paths without changing any candidate file. '
   'Its binding covers the complete proposal identity; calculator success is mechanical evidence, not an engineering verdict or automatic acceptance. '
   'Inspect the actual final calculation and sources, obtain the candidate fingerprint with proposal before independent probes, and execute decisive independent jobs. '
   'Persist review.json in your audit scope using the native write action, with verdict (accept, revise or insufficient_evidence), '
   'design_sha256 from the exact candidate file, candidate_sha256 from proposal, reason, evidence (actual successful independent job IDs) and conditions. '
   'A failed check or missing required evidence is not approval. Native controller binding, final recalculation and adoption checks remain in force.\n')
 raise ValueError('unknown native revision role')

def _prompt(reference,skill,role,request,context,revision,scope,task):
 return (reference+'\nROLE: '+role+'\nOPTIONAL SKILL GUIDANCE (engineering methods):\n'+skill+task
  +'\nREQUEST:\n'+json.dumps(request,ensure_ascii=False)
  +'\nSERVER CONTEXT:\n'+json.dumps({'run_dir':context['remote_run_dir'],'scope':scope,'candidate_scope':revision['candidate_scope'],'tools':context['tools']},ensure_ascii=False)
  +_native_contract(role))

def summaries(results):
 return {k:v if k=='budget'else{p:v[p]for p in ('status','reason','completed','tool_isolation_ok','exit_code','stop_reason','elapsed_seconds')if p in v}for k,v in results.items()}

def run(request,context,remote):
 # Reject a lost or changed caller contract before creating any remote revision.
 task=_delivery_task(context)
 parent=context.get('session_id')
 actor=context.get('session_actor_sha256')
 if not isinstance(parent,str)or not parent or not isinstance(actor,str)or not re.fullmatch(r'[0-9a-f]{64}',actor):raise ValueError('trusted implementation parent session missing')
 revision=remote.call(dict(request,action='revision_begin',parent_session_id=parent,parent_actor_sha256=actor));harness=Path(context['harness_dir']);capabilities=context['capabilities'];results={};cache=Path(context['cache']);tag='revision-'+str(time.time_ns())
 reference=(harness/'skills/rtl-implementation/references/architecture-revision.md').read_text()
 for capability,role,scope in [('microarchitecture-design','architecture',revision['candidate_scope']),('microarchitecture-audit','review',revision['audit_scope'])]:
  if not capabilities.get(capability)and role=='review':results[role]={'status':'not_run','reason':'optional skill unavailable'};continue
  child=dict(context,scope=scope,role=role,candidate_scope=revision['candidate_scope']);path=cache/(tag+'-'+role+'.context.json');path.write_text(json.dumps(child))
  skill=(harness/'skills'/(capability if capabilities.get(capability)else'rtl-implementation')/'SKILL.md').read_text()
  prompt=_prompt(reference,skill,role,request,context,revision,scope,task)
  result=run_session(prompt,role if capabilities.get(capability)else'implementation',context['models'],Path(context['project_root']),path,cache,remote,tag+'-'+role);results[role]=result
  if not result['completed'] or not result['tool_isolation_ok']:return {'revision':revision,'results':summaries(results),'adopted':False,'reason':'delegate tool/transport failure'}
  if role=='architecture'and capabilities.get(capability):
   check=remote.call({'action':'evaluate','scope':scope});results['budget']=check
   if not check['ok']:return {'revision':revision,'results':summaries(results),'adopted':False,'reason':'actual budget calculation failed'}
 result=remote.call({'action':'revision_adopt','revision':revision['revision']});return dict(result,revision=revision,results=summaries(results))
