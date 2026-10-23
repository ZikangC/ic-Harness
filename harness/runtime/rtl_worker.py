#!/usr/bin/env python3
"""Server-side file and process operations. No hardware design decisions."""
from __future__ import annotations
import argparse, base64, bisect, datetime, hashlib, json, os, re, shutil, signal, subprocess, sys, time
from pathlib import Path
from contextlib import nullcontext
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from harness.runtime import rtl_readback_store as readback_store
from harness.runtime import rtl_readiness as readiness
from harness.runtime import rtl_repair_budget as repair_budgets


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,data):
 p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix(p.suffix+'.tmp');t.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');t.replace(p)
def safe(base,name):
 p=(base/name).resolve();p.relative_to(base.resolve());return p
def inventory(base):
 return {str(p.relative_to(base)):sha(p)for p in sorted(base.rglob('*'))if p.is_file()and '__pycache__'not in p.parts and not p.name.startswith('._')and p.name!='.DS_Store'}
def source_inventory(base):
 # Build dependencies include extensionless scripts, Makefiles and memory images.
 return closure_inventory(base)

def digest(value):
 return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def closure_inventory(base):
 """All entries in a self-contained tree, including modes and internal links."""
 base=base.resolve();result={}
 for p in sorted(base.rglob('*')):
  name=str(p.relative_to(base))
  if p.is_symlink():
   target=p.resolve(strict=True)
   try:relative=str(target.relative_to(base))
   except ValueError:raise ValueError('build closure has external symlink: '+name)
   result[name]={'type':'symlink','target':relative}
  elif p.is_file():result[name]={'type':'file','sha256':sha(p),'mode':p.stat().st_mode&0o777}
  elif p.is_dir():result[name]={'type':'directory','mode':p.stat().st_mode&0o777}
  else:raise ValueError('unsupported build closure entry: '+name)
 return result

def copy_closure(source,destination):
 before=closure_inventory(source);shutil.copytree(source,destination,symlinks=True)
 # Absolute internal links must resolve within the frozen copy, not live work.
 for name,entry in before.items():
  if entry['type']=='symlink':
   link=destination/name;link.unlink();link.symlink_to(os.path.relpath(destination/entry['target'],link.parent))
 if closure_inventory(source)!=before or closure_inventory(destination)!=before:raise ValueError('build closure changed while freezing')
 return before

def reviews(out):
 return [json.loads(p.read_text())for p in sorted((out/'rtl/reviews').glob('review-*.json'),key=lambda p:int(p.stem.split('-')[-1]))]

def get_review(out,name=None):
 if name is None:
  found=reviews(out)
  return found[-1]if found else None
 if not isinstance(name,str)or not re.fullmatch(r'review-[1-9][0-9]*',name):raise ValueError('invalid RTL review id')
 p=out/'rtl/reviews'/(name+'.json')
 if not p.is_file():raise ValueError('unknown RTL review: '+name)
 return json.loads(p.read_text())

def save_review(out,review):save(out/'rtl/reviews'/(review['review']+'.json'),review)

def active_jobs(out,scopes=None):
 result=[]
 for p in sorted((out/'jobs').glob('*')):
  pending=not(p/'result.json').exists()
  if not pending:
   isolation=json.loads((p/'result.json').read_text()).get('isolation')
   pending=isinstance(isolation,dict)and isolation.get('cleanup_complete')is False
  if (p/'request.json').is_file()and pending:
   request=json.loads((p/'request.json').read_text())
   if scopes is None or request.get('scope','work')in scopes:result.append(p.name)
 return result

def require_idle(out,scopes=None):
 if active_jobs(out,scopes):raise ValueError('unfinished jobs prevent RTL transition')

def architecture_identity(out):
 current=json.loads((out/'architecture-current.json').read_text())
 closure=closure_inventory(safe(out,current['path']))
 if current.get('baseline_sha256')and digest(closure)!=current['baseline_sha256']:raise ValueError('adopted architecture baseline changed')
 result={'current':current,'inventory':closure}
 if 'parent_session_id'in current:result['parent_readback']=readback_store.readback_status(out,current,closure)
 return result

def check_candidate(out,review,current_architecture=False):
 if closure_inventory(safe(out,review['candidate_scope']))!=review['work_inventory']:raise ValueError('frozen RTL candidate changed')
 if closure_inventory(safe(out,review['architecture_scope']))!=review['architecture_identity']['inventory']:raise ValueError('frozen candidate architecture changed')
 identity={'work':review['work_inventory'],'architecture':review['architecture_identity']}
 if digest(identity)!=review['candidate_sha256']:raise ValueError('candidate metadata fingerprint changed')
 if current_architecture and architecture_identity(out)!=review['architecture_identity']:raise ValueError('architecture changed since RTL candidate freeze')

def integrity_guards(out):
 guards=[]
 for handoff in sorted((out/'architecture').glob('revision-*/handoff.json')):
  published=json.loads(handoff.read_text()).get('integrity_guards',[])
  check_guards(out,published);guards.extend(published)
 for review in reviews(out):
  check_candidate(out,review)
  # The successful check already hashed these trees against these exact maps.
  # Reuse only this call's validated maps; every call still checks fresh bytes.
  validated={'candidate_scope':review['work_inventory'],'architecture_scope':review['architecture_identity']['inventory']}
  for key in ('candidate_scope','architecture_scope'):
   guards.append({'path':review[key],'inventory':validated[key]})
  repair=review.get('repair')
  if review.get('decision'):
   path=review['audit_scope']
   if closure_inventory(safe(out,path))!=review['decision_audit_inventory']:raise ValueError('decided RTL audit evidence changed')
   guards.append({'path':path,'inventory':review['decision_audit_inventory']})
  elif repair and repair['status']=='active':
   path=review['audit_scope']
   if closure_inventory(safe(out,path))!=repair['audit_inventory']:raise ValueError('audit tests changed during RTL repair')
   guards.append({'path':path,'inventory':repair['audit_inventory']})
 return guards

def check_guards(out,guards):
 for guard in guards:
  if closure_inventory(safe(out,guard['path']))!=guard['inventory']:raise ValueError('protected RTL tree changed: '+guard['path'])

def review_status(out,review):
 if review is None:return {'review':None}
 check_candidate(out,review)
 decision=review.get('decision')
 if decision and sha(safe(out,decision['report_path']))!=decision['report_sha256']:raise ValueError('RTL decision report changed')
 if decision and closure_inventory(safe(out,review['audit_scope']))!=review['decision_audit_inventory']:raise ValueError('decided RTL audit evidence changed')
 result={key:value for key,value in review.items()if key not in ('work_inventory','architecture_identity','decision_audit_inventory')}
 if result.get('repair'):result['repair']={key:value for key,value in result['repair'].items()if key not in ('audit_inventory','work_inventory')}
 result['inventory_path']='rtl/reviews/'+review['review']+'.json'
 result['architecture_identity_sha256']=digest(review['architecture_identity'])
 result['architecture_current']=architecture_identity(out)==review['architecture_identity']
 result['parent_readback']=review['architecture_identity'].get('parent_readback')
 result['work_current']=closure_inventory(out/'work')==review['work_inventory']
 return result

def candidate_architecture_context(out,review):
 """Controller-only primary sources from this candidate's frozen packet.

 The normal status remains small. No hardware fields or approval conclusions
 are inferred here; provenance and decisions are carried as original text.
 """
 base=safe(out,review['architecture_scope']);identity=review['architecture_identity']
 expected=identity['inventory']
 if closure_inventory(base)!=expected:raise ValueError('frozen candidate architecture changed')
 files={}
 for name,entry in expected.items():
  target=entry
  if entry['type']=='symlink':target=expected.get(entry['target'],{})
  if target.get('type')!='file':continue
  path=safe(base,name);raw_size=path.stat().st_size
  files[name]={'path':'@run/'+review['architecture_scope']+'/'+name,
               'sha256':target['sha256'],'bytes':raw_size}
 primary={}
 for name in ('architecture.md','SOURCE.md','decision.json'):
  path='@run/'+review['architecture_scope']+'/'+name
  if name not in expected:
   if name=='architecture.md':raise ValueError('frozen candidate architecture draft missing')
   primary[name]={'status':'missing','path':path};continue
  if name not in files:raise ValueError('architecture primary source is not a file: '+name)
  raw=safe(base,name).read_bytes()
  if hashlib.sha256(raw).hexdigest()!=files[name]['sha256']or len(raw)!=files[name]['bytes']:
   raise ValueError('architecture primary source changed: '+name)
  primary[name]={'status':'present',**files[name],'content':raw.decode('utf-8')}
 actual=architecture_identity(out)
 if closure_inventory(base)!=expected:raise ValueError('frozen candidate architecture changed during context read')
 return {'version':1,'review':review['review'],'candidate_sha256':review['candidate_sha256'],
         'scope':review['architecture_scope'],'identity':identity,'identity_sha256':digest(identity),
         'run_current':actual['current'],'matches_run_current':actual==identity,
         'files':files,'primary':primary}

def verify_inputs(out):
 m=json.loads((out/'manifest.json').read_text())
 for n,h in m['input_sha256'].items():
  if sha(out/'snapshot/input'/n)!=h:raise ValueError('frozen input changed: '+n)
 for n,h in m.get('input_bundle_sha256',{}).items():
  if sha(out/'snapshot/input'/n)!=h:raise ValueError('frozen input metadata changed: '+n)
 for n,h in m.get('physical_input_sha256',{}).items():
  if sha(out/'snapshot/input-physical'/n)!=h or sha(Path(m['project_root'])/'input'/n)!=h:raise ValueError('original physical input changed: '+n)
 if inventory(out/'snapshot/harness')!=m['harness_sha256']:raise ValueError('frozen harness changed')
 if inventory(out/'snapshot/architecture')!=m['architecture_sha256']:raise ValueError('frozen architecture changed')
 return m

def proposal_identity(candidate):
 closure=closure_inventory(candidate)
 return {'candidate_sha256':digest(closure),'files':{n:e['sha256']for n,e in closure.items()if e['type']=='file'},'entries':len(closure)}

def architecture_budget(out,candidate,report,report_path=None):
 document=json.loads((candidate/'design.json').read_text());selected=document.get('selected');value=report.get('candidates',{}).get(selected,{})
 summary={k:value[k]for k in ('macro_inventory_bits',)if k in value}
 area=value.get('ledger',{}).get('area_total')
 if area is not None:summary['area_total']=area
 summary['workloads']={name:{k:v[k]for k in ('clock_hz','task_cycles','cycles_per_item','items_per_second')if k in v}for name,v in value.get('workloads',{}).items()}
 report_path=report_path or candidate/'budgets.json'
 return {'ok':report['ok'],'design_sha256':report.get('design_sha256'),'gate':report.get('gate'),'errors':report.get('errors'),'selected':selected,'selected_estimates':summary,'candidates':{n:{'checks':v.get('checks'),'errors':v.get('errors')}for n,v in report.get('candidates',{}).items()},'full_report':str(report_path.relative_to(out)),'report_sha256':sha(report_path)}

def publish_architecture_baseline(out,revision,candidate,candidate_id,status,origin,review_sha256):
 """Keep reviewed proposal bytes intact; publish native revision provenance separately."""
 staging=revision/'baseline.preparation';baseline=revision/'baseline'
 readback_store.require_active_parent(out,origin.get('parent_session_id'),origin.get('parent_actor_sha256'))
 if staging.exists()or baseline.exists():raise ValueError('architecture baseline publication already attempted')
 proposal=copy_closure(candidate,staging)
 if digest(proposal)!=candidate_id:raise ValueError('architecture proposal changed before publication')
 review=revision/'audit/review.json';binding=staging/'budget_binding.json'
 reviewed=status=='accept';calculated=binding.exists();approved=reviewed and calculated and(out/'snapshot/harness/skills/microarchitecture-audit/SKILL.md').exists()
 # Native records replace proposal entries without following internal symlinks.
 for name in ('review.json','decision.json','SOURCE.md'):
  p=staging/name
  if p.is_symlink()or p.is_file():p.unlink()
 if review_sha256 is not None:
  shutil.copyfile(review,staging/'review.json')
  if sha(staging/'review.json')!=review_sha256:raise ValueError('architecture review changed before publication')
 else:
  if review.exists():raise ValueError('architecture review changed before publication')
  (staging/'review.json').write_text(json.dumps({'verdict':'not_run','candidate_sha256':candidate_id,'reason':'optional architecture review unavailable'},indent=2)+'\n')
 decision={'deliverable':approved,'implementation_verified':False,'scope':'implementation architecture revision; not full architecture-stage acceptance or RTL validation','review_status':status,'budget_status':'passed'if calculated else'not_run','candidate_sha256':candidate_id,'design_sha256':sha(staging/'design.json'),'review_path':str(review.relative_to(out))if review_sha256 is not None else None,'review_sha256':sha(staging/'review.json'),'budget_binding_sha256':sha(staging/'budget_binding.json')if calculated else None}
 (staging/'decision.json').write_text(json.dumps(decision,indent=2)+'\n')
 (staging/'SOURCE.md').write_text('# Current implementation architecture revision\n\nThis native handoff replaces inherited approval records; it does not validate RTL or physical implementation.\n\n'+
  'Parent baseline: @run/'+str(Path(origin['baseline_path']).relative_to(out))+' (file-map SHA256 '+digest(origin['baseline_sha256'])+').\n'+
  'Reviewed proposal: @run/'+str(candidate.relative_to(out))+' (complete closure SHA256 '+candidate_id+').\n'+
  'Current review and scoped decision: @architecture/review.json and @architecture/decision.json. Review status: '+status+'; budget status: '+decision['budget_status']+'.\n'+
  'Original requirements remain at @input/. Current design/prose/budgets and all proposal sidecars are carried together. Prior approvals remain historical at the parent path.\n')
 if origin.get('baseline_identity')is not None and architecture_identity(out)!=origin['baseline_identity']:raise ValueError('architecture baseline changed during revision')
 required=readback_store.required_files(out,staging,baseline)
 closure=closure_inventory(staging);staging.rename(baseline)
 handoff={'path':str(baseline.relative_to(out)),'baseline_sha256':digest(closure),'candidate_path':str(candidate.relative_to(out)),'candidate_sha256':candidate_id,'approved_for_implementation':approved,'review_status':status if approved else'not_run','native_review_status':status,'files':inventory(baseline),'scope':decision['scope'],'parent_session_id':origin['parent_session_id'],'parent_actor_sha256':origin['parent_actor_sha256'],'readback_required_files':required}
 if calculated:handoff['budget']=architecture_budget(out,baseline,json.loads((baseline/'budgets.json').read_text()))
 audit=closure_inventory(revision/'audit')
 if review_sha256 is not None and sha(review)!=review_sha256:raise ValueError('architecture review changed before publication')
 handoff['integrity_guards']=[{'path':str(candidate.relative_to(out)),'inventory':proposal},{'path':str((revision/'audit').relative_to(out)),'inventory':audit},{'path':str(baseline.relative_to(out)),'inventory':closure}]
 check_guards(out,handoff['integrity_guards'])
 save(revision/'handoff.json',handoff)
 handoff['handoff_path']=str((revision/'handoff.json').relative_to(out));handoff['handoff_sha256']=sha(revision/'handoff.json')
 return handoff

def scope_relative_path(scope,name,allow_root=False):
 """Resolve ordinary local or current-scope-qualified paths without guessing siblings."""
 if not isinstance(name,str)or not name or '\0'in name:raise ValueError('path must be a nonempty relative string')
 if name.startswith('@'):raise ValueError('aliases are read-only; use a scope-relative path')
 if name.startswith('/')or'..'in name.split('/'):raise ValueError('absolute paths and parent traversal are unavailable; use a scope-relative path')
 force_local=name.startswith('./');name=str(Path(name))
 if name.startswith('@'):raise ValueError('aliases are read-only; use a scope-relative path')
 reserved=r'^(?:work|readiness/campaign-[1-9][0-9]*/(?:work|audit)|architecture/revision-[0-9]+/(?:candidate|audit|baseline(?:\.preparation)?)|audit/review-[1-9][0-9]*|rtl/candidates/candidate-[1-9][0-9]*/(?:work|architecture)|snapshot/(?:harness|input(?:-physical)?|architecture))(?:/|$)'
 if not force_local:
  if name==scope:name='.'
  elif name.startswith(scope+'/'):
   name=name[len(scope)+1:]
   if name==scope or name.startswith(scope+'/')or re.match(reserved,name):raise ValueError('repeated or nested run-qualified prefix; use ./ for a literal local path')
  elif re.match(reserved,name):
   raise ValueError('run-qualified path is outside current scope '+scope+'; use ./ for a literal local path or @run/ for read-only access')
 if name=='.'and not allow_root:raise ValueError('write requires a file path within current scope '+scope)
 return './'+name if force_local and name!='.'else name

def scope_local_locator(scope,name):
 """Keep a listed local name bound to the same file when supplied back to read."""
 try:
  if scope_relative_path(scope,name,allow_root=True)==name:return name
 except ValueError:pass
 # Native aliases remain read-only; a literal top-level @ name needs its run locator.
 return '@run/'+scope+'/'+name if name.startswith('@')else'./'+name

def read_path(out,work,name,physical=False,review=None):
 if name.startswith('@input/'):
  relative=name[7:]
  if physical:
   mapping=json.loads((out/'manifest.json').read_text())['input_source_paths']
   if relative in mapping:return safe(out/'snapshot/input-physical',mapping[relative])
  return safe(out/'snapshot/input',relative)
 if name.startswith('@architecture/'):
  current=architecture_identity(out)['current'];return safe(out/current['path'],name[14:])
 if name.startswith('@skill/'):return safe(out/'snapshot/harness/skills',name[7:])
 if name.startswith('@run/'):return safe(out,name[5:])
 if name.startswith('@candidate/'):
  if review is None and work.parent==out/'audit':review=work.name
  if review is None:raise ValueError('@candidate requires an audit scope or review id')
  metadata=get_review(out,review);check_candidate(out,metadata)
  return safe(out/metadata['candidate_scope'],name[11:])
 return safe(work,scope_relative_path(str(work.relative_to(out)),name,allow_root=True))

def read_text_page(raw,name,source_id,req,resolved_run_path=None):
 """Bound every page to one source; byte ranges include original line endings."""
 source_sha256=hashlib.sha256(raw).hexdigest();decoded=raw.decode('utf-8')
 parts=decoded.splitlines(keepends=True);starts=[0];characters=[0]
 for part in parts:starts.append(starts[-1]+len(part.encode('utf-8')));characters.append(characters[-1]+len(part))
 count=min(300,int(req.get('lines',120)))
 if count<1:raise ValueError('read lines must be positive')
 cursor=req.get('cursor')
 if cursor is not None:
  if 'start'in req:raise ValueError('read cursor cannot be combined with start')
  try:
   if not isinstance(cursor,str)or len(cursor)>4096:raise ValueError()
   value=json.loads(base64.b64decode(cursor+'='*(-len(cursor)%4),altchars=b'-_',validate=True))
   if (not isinstance(value,dict)or set(value)!={'v','source_id','sha256','byte_offset'}or
       type(value['v'])is not int or value['v']!=1 or value['source_id']!=source_id or value['sha256']!=source_sha256):raise ValueError()
   offset=value['byte_offset']
   if type(offset)is not int or not 0<=offset<=len(raw):raise ValueError()
   if offset<len(raw)and raw[offset]&0xc0==0x80:raise ValueError()
  except (ValueError,TypeError,KeyError,UnicodeError):raise ValueError('invalid, stale or differently bound read cursor')from None
 else:offset=starts[min(max(1,int(req.get('start',1)))-1,len(parts))]
 first=min(bisect.bisect_right(starts,offset)-1,len(parts))
 character_offset=characters[first]+len(raw[starts[first]:offset].decode('utf-8'))
 requested_end=starts[min(first+count,len(parts))]
 # The content field is exact; text preserves the numbered display used by
 # existing small control-receipt readers. Bound their combined JSON response.
 window=raw[offset:requested_end].decode('utf-8')
 def position(character):
  line=min(bisect.bisect_right(characters,character)-1,len(parts))
  if line==len(parts)and parts and not parts[-1].endswith(('\n','\r','\v','\f','\x1c','\x1d','\x1e','\x85','\u2028','\u2029')):line-=1
  return {'line':line+1,'column':character-characters[line]+1}
 def response(char_count):
  content=window[:char_count];end=offset+len(content.encode('utf-8'))
  next_cursor=None
  if end<len(raw):
   value={'v':1,'source_id':source_id,'sha256':source_sha256,'byte_offset':end}
   next_cursor=base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).decode().rstrip('=')
  return {'path':name,'resolved_run_path':resolved_run_path,'sha256':source_sha256,'source_id':source_id,'bytes':len(raw),'encoding':'utf-8',
          'total_lines':len(parts),'text':'\n'.join(f'{first+i+1}: {line}'for i,line in enumerate(content.splitlines())),
          'content':content,'returned_range':{'byte_start':offset,'byte_end':end,'start':position(character_offset),'end':position(character_offset+char_count)},
          'truncated':end<requested_end,'eof':end==len(raw),'next_cursor':next_cursor}
 low,high=0,min(len(window),24000)
 while low<high:
  middle=(low+high+1)//2
  if len(json.dumps(response(middle),ensure_ascii=False).encode('utf-8'))<=24000:low=middle
  else:high=middle-1
 result=response(low)
 if len(json.dumps(result,ensure_ascii=False).encode('utf-8'))>24000 or(window and low==0):raise ValueError('read metadata exceeds page limit')
 return result

def rtl_transition(out,req):
 action=req['action']
 if action=='rtl_review_begin':
  require_idle(out)
  for previous in reviews(out):
   if (previous.get('repair')or{}).get('status')=='active':raise ValueError('unfinished RTL repair prevents candidate freeze')
  seed=get_review(out,req['seed_review'])if req.get('seed_review')else None
  if seed and(seed.get('repair')or{}).get('status')!='completed':raise ValueError('seed review needs completed RTL repair')
  if seed:review_status(out,seed)
  existing=reviews(out);number=1+max([int(r['review'].split('-')[-1])for r in existing],default=0)
  while(out/f'rtl/candidates/candidate-{number}').exists()or(out/f'audit/review-{number}').exists():number+=1
  name=f'review-{number}';candidate=f'rtl/candidates/candidate-{number}/work';arch=f'rtl/candidates/candidate-{number}/architecture';audit=f'audit/{name}'
  architecture=architecture_identity(out)
  work_inventory=copy_closure(out/'work',out/candidate)
  copy_closure(safe(out,architecture['current']['path']),out/arch)
  if architecture_identity(out)!=architecture:raise ValueError('architecture changed while freezing RTL candidate')
  if seed:copy_closure(out/seed['audit_scope'],out/audit)
  else:(out/audit).mkdir(parents=True)
  value={'review':name,'audit_scope':audit,'candidate_scope':candidate,'architecture_scope':arch,'candidate_sha256':digest({'work':work_inventory,'architecture':architecture}),'work_inventory':work_inventory,'architecture_identity':architecture,'seed_review':seed['review']if seed else None,'decision':None,'repair':None,'created_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
  save_review(out,value)
  return review_status(out,value)
 review=get_review(out,req.get('review'))
 if action=='rtl_review_status':
  result=review_status(out,review)
  include=req.get('include_architecture_context',False)
  if type(include)is not bool:raise ValueError('architecture context flag must be boolean')
  if include:
   if review is None:raise ValueError('no frozen RTL candidate architecture')
   result['architecture_context']=candidate_architecture_context(out,review)
  return result
 if review is None:raise ValueError('no RTL review exists')
 if action=='rtl_review_decision':
  if review.get('decision'):raise ValueError('RTL review already has a decision')
  if review.get('repair'):raise ValueError('RTL review already entered repair')
  require_idle(out);check_candidate(out,review,current_architecture=True)
  if closure_inventory(out/'work')!=review['work_inventory']:raise ValueError('live work changed since RTL candidate freeze')
  name=req['report_path'];prefix=review['audit_scope']+'/'
  if name.startswith(prefix):name=name[len(prefix):]
  report=safe(out/review['audit_scope'],name);data=json.loads(report.read_text())
  verdict=data.get('verdict');allowed={'pass','revise','insufficient_evidence','blocked'}
  expected=req.get('expected_verdict')
  if verdict not in allowed or(expected=='final'and verdict=='revise')or(expected!='final'and verdict!=expected):raise ValueError('RTL verdict differs from selected decision action')
  if verdict=='pass'and review['architecture_identity']['current'].get('approved_for_implementation')is False:raise ValueError('adopted architecture revision lacks required approval evidence')
  if verdict=='pass'and 'parent_session_id'in review['architecture_identity']['current']and review['architecture_identity'].get('parent_readback',{}).get('complete')is not True:raise ValueError('adopted architecture revision lacks complete original parent readback')
  if data.get('candidate_sha256')!=review['candidate_sha256']:raise ValueError('RTL report has stale candidate binding')
  if not isinstance(data.get('reason'),str)or not data['reason'].strip():raise ValueError('RTL report requires reason')
  evidence=data.get('evidence')
  if not isinstance(evidence,list)or any(not isinstance(j,str)or not re.fullmatch(r'[0-9]+',j)for j in evidence):raise ValueError('RTL evidence must be job IDs')
  successful=[]
  for job_id in evidence:
   job=safe(out/'jobs',job_id)
   if not(job/'request.json').is_file()or not(job/'result.json').is_file():raise ValueError('RTL evidence job is missing or unfinished: '+job_id)
   request=json.loads((job/'request.json').read_text());result=json.loads((job/'result.json').read_text())
   if request.get('scope')!=review['audit_scope']or request.get('candidate_sha256')!=review['candidate_sha256']or result.get('candidate_sha256')!=review['candidate_sha256']:raise ValueError('RTL evidence is not bound to this independent review candidate')
   if result.get('exit_code')==0 and not result.get('timed_out')and not result.get('error'):successful.append(job_id)
  if verdict=='pass'and not successful:raise ValueError('pass requires successful independent evidence bound to candidate')
  decision={'verdict':verdict,'report_path':str(report.relative_to(out)),'report_sha256':sha(report),'candidate_sha256':review['candidate_sha256'],'reason':data['reason'],'evidence':evidence,'recorded_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
  review['decision']=decision;review['decision_audit_inventory']=closure_inventory(out/review['audit_scope']);save_review(out,review);return review_status(out,review)
 if action=='rtl_repair_begin':
  decision=review.get('decision')
  if not decision or decision['verdict']!='revise':raise ValueError('RTL repair requires revise decision')
  require_idle(out);review_status(out,review)
  repair=review.get('repair')
  if repair:
   if repair['status']=='active':integrity_guards(out)
   return review_status(out,review)
  if any((r.get('repair')or{}).get('status')=='active'for r in reviews(out)if r['review']!=review['review']):raise ValueError('another RTL repair is active')
  check_candidate(out,review,current_architecture=True)
  if closure_inventory(out/'work')!=review['work_inventory']:raise ValueError('live work changed before RTL repair')
  review['repair']={'status':'active','audit_inventory':closure_inventory(out/review['audit_scope']),'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()};save_review(out,review)
  return review_status(out,review)
 if action=='rtl_repair_end':
  repair=review.get('repair')
  if not repair:raise ValueError('RTL repair has not begun')
  require_idle(out);review_status(out,review)
  if repair['status']=='completed':return review_status(out,review)
  integrity_guards(out)
  for job in (out/'jobs').glob('*'):
   request=json.loads((job/'request.json').read_text())
   if request.get('repair_review')==review['review']:
    result=json.loads((job/'result.json').read_text())
    if result.get('integrity_error'):raise ValueError('RTL repair job changed protected evidence: '+job.name)
  repair.update(status='completed',work_inventory=closure_inventory(out/'work'),completed_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
  repair['work_sha256']=digest(repair['work_inventory']);save_review(out,review)
  result=review_status(out,review);result['work_sha256']=repair['work_sha256'];return result
 raise ValueError('unsupported RTL transition: '+action)

def supervisor(job):
 # New runs never fall back to the old post-check-only executor.
 raise RuntimeError('jobs require the configured read-only Docker job launcher')

def writable_scope_links(base):
 """Reject writable aliases to an inode outside the declared source scope."""
 counts={};links={}
 for path in base.rglob('*'):
  if path.is_symlink()or not path.is_file():continue
  info=path.stat();key=(info.st_dev,info.st_ino)
  counts[key]=counts.get(key,0)+1;links[key]=info.st_nlink
 if any(links[key]!=count for key,count in counts.items()):
  raise ValueError('writable scope contains external hardlinks')

def job_check(out,req):
 job_id=req.get('job_id')
 if not isinstance(job_id,str)or not re.fullmatch(r'[0-9]+',job_id):raise ValueError('invalid job id')
 phase=req.get('phase')
 if phase not in ('before','after'):raise ValueError('invalid job check phase')
 job=safe(out/'jobs',job_id);request=json.loads((job/'request.json').read_text())
 manifest=json.loads((out/'manifest.json').read_text())
 if request.get('job_isolation')!=manifest.get('job_isolation')or not request.get('job_isolation'):raise ValueError('job isolation identity changed')
 scope=request.get('scope')
 if not isinstance(scope,str)or(scope!='work'and not re.fullmatch(r'(architecture/revision-[0-9]+/(candidate|audit)|audit/review-[1-9][0-9]*|readiness/campaign-[1-9][0-9]*/(work|audit))',scope)):raise ValueError('invalid job scope')
 source=safe(out,scope)
 if request.get('run_dir')!=str(out)or request.get('source_dir')!=str(source):raise ValueError('job paths differ from recorded scope')
 safe(source,os.path.relpath(request['cwd'],source))
 check_guards(out,request.get('integrity_guards',[]));writable_scope_links(source)
 if scope.startswith('readiness/'):
  campaign=readiness.from_scope(out,scope);readiness.check_baseline(out,campaign)
  if request.get('readiness_budget',{}).get('source_sha256')!=campaign['source_sha256']:raise ValueError('readiness source identity changed')
  return {'ok':True,('source_at_start_sha256'if phase=='before'else'source_after_sha256'):campaign['source_files']}
 return {'ok':True,('source_at_start_sha256'if phase=='before'else'source_after_sha256'):source_inventory(source)}

def summary_job(job):
 req=json.loads((job/'request.json').read_text());result=job/'result.json'
 s={'job_id':job.name,'status':'completed'if result.exists()else'running','argv':req['argv'],'cwd':req['cwd']}
 if result.exists():s.update(json.loads(result.read_text()))
 if isinstance(s.get('isolation'),dict)and s['isolation'].get('cleanup_complete')is False:s['status']='cleanup_failed'
 # The complete closure remains server-side; summaries must stay small.
 for key in ('source_sha256','source_at_start_sha256','source_after_sha256'):
  files=s.get(key)
  if isinstance(files,dict)and(key!='source_sha256'or len(files)>40):
   s[key+'_closure_sha256']=digest(files);s[key+'_entries']=len(files);s[key+'_truncated']=True
   s[key]={n:files[n]for n in sorted(files)[:40]}if key=='source_sha256'else{}
 s['request_path']=str(job/'request.json');s['result_path']=str(result)
 for n in ('stdout','stderr'):
  p=job/(n+'.log');s[n+'_path']=str(p)
  if p.exists():
   with p.open('rb')as f:f.seek(max(0,p.stat().st_size-4000));s[n+'_tail']=f.read().decode(errors='replace')
 return s

def dispatch(root,run_id,req):
 if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',run_id):raise ValueError('unsafe run id')
 out=root/'output'/run_id;action=req['action']
 if action in ('init','preflight'):
  import shutil
  baseline=root/'result/architecture';names=['architecture.md','design.json','budgets.json','review.json','decision.json','input/spec.md','input/reference.py','SOURCE.md']
  names += [str(p.relative_to(baseline)) for p in (baseline/'input').rglob('*') if p.is_file() and str(p.relative_to(baseline)) not in names and not p.name.startswith('._') and p.name!='.DS_Store']
  for n in names:
   if not (baseline/n).is_file():raise ValueError('missing architecture handoff: '+n)
  if json.loads((baseline/'decision.json').read_text()).get('deliverable')is not True:raise ValueError('baseline not deliverable')
  # Bind every copied packet file, including optional budget/readback evidence.
  names=sorted(inventory(baseline))
  sys.path.insert(0,str(root));from harness.runtime.input_bundle import load_input_bundle
  source_texts,source_files,bundle_check=load_input_bundle(root/'input');inputs={n:hashlib.sha256(t.encode()).hexdigest()for n,t in source_texts.items()}
  if any(sha(baseline/'input'/n)!=h for n,h in inputs.items()):raise ValueError('current input differs from architecture freeze; assess applicability')
  bundle_hashes={n[6:]:hashlib.sha256(data).hexdigest()for n,data in source_files.items()}
  source_paths=bundle_check['source_paths']
  physical={name:sha(root/'input'/name)for name in set(source_paths.values())}
  if (root/'input/sources.json').exists():physical['sources.json']=sha(root/'input/sources.json')
  if req.get('input_bundle_sha256',bundle_hashes)!=bundle_hashes or req.get('input_source_paths',source_paths)!=source_paths:raise ValueError('local/server input closure metadata differs')
  if inputs!=req['input_sha256']:raise ValueError('local and server inputs differ')
  code=inventory(root/'harness')
  if code!=req['harness_sha256']:raise ValueError('local and server harness differ')
  isolation=req.get('job_isolation')
  if not isinstance(isolation,dict)or isolation.get('backend')!='docker_readonly_job_v1':raise ValueError('read-only Docker job backend is required')
  if isolation.get('uid')!=os.getuid()or isolation.get('gid')!=os.getgid():raise ValueError('job execution user differs from configured container user')
  if action=='preflight':return {'ok':True,'input_sha256':inputs,'architecture_inputs_match':True,'server_harness_matches_local':True,'container_project':str(root),'reference_execution':'not_run','job_isolation':isolation}
  identity={'input_sha256':inputs,'input_bundle_sha256':bundle_hashes,'input_source_paths':source_paths,'physical_input_sha256':physical,'project_root':str(root),'harness_sha256':code,'architecture_sha256':{n:sha(baseline/n)for n in names},'settings':req['settings'],'models':req['models'],'job_isolation':isolation}
  if out.exists():
   if not req.get('resume'):raise ValueError('run already exists; use --resume')
   previous=json.loads((out/'manifest.json').read_text())
   if any(previous[k]!=v for k,v in identity.items()):raise ValueError('resume identity differs; use a new run')
   if inventory(out/'snapshot/harness')!=code:raise ValueError('frozen harness changed')
   if inventory(out/'snapshot/architecture')!=identity['architecture_sha256']:raise ValueError('frozen architecture changed')
   verify_inputs(out)
  else:
   out.mkdir(parents=True);(out/'snapshot').mkdir();shutil.copytree(root/'harness',out/'snapshot/harness',ignore=shutil.ignore_patterns('__pycache__','._*','.DS_Store'));shutil.copytree(baseline,out/'snapshot/architecture');(out/'snapshot/input').mkdir();(out/'snapshot/input-physical').mkdir()
   for n,data in source_files.items():
    p=out/'snapshot'/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
   for n in physical:
    p=out/'snapshot/input-physical'/n;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(root/'input'/n,p)
   (out/'work').mkdir();save(out/'manifest.json',dict(identity,stage='rtl',created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),model_execution='local OMP',tool_execution='configured server container'))
   save(out/'status.json',{'status':'initialized','implementation_verified':False});save(out/'architecture-current.json',{'path':'snapshot/architecture','review_status':'accepted_estimate_only'})
   verify_inputs(out)
  return {'run_dir':str(out),'worker':str(out/'snapshot/harness/runtime/rtl_worker.py'),'work_dir':str(out/'work'),'input_sha256':inputs,'capabilities':{n:(out/'snapshot/harness/skills'/n/'SKILL.md').is_file()for n in ('microarchitecture-design','microarchitecture-audit')},'tools':req['settings'].get('tools',{}),'input_source_paths':source_paths,'reference_path':str(out/'snapshot/input-physical'/source_paths['reference.py'])}
 verify_inputs(out)
 if action=='rtl_readiness_seed':
  from harness.runtime.rtl_readiness_seed import seed
  return seed(out,req)
 if action in ('rtl_diagnosis_prepare','rtl_diagnosis_complete','rtl_diagnosis_admission'):
  from harness.runtime import rtl_diagnosis
  return {'rtl_diagnosis_prepare':rtl_diagnosis.prepare,
          'rtl_diagnosis_complete':rtl_diagnosis.complete,
          'rtl_diagnosis_admission':rtl_diagnosis.admission}[action](out,req)
 if action=='rtl_readiness_begin':return readiness.begin(out,req,require_idle)
 if action=='rtl_readiness_status':return readiness.status(out,req)
 if action=='rtl_readiness_resolve':return readiness.resolve(out,req)
 if action in ('rtl_readiness_handoff','rtl_readiness_handoff_status'):
  from harness.runtime import rtl_readiness_handoff
  return rtl_readiness_handoff.commit(out,req,require_idle)if action=='rtl_readiness_handoff'else rtl_readiness_handoff.status(out,req)
 if action=='rtl_readiness_complete':return readiness.complete(out,req,require_idle)
 if action=='rtl_session_begin':return readback_store.begin_session(out,req)
 if action=='rtl_session_end':
  actor,_=readback_store._actor(out,req.get('session_id'))
  if actor['role']in ('rtl_readiness','readiness_review','readiness_diagnosis'):return readback_store.end_session(out,req,{}, {})
  current=json.loads((out/'architecture-current.json').read_text())
  return readback_store.end_session(out,req,current,closure_inventory(safe(out,current['path'])))
 if action=='job_check':return job_check(out,req)
 if action in ('put_evidence','append_evidence'):
  p=safe(out,req['path']);p.relative_to(out/'control');readback_store.protect_control_path(out,p,action);p.parent.mkdir(parents=True,exist_ok=True)
  with p.open('a'if action=='append_evidence'else'w')as f:f.write(req['content'])
  return {'path':str(p),'sha256':sha(p)}
 if action=='jobs':
  if req.get('pending_only'):
   return {'jobs':[{'job_id':name,'status':'cleanup_failed'if(out/'jobs'/name/'result.json').exists()else'running'}for name in active_jobs(out)]}
  return {'jobs':[summary_job(p)for p in sorted((out/'jobs').glob('*'))[-20:]]}
 if action=='cancel_all':
  raise ValueError('cancel jobs through the configured host Docker launcher')
 if action=='status':
  job=safe(out/'jobs',req['job_id'])
  if req.get('brief'):
   if not(job/'request.json').is_file():raise ValueError('unknown job')
   result=job/'result.json';data=json.loads(result.read_text())if result.exists()else{}
   cleanup_failed=isinstance(data.get('isolation'),dict)and data['isolation'].get('cleanup_complete')is False
   return {'job_id':job.name,'status':'cleanup_failed'if cleanup_failed else'completed'if result.exists()else'running','result_path':str(result),**{key:data[key]for key in ('exit_code','timed_out','error','elapsed_seconds','candidate_sha256','isolation')if key in data}}
  return summary_job(job)
 if action=='cancel':
  raise ValueError('cancel jobs through the configured host Docker launcher')
 if action=='set_status':save(out/'status.json',req['value']);return {'ok':True}
 if action.startswith(('rtl_review_','rtl_repair_')):return rtl_transition(out,req)
 scope=req.get('scope','work')
 if scope!='work'and not re.fullmatch(r'(architecture/revision-[0-9]+/(candidate|audit)|audit/review-[1-9][0-9]*|readiness/campaign-[1-9][0-9]*/(work|audit))',scope):raise ValueError('invalid editing scope')
 readiness_scope=scope.startswith('readiness/')
 if readiness_scope:
  if action not in ('list','read','write','replace','submit'):raise ValueError('unsupported readiness workspace action')
  campaign=readiness.from_scope(out,scope,writable=action in ('write','replace','submit'))
  if action in ('write','replace'):require_idle(out,[scope])
 if scope.startswith('audit/'):
  metadata=get_review(out,scope.split('/')[-1])
  if req.get('review')not in (None,metadata['review']):raise ValueError('review does not match audit scope')
  check_candidate(out,metadata,current_architecture=action=='submit')
  if action in ('write','replace','submit')and metadata.get('decision'):raise ValueError('decided audit scope is frozen')
  if action in ('write','replace'):require_idle(out,[scope])
 work=safe(out,scope)
 if scope.startswith('architecture/')and(work.parent/'handoff.json').exists()and action in ('write','replace','evaluate','submit'):raise ValueError('published architecture revision is read-only; begin a new revision')
 work.mkdir(parents=True,exist_ok=True)
 if action=='list':
  base=read_path(out,work,req.get('path','.'),review=req.get('review'));return {'files':[{ 'path':scope_local_locator(scope,str(p.relative_to(work)))if p.is_relative_to(work)else req.get('path','.').rstrip('/')+'/'+str(p.relative_to(base)),'bytes':p.stat().st_size}for p in sorted(base.rglob('*'))if p.is_file()][:500]}
 if action=='read':
  n=req['path'];p=read_path(out,work,n,review=req.get('review'))
  raw=p.read_bytes()
  if b'\0'in raw[:4096]:return {'path':n,'bytes':len(raw),'sha256':sha(p),'error':'binary file; generate a small textual report on the server'}
  source_id=digest({'run':str(out.resolve()),'scope':scope,'review':req.get('review'),'path':n,'resolved_path':str(p.resolve())})
  try:resolved_run_path=str(p.resolve().relative_to(out.resolve()))
  except ValueError:resolved_run_path=None
  return read_text_page(raw,n,source_id,req,resolved_run_path)
 if action in ('write','replace'):
  name=scope_relative_path(scope,req['path']);p=safe(work,name);existing=sha(p)if p.is_file()else None
  if req.get('base_sha256')!=existing:raise ValueError('file version changed; read before editing')
  content=req['content']
  if action=='replace':
   old=req['old'];text=p.read_text()
   if text.count(old)!=1:raise ValueError('replacement old text must occur exactly once')
   content=text.replace(old,content,1)
  p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name(p.name+'.tmp');tmp.write_text(content);tmp.replace(p)
  return {'path':name,'scope':scope,'run_path':str(Path(scope)/name),'sha256':sha(p),'bytes':p.stat().st_size}
 if action=='submit':
  session_binding={}
  if readiness_scope and ('session_id'in req or 'session_actor_sha256'in req):
   actor,actor_path=readback_store._actor(out,req.get('session_id'))
   if(req.get('session_actor_sha256')!=sha(actor_path)or actor['role']!='rtl_readiness'or actor['scope']!=scope
      or (out/'control/sessions'/(actor['session_id']+'.end.json')).exists()):raise ValueError('readiness job actor binding mismatch or ended session')
   if sha(out/'control'/(actor['session_id']+'.prompt.txt'))!=actor['prompt_sha256']:raise ValueError('readiness job registered prompt changed')
   session_binding={'session_id':actor['session_id'],'session_actor_sha256':sha(actor_path)}
  argv=req['argv']
  if not isinstance(argv,list)or not argv or any(not isinstance(v,str)for v in argv):raise ValueError('argv must be nonempty string array')
  argv=[str(read_path(out,work,v,physical=True,review=req.get('review')))if v.startswith(('@input/','@architecture/','@skill/','@run/','@candidate/'))else v for v in argv]
  allowed=json.loads((out/'manifest.json').read_text())['settings']['tools']
  if argv[0]in allowed:argv=[allowed[argv[0]],*argv[1:]]
  if argv[0]not in set(allowed.values()):raise ValueError('use configured tool names: '+','.join(allowed))
  guards=[]if readiness_scope else integrity_guards(out);repair=next((r for r in reviews(out)if(r.get('repair')or{}).get('status')=='active'),None)
  repair_budget=repair_budgets.binding(out,repair)if scope=='work'else None
  default_timeout=campaign['budgets']['max_job_seconds']if readiness_scope else(repair_budget['budgets']['max_job_seconds']if repair_budget else 600)
  timeout_value=req.get('timeout_seconds',default_timeout)
  if (readiness_scope or repair_budget is not None)and type(timeout_value)is not int:raise ValueError('readiness timeout must be an integer')
  timeout=int(timeout_value)
  if timeout<=0:raise ValueError('command timeout must be positive; total run is unlimited')
  if repair and scope.startswith('audit/'):raise ValueError('audit jobs cannot run during RTL repair')
  candidate_id=campaign['source_sha256']if readiness_scope else(metadata['candidate_sha256']if scope.startswith('audit/')else(proposal_identity(work.parent/'candidate')['candidate_sha256']if scope.endswith('/audit')else None))
  manifest=json.loads((out/'manifest.json').read_text());isolation=manifest.get('job_isolation')
  if not isinstance(isolation,dict)or isolation.get('backend')!='docker_readonly_job_v1':raise ValueError('read-only Docker job backend is required')
  writable_scope_links(work)
  cwd=safe(work,scope_relative_path(scope,req.get('cwd','.'),allow_root=True))
  reservation_context=readiness.submit_reservation(out,scope,timeout,argv,cwd)if readiness_scope else(repair_budgets.submit_reservation(out,repair,repair_budget,timeout)if repair_budget is not None else nullcontext(None))
  with reservation_context as reservation:
   jobid=str(time.time_ns());job=out/'jobs'/jobid;job.mkdir(parents=True)
   record={'argv':argv,'cwd':str(cwd),'run_dir':str(out),'scope':scope,'timeout_seconds':timeout,'source_dir':str(work),'source_sha256':campaign['source_files']if readiness_scope else source_inventory(work),'candidate_sha256':candidate_id,'integrity_guards':guards,'repair_review':repair['review']if repair and scope=='work'else None,'job_isolation':isolation}
   record.update(session_binding)
   if readiness_scope:record['readiness_budget']=reservation
   if repair_budget is not None:record['repair_budget']=reservation
   save(job/'request.json',record)
  return {'job_id':jobid,'status':'queued','logs':str(job)}
 if action=='revision_begin':
  readback_store.require_active_parent(out,req.get('parent_session_id'),req.get('parent_actor_sha256'))
  identity=architecture_identity(out);base=out/identity['current']['path'];parent=out/'architecture';parent.mkdir(exist_ok=True)
  number=1+max([int(p.name.split('-')[-1])for p in parent.glob('revision-*')],default=0);rev=parent/f'revision-{number}';candidate=rev/'candidate';copy_closure(base,candidate);(rev/'audit').mkdir()
  # The parent remains preserved at its own path; its approval/calculation is not this proposal's.
  for name in ('review.json','decision.json','budgets.json','budget_binding.json','SOURCE.md'):
   p=candidate/name
   if p.exists()or p.is_symlink():p.unlink()
  (candidate/'SOURCE.md').write_text('# Unapproved implementation architecture proposal\n\nParent baseline and its historical approval: @run/'+str(base.relative_to(out))+'.\nOriginal requirements remain at @input/. This proposal needs its own calculation and native review; inherited approval is not current evidence.\n')
  save(rev/'request.json',dict(req,baseline_path=str(base),baseline_sha256=inventory(base),baseline_identity=identity))
  return {'revision':str(rev.relative_to(out)),'candidate_scope':str(candidate.relative_to(out)),'audit_scope':str((rev/'audit').relative_to(out))}
 if action=='proposal':return proposal_identity(work.parent/'candidate'if scope.endswith('/audit')else work)
 if action=='evaluate':
  independent=scope.startswith('architecture/')and scope.endswith('/audit')
  candidate=work.parent/'candidate'if independent else work
  if independent:require_idle(out,[scope,str(candidate.relative_to(out))])
  candidate_id=proposal_identity(candidate)['candidate_sha256']if independent else None
  destination=work
  if independent:
   if(work/'calculations').is_symlink():raise ValueError('independent calculation directory must not be a symlink')
   destination=safe(work,'calculations/'+str(time.time_ns()))
   destination.mkdir(parents=True,exist_ok=False)
  design=safe(candidate,'design.json');design_sha256=sha(design);document=json.loads(design.read_text());baseline=json.loads((out/'snapshot/architecture/design.json').read_text())
  if document.get('requirements')!=baseline.get('requirements'):raise ValueError('revision attempted to replace original requirements')
  sys.path.insert(0,str(out/'snapshot'));from harness.runtime.design_session import execute_tool
  sources={n:(out/'snapshot/input'/n).read_text()for n in json.loads((out/'manifest.json').read_text())['input_sha256']};label=str(destination.relative_to(out))+'/budget'
  report=execute_tool(out,label,document,sources,require_document=False)
  save(destination/'budgets.json',report)
  binding={'design_file_sha256':design_sha256,'report_sha256':sha(destination/'budgets.json')}
  if independent:binding['candidate_sha256']=candidate_id
  save(destination/'budget_binding.json',binding)
  if independent and proposal_identity(candidate)['candidate_sha256']!=candidate_id:raise ValueError('architecture proposal changed during independent calculation')
  result=architecture_budget(out,candidate,report,destination/'budgets.json')
  if independent:result.update(mode='independent_verification',candidate_scope=str(candidate.relative_to(out)),candidate_sha256=candidate_id,design_file_sha256=design_sha256,execution_path=label+'.execution.json',binding_path=str((destination/'budget_binding.json').relative_to(out)))
  return result
 if action=='revision_adopt':
  revision=safe(out,req['revision']);candidate=revision/'candidate';review=revision/'audit/review.json'
  require_idle(out,[str(candidate.relative_to(out)),str((revision/'audit').relative_to(out))])
  candidate_id=proposal_identity(candidate)['candidate_sha256']
  original=json.loads((out/'snapshot/architecture/design.json').read_text());updated=json.loads((candidate/'design.json').read_text())
  if updated.get('requirements')!=original.get('requirements'):raise ValueError('revision attempted to replace original requirements')
  status='not_run';review_sha256=None
  if review.exists():
   raw=review.read_bytes();data=json.loads(raw);review_sha256=hashlib.sha256(raw).hexdigest();status=data.get('verdict','invalid')
   if data.get('design_sha256')!=sha(candidate/'design.json'):raise ValueError('review not bound to current design bytes')
   if data.get('candidate_sha256')!=candidate_id:raise ValueError('review is stale for candidate files')
   if status!='accept':return {'adopted':False,'review':data}
  binding=candidate/'budget_binding.json';budget=candidate/'budgets.json'
  if (out/'snapshot/harness/skills/microarchitecture-design/SKILL.md').exists() and not binding.exists():raise ValueError('revision needs fresh budget execution')
  if binding.exists():
   bound=json.loads(binding.read_text())
   if bound!={'design_file_sha256':sha(candidate/'design.json'),'report_sha256':sha(budget)}:raise ValueError('budget is stale for revised design')
   b=json.loads(budget.read_text())
   if b.get('gate',{}).get('status')=='unknown':raise ValueError('revised budget scope unresolved')
   if b.get('ok')is not True:raise ValueError('failed budget cannot be bypassed as missing skill')
   selected=json.loads((candidate/'design.json').read_text()).get('selected');checks=b.get('candidates',{}).get(selected,{}).get('checks',[])
   if any(c.get('kind')=='hard'and c.get('estimate_pass')is False for c in checks):raise ValueError('selected hard budget violation')
  if (out/'snapshot/harness/skills/microarchitecture-audit/SKILL.md').exists():
   if not review.exists():raise ValueError('installed audit skill requires an actual review')
   cited=data.get('evidence',[]);evidence=[]
   if not isinstance(cited,list):raise ValueError('review evidence must name actual job IDs')
   for job_id in cited:
    if not isinstance(job_id,str):continue
    job=safe(out/'jobs',job_id)
    if not(job/'request.json').exists():continue
    if not(job/'result.json').exists():continue
    q=json.loads((job/'request.json').read_text());r=json.loads((job/'result.json').read_text())
    if q.get('scope')==str((revision/'audit').relative_to(out)) and q.get('candidate_sha256')==candidate_id and r.get('exit_code')==0 and not r.get('timed_out') and not r.get('error'):evidence.append(job.name)
   if not evidence:raise ValueError('review needs executed independent evidence bound to candidate')
  identity=architecture_identity(out);current=out/identity['current']['path'];origin=json.loads((revision/'request.json').read_text())
  if str(current)!=origin['baseline_path']or inventory(current)!=origin['baseline_sha256']or(origin.get('baseline_identity')is not None and identity!=origin['baseline_identity']):raise ValueError('architecture baseline changed during revision')
  handoff=publish_architecture_baseline(out,revision,candidate,candidate_id,status,origin,review_sha256)
  current={k:handoff[k]for k in ('path','review_status','native_review_status','approved_for_implementation','baseline_sha256','candidate_path','candidate_sha256','handoff_path','handoff_sha256','scope','parent_session_id','parent_actor_sha256','readback_required_files')}
  current['design_sha256']=sha(out/current['path']/'design.json');save(out/'architecture-current.json',current)
  return {'adopted':True,'review_status':current['review_status'],'path':str(out/current['path']),'current':current,'artifacts':handoff['files'],'budget':handoff.get('budget')}
 raise ValueError('unsupported action: '+action)

def main():
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path);p.add_argument('--run');p.add_argument('--job',type=Path);a=p.parse_args()
 if a.job:supervisor(a.job);return
 try:result=dispatch(a.root.resolve(),a.run,json.load(sys.stdin));print(json.dumps(result,ensure_ascii=False))
 except Exception as e:print(json.dumps({'error':type(e).__name__+': '+str(e)},ensure_ascii=False));raise SystemExit(1)
if __name__=='__main__':main()
