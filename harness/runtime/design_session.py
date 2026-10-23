"""Bounded skill-tool conversation; transport and execution, no architecture strategy."""
import json
import copy
from harness.prompts.architecture import parse_design, parse_payload, FINAL_RESPONSE, work_model_prompt, design_data_view, design_history_view
from harness.runtime.run_store import dump,digest
from harness.runtime.skill_tools import execute_script

class DesignToolLimit(RuntimeError):pass

def identity(document):
    data={k:v for k,v in document.items() if k not in ('architecture_md','tool_receipt')}
    return digest(json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())

def progress_signature(document):
    """Track executable declarations, not prose or citation placement.

    This is a syntactic progress test. It cannot infer whether revised prose
    changes a source interpretation; audit still owns that semantic judgment.
    """
    editorial={'basis','source','uncertainty','organization','risks','ports_and_schedule'}
    def machine(value):
        if isinstance(value,dict):
            return {k:machine(v) for k,v in value.items() if k not in editorial and not (k in ('citations','references') and isinstance(v,list) and all(isinstance(x,dict) and ('file' in x or 'evidence_id' in x) for x in v))}
        if isinstance(value,list):return [machine(v) for v in value]
        return value
    work=copy.deepcopy(document.get('work_model',{}))
    # Keep shape/count/repetition/status/dependency changes even when candidates
    # are unchanged. Natural-language descriptions are not machine costs.
    for row in work.get('items',[]) if isinstance(work,dict) else []:
        if isinstance(row,dict):
            for key in ('semantics','condition','state_lifetime'):row.pop(key,None)
            if isinstance(row.get('count'),dict):row['count'].pop('reason',None)
    view={'selected':document.get('selected'),'candidates':machine(document.get('candidates',[])),'work_model':machine(work)}
    return digest(json.dumps(view,sort_keys=True,separators=(',',':')).encode())


def protocol_feedback(error):
    """Transport diagnosis only; parser rejection is not an architecture finding."""
    syntax=isinstance(error,json.JSONDecodeError)
    if 'span needs integer bounds' in str(error):
        return {'kind':'source_location','diagnostics':[str(error)],
                'action':'Choose actual original-source bounds within the reported file range and resubmit the explicit reference. Preserve unaffected judgments. The host does not clamp bounds or select evidence.',
                'scope':'source location only; no semantic correction or architecture finding'}
    return {'kind':'json_syntax' if syntax else 'protocol_structure','diagnostics':[str(error)],
            'action':('Return one complete JSON object with valid serialization; preserve the intended declarations. Never invent a truncated suffix.' if syntax else 'Return exactly the requested text marker and fields for this entry; no narration, native tool tags or extra response objects.'),
            'scope':'response transport only; no conclusion about design feasibility or correctness'}


def calculation_feedback(report):
    """Explain existing diagnostics without altering checks or design data.

    Only exact calculator contradictions are labelled resource failures. Missing
    fields, undefined expressions and unknown models do not establish infeasibility.
    Budget conclusions use complete valid data; the raw report remains available.
    """
    groups={}
    service=('declared demand exceeds service','inflight below latency*rate lower bound',
             'response queue below reserved outstanding bytes','live data exceeds allocation',
             'buffer allocation exceeds referenced macro','accesses exceed service in window',
             'overlap interval below whole-task cost lower bound','physical requests exceed input','operand used before synchronous read is ready','demand exceeds allocation','simultaneous live demand exceeds macro capacity')
    for error in report.get('errors',[]):
        text=str(error)
        if any(message in text for message in service):kind='declared_resource_contradiction'
        elif any(message in text.lower() for message in ('citation','quote','excerpt')):kind='source_evidence_declaration'
        elif any(message in text.lower() for message in ('undefined','identifier','expression','formula','syntax','division by zero','unit','dimension')):kind='expression_or_unit_declaration'
        else:kind='schema_or_mapping_declaration'
        groups.setdefault(kind,[]).append(error)
    actions={
        'declared_resource_contradiction':'The declared supply, credits, live capacity or interval cannot support its declared demand. Revise the candidate resource/rate/schedule and recalculate all affected costs; preserve source requirements. This rejects this declaration, not all possible architectures.',
        'source_evidence_declaration':'Use exact excerpts from the frozen sources and correct referenced fields; do not alter source text or manufacture supporting numbers.',
        'expression_or_unit_declaration':'Correct the named identifiers/formulas/units using existing facts and explicit justified assumptions, then recalculate. An unevaluable expression is not a failed hardware budget.',
        'schema_or_mapping_declaration':'Correct the affected complete candidates using actual existing ids and replace_candidates with the current base hash; the executor preserves other candidates. Recalculate the full candidate set. Source requirements remain frozen.'}
    feedback={'data_valid':bool(report.get('ok')),'issues':[{'kind':kind,'diagnostics':errors,'action':actions[kind]} for kind,errors in groups.items()],
              'scope':'classification of existing checks only; no new requirements, acceptance threshold or impossibility proof'}
    gate=report.get('gate',{})
    if report.get('ok'):
        if gate.get('hard_budget_violations'):
            feedback['issues'].append({'kind':'hard_budget_exceeded','constraint_ids':gate['hard_budget_violations'],
                                      'action':'Valid estimates exceed these hard limits. Revise the organization or select another evaluated candidate within the limits; if search stops, report the unmet limits honestly. Do not lower requirements or claim impossibility without a general lower-bound argument.'})
        if gate.get('status')=='unknown':
            feedback['issues'].append({'kind':'unresolved_design_evidence','constraint_ids':gate.get('unverified_design_budgets',[]),'models':gate.get('unsupported_models',[]),
                                      'action':'Supply justified design-stage evidence or explicitly retain this unresolved condition. Unsupported cost models are limitations of the evidence, not proof that the hardware cannot work; unknown does not pass the delivery gate.'})
    elif gate.get('hard_budget_violations'):
        feedback['provisional_budget_ids']=gate['hard_budget_violations']
        feedback['budget_action']='Repair invalid declarations before treating partial totals as budget conclusions.'
    return feedback


def parse_turn(text):
    if text.count('<<<BUDGET_REQUEST>>>')==1 and '<<<ARCHITECTURE_DATA>>>' not in text:
        request=parse_payload(text.split('<<<BUDGET_REQUEST>>>',1)[1])
        if request.get('tool')=='evaluate_design':
            if set(request)!={'tool','document'} or not isinstance(request['document'],dict):
                extra=sorted(set(request)-{'tool','document'})
                missing=sorted({'tool','document'}-set(request))
                raise ValueError('evaluate_design requires one document object inside a wrapper with only tool and document; wrapper extra keys='+str(extra)+'; missing keys='+str(missing)+'. All design fields belong inside document; the host does not relocate them.')
        elif request.get('tool')=='replace_candidates':
            validate_replacement(request)
        else:raise ValueError('only evaluate_design or replace_candidates is supported')
        return {'tool_request':request}
    return {'final':parse_design(text)}

def execute_tool(out,label,document,sources,require_document=False):
    """Run ONLY the frozen skill script. No model-supplied paths/commands/code."""
    script=out/'snapshot/harness/skills/microarchitecture-design/scripts/evaluate.py'
    report=execute_script(out,label,script,{'document':document,'sources':sources},['--require-document'] if require_document else [])
    if report['design_sha256']!=identity(document):raise ValueError('calculator identity mismatch')
    return report


def validate_replacement(request):
    required={'tool','base_design_sha256','candidates'}
    optional={'selected','selection_conditions','revision_response','requirement_corrections','work_model','work_model_revision'}
    if not isinstance(request,dict) or not required<=set(request) or set(request)-(required|optional) or request['tool']!='replace_candidates':
        raise ValueError('replace_candidates requires tool, base_design_sha256, candidates and only declared optional fields')
    if not isinstance(request['base_design_sha256'],str) or not request['base_design_sha256'] or not isinstance(request['candidates'],list):
        raise ValueError('replacement requires the actual base hash and complete candidate array')
    if not request['candidates'] and not set(request)&optional:raise ValueError('empty replacement with no updates')
    for key in ('selection_conditions','revision_response','requirement_corrections'):
        if key in request and not isinstance(request[key],list):raise ValueError(key+' must be an array')
    for key in ('work_model','work_model_revision'):
        if key in request and not isinstance(request[key],dict):raise ValueError(key+' must be an object')
    if 'selected' in request and (not isinstance(request['selected'],str) or not request['selected']):raise ValueError('selected needs an existing candidate id')


def replace_candidates(document,request):
    """Replace model-authored whole candidates by id; preserve all other data.

    This is a transport operation. No field merging, resource inference or data
    repair happens here; the complete result must pass the same calculator.
    """
    validate_replacement(request)
    if document is None or request['base_design_sha256']!=identity(document):
        raise ValueError('revision base does not match the last evaluated design')
    rows=document.get('candidates')
    if not isinstance(rows,list) or any(not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'] for row in rows):
        raise ValueError('base needs existing candidate ids; submit a full evaluate_design request')
    ids=[row['id'] for row in rows]
    if len(ids)!=len(set(ids)):raise ValueError('base candidate ids must be unique; submit a full evaluate_design request')
    replacements={}
    for row in request['candidates']:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str) or row['id'] not in ids:
            raise ValueError('replacement must name an existing candidate id; new ids need full evaluate_design')
        if row['id'] in replacements:raise ValueError('replacement candidate ids must be unique')
        replacements[row['id']]=copy.deepcopy(row)
    if 'selected' in request and request['selected'] not in ids:raise ValueError('selected must name an existing candidate')
    if 'work_model' in request and request['work_model']!=document.get('work_model'):
        revision=request.get('work_model_revision',{})
        if not isinstance(revision,dict) or not revision.get('reason') or not revision.get('references'):
            raise ValueError('this work_model replacement needs its own work_model_revision reason and original-input references')
    result=copy.deepcopy(document)
    result['candidates']=[replacements.get(row['id'],row) for row in result['candidates']]
    for key in set(request)-{'tool','base_design_sha256','candidates'}:result[key]=copy.deepcopy(request[key])
    # Python structural equality treats True == 1. The executed JSON identity
    # must preserve field types, including repairs from invalid numeric flags.
    if identity(result)==identity(document):raise ValueError('replacement does not change evaluated data')
    return result


def work_context_identity(requirements,sources):
    return digest(json.dumps({'requirements':requirements,'sources':sources},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())


def prepare_work_model(out,label,prompt,requirements,sources,invoke,max_responses):
    cache=out/'work-model.current.json'
    if cache.exists():
        record=json.loads(cache.read_text())
        actual=digest(json.dumps(record['work_model'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
        if actual!=record['work_model_sha256'] or record.get('context_sha256')!=work_context_identity(requirements,sources):raise ValueError('work-model checkpoint identity mismatch')
        return record['work_model']
    previous=None;errors=[];feedback=None
    base=out/'snapshot/harness/skills/microarchitecture-design'
    script=base/'scripts/evaluate.py'
    skill=(base/'SKILL.md').read_text();reference=(base/'references/work-model.md').read_text()
    for step in range(max_responses):
        name=f'{label}.work-{step}';path=out/(name+'.json');failure=out/(name+'.feedback.json')
        if path.exists():model=json.loads(path.read_text())
        elif failure.exists():
            feedback=json.loads(failure.read_text());errors=feedback['diagnostics'];continue
        else:
            message=work_model_prompt(skill,reference,sources,requirements,previous,errors)
            if feedback:message+='\nCURRENT WORK MODEL CHECK FEEDBACK\n'+json.dumps(feedback,ensure_ascii=False)
            try:model=invoke(name,'architecture',message,lambda text:parse_payload(text.split('<<<WORK_MODEL>>>',1)[-1]))
            except ValueError as e:
                feedback=protocol_feedback(e);errors=feedback['diagnostics'];dump(failure,feedback);continue
            dump(path,model)
        check=execute_script(out,name+'.check',script,{'work_model':model,'requirements':requirements,'sources':sources},['--work-model-only'])
        if check['ok']:
            dump(cache,{'work_model':model,'work_model_sha256':check['work_model_sha256'],'origin':name,'context_sha256':work_context_identity(requirements,sources)});return model
        previous=model;errors=check['errors'];feedback=calculation_feedback(check)
    raise DesignToolLimit('bounded work-model extraction failed; no candidates support delivery')


def generate(out,label,prompt,requirements,sources,invoke,max_tool_calls,max_format_repairs=2,revision_base=None):
    """Requests/results persist so resume need not repeat completed model turns."""
    base_path=out/(label+'.revision-base.request.json')
    if revision_base is None and base_path.exists():raise ValueError('revision-base checkpoint requires the same controller proposal')
    if revision_base is not None:
        if not isinstance(revision_base,dict) or revision_base.get('requirements')!=requirements or not isinstance(revision_base.get('work_model'),dict):
            raise ValueError('revision base needs the same frozen requirements and a complete work_model')
        revision_base=copy.deepcopy({k:v for k,v in revision_base.items() if k not in ('architecture_md','tool_receipt')})
        if base_path.exists() and json.loads(base_path.read_text())!={'document':revision_base,'sources':sources}:
            raise ValueError('revision-base checkpoint context mismatch')
    start_path=out/(label+'.work-summary.request.json')
    if start_path.exists():
        start=json.loads(start_path.read_text())
        if set(start)!={'work_model','requirements','sources'} or start['requirements']!=requirements or start['sources']!=sources:
            raise ValueError('initial work-summary checkpoint context mismatch')
        # Replay this conversation from its original accepted interpretation.
        # current.json may already contain a later revision from this same label.
        work_model=start['work_model']
        if revision_base is not None and work_model!=revision_base['work_model']:
            raise ValueError('revision-base work-model checkpoint mismatch')
    else:
        work_model=revision_base['work_model'] if revision_base is not None else prepare_work_model(out,label,prompt,requirements,sources,invoke,max_tool_calls)
    summary_check=execute_script(out,label+'.work-summary',out/'snapshot/harness/skills/microarchitecture-design/scripts/evaluate.py',{'work_model':work_model,'requirements':requirements,'sources':sources},['--work-model-only'])
    if not summary_check['ok']:raise ValueError('accepted work-model checkpoint fails executed checks')
    work_summary=summary_check['compact_summary']
    history=[];last_receipt=None;last_document=None;tool_calls=0;format_repairs=0
    if revision_base is not None:
        # Revalidate this immutable previous proposal using this run's frozen
        # calculator. This supplies a replacement base, not a revised delivery.
        last_receipt=execute_tool(out,label+'.revision-base',revision_base,sources)
        last_document=revision_base
        history.append({'revision_base':{'origin':label+'.revision-base','design_sha256':last_receipt['design_sha256'],
                        'scope':'Previous controller proposal rechecked for replacement identity; a new model-requested calculation is required before final delivery.'},'result':last_receipt})
    # Allow bounded format repairs without spending calculation requests.
    # Global call/time budgets still apply to every provider invocation.
    for turn in range(2*max_tool_calls+max_format_repairs+2):
        name=f'{label}.step-{turn}';path=out/(name+'.json')
        compact=design_history_view(history)
        message=prompt+'\nCURRENT SOURCE-BACKED WORK MODEL (fallible interpretation)\n'+json.dumps(work_model,ensure_ascii=False)+'\nSKILL TOOL HISTORY (current data shown once)\n'+json.dumps(compact,ensure_ascii=False)
        message+='\nEXECUTED DECLARED-WORK SUMMARY\n'+json.dumps(work_summary,ensure_ascii=False)
        message+='\nFEEDBACK SCOPE: any supplied independent architecture audit is a semantic judgment to verify against the frozen inputs and actual evidence, not a new source requirement. Transport/report-validation errors require protocol repairs; calculator declaration errors require data repairs; valid budget failures require architectural search or an honest stopped-search result.'
        if last_document is not None:
            message+='\nCURRENT EVALUATED DATA\n'+json.dumps(design_data_view(last_document,requirements,work_model),ensure_ascii=False)
            message+='\nVIEW NOTE: exact duplicate requirements and unchanged work_model are shown in their existing sections only; saved declarations and all executed checks remain complete.'
        if revision_base is not None and tool_calls==0:
            message+='\nCURRENT STEP: revise the rechecked base using <<<BUDGET_REQUEST>>> and replace_candidates with its actual base hash, or a complete evaluate_design. An empty candidates array is allowed only with complete optional field replacements; no deep merge. Request calculation before final Markdown; the base recheck is not revised delivery evidence.'
        elif last_receipt is None:
            message+='\nCURRENT STEP: propose candidates and request calculation. Return ONLY <<<BUDGET_REQUEST>>> then one JSON object. No Markdown documents, no narration or native tool tags.'
        elif not last_receipt['ok']:
            if tool_calls>=max_tool_calls:raise DesignToolLimit('calculation requests exhausted with invalid data; final narrative blocked; see saved tool results')
            message+='\nCURRENT STEP: repair the reported data errors and request recalculation. Use replace_candidates with complete affected candidates and the actual base hash; other candidates are preserved. Do not write final Markdown while data checks fail. Hard-budget overruns with valid data are assessed separately.'
        else:
            message+='\nCURRENT STEP: inspect the actual calculation result; request recalculation if data changes, otherwise finalize.\n'+FINAL_RESPONSE
        if last_receipt is not None:message+='\nCURRENT CALCULATION FEEDBACK\n'+json.dumps(calculation_feedback(last_receipt),ensure_ascii=False)
        if tool_calls>=max_tool_calls:message+='\nTool-call limit reached. Finalize the last evaluated data unchanged, or explicitly report a requirement correction. Do not submit another tool request.'
        if path.exists():
            reply=json.loads(path.read_text())
        else:
            try:reply=invoke(name,'architecture',message,parse_turn)
            except ValueError as error:
                reply={'format_error':str(error),'feedback':protocol_feedback(error),'instruction':'Return a text protocol response, no native tool-call tags; do not alter requirements.'}
            dump(path,reply)
        if 'format_error' in reply:
            format_repairs+=1
            if format_repairs>max_format_repairs:raise DesignToolLimit('local design format repair limit reached')
            history.append(reply);continue
        if 'tool_request' in reply:
            if tool_calls>=max_tool_calls:raise DesignToolLimit('per-design calculation request limit reached')
            request=reply['tool_request']
            try:
                document=replace_candidates(last_document,request) if request['tool']=='replace_candidates' else copy.deepcopy(request['document'])
            except (KeyError,TypeError,ValueError,IndexError) as error:
                history.append({'revision_error':str(error),'feedback':{'kind':'revision_declaration','diagnostics':[str(error)],'action':'Use replace_candidates with complete candidates carrying existing ids and the actual last base hash. Unsubmitted candidates are preserved, omitted fields in a submitted candidate are not filled. Use full evaluate_design to change the candidate set; frozen requirements remain unchanged.'}});continue
            if document.get('requirements',requirements)!=requirements:
                return {'requirements':document['requirements']}
            document['requirements']=requirements
            document.setdefault('work_model',work_model)
            if document['work_model']!=work_model:
                revision=document.get('work_model_revision',{})
                if not isinstance(revision,dict) or not revision.get('reason') or not revision.get('references'):
                    history.append({'revision_error':'work_model change needs work_model_revision reason and original-input references'});continue
            tool_calls+=1
            result_path=out/(name+'.tool.result.json')
            report=json.loads(result_path.read_text()) if result_path.exists() else execute_tool(out,name+'.tool',document,sources)
            last_receipt=report;last_document=document
            if report.get('work_model',{}).get('ok') and report['work_model'].get('revision_ok'):
                work_summary=report['work_model']['compact_summary']
                work_model=document['work_model'];dump(out/'work-model.current.json',{'work_model':work_model,'work_model_sha256':report['work_model']['work_model_sha256'],'origin':name,'context_sha256':work_context_identity(requirements,sources)})
            corrections=[c for c in document.get('requirement_corrections',[]) if isinstance(c,dict) and c.get('requires_change') is True]
            if corrections:return document
            history.append({'request':{'tool':'evaluate_design','document':document},'result':report,'feedback':calculation_feedback(report)})
            continue
        final=reply['final']
        if tool_calls==0:
            history.append({'error':'final narrative rejected: submit a model-requested calculation for this design session before Markdown delivery'});continue
        if last_receipt is not None and not last_receipt['ok']:
            history.append({'error':'final narrative rejected: repair data and recalculate before Markdown delivery'});continue
        if set(final)!={'evaluated_design_sha256','architecture_md'} or last_receipt is None or final['evaluated_design_sha256']!=last_receipt['design_sha256']:
            history.append({'error':'final must reference the actual last calculation and contain exactly one Markdown document'});continue
        document=dict(last_document,architecture_md=final['architecture_md'])
        if document.get('requirements',requirements)!=requirements:return document
        document['requirements']=requirements
        if identity(document)!=last_receipt['design_sha256']:
            history.append({'error':'final data identity differs from the executed calculation'});continue
        document['tool_receipt']={'design_sha256':last_receipt['design_sha256'],'calculated_by':'frozen skill scripts/evaluate.py'}
        return document
    raise DesignToolLimit('per-design response limit reached before a final design with matching calculation evidence')
