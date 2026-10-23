"""Read-only audit tool transport, checkpoints and report execution."""
import json
import hashlib
import copy
import importlib.util
from harness.adapters.omp import parse_json
from harness.prompts.architecture import audit as audit_prompt, audit_baseline, parse_payload, source_summary, baseline_feedback, compact_audit_repair
from harness.runtime.run_store import dump,digest,architecture_text
from harness.runtime.skill_tools import execute_script
from harness.runtime.source_evidence import checkpoint as evidence_checkpoint, catalogue, prompt_catalogue, selected_span, is_original_source, original_sources
from harness.runtime.design_session import execute_tool, identity as design_identity, protocol_feedback, calculation_feedback

class AuditSessionLimit(RuntimeError):pass
class AuditReportRejected(AuditSessionLimit):pass
class TransportBindingError(ValueError):pass

RESERVED_METADATA={'packet_sha256','baseline_sha256','base_sha256','base_report_sha256','input_sha256',
                   'raw_response_sha256','parsed_response_sha256','decoded_sha256','canonical_sha256',
                   'manifest_sha256','snapshot_sha256','target_sha256','host_binding','provenance','_host','request_label','request_id','source_sha256',
                   'source_declaration','source_execution','execution_dependencies'}


def model_content(value):
    if not isinstance(value,dict):raise ValueError('model response content must be one object')
    fields=sorted(set(value)&RESERVED_METADATA)
    if fields:raise ValueError('model supplied reserved host metadata: '+','.join(fields))
    return value


def frozen_transport_context(out,sources):
    manifest=out/'manifest.json'
    if not manifest.is_file():raise TransportBindingError('audit transport requires the frozen run manifest')
    record=json.loads(manifest.read_text());expected=record.get('sha256')
    if not isinstance(expected,dict):raise TransportBindingError('frozen manifest lacks snapshot fingerprints')
    actual={}
    for path in sorted((out/'snapshot').rglob('*')):
        if not path.is_file():continue
        relative=path.relative_to(out/'snapshot');name=str(relative);data=path.read_bytes()
        generated_cache=path.name=='.DS_Store' or (path.suffix=='.pyc' and len(data)>=16 and data.startswith(importlib.util.MAGIC_NUMBER))
        if name not in expected and relative.parts[0]=='harness' and generated_cache:continue
        actual[name]=digest(data)
    if actual!=expected:raise TransportBindingError('audit transport refused: manifest/snapshot changed')
    inputs={name:digest(text.encode()) for name,text in original_sources(sources).items()}
    frozen_inputs=original_sources({name[len('input/'):]:sha for name,sha in expected.items() if name.startswith('input/')})
    if inputs!=frozen_inputs:raise TransportBindingError('audit transport inputs differ from frozen snapshot')
    return {'manifest_sha256':digest(manifest.read_bytes()),'snapshot_sha256':report_identity(actual),'input_sha256':inputs}


def response_context(out,sources,kind,target=None,target_label=None,packet_sha256=None,baseline_sha256=None):
    result=dict(frozen_transport_context(out,sources),kind=kind,target_sha256=report_identity(target) if target is not None else None,target_label=target_label)
    if packet_sha256 is not None:result['packet_sha256']=packet_sha256
    if baseline_sha256 is not None:result['baseline_sha256']=baseline_sha256
    return result


def bound_response(out,label,invoke,parser,context,prompt):
    """Capture actual parser input; never reconstruct raw text from a dict."""
    binding_path=out/(label+'.binding.json');decoded_path=out/(label+'.model.json')
    raw_path=out/(label+'.model.response.txt');parsed_path=out/(label+'.parser.response.txt')
    if context.get('target_sha256') is not None:
        target_path=out/(str(context.get('target_label'))+'.canonical.json')
        if not target_path.is_file() or report_identity(json.loads(target_path.read_text()))!=context['target_sha256']:
            raise TransportBindingError('current repair target lacks its bound canonical version')
    if binding_path.exists():
        binding=json.loads(binding_path.read_text())
        if binding.get('label')!=label or binding.get('context')!=context:raise TransportBindingError('saved audit response targets a different frozen context/version')
        if (not raw_path.is_file() or not parsed_path.is_file() or digest(raw_path.read_bytes())!=binding.get('raw_response_sha256') or
            digest(parsed_path.read_bytes())!=binding.get('parsed_response_sha256')):raise TransportBindingError('saved audit raw response changed')
        try:decoded=parser(parsed_path.read_text())
        except (ValueError,TypeError) as error:
            if binding.get('parser_error')!=str(error):raise TransportBindingError('saved audit parser rejection changed')
            raise ValueError(str(error)) from error
        if binding.get('parser_error') is not None:raise TransportBindingError('saved rejected audit response became valid')
        if not decoded_path.is_file() or report_identity(decoded)!=binding.get('decoded_sha256') or json.loads(decoded_path.read_text())!=decoded:
            raise TransportBindingError('saved audit decoded response changed')
        canonical=out/(label+'.canonical.json')
        if binding.get('canonical_sha256') is not None and (not canonical.is_file() or report_identity(json.loads(canonical.read_text()))!=binding['canonical_sha256']):
            raise TransportBindingError('saved canonical audit content changed')
        return decoded
    if any(path.exists() for path in (decoded_path,raw_path,parsed_path)):
        raise TransportBindingError('incomplete audit response provenance; refuse unbound replay')
    attempts=[]
    def capture(text):
        if not isinstance(text,str):raise ValueError('audit parser requires actual model response text')
        if not attempts:raw_path.write_text(text)
        attempts.append(text);parsed_path.write_text(text)
        return parser(text)
    try:
        decoded=invoke(label,'review',prompt,capture)
        if not attempts:raise TransportBindingError('model adapter bypassed parser; actual raw response unavailable')
        checked=parser(attempts[-1])
        if decoded!=checked:raise TransportBindingError('model adapter returned content different from actual raw response')
    except ValueError as error:
        if attempts:dump(binding_path,{'label':label,'context':context,'raw_response_sha256':digest(raw_path.read_bytes()),
                                    'parsed_response_sha256':digest(parsed_path.read_bytes()),'parser_error':str(error)})
        raise
    except Exception as error:
        if attempts:
            try:parser(attempts[-1])
            except (ValueError,TypeError) as rejected:
                dump(binding_path,{'label':label,'context':context,'raw_response_sha256':digest(raw_path.read_bytes()),
                                   'parsed_response_sha256':digest(parsed_path.read_bytes()),'parser_error':str(rejected),
                                   'adapter_error_type':type(error).__name__})
        raise
    dump(decoded_path,decoded)
    dump(binding_path,{'label':label,'context':context,'raw_response_sha256':digest(raw_path.read_bytes()),
                       'parsed_response_sha256':digest(parsed_path.read_bytes()),'decoded_sha256':report_identity(decoded),'parser_error':None})
    return decoded


def executed_script_result(out,label,script,request,arguments=()):
    """Replay only the actual frozen-script request/result receipt."""
    result_path=out/(label+'.result.json')
    if not result_path.exists():return execute_script(out,label,script,request,arguments)
    request_path=out/(label+'.request.json');execution_path=out/(label+'.execution.json')
    if not request_path.is_file() or not execution_path.is_file():raise TransportBindingError('saved audit script result lacks execution provenance')
    execution=json.loads(execution_path.read_text())
    if (json.loads(request_path.read_text())!=request or execution.get('exit_code')!=0 or
        execution.get('script_sha256')!=digest(script.read_bytes()) or
        execution.get('request_sha256')!=digest(request_path.read_bytes()) or
        execution.get('result_sha256')!=digest(result_path.read_bytes())):
        raise TransportBindingError('saved audit script request/result execution changed')
    return json.loads(result_path.read_text())


def bind_canonical(out,label,value):
    path=out/(label+'.binding.json');binding=json.loads(path.read_text());sha=report_identity(value)
    if binding.get('canonical_sha256') is not None and binding['canonical_sha256']!=sha:raise TransportBindingError('canonical audit content differs from bound version')
    dump(out/(label+'.canonical.json'),value);binding['canonical_sha256']=sha;dump(path,binding)
    return value


def canonical_report(content,context):
    model_content(content)
    return dict(copy.deepcopy(content),packet_sha256=context['packet_sha256'],baseline_sha256=context['baseline_sha256'])

def report_identity(value):
    return digest(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())

def baseline_semantics(value):
    result=copy.deepcopy(value)
    if isinstance(result,dict):
        result.pop('source_revision',None)
        for row in result.get('obligations',[]) if isinstance(result.get('obligations'),list) else []:
            if isinstance(row,dict):row.pop('references',None)
    return result


def citation_only_failure(check):
    diagnostic=check.get('citation_diagnostics',[])
    citation_errors={'baseline obligation/'+str(row['obligation_id'])+': '+row['error'] for row in diagnostic}
    return bool(diagnostic) and all(error in citation_errors or error=='baseline must inspect both spec.md and reference.py' for error in check.get('errors',[]))

def retain_bindings(citations,bindings):
    """Retain actual checker-created source locations, not inferred evidence."""
    for binding in bindings:
        row=binding.get('resolved') if isinstance(binding,dict) else None
        if isinstance(row,dict) and 'evidence_id' in row and not any(old.get('evidence_id')==row['evidence_id'] for old in citations):
            citations.append(copy.deepcopy(row))


def evidence_reference(evidence_id,citations):
    if not isinstance(evidence_id,str) or len([r for r in citations if r.get('evidence_id')==evidence_id])!=1:
        raise ValueError('repair requires an actual frozen evidence_id')
    return {'evidence_id':evidence_id}


def replace_reference(rows,update,citations,sources=None,input_only=False):
    i=update['row_index'];j=update['reference_index']
    if type(i) is not int or not 0<=i<len(rows) or type(j) is not int:
        raise ValueError('repair row/reference index must identify existing data')
    refs=rows[i].get('references')
    if not isinstance(refs,list) or not 0<=j<len(refs):raise ValueError('repair requires an existing reference entry')
    if 'reference' in update:
        if sources is None:raise ValueError('span repair requires frozen source text')
        selected_span(update['reference'],sources,input_only=input_only)
        refs[j]=copy.deepcopy(update['reference'])
    else:refs[j]=evidence_reference(update['evidence_id'],citations)


def repair_baseline(previous,request,citations,sources=None):
    model_content(request)
    if previous is None or set(request)!={'reference_updates'}:
        raise ValueError('baseline repair requires the host current baseline and reference_updates only')
    updates=request['reference_updates']
    if not isinstance(updates,list) or not 1<=len(updates)<=64:raise ValueError('baseline repair requires 1..64 reference updates')
    result=copy.deepcopy(previous);seen=set()
    for update in updates:
        if not isinstance(update,dict) or set(update) not in ({'obligation_id','reference_index','evidence_id'},{'obligation_id','reference_index','reference'}):raise ValueError('baseline repair updates an existing reference using evidence_id OR reference span')
        if type(update['reference_index']) is not int:raise ValueError('baseline reference_index must be an existing integer index')
        if 'evidence_id' in update and any(not is_original_source(row.get('file')) for row in citations if row.get('evidence_id')==update['evidence_id']):raise ValueError('baseline repair permits original-input evidence only')
        matches=[i for i,row in enumerate(result.get('obligations',[])) if isinstance(row,dict) and row.get('id')==update['obligation_id']]
        if len(matches)!=1:raise ValueError('baseline repair needs a unique existing obligation_id')
        key=(matches[0],update['reference_index'])
        if key in seen:raise ValueError('duplicate baseline reference update')
        seen.add(key)
        replace_reference(result['obligations'],dict(update,row_index=matches[0]),citations,sources,input_only=True)
    return result


def repair_report(previous,request,citations,sources=None):
    model_content(request)
    allowed={'checks','requirements','reference_updates','reference_replacements'}
    if previous is None or set(request)-allowed:
        raise ValueError('report repair requires the host current report and permitted row/reference replacements only')
    result=copy.deepcopy(previous);changed=False;row_targets=set()
    for collection,key in (('checks','topic'),('requirements','id')):
        rows=request.get(collection,[])
        if not isinstance(rows,list):raise ValueError('repair '+collection+' must be complete row arrays')
        existing=result.get(collection,[]);seen=set()
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get(key),str):raise ValueError('repair row requires existing '+key)
            matches=[i for i,old in enumerate(existing) if isinstance(old,dict) and old.get(key)==row[key]]
            if len(matches)!=1 or row[key] in seen:raise ValueError('repair rows require unique existing '+key)
            statuses={'pass','conditional','fail'} if collection=='checks' else {'covered','missing','contradicted','deferred'}
            old=existing[matches[0]]
            if old.get('status') not in statuses or row.get('status')!=old['status']:
                raise ValueError('report repair must preserve a legal existing engineering status; missing/invalid or changed status requires a new audit version')
            seen.add(row[key]);row_targets.add((collection,matches[0]));existing[matches[0]]=copy.deepcopy(row);changed=True
    replacements=request.get('reference_replacements',[]);reference_targets=set()
    if not isinstance(replacements,list) or len(replacements)>64:raise ValueError('reference_replacements must be an array of at most 64 targets')
    for replacement in replacements:
        reference_keys={'checks':'topic','requirements':'id','realization_reviews':'count_check_id'}
        if not isinstance(replacement,dict) or replacement.get('collection') not in reference_keys:
            raise ValueError('reference replacement targets checks/topic, requirements/id or realization_reviews/count_check_id only')
        collection=replacement['collection'];key=reference_keys[collection]
        if set(replacement)!={'collection',key,'references'} or not isinstance(replacement[key],str):
            raise ValueError('reference replacement needs collection, existing '+key+' and complete references only')
        rows=result.get(collection,[])
        matches=[i for i,row in enumerate(rows) if isinstance(row,dict) and row.get(key)==replacement[key]]
        if len(matches)!=1:raise ValueError('reference replacement needs a unique existing '+key)
        target=(collection,matches[0])
        if target in reference_targets or target in row_targets:raise ValueError('duplicate or overlapping report row/reference replacement')
        refs=replacement['references']
        if not isinstance(refs,list) or not 1<=len(refs)<=64 or not all(isinstance(ref,dict) for ref in refs):
            raise ValueError('reference replacement requires 1..64 model-selected evidence references')
        for ref in refs:
            if 'evidence_id' in ref:
                if set(ref)!={'evidence_id'}:raise ValueError('evidence_id cannot have location overrides')
                evidence_reference(ref['evidence_id'],citations)
            elif 'file' in ref and 'quote' not in ref:
                if sources is None:raise ValueError('span replacement requires frozen source text')
                selected_span(ref,sources)
        # Only this field changes; judgments, explanation and siblings stay frozen.
        rows[matches[0]]['references']=copy.deepcopy(refs);reference_targets.add(target);changed=True
    updates=request.get('reference_updates',[]);seen=set()
    if not isinstance(updates,list) or len(updates)>64:raise ValueError('report reference_updates must be an array of at most 64 updates')
    collections={'checks','requirements','findings','unverified','revision_checks','baseline_corrections','realization_reviews'}
    observation_targets=set()
    for update in updates:
        if isinstance(update,dict) and update.get('collection')=='observations':
            if set(update)!={'collection','request_id','calculation_id','reference'} or not all(
                    isinstance(update[key],str) and update[key].strip() for key in ('request_id','calculation_id')):
                raise ValueError('observation repair needs the unique existing request_id/calculation_id and a replacement calculation reference only')
            original=previous.get('observations',[])
            if not isinstance(original,list):raise ValueError('observation repair requires existing observations')
            matches=[i for i,row in enumerate(original) if isinstance(row,dict) and
                     row.get('request_id')==update['request_id'] and row.get('calculation_id')==update['calculation_id']]
            if len(matches)!=1:raise ValueError('observation repair needs a unique existing request_id/calculation_id')
            index=matches[0];reference=update['reference'];old=original[index]
            if index in observation_targets:raise ValueError('duplicate observation reference update')
            if not isinstance(reference,dict) or set(reference)!={'request_id','calculation_id','actual'} or not all(
                    isinstance(reference[key],str) and reference[key].strip() for key in ('request_id','calculation_id')):
                raise ValueError('observation repair reference requires request_id/calculation_id/actual only')
            if type(reference['actual']) not in (int,float) or type(reference['actual']) is not type(old.get('actual')) or reference['actual']!=old['actual']:
                raise ValueError('observation repair must preserve the exact existing numeric actual; changed values need a new audit judgment')
            # The model selects the destination. Full report validation must
            # still bind it to real execution; no guessed ID or new judgment.
            result['observations'][index].update(copy.deepcopy(reference))
            observation_targets.add(index);changed=True;continue
        if not isinstance(update,dict) or set(update) not in ({'collection','row_index','reference_index','evidence_id'},{'collection','row_index','reference_index','reference'}) or update['collection'] not in collections:
            raise ValueError('report repair updates references in declared report collections only')
        key=(update['collection'],update['row_index'],update['reference_index'])
        if (update['collection'],update['row_index']) in row_targets|reference_targets:
            raise ValueError('overlapping report row/reference replacement')
        if key in seen:raise ValueError('duplicate report reference update')
        seen.add(key);replace_reference(result.get(update['collection'],[]),update,citations,sources,input_only=update['collection']=='baseline_corrections');changed=True
    if not changed or result==previous:raise ValueError('report repair did not change any permitted data')
    return result


def parse_baseline_turn(text):
    analysis_marker='<<<BASELINE_ANALYSIS_REPAIR>>>'
    if analysis_marker in text:
        if text.count(analysis_marker)!=1 or '<<<AUDIT_BASELINE>>>' in text or '<<<BASELINE_REPAIR>>>' in text:raise ValueError('one source analysis repair frame required')
        return {'analysis_repair':model_content(parse_payload(text.split(analysis_marker,1)[1]))}
    marker='<<<BASELINE_REPAIR>>>'
    if marker in text:
        if text.count(marker)!=1 or '<<<AUDIT_BASELINE>>>' in text:raise ValueError('one baseline repair frame required')
        return {'baseline_repair':model_content(parse_payload(text.split(marker,1)[1]))}
    return model_content(parse_payload(text.split('<<<AUDIT_BASELINE>>>',1)[-1]))


def tool_feedback(request,result,executed=None):
    """Distinguish a failed audit tool declaration from an executed comparison."""
    if not result.get('ok'):
        return {'kind':'audit_evidence_tool_declaration','tool':request['tool'],'diagnostics':result.get('errors',[]),
                'subreceipts':[{'id':row.get('id'),'section':row.get('section'),'status':row.get('status'),'errors':row.get('errors',[])} for row in result.get('subreceipts',[])],
                'valid_calculation_ids':(executed or {}).get('calculation_ids',[]),'details':calculation_feedback(result),
                'action':'Repair the smallest failed declaration using its actual section fields; do not rewrite valid rows or choose a new semantic mapping merely to fix format. Only binding-verified executed child values listed separately may be observed. Failed rows and unverified partial values are not observation evidence; whole incomplete evidence cannot establish acceptance.'}
    mismatches=[name for name,comparison in result.get('comparisons',{}).items() if comparison.get('matches_expected') is False]
    return {'kind':'audit_executed_evidence','tool':request['tool'],'comparison_mismatches':mismatches,
            'action':'Use actual successful receipt ids and values. A mismatch with an independently declared expected value is evidence to interpret against the original inputs and proposal, not a transport failure or automatic infeasibility verdict.'}


def report_feedback(review,check):
    if not check.get('ok'):
        return {'kind':'audit_report_validation','diagnostics':check.get('errors',[]),
                'reference_diagnostics':check.get('reference_diagnostics',[]),
                'action':'For missing input/proposal evidence, replace the complete references array by checks/topic or requirements/id. Include every required role and retain relevant existing references; an index update cannot append a missing entry. For an erroneous observation calculation reference, use the observation reference_updates form with its unique existing request_id/calculation_id and a model-selected executed reference; actual and interpretation remain frozen. Preserve judgments against the unchanged packet; report-format errors are not architecture findings.'}
    return {'kind':'audit_semantic_judgment','verdict':review.get('verdict'),
            'open_findings':[f for f in review.get('findings',[]) if f.get('disposition')=='open'],
            'scope':'validated report structure/evidence; semantic judgments remain fallible, and model accept cannot override program gates'}


def pending_engineering_followup(history,source_execution):
    """One bound, actionable partial check; never infer a chip defect from it."""
    if (source_execution or {}).get('coverage_complete') is not True:return None
    for index,entry in enumerate(history):
        if entry.get('request',{}).get('tool')!='check_proposal':continue
        result=entry.get('result',{});verified=entry.get('executed_values') or {}
        if result.get('ok') is not True or result.get('source_coverage_complete') is not True:continue
        if not any(child.get('section')!='input' and child.get('checks') for child in verified.get('subreceipts',[])):continue
        gaps={key:result[key] for key in ('missing_profiles','missing_count_checks','missing_flow_checks','missing_service_declarations') if result.get(key)}
        # Declared but unsupported physical paths alone do not justify another
        # request. Unselected groups and typed partial issues remain traceable.
        requested={row.get('cost_group') for row in entry['request'].get('model',{}).get('operand_checks',[]) if isinstance(row,dict)}
        operand_gaps=set(result.get('missing_operand_checks',[]))-requested
        partial=[]
        for check in result.get('checks',[]):
            actual=check.get('actual')
            if isinstance(actual,dict) and any(actual.get(key) for key in
                    ('missing_operand_positions','partial_issues','MAC_issues_with_fewer_than_two_input_roles')):
                partial.append(check['id'])
        if operand_gaps:gaps['missing_operand_checks']=sorted(operand_gaps)
        if partial:gaps['partial_operand_checks']=partial
        if not gaps:continue
        # An existing later engineering execution already supplies this one
        # opportunity, including a truthful failed/unsupported declaration.
        if any(row.get('request',{}).get('tool') in ('recompute','check_proposal') for row in history[index+1:]):return None
        return {'request_id':result['request_id'],'gaps':gaps}
    return None


def packet(sources,document,budget,previous_audit=None,baseline=None):
    baseline_bindings=(baseline or {}).get('citation_bindings',[])
    source_analysis=(baseline or {}).get('analysis_result')
    source_execution=(baseline or {}).get('source_execution')
    execution_dependencies={k:(baseline or {}).get(v) for k,v in (
        ('checker_bundle_sha256','audit_script_bundle_sha256'),('calculator_bundle_sha256','calculator_bundle_sha256'))}
    if baseline:baseline={k:baseline[k] for k in ('baseline','baseline_sha256')}
    proposal={k:v for k,v in document.items() if k not in ('architecture_md','tool_receipt')}
    evidence=dict(sources,**{'design.json':json.dumps(proposal,ensure_ascii=False,indent=2),'budget.json':json.dumps(budget,ensure_ascii=False,indent=2)})
    evidence['architecture.md']=architecture_text(document,budget)
    if baseline:evidence['audit-baseline.json']=json.dumps(baseline,ensure_ascii=False,indent=2)
    if source_analysis is not None:evidence['source-analysis.json']=json.dumps(source_summary(source_analysis,source_execution),ensure_ascii=False,indent=2)
    identity=digest(json.dumps({'sources':evidence,'previous_audit':previous_audit},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
    data_hash=design_identity(document)
    citations=catalogue(evidence)['citations'];retain_bindings(citations,baseline_bindings)
    return {'packet_sha256':identity,'sources':evidence,'document':document,'budget':budget,'previous_audit':previous_audit,'baseline':baseline,'source_analysis':source_analysis,'source_execution':source_execution,'execution_dependencies':execution_dependencies,'calculations':[],'proposal_checks':[],'citations':citations,
            'identity_check':{'design_data_sha256':data_hash,'budget_design_sha256':budget.get('design_sha256'),
                              'budget_matches_design_data':budget.get('design_sha256')==data_hash,
                              'scope':'packet hash includes inputs, Markdown and budget; it is not the design-data hash. Match does not prove semantic correctness.'}}


def parse_turn(text):
    marker='<<<AUDIT_TOOL_REQUEST>>>';final='<<<AUDIT_REPORT>>>'
    repair='<<<AUDIT_REPAIR>>>'
    if repair in text:
        if text.count(repair)!=1 or marker in text or final in text:raise ValueError('one audit repair frame required')
        return {'report_repair':model_content(parse_payload(text.split(repair,1)[1]))}
    if text.count(marker)==1 and final not in text:
        request=parse_payload(text.split(marker,1)[1])
        allowed={'recompute':{'tool','facts','assumptions','calculations','numeric_probes'},'lookup_source':{'tool','queries'},'check_proposal':{'tool','model'}}
        tool=request.get('tool')
        if not isinstance(tool,str) or tool not in allowed:raise ValueError('unknown audit tool; permitted tool values are recompute|lookup_source|check_proposal')
        extra=set(request)-allowed[tool]
        if extra:raise ValueError(tool+' request has unexpected fields '+str(sorted(extra))+'; allowed fields '+str(sorted(allowed[tool]))+'. request_id is issued by the host after execution; omit it from requests.')
        return {'tool_request':request}
    if text.count(final)==1 and marker not in text:return {'final':model_content(parse_payload(text.split(final,1)[1]))}
    return {'final':model_content(parse_json(text))}


def script_bundle_hash(directory):
    entries={str(path.relative_to(directory)):digest(path.read_bytes()) for path in sorted(directory.rglob('*'))
             if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc' and path.name!='.DS_Store'}
    return report_identity(entries)


def baseline_identity(base,sources):
    original=original_sources(sources)
    return report_identity({'sources':original,'skill':(base/'SKILL.md').read_text(),'reference':(base/'references/review-contract.md').read_text(),
                            'reference_bundle_sha256':script_bundle_hash(base/'references'),
                            'audit_script_bundle_sha256':script_bundle_hash(base/'scripts'),
                            'calculator_bundle_sha256':script_bundle_hash(base.parent/'microarchitecture-design/scripts')})


def verify_baseline_record(out,sources,record,invoke,require_complete=False):
    """Replay bound source evidence for cache or same-run explicit handoff."""
    base=out/'snapshot/harness/skills/microarchitecture-audit';script=base/'scripts/validate_review.py'
    original=original_sources(sources)
    transport=frozen_transport_context(out,original)
    calculator=base.parent/'microarchitecture-design/scripts/evaluate.py';analysis_script=base/'scripts/source_model.py'
    bundles={'audit_script_bundle_sha256':script_bundle_hash(base/'scripts'),
             'calculator_bundle_sha256':script_bundle_hash(calculator.parent)}
    dependencies={'checker_bundle_sha256':bundles['audit_script_bundle_sha256'],
                  'calculator_bundle_sha256':bundles['calculator_bundle_sha256']}
    if not isinstance(record,dict) or record.get('identity')!=baseline_identity(base,original):
        raise TransportBindingError('prepared baseline belongs to different inputs or method')
    baseline=record.get('baseline')
    if not isinstance(baseline,dict) or record.get('baseline_sha256')!=report_identity(baseline):
        raise TransportBindingError('cached/prepared audit baseline changed')
    request={'mode':'source','model':baseline.get('analysis'),'sources':original}
    if (not isinstance(record.get('analysis_result'),dict) or
        record.get('analysis_request_sha256')!=report_identity(request) or
        record.get('analysis_result_sha256')!=report_identity(record['analysis_result']) or
        any(record.get(k)!=v for k,v in bundles.items())):
        raise TransportBindingError('cached/prepared quantitative baseline evidence missing or changed')
    origin=record.get('origin');binding_path=out/(str(origin)+'.binding.json')
    if not binding_path.is_file():raise TransportBindingError('cached/prepared baseline lacks original response provenance')
    binding=json.loads(binding_path.read_text())
    if any(binding.get('context',{}).get(k)!=v for k,v in transport.items()):
        raise TransportBindingError('cached/prepared baseline response belongs to a stale snapshot')
    bound_response(out,origin,invoke,parse_baseline_turn,binding['context'],'')
    if binding.get('canonical_sha256')!=report_identity(baseline):
        raise TransportBindingError('cached/prepared baseline differs from bound canonical version')
    analysis_name=str(origin)+'.analysis-'+report_identity(dict(request=request,**bundles))[:12]
    if not (out/(analysis_name+'.result.json')).is_file():
        raise TransportBindingError('cached/prepared baseline lacks actual quantitative execution provenance')
    executed=executed_script_result(out,analysis_name,analysis_script,request,['--calculator',str(calculator)])
    if record['analysis_result']!=executed:
        raise TransportBindingError('cached/prepared baseline differs from actual quantitative execution')
    spec=importlib.util.spec_from_file_location('frozen_prepared_source_validation',script)
    validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
    selected=validator.executed_source_values(executed,original,request['model'],dependencies)
    if selected.get('errors') or record.get('source_execution')!=selected:
        raise TransportBindingError('cached/prepared source transaction evidence is unbound or changed')
    check_request={'mode':'baseline','baseline':baseline,'sources':original,
                   'citations':catalogue(original,input_only=True)['citations'],
                   'source_analysis':executed,'execution_dependencies':dependencies}
    if not (out/(str(origin)+'.check.result.json')).is_file():
        raise TransportBindingError('cached/prepared baseline lacks actual source reference validation')
    checked=executed_script_result(out,str(origin)+'.check',script,check_request)
    if not checked.get('ok') or record.get('citation_bindings')!=checked.get('citation_bindings',[]):
        raise TransportBindingError('cached/prepared baseline original references/structure failed or changed')
    complete=executed.get('ok') is True and selected.get('coverage_complete') is True
    if require_complete and not complete:
        raise TransportBindingError('cached source transaction evidence is incomplete')
    if not complete and record.get('quantitative_status')!=('partial' if selected['subreceipts'] else 'failed'):
        raise TransportBindingError('prepared incomplete source coverage lacks its actual failure status')
    return record


def prepare_baseline(out,label,sources,invoke,max_repairs):
    """Source-only call and identity-bound reuse; never send the proposal."""
    base=out/'snapshot/harness/skills/microarchitecture-audit'
    skill=(base/'SKILL.md').read_text();reference=(base/'references/source-baseline.md').read_text();script=base/'scripts/validate_review.py'
    original=original_sources(sources)
    transport=frozen_transport_context(out,original)
    calculator=base.parent/'microarchitecture-design/scripts/evaluate.py'
    analysis_script=base/'scripts/source_model.py'
    bundles={'audit_script_bundle_sha256':script_bundle_hash(base/'scripts'),
             'calculator_bundle_sha256':script_bundle_hash(calculator.parent)}
    dependencies={'checker_bundle_sha256':bundles['audit_script_bundle_sha256'],
                  'calculator_bundle_sha256':bundles['calculator_bundle_sha256']}
    validator_spec=importlib.util.spec_from_file_location('frozen_source_validation',script)
    validator=importlib.util.module_from_spec(validator_spec);validator_spec.loader.exec_module(validator)
    evidence=evidence_checkpoint(out,'source-evidence.input',original,input_only=True)
    key=baseline_identity(base,original)
    path=out/'audit-baseline.current.json';cached=json.loads(path.read_text()) if path.exists() else None
    invalid=out/'audit-baseline.invalidated.json';feedback=json.loads(invalid.read_text()) if invalid.exists() else None
    invalidated=cached and feedback and feedback.get('baseline_sha256')==cached['baseline_sha256']
    if cached and cached.get('identity')==key and not invalidated:
        verify_baseline_record(out,original,cached,invoke,require_complete=True)
        dump(out/(label+'.baseline-reuse.json'),{'identity':key,'baseline_sha256':cached['baseline_sha256'],
             'analysis_request_sha256':cached['analysis_request_sha256']});return cached
    previous=cached['baseline'] if invalidated else None;previous_label=cached.get('origin') if invalidated else None;errors=[];diagnosis=None;citation_only=False
    unvalidated=None;last_checked=False
    quantity_name=label+'.baseline-quantities-'+key[:12]
    quantities=execute_script(out,quantity_name,calculator,{'sources':original},['--source-quantities-only'])
    for step in range(max_repairs+1):
        last_checked=False;unvalidated=None
        name=f'{label}.baseline-{step}';saved=out/(name+'.json');failure=out/(name+'.feedback.json')
        frame_context=response_context(out,original,'baseline',target=previous,target_label=previous_label)
        if saved.exists() or failure.exists():
            try:reply=bound_response(out,name,invoke,parse_baseline_turn,frame_context,'')
            except TransportBindingError:raise
            except ValueError as error:
                if not failure.exists():raise
                diagnosis=json.loads(failure.read_text());errors=diagnosis['diagnostics'];continue
        else:
            message=audit_baseline(skill,reference,original,previous,errors,(feedback or {}).get('corrections'))
            message+='\n'+quantities['view']
            message+=prompt_catalogue(evidence)
            if diagnosis and diagnosis.get('analysis_failed') and diagnosis.get('baseline_structure_ok'):
                message+='\nCURRENT STEP: return ONLY <<<BASELINE_ANALYSIS_REPAIR>>> {analysis:<complete corrected analysis>,source_revision:{reason,references:[original source spans]}}. The host binds the current baseline version and preserves obligations, scope, ambiguities and top-level limits. Do not submit identity metadata. No baseline or proposal tool frame applies.'
            elif previous is not None:
                message+='\nFor citation-only repairs return ONLY <<<BASELINE_REPAIR>>> with {reference_updates:[{obligation_id,reference_index,reference:{file,start,end}}]}. The host binds the current baseline. An intentional source reinterpretation or structural repair returns the full baseline with source_revision={reason,references:[original-input evidence]}; preserve valid fields including top-level limits. Do not submit identity metadata.'
            if diagnosis:message+='\nCURRENT BASELINE CHECK FEEDBACK\n'+json.dumps(baseline_feedback(diagnosis),ensure_ascii=False)
            try:
                reply=bound_response(out,name,invoke,parse_baseline_turn,frame_context,message)
            except TransportBindingError:raise
            except (KeyError,TypeError,ValueError,IndexError) as e:
                diagnosis=protocol_feedback(e);errors=diagnosis['diagnostics'];dump(failure,diagnosis);continue
        try:
            baseline=reply
            if isinstance(baseline,dict) and 'analysis_repair' in baseline:
                request=baseline['analysis_repair'];dump(out/(name+'.analysis-repair.json'),request)
                if previous is None or not isinstance(request,dict) or set(request)!={'analysis','source_revision'}:raise ValueError('analysis repair requires the host current baseline and analysis/source_revision only')
                revision=request['source_revision']
                if not isinstance(request['analysis'],dict) or not isinstance(revision,dict) or not revision.get('reason') or not revision.get('references'):raise ValueError('analysis repair requires complete analysis and explicit original-source revision evidence')
                baseline=copy.deepcopy(previous);baseline['analysis']=copy.deepcopy(request['analysis']);baseline['source_revision']=copy.deepcopy(revision)
            elif isinstance(baseline,dict) and 'baseline_repair' in baseline:
                if diagnosis and diagnosis.get('analysis_failed'):raise ValueError('failed quantitative analysis needs source_revision in a full baseline or analysis repair; reference repair cannot change analysis')
                dump(out/(name+'.repair.json'),baseline['baseline_repair'])
                baseline=repair_baseline(previous,baseline['baseline_repair'],evidence['citations'],original)
            elif previous is not None:
                changed=baseline_semantics(baseline)!=baseline_semantics(previous)
                if changed or not citation_only:
                    revision=baseline.get('source_revision',{}) if isinstance(baseline,dict) else {}
                    if not isinstance(revision,dict) or not isinstance(revision.get('reason'),str) or not revision['reason'].strip() or not revision.get('references'):
                        raise ValueError('full baseline source/structure revision needs explicit source_revision reason and original-input references; citation repair cannot rewrite semantics')
        except (KeyError,TypeError,ValueError,IndexError) as e:
            diagnosis=protocol_feedback(e);errors=diagnosis['diagnostics'];dump(failure,diagnosis);continue
        baseline=bind_canonical(out,name,baseline);dump(saved,baseline)
        analysis_request={'mode':'source','model':baseline.get('analysis') if isinstance(baseline,dict) else None,'sources':original}
        analysis_request_sha256=report_identity(analysis_request)
        if isinstance(analysis_request['model'],dict):
            analysis_name=name+'.analysis-'+report_identity(dict(request=analysis_request,**bundles))[:12]
            analysis_path=out/(analysis_name+'.result.json')
            if analysis_path.exists():
                saved_request=json.loads((out/(analysis_name+'.request.json')).read_text())
                if report_identity(saved_request)!=analysis_request_sha256:raise ValueError('saved source analysis request changed')
                execution=json.loads((out/(analysis_name+'.execution.json')).read_text())
                if (execution.get('script_sha256')!=digest(analysis_script.read_bytes()) or
                    execution.get('request_sha256')!=digest((out/(analysis_name+'.request.json')).read_bytes()) or
                    execution.get('result_sha256')!=digest(analysis_path.read_bytes())):
                    raise ValueError('saved source analysis execution evidence changed')
                analysis_result=json.loads(analysis_path.read_text())
            else:analysis_result=execute_script(out,analysis_name,analysis_script,analysis_request,['--calculator',str(calculator)])
        else:
            analysis_result={'ok':False,'errors':['baseline.analysis must be an object containing the independent source quantity model'],
                             'scope':'missing model declaration; no quantitative source analysis was executed'}
            dump(out/(name+'.analysis.declaration-check.json'),analysis_result)
        source_execution=validator.executed_source_values(analysis_result,original,analysis_request['model'],dependencies)
        dump(out/(name+'.source-execution.json'),source_execution)
        check=execute_script(out,name+'.check',script,{'mode':'baseline','baseline':baseline,'sources':original,
                             'citations':evidence['citations'],'source_analysis':analysis_result,'execution_dependencies':dependencies})
        last_checked=True
        if check['ok'] and isinstance(analysis_request['model'],dict):
            record={'identity':key,'baseline':baseline,'baseline_sha256':report_identity(baseline),'origin':name,'scope':check['scope'],
                    'citation_bindings':check.get('citation_bindings',[]),'analysis_result':analysis_result,
                    'analysis_request_sha256':analysis_request_sha256,'analysis_result_sha256':report_identity(analysis_result),
                    'source_execution':source_execution,**bundles}
            if analysis_result.get('ok') and source_execution.get('coverage_complete') is True and not source_execution.get('errors'):
                dump(path,record);return record
            unvalidated=dict(record,quantitative_status='partial' if source_execution.get('subreceipts') else 'failed',
                scope='Original-source references/structure passed; quantitative source coverage is incomplete. Only binding-verified successful source transactions are available for local checks; accept and baseline-cache reuse are unavailable.')
        previous=baseline;previous_label=name;errors=check.get('errors',[])+analysis_result.get('errors',[])+source_execution.get('errors',[])
        analysis_failed=not analysis_result.get('ok') or source_execution.get('coverage_complete') is not True;citation_only=not analysis_failed and citation_only_failure(check)
        diagnosis={'kind':'audit_baseline_declaration','diagnostics':errors,'citation_diagnostics':check.get('citation_diagnostics',[]),
                   'analysis_failed':analysis_failed,'baseline_structure_ok':check['ok'],'analysis_result':analysis_result,'source_execution':source_execution,'citation_only':citation_only,
                   'action':'Repair source analysis only when obligations/references already passed; preserve valid baseline fields. Use explicit source_revision; the host binds the current baseline version. Reference repair cannot change analysis. No architecture rejection follows from a malformed baseline.'}
        dump(out/(name+'.source-feedback.json'),diagnosis)
    if last_checked and unvalidated is not None:
        dump(out/(label+'.baseline-unvalidated.json'),unvalidated)
        return unvalidated
    raise AuditSessionLimit('bounded source-only baseline responses exhausted')


def run(out,label,sources,document,budget,invoke,max_tool_calls,previous_audit=None,max_baseline_repairs=0,max_report_repairs=3,prepared_baseline=None):
    if type(max_report_repairs) is not int or max_report_repairs<0:raise ValueError('max_report_repairs must be a nonnegative integer')
    if prepared_baseline is None:baseline=prepare_baseline(out,label,sources,invoke,max_baseline_repairs)
    else:
        baseline=verify_baseline_record(out,sources,prepared_baseline,invoke)
        invalid=out/'audit-baseline.invalidated.json'
        invalidated=json.loads(invalid.read_text()) if invalid.exists() else None
        if invalidated and invalidated.get('baseline_sha256')==baseline['baseline_sha256']:
            baseline=prepare_baseline(out,label,sources,invoke,max_baseline_repairs)
        else:dump(out/(label+'.baseline-handoff.json'),{'origin':baseline['origin'],'baseline_sha256':baseline['baseline_sha256'],
                       'analysis_request_sha256':baseline['analysis_request_sha256'],
                       'source_coverage_complete':baseline['source_execution']['coverage_complete']})
    data=packet(sources,document,budget,previous_audit,baseline)
    dump(out/(label+'.packet.json'),data)
    evidence=evidence_checkpoint(out,label+'.source-evidence',data['sources'])
    base=out/'snapshot/harness/skills/microarchitecture-audit'
    skill=(base/'SKILL.md').read_text();reference=(base/'references/review-contract.md').read_text();script=base/'scripts/validate_review.py'
    calculator=out/'snapshot/harness/skills/microarchitecture-design/scripts/evaluate.py'
    validator_spec=importlib.util.spec_from_file_location('frozen_audit_validation',script)
    validator=importlib.util.module_from_spec(validator_spec);validator_spec.loader.exec_module(validator)
    quantities=executed_script_result(out,label+'.quantities',calculator,{'sources':original_sources(sources)},['--source-quantities-only'])
    def validate(review,name):
        return execute_script(out,name+'.report-check',script,{'mode':'validate','review':review,'context':data})
    history=[];tool_calls=0;last_report=None;last_report_label=None;repair_calls=0;has_initial_report=False
    last_report_validation=None;compact_attempted=False
    followup_started=False;followup_pending=False
    response_limit=2*max_tool_calls+2+max_report_repairs
    for step in range(response_limit):
        if not has_initial_report and step>=2*max_tool_calls+2:raise AuditSessionLimit('bounded audit responses exhausted before first report')
        name=f'{label}.step-{step}';path=out/(name+'.json')
        if last_report is not None:
            if repair_calls>=max_report_repairs:raise AuditSessionLimit('local audit report repair limit reached')
            repair_calls+=1
        repair_context={} if last_report is not None else None
        prompt=compact_audit_repair(reference,last_report,last_report_validation,history) if last_report is not None and not compact_attempted else None
        if prompt is not None:compact_attempted=True
        else:
            prompt=audit_prompt(skill,data['sources'],reference=reference,packet_sha256=data['packet_sha256'],baseline_sha256=baseline['baseline_sha256'],history=history,previous_audit=previous_audit,tool_limit=tool_calls>=max_tool_calls,identity_check=data['identity_check'],report_repair_context=repair_context,source_execution=data['source_execution'])
            prompt+='\n'+quantities['view']
            prompt+=prompt_catalogue(evidence)
        frame_context=response_context(out,sources,'audit',target=last_report,target_label=last_report_label,
                                       packet_sha256=data['packet_sha256'],baseline_sha256=baseline['baseline_sha256'])
        if path.exists() and not (out/(name+'.binding.json')).exists():raise TransportBindingError('unbound audit checkpoint cannot be resumed')
        try:reply=bound_response(out,name,invoke,parse_turn,frame_context,prompt)
        except TransportBindingError:raise
        except ValueError as e:reply={'format_error':str(e),'feedback':protocol_feedback(e)}
        if path.exists() and json.loads(path.read_text())!=reply:raise TransportBindingError('saved audit reply differs from bound raw response')
        dump(path,reply)
        if 'format_error' in reply:history.append({'kind':'report_format_repair','errors':[reply['format_error']],'feedback':reply.get('feedback',protocol_feedback(ValueError(reply['format_error'])))});continue
        if 'tool_request' in reply:
            if last_report is not None:
                history.append({'kind':'report_repair_rejected','error':'report repair cannot execute new tools; calculations are frozen'});continue
            if tool_calls>=max_tool_calls:raise AuditSessionLimit('per-audit calculation request limit reached')
            tool_calls+=1
            if reply['tool_request']['tool']=='check_proposal':
                result=executed_script_result(out,name+'.tool',base/'scripts/candidate_checks.py',
                    {'mode':'proposal','model':reply['tool_request']['model'],'proposal':document,'budget':budget,
                     'baseline':data['source_analysis'],'source_declaration':baseline['baseline']['analysis'],
                     'execution_dependencies':data['execution_dependencies'],'sources':data['sources'],'citations':data['citations']},['--calculator',str(calculator)])
            else:
                mode='recompute' if reply['tool_request']['tool']=='recompute' else 'lookup'
                result=executed_script_result(out,name+'.tool',script,{'mode':mode,'request':reply['tool_request'],'sources':data['sources'],'citations':data['citations']},['--calculator',str(calculator)] if mode=='recompute' else [])
            receipt=dict(result,request_id=name)
            verified=validator.executed_values(receipt,data['sources'],data['source_analysis'],document,budget,data['execution_dependencies']) if reply['tool_request']['tool'] in ('recompute','check_proposal') else None
            retain_bindings(data['citations'],result.get('citation_bindings',[]))
            if reply['tool_request']['tool']=='check_proposal':
                data['calculations'].append(receipt);data['proposal_checks'].append(receipt)
            elif reply['tool_request']['tool']=='recompute':data['calculations'].append(receipt)
            else:
                for ref in result.get('evidence',[]):
                    if not any(x['evidence_id']==ref['evidence_id'] for x in data['citations']):data['citations'].append(ref)
            history.append({'kind':'tool_result','request':reply['tool_request'],'result':receipt,'executed_values':verified,'feedback':tool_feedback(reply['tool_request'],result,verified)})
            if followup_pending and reply['tool_request']['tool'] in ('recompute','check_proposal'):followup_pending=False
            continue
        try:
            if 'report_repair' in reply:
                dump(out/(name+'.repair.json'),reply['report_repair'])
                review=repair_report(last_report,reply['report_repair'],data['citations'],data['sources'])
                dump(out/(name+'.report.json'),review)
            elif last_report is not None:raise ValueError('after report rejection use AUDIT_REPAIR; preserve the report and actual receipts')
            else:review=canonical_report(reply['final'],frame_context)
        except (KeyError,TypeError,ValueError,IndexError) as error:
            history.append({'kind':'report_repair_rejected','error':str(error)});continue
        review=bind_canonical(out,name,review)
        has_initial_report=True
        check=validate(review,name)
        retain_bindings(data['citations'],check.get('bindings',[]))
        if not check['ok']:
            errors=check.get('errors',[])
            unsafe_judgment=any('calculation reference' in e or
                (e.startswith('accept contradicts') and not e.startswith('accept contradicts an unverified design decision:')) for e in errors)
            invalid_status=any(isinstance(rows,list) and any(isinstance(row,dict) and row.get('status') not in statuses for row in rows)
                for collection,statuses in (('checks',{'pass','conditional','fail'}),('requirements',{'covered','missing','contradicted','deferred'}))
                for rows in [review.get(collection,[])])
            invalid_unverified_kind=isinstance(review.get('unverified'),list) and any(isinstance(row,dict) and
                row.get('kind') not in ('design_decision','implementation_evidence','audit_evidence') for row in review['unverified'])
            if invalid_status or invalid_unverified_kind:
                if unsafe_judgment or 'scope' not in check:
                    raise AuditReportRejected('invalid engineering status or unverified kind with unsafe evidence or incomplete structural validation; a new audit version is required')
                history.append({'kind':'audit_evidence_reopen','rejected_report':review,'validation':check,
                    'action':'The rejected status or unverified kind belongs to the model. Submit a new complete independent judgment using the same packet, actual receipts and remaining existing allowance. The host does not fill either field; references-only repair cannot change them. Preserve the old raw report. No design edits, cap resets or extra calls.'})
                last_report=None;last_report_label=None
                continue
            missing_evidence=not unsafe_judgment and any(e.startswith('accept lacks') or e.startswith('accept requires executed check_proposal') for e in errors)
            judgment_gap=not unsafe_judgment and any(e in (
                'accept requires successful independent source analysis',
                'accept requires exactly one independent review per transformed count correspondence',
                'open blocking design finding requires revise; missing audit evidence cannot defer an established design defect',
                ) or
                e.startswith(('accept must disclose unverified executed service capability as a resource_service condition:',
                              'missing independent baseline coverage:','missing extracted constraint mappings:','unverified design decision requires revise and an open blocking design finding:','accept leaves transformed semantic correspondence unreviewed:',
                              'counterexample requires explicit adjudication:','counterexample/','counterexample review must name',
                              'duplicate counterexample review:','counterexample review requires actual request/receipt/check ids',
                              'counterexample review must be an object','counterexample_reviews must be a list')) for e in errors)
            if missing_evidence or judgment_gap:
                failed_source=data.get('source_analysis',{}).get('ok') is not True
                if missing_evidence and tool_calls>=max_tool_calls and not failed_source:raise AuditReportRejected('accept lacks executed audit evidence; tool limit reached, reference repair cannot close this gap')
                history.append({'kind':'audit_evidence_reopen','rejected_report':review,'validation':check,
                    'action':'The rejected report version is preserved. Make a new independent audit judgment using the same packet, actual receipts and remaining existing allowance. This is not a reference repair. Acquire missing evidence if tools remain; disclose executed unknown capabilities, and do not accept a failed source analysis. No design edits, cap resets or extra calls.'})
                last_report=None;last_report_label=None
                continue
            if any('revise requires an open blocking' in e for e in check.get('errors',[])):
                raise AuditReportRejected('audit revise verdict has no necessary blocking change; new independent audit judgment required, not a design revision')
            last_report=review;last_report_label=name;last_report_validation=check
            history.append({'kind':'report_validation_repair','packet_sha256':data['packet_sha256'],'rejected_report':review,'validation':check,'feedback':report_feedback(review,check)})
            continue
        if followup_pending and review.get('verdict')!='revise':
            terminal=tool_calls>=max_tool_calls or step+2>=response_limit
            if review.get('verdict')=='insufficient_evidence' and terminal:
                followup_pending=False
            else:
                history.append({'kind':'audit_evidence_reopen','report':review,'validation':check,
                    'action':'This report is preserved, but another insufficient judgment cannot bypass the pending engineering follow-up while allowance remains. Select one critical hypothesis grounded in original/proposal evidence and execute recompute or check_proposal before independently judging again. A valid necessary revise may proceed without artificial arithmetic. This is not reference repair or a demand to complete every coverage field.'})
                last_report=None;last_report_label=None
                continue
        gap=pending_engineering_followup(history,data.get('source_execution')) if not followup_started else None
        if review.get('verdict')=='insufficient_evidence' and gap and tool_calls<max_tool_calls and step+2<response_limit:
            followup_started=True;followup_pending=True
            history.append({'kind':'audit_evidence_reopen','report':review,'validation':check,'pending_check':gap,
                'action':'This valid insufficient-evidence report is preserved. Within the remaining existing allowance, select one unresolved critical hypothesis using original-input/proposal evidence and execute recompute or check_proposal before another insufficient judgment; a validated necessary revise may proceed without artificial arithmetic. Do not fill every coverage field or assume the gap is a chip defect. A genuinely failed/unsupported execution or no new effective evidence may still end honestly with insufficient_evidence. No design edits, cap resets, extra calls or reference-only repair.'})
            last_report=None;last_report_label=None
            continue
        dump(out/(label+'.json'),review);dump(out/(label+'.check.json'),check)
        dump(out/(label+'.feedback.json'),report_feedback(review,check))
        if review.get('baseline_corrections'):dump(out/'audit-baseline.invalidated.json',{'baseline_sha256':baseline['baseline_sha256'],'corrections':review['baseline_corrections'],'origin':label})
        return review,check
    raise AuditSessionLimit('bounded audit responses exhausted without an evidence-valid report')


def diagnose(out,sources,invoke,settings):
    document=json.loads((out/'snapshot/audit/design.json').read_text())
    checks=execute_tool(out,'diagnostic-budget',document,sources,require_document=True)
    review,validation=run(out,'review-0',sources,document,checks,invoke,settings['max_audit_tool_calls'],max_baseline_repairs=settings['max_audit_baseline_repairs'],max_report_repairs=settings.get('max_audit_report_repairs',3))
    return {'status':'audit_completed','verdict':review['verdict'],'report_check_ok':validation['ok'],'budget_gate':checks.get('gate'),
            'scope':'diagnostic review only; no architecture delivery, design edits, RTL or physical proof'}
