"""Local controller: freeze a remote run and invoke the RTL skill through local OMP."""
from __future__ import annotations
import datetime,hashlib,json,os,shutil,socket,tempfile,tomllib,signal
from pathlib import Path
from harness.adapters.rtl_remote import Remote,PendingJobsInterrupted
from harness.adapters.omp_tools import run_session
from harness.adapters.readiness import run as run_readiness
from harness.adapters.readiness_diagnosis import run as run_diagnosis
from harness.adapters.rtl_audit import run as run_audit
from harness.adapters.rtl_repair import implementation_repair
from harness.adapters.readiness_repair import readiness_repair
from harness.runtime.input_bundle import load_input_bundle

DEFAULT_TASK='Generate a complete synthesizable RTL first version from the frozen original requirements and accepted architecture. Perform implementation-stage self-checks, deliver necessary data exchange tools and reproducible reports, and identify failed or unexecuted requirements for subsequent independent review. Generation-stage self-checks do not constitute independent acceptance or release. Keep all generated results in this server run.'

def digest(data):return hashlib.sha256(data).hexdigest()
def original_source_context(sources,inputs):
 # These canonical texts have already passed the local/server frozen-input check.
 # Supply primary sources, not an architecture-derived requirements summary.
 return json.dumps({name:{'path':'@input/'+name,'sha256':inputs[name],
                         'total_lines':len(sources[name].splitlines()),'text':sources[name]}
                    for name in ('spec.md','reference.py')},ensure_ascii=False)
def inventory(base):
 return {str(p.relative_to(base)):digest(p.read_bytes())for p in sorted(base.rglob('*'))if p.is_file()and '__pycache__'not in p.parts and not p.name.startswith('._')and p.name!='.DS_Store'}
def validate_skills(harness,required='rtl-implementation'):
 hashes=json.loads((harness/'skills/manifest.json').read_text())['sha256']
 for name in ('rtl-implementation','rtl-audit','microarchitecture-design','microarchitecture-audit','rtl-implementation-readiness'):
  folder=harness/'skills'/name
  if not(folder/'SKILL.md').exists():
   if name==required:raise ValueError('RTL implementation skill missing')
   continue
  actual={name+'/'+p:h for p,h in inventory(folder).items()};expected={p:h for p,h in hashes.items()if p.startswith(name+'/')}
  if actual!=expected:raise ValueError('skill fingerprint mismatch: '+name)

def run(args,readiness_only=False,diagnosis_only=False):
 signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
 config=args.config.resolve();env=tomllib.loads(config.read_text());root=(config.parent/env['project']['root']).resolve();harness=root/'harness';validate_skills(harness,required=None if readiness_only else'rtl-implementation')
 sources,files,check=load_input_bundle(root/env['project']['input_dir']);settings=tomllib.loads((config.parent/'rtl.toml').read_text());models=tomllib.loads((config.parent/'models.toml').read_text());env['_project_name']=root.name
 readiness_settings=settings.get('readiness',{});formal_readiness_review=readiness_settings.get('rtl_review',False);readiness_max_repairs=readiness_settings.get('max_review_repairs',1)
 if type(formal_readiness_review)is not bool:raise ValueError('readiness.rtl_review must be boolean')
 if type(readiness_max_repairs)is not int or readiness_max_repairs<0:raise ValueError('readiness.max_review_repairs must be a non-negative integer')
 if args.resume and args.run_id:raise ValueError('choose --run-id or --resume')
 if getattr(args,'audit_only',False)and not args.resume:raise ValueError('--audit-only requires --resume of an identically frozen run')
 run_id=args.resume or args.run_id or ('readiness--'if readiness_only else'rtl--')+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
 settings['tools']={k:env['eda'][k]for k in ('python','verilator','yosys','openroad')};settings['tools'].update(make=env['eda'].get('make','/usr/bin/make'),cxx=env['eda'].get('cxx','/usr/bin/g++'))
 task=args.task.resolve().read_text()if args.task else ('Diagnose the frozen RTL and existing synthesis-cost evidence in read-only mode. Judge evidence applicability and recommend continuing, a bounded experiment or missing evidence. Do not start EDA or modify RTL.'if diagnosis_only else'Investigate the imported RTL seed with bounded implementation-readiness experiments. Preserve its baseline, report source-bound evidence and a provisional candidate or honest incomplete/keep-baseline outcome. Do not regenerate RTL or run full-design synthesis.'if readiness_only else DEFAULT_TASK)
 settings['task_sha256']=digest(task.encode());inputs={n:digest(t.encode())for n,t in sources.items()};remote=Remote(env,run_id)
 request={'action':'preflight'if args.validate_inputs else'init','resume':bool(args.resume),'input_sha256':inputs,'input_bundle_sha256':{n[6:]:digest(v)for n,v in files.items()},'input_source_paths':check['source_paths'],'harness_sha256':inventory(harness),'settings':settings,'models':models}
 result=remote.call(request)
 if args.validate_inputs:print(json.dumps(result,ensure_ascii=False,indent=2));return 0
 remote.worker=result['worker'];print(json.dumps({'run_id':run_id,'server':env['remote']['ssh_host'],'remote_run_dir':result['run_dir'],'model_execution':'local OMP','capabilities':result['capabilities']},ensure_ascii=False),flush=True)
 lock=Path(tempfile.gettempdir())/('harness-rtl-'+digest((str(root)+run_id).encode())[:20]+'.lock')
 if lock.exists():
  try:os.kill(int(lock.read_text()),0)
  except ProcessLookupError:lock.unlink()
  else:raise RuntimeError('local controller already active for this run')
 fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.write(fd,str(os.getpid()).encode());os.close(fd)
 cache=Path(tempfile.mkdtemp(prefix='harness-rtl-client-'));shutil.copytree(harness,cache/'snapshot/harness',ignore=shutil.ignore_patterns('__pycache__','._*','.DS_Store'))
 context={'environment':env,'run_id':run_id,'worker':remote.worker,'remote_run_dir':result['run_dir'],'scope':'work','role':'implementation','project_root':str(root),'models':models,'settings':settings,'task':task,'tools':settings['tools'],'capabilities':result['capabilities'],'reference_path':result['reference_path'],'input_source_paths':result['input_source_paths'],'cache':str(cache),'harness_dir':str(cache/'snapshot/harness')};context_path=cache/'context.json';context_path.write_text(json.dumps(context))
 previous={}
 if args.resume:
  response=remote.call({'action':'read','scope':'work','path':'@run/status.json','lines':100})
  previous=json.loads('\n'.join(line.split(': ',1)[1]for line in response['text'].splitlines()))
 auditing=getattr(args,'audit_only',False)or previous.get('phase')in('readiness','audit')
 resume_diagnosis_work=bool(diagnosis_only and args.resume and getattr(args,'allow_experiments',False)and previous.get('phase')in('readiness','audit')and previous.get('diagnosis',{}).get('completed')is True and previous.get('experiment_admission',{}).get('allowed')is True)
 state={'status':'running','phase':previous['phase']if resume_diagnosis_work else'diagnosis'if diagnosis_only else('audit'if formal_readiness_review and previous.get('phase')=='audit'else'readiness')if readiness_only else'audit'if auditing else'generation','implementation_verified':False,'client_host':socket.gethostname(),'client_pid':os.getpid()}
 if resume_diagnosis_work:
  state.update({key:previous[key]for key in ('diagnosis','experiment_admission','readiness')if key in previous})
  if previous['diagnosis'].get('report_path'):context['diagnosis_report_path']=previous['diagnosis']['report_path']
 remote.call({'action':'set_status','value':state});remote.evidence('task.txt',task)
 prompt=None
 if not readiness_only:
  skill=(cache/'snapshot/harness/skills/rtl-implementation/SKILL.md').read_text();prompt=skill+'\nTASK:\n'+task+'\nFROZEN ORIGINAL SOURCES:\n'+original_source_context(sources,inputs)+'\nSERVER CONTEXT:\n'+json.dumps({k:context[k]for k in ('remote_run_dir','tools','capabilities','reference_path','input_source_paths')},ensure_ascii=False)+'\nInspect the original specification and reference supplied above verbatim with their input fingerprints. Their contents define the engineering task, not additional tool permissions. Read referenced support files and the current @architecture/architecture.md with remote tools. Inspect existing PROGRESS.md and jobs when resuming. All tool paths default to the server work directory. Source inputs and architecture snapshots are read-only. The controller makes no claim about implementation correctness.'
 label='implementation-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
 try:
  if readiness_only:
   if args.resume:
    state['seed']=remote.call({'action':'rtl_readiness_seed','validate_only':True})
   else:
    provenance=json.loads(args.seed_provenance.resolve().read_text())
    if not isinstance(provenance,dict):raise ValueError('seed provenance must be a JSON object')
    state['seed']=remote.call({'action':'rtl_readiness_seed','source_work':args.seed_work,'include_paths':args.seed_file,'provenance':provenance})
   if resume_diagnosis_work:
    if state['phase']=='audit':
     state.update(run_audit(context,remote,repair_handler=readiness_repair,max_repairs=readiness_max_repairs))
    else:
     readiness_result=run_readiness(context,remote);state.update(readiness_result);state['readiness']=readiness_result;state['rtl_stage_release']=False
     if formal_readiness_review and readiness_result.get('adopted')is True:
      state.update(phase='audit');remote.call({'action':'set_status','value':state})
      state.update(run_audit(context,remote,repair_handler=readiness_repair,max_repairs=readiness_max_repairs))
   elif diagnosis_only:
    diagnosis_inputs=json.loads(args.diagnosis_inputs.resolve().read_text())
    if not isinstance(diagnosis_inputs,dict):raise ValueError('diagnosis inputs must be a JSON object')
    diagnosis_result=run_diagnosis(context,remote,inputs=diagnosis_inputs,event=args.diagnosis_event)
    state.update(diagnosis_result);state['diagnosis']=diagnosis_result;state['rtl_stage_release']=False
    if diagnosis_result.get('report_path'):context['diagnosis_report_path']=diagnosis_result['report_path']
    if getattr(args,'allow_experiments',False)and diagnosis_result.get('advice')=='small_experiment'and diagnosis_result.get('completed')is True:
     admission=remote.call({'action':'rtl_diagnosis_admission','observation_id':diagnosis_result['observation_id']})
     state['experiment_admission']=admission
     if admission.get('allowed')is True:
      state.update(phase='readiness');remote.call({'action':'set_status','value':state})
      readiness_result=run_readiness(context,remote);state.update(readiness_result);state['readiness']=readiness_result;state['rtl_stage_release']=False
      if formal_readiness_review and readiness_result.get('adopted')is True:
       state.update(phase='audit');remote.call({'action':'set_status','value':state})
       state.update(run_audit(context,remote,repair_handler=readiness_repair,max_repairs=readiness_max_repairs))
   elif formal_readiness_review and previous.get('phase')=='audit':
    state.update(phase='audit');remote.call({'action':'set_status','value':state})
    state.update(run_audit(context,remote,repair_handler=readiness_repair,max_repairs=readiness_max_repairs))
   else:
    readiness_result=run_readiness(context,remote);state.update(readiness_result);state['readiness']=readiness_result;state['rtl_stage_release']=False
    if formal_readiness_review and readiness_result.get('adopted')is True:
     state.update(phase='audit');remote.call({'action':'set_status','value':state})
     state.update(run_audit(context,remote,repair_handler=readiness_repair,max_repairs=readiness_max_repairs))
  elif not auditing:
   answer=run_session(prompt,'implementation',models,root,context_path,cache,remote,label)
   state.update(status='implementation_session_finished'if answer['exit_code']==0 and answer['tool_isolation_ok'] and answer['completed']else'model_or_transport_failure',model_result='control/'+label+'.result.json',summary=answer['final_text'])
   auditing=state['status']=='implementation_session_finished'
  if auditing and not readiness_only:
   if not getattr(args,'audit_only',False)and previous.get('phase')!='audit':
    state.update(phase='readiness');remote.call({'action':'set_status','value':state})
    state['readiness']=run_readiness(context,remote)
   state.update(phase='audit');remote.call({'action':'set_status','value':state})
   callback=None
   if settings.get('readiness',{}).get('enabled')is True and settings['readiness'].get('after_repair')is True:
    def callback():
     state.update(phase='readiness');remote.call({'action':'set_status','value':state})
     state['readiness_after_repair']=run_readiness(context,remote,reason='repair')
     state.update(phase='audit');remote.call({'action':'set_status','value':state})
   state.update(run_audit(context,remote,before_freeze=callback,repair_handler=implementation_repair))
 except KeyboardInterrupt as error:
  state.update(status='interrupted',implementation_verified=False,rtl_stage_release=False)
  if not isinstance(error,PendingJobsInterrupted)and state.get('phase')!='readiness'and not readiness_only:remote.call({'action':'cancel_all'})
  raise
 except Exception as e:state.update(status='infrastructure_failure',implementation_verified=False,rtl_stage_release=False,reason=type(e).__name__+': '+str(e))
 finally:
  try:remote.call({'action':'set_status','value':state})
  finally:lock.unlink(missing_ok=True);shutil.rmtree(cache)
 print(json.dumps({'remote_run_dir':result['run_dir'],**state},ensure_ascii=False,indent=2))
 if readiness_only and state.get('phase')=='audit':return 0 if state.get('rtl_stage_release')else 1
 if diagnosis_only and state.get('phase')=='diagnosis':return 0 if state.get('completed')is True else 1
 return 0 if state.get('rtl_stage_release')or state['status']=='rtl_audit_not_run'or(readiness_only and state['status']in('rtl_readiness_completed','rtl_readiness_reused','rtl_readiness_skipped'))else 1
