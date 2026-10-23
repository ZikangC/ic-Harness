"""Request assembly and shared wire formats, independent of model transport."""
import json
import re
import copy
from harness.adapters.omp import parse_json, json_decoder


AUTO_QUANTITIES = {'area','capacity','task_cycles','cycles_per_item','items_per_second'}
DATA = """Use the candidate declaration and response protocol in the supplied design skill budget-tools reference. The executor supplies frozen requirements and the accepted source-backed work_model. Submit model-authored candidate choices, not calculator-produced totals. No Markdown in calculation requests."""
FINAL_RESPONSE = """Return exactly two markers in this order:
<<<ARCHITECTURE_DATA>>>
{"evaluated_design_sha256":"exact hash from the most recent executed calculation"}
<<<ARCHITECTURE_MD>>>
One complete plain Markdown architecture.md meeting the supplied design skill's delivery requirements. No other document fields or response markers; no JSON-encoded prose. Keep the implementation contract in this document.
"""
REVIEW_TOPICS = ('requirements','cycle_accounting','resource_service','abi','numerics','area_basis')


def parse_payload(text):
    """One JSON protocol object, with optional code-fence framing only."""
    payload=text.strip()
    if payload.startswith('```'):
        first,separator,payload=payload.partition('\n')
        if not separator or first not in ('```','```json'):raise ValueError('invalid JSON code fence')
        payload=payload.strip()
    value,end=json_decoder().raw_decode(payload)
    if not isinstance(value,dict) or payload[end:].strip() not in ('','```'):
        raise ValueError('expected one complete JSON protocol object; ambiguous suffix rejected')
    return value



def numbered(text):
    return '\n'.join(f'{i}: {line}' for i,line in enumerate(text.splitlines(),1))


def context(sources):
    return ('FROZEN SOURCE NAMES: spec.md and reference.py are the logical primary inputs. '
            'Any support/<input-relative-path> files are declared original helpers/configuration; follow relevant imports and definitions within this supplied closure. '
            'A noncanonical reference entry also appears at its original support/ path with identical content; do not count the alias twice. '
            'Do not assume undeclared local files. Source claims and file identity are not semantic proof.\n\n'+
            '\n\n'.join('FILE '+name+'\n'+numbered(text) for name,text in sources.items()))


def source_summary(analysis, execution):
    """Model-facing view of host-verified data; execution evidence stays on disk."""
    analysis=analysis if isinstance(analysis,dict) else {}
    execution=execution if isinstance(execution,dict) else {}
    applicable=[{key:copy.deepcopy(row[key]) for key in ('section','declaration_id','workload','dependencies','status','errors') if key in row}
                for row in execution.get('applicability',[]) if isinstance(row,dict)]
    return {'kind':'host_verified_source_view','ok':analysis.get('ok') is True,
            'coverage_complete':execution.get('coverage_complete') is True,
            'verified_data':copy.deepcopy(execution.get('verified_data',{})),
            'declaration_errors':copy.deepcopy(analysis.get('errors',[])),
            'verification_errors':copy.deepcopy(execution.get('errors',[])),
            'missing_declarations':copy.deepcopy(analysis.get('missing_declarations',[])),
            'applicability':applicable,
            'scope':'Host-derived availability and typed quantitative view of auditor-selected declarations; not original-source semantic evidence. Raw script responses, transaction receipts and dependency fingerprints remain in the packet and execution sidecars. Cite the frozen original input files, including supplied support/ files, for semantics; source transaction IDs are not audit observation references.'}


def baseline_feedback(diagnosis):
    """Render diagnostics without duplicating full execution receipts/hashes."""
    view={key:value for key,value in diagnosis.items() if key not in ('analysis_result','source_execution')}
    if 'analysis_result' in diagnosis or 'source_execution' in diagnosis:
        view['source_analysis']=source_summary(diagnosis.get('analysis_result'),diagnosis.get('source_execution'))
    return view


def tool_view(entry):
    """Render semantic tool results once; complete execution proofs stay stored."""
    view=copy.deepcopy(entry)
    original=entry.get('result',{})
    if not isinstance(original,dict):return view
    omitted={'binding','model_sha256','receipt_sha256','declaration_sha256','comparison_evidence','values','calculation_ids','subreceipts','work_result'}
    result={key:copy.deepcopy(value) for key,value in original.items() if key not in omitted}
    if 'subreceipts' in original:
        if not isinstance(original['subreceipts'],list):
            result['subreceipts']=copy.deepcopy(original['subreceipts'])
            view['result']=result
            return view
        result['subreceipts']=[]
        for row in original['subreceipts']:
            if not isinstance(row,dict):
                result['subreceipts'].append(copy.deepcopy(row));continue
            child={key:copy.deepcopy(value) for key,value in row.items() if key not in omitted}
            # The parent keeps actual checks, observations and citation text.
            # Remove a repeated child collection only after exact containment.
            if row.get('status')=='executed':
                for key in ('checks','observations','citation_bindings'):
                    if isinstance(row.get(key),list) and isinstance(original.get(key),list) and all(item in original[key] for item in row[key]):
                        child.pop(key,None)
            result['subreceipts'].append(child)
    if isinstance(original.get('work_result'),dict):
        work=original['work_result']
        compact={key:copy.deepcopy(value) for key,value in work.items() if key not in omitted}
        for key in ('observations','citation_bindings'):
            children=work.get(key);parents=original.get(key)
            if not isinstance(children,list) or not isinstance(parents,list):continue
            def semantic(item):
                if not isinstance(item,dict):return item
                return {field:value for field,value in item.items() if field!='receipt_id'}
            if all(any(semantic(child)==semantic(parent) for parent in parents) for child in children):compact.pop(key,None)
        result['work_result']=compact
    view['result']=result
    if isinstance(entry.get('executed_values'),dict):
        view['executed_values']={key:copy.deepcopy(value) for key,value in entry['executed_values'].items()
                                 if key not in ('values','calculation_ids','checks','subreceipts')}
    return view


def extract(skill, sources, previous=None, errors=None, quantities=''):
    prompt = skill+'\nTASK: Extract original requirements only.\n'+''+'\n'+context(sources)
    if quantities:
        prompt += '\nFROZEN SOURCE QUANTITY SELECTORS (line and number are both one based; lexical locations, not requirement interpretations)\n'+quantities
    if previous is not None:
        prompt += '\nCorrect these errors while preserving valid declarations:\n'+json.dumps(errors,ensure_ascii=False)+'\nPREVIOUS RAW REQUIREMENT DECLARATION (the repair base; not materialized values)\n'+json.dumps(previous,ensure_ascii=False)
    return prompt


def work_model_prompt(skill,reference,sources,requirements,previous=None,errors=None):
    message=skill+'\n'+reference+'\nCURRENT TASK: extract a source-backed functional work model BEFORE any candidate or resource decision. Return ONLY <<<WORK_MODEL>>> followed by one work_model JSON object. It is not accepted yet and must be corrected when checks fail. No candidate schema, budget request or architecture document applies in this entry.\n'+context(sources)
    message+='\nFROZEN REQUIREMENT FACTS AND CONSTRAINTS\n'+json.dumps(requirements,ensure_ascii=False)
    message+='\nREQUIRED PERFORMANCE PROFILE IDS: '+json.dumps(sorted({c['workload'] for c in requirements.get('constraints',[]) if isinstance(c.get('workload'),str)}))
    message+='\nAVAILABLE INPUT SYMBOLS: '+json.dumps([f['id'] for f in requirements.get('facts',[])])+'. Profile quantity is the number of workload elements in one RUN (not nodes, operations, cycles or number of RUNs); use input fact identifiers or source-supported literals and an explicit unit. workload_elements exists only inside task counts; scoped repetition is program-derived.'
    if previous is not None:message+='\nREPAIR THE UNACCEPTED INTERPRETATION; do not preserve invalid fields.\n'+json.dumps({'previous':previous,'errors':errors},ensure_ascii=False)
    return message


def audit_baseline(skill,reference,sources,previous=None,errors=None,corrections=None):
    message=skill+'\n'+reference+'\nCURRENT TASK: source-only baseline entry. No proposal, designer extraction, work model, prior architecture audit or budget is available. Return ONLY <<<AUDIT_BASELINE>>> then ONE complete baseline JSON including scope, obligations, ambiguities, analysis AND top-level limits. The host executes analysis against these same inputs after this response and binds input/response provenance. Omit identity and repair-target metadata; submit source interpretation only. This entry has no tool request or report protocol. Source revisions are bounded by the existing limit.\n'+context(sources)
    if previous is not None:message+='\nBASELINE REPAIR (source interpretation only)\n'+json.dumps({'previous':previous,'errors':errors,'source_corrections':corrections or []},ensure_ascii=False)
    return message



def design_data_view(document, requirements, work_model=None):
    """Omit only data exactly equal to already-present frozen/current sections."""
    view=copy.deepcopy(document)
    if view.get('requirements')==requirements:view.pop('requirements',None)
    if work_model is not None and view.get('work_model')==work_model:view.pop('work_model',None)
    return view


def design_history_view(history):
    """Keep current evidence once; older results retain all diagnostics and gate."""
    latest=max((i for i,row in enumerate(history) if isinstance(row.get('result'),dict)),default=-1)
    view=[]
    for i,row in enumerate(history):
        entry=copy.deepcopy({k:v for k,v in row.items() if k!='request'})
        if isinstance(entry.get('result'),dict):
            if i!=latest:
                entry['result']={k:v for k,v in entry['result'].items()
                                 if k in ('ok','errors','gate','design_sha256','unchecked','scope')}
                entry['superseded_calculation']='Full evidence retained in the saved result; this historical view is not current acceptance evidence.'
            else:
                # This exact current feedback is also displayed at the end.
                entry.pop('feedback',None)
        view.append(entry)
    return view


def design(skill, sources, requirements, previous=None, findings=None, feedback=None):
    prompt = skill+'\nTASK: '+('Revise the design' if previous else 'Create candidate microarchitectures')+'\nSTRUCTURED CANDIDATE DATA SCHEMA\n'+DATA+'\n'+context(sources)+'\nEXTRACTED REQUIREMENTS (fallible summary; cannot silently change)\n'+json.dumps(requirements,ensure_ascii=False)
    prompt += '\nDESIGN TOOL PROTOCOL: Follow Execution and identity, and Repair and gate, in the supplied budget-tools reference. This is a text response protocol. The first turn ONLY submits <<<BUDGET_REQUEST>>>; no Markdown. With a supplied rechecked revision base, use replace_candidates or a complete evaluate_design; without one, submit a complete evaluate_design. Read the actual result before choosing a repair or final response. Subsequent turns follow that same contract and current execution feedback.\n'
    prompt += '\nMODEL-DECLARED METRIC KEYS (other constraints are automatic): '+json.dumps([c['id'] for c in requirements['constraints'] if c.get('quantity') not in AUTO_QUANTITIES])+'\nAVAILABLE INPUT FACT IDENTIFIERS: '+json.dumps([f['id'] for f in requirements['facts']])+'\n'
    seed=feedback.get('native_revision_seed') if isinstance(feedback,dict) else None
    seeded=isinstance(seed,dict) and isinstance(previous,dict) and seed.get('proposal')==previous and not findings
    if feedback and (seeded or not (previous and findings)):
        supplemental=feedback
        if seeded:
            supplemental={key:value for key,value in feedback.items() if key!='native_revision_seed'}
            draft=supplemental.get('native_draft')
            if isinstance(draft,dict) and draft and all(key in previous and value==previous[key] for key,value in draft.items()):
                supplemental={key:value for key,value in supplemental.items() if key!='native_draft'}
        prompt += '\nSUPPLEMENTAL CURRENT-DESIGN REVIEW, NOT REQUIREMENTS\n'+json.dumps(supplemental,ensure_ascii=False)
    if previous:
        prompt += '\nREVISION BASE: the executor supplies the complete rechecked prior candidate data and actual hash in CURRENT EVALUATED DATA. The prior Markdown and receipts are not editable candidate fields.'
        if findings or not seeded:prompt += '\nUNRESOLVED CHECKS/AUDIT\n'+json.dumps(findings,ensure_ascii=False)
        if isinstance(previous.get('architecture_md'),str):
            prompt += '\nPREVIOUS UNIFIED ARCHITECTURE DOCUMENT (revision context; not a replaceable candidate field)\n'+previous['architecture_md']
    return prompt


def compact_audit_repair(reference,report,validation,history):
    """A bound text-field repair, never a compact engineering audit."""
    errors=(validation or {}).get('errors',[])
    if not isinstance(report,dict) or not validation or validation.get('ok') is not False or 'scope' not in validation or not isinstance(errors,list) or len(errors)!=1 or not isinstance(report.get('checks'),list):return None
    if any(not isinstance(report.get(key),list) for key in ('requirements','observations','findings','unverified','revision_checks')):return None
    topics=[row['topic'] for row in report['checks'] if isinstance(row,dict) and isinstance(row.get('topic'),str) and
            (not isinstance(row.get('evidence'),str) or not row['evidence'].strip()) and
            errors[0]=='check/'+row['topic']+'.evidence must be a nonempty reasoning STRING; references belong in the separate references array']
    if len(topics)!=1:return None
    begin='## Frozen evidence and local repairs';end='## Read-only source lookup'
    if reference.count(begin)!=1 or reference.count(end)!=1 or reference.index(begin)>=reference.index(end):return None
    contract=reference.split(begin,1)[1].split(end,1)[0]
    if not contract.strip():return None
    numeric=set();checks=set()
    def selected(value):
        if isinstance(value,dict):
            if all(isinstance(value.get(k),str) for k in ('request_id','calculation_id')):numeric.add((value['request_id'],value['calculation_id']))
            if all(isinstance(value.get(k),str) for k in ('request_id','receipt_id','check_id')):checks.add((value['request_id'],value['receipt_id'],value['check_id']))
            for child in value.values():selected(child)
        elif isinstance(value,list):
            for child in value:selected(child)
    selected(report);calculations=[];check_refs=[]
    for entry in history:
        verified=entry.get('executed_values') or {};request_id=entry.get('result',{}).get('request_id')
        calculations.extend({'request_id':request_id,'calculation_id':cid,'actual':verified['values'][cid]}
                            for cid in verified.get('calculation_ids',[]) if (request_id,cid) in numeric)
        check_refs.extend({'request_id':request_id,'receipt_id':child['id'],'check_id':row['id'],'status':row['status']}
                          for child in verified.get('subreceipts',[]) for row in child.get('checks',[])
                          if (request_id,child['id'],row['id']) in checks)
    view={'target_report':report,'current_errors':errors,
          'current_check_reference_bindings':[row for row in validation.get('bindings',[]) if row.get('location')==topics[0]],
          'referenced_bound_calculations':calculations,'referenced_bound_checks':check_refs}
    return ('LOCAL STRUCTURAL REPORT REPAIR ONLY\n'+begin+contract+
            '\nCURRENT RESPONSE FEEDBACK\n'+json.dumps(view,ensure_ascii=False)+
            '\nCURRENT STEP: return ONLY <<<AUDIT_REPAIR>>> with checks containing the complete existing '+topics[0]+
            ' row. Repair only its required evidence string; preserve its topic/status and complete references array, all engineering judgments, numeric observations and interpretations. Do not invent or select new evidence, run tools or reopen engineering analysis. The complete original packet and validator still check the result. No host-supplied field values, automatic rename, new allowance or full report rewrite. Omit reserved host metadata.')


def audit(skill, sources, document=None, checks=None, reference='', packet_sha256='', baseline_sha256='', history=None, previous_audit=None, tool_limit=False, identity_check=None, report_repair_context=None, source_execution=None):
    prompt=skill+'\n'+reference+'\nTASK: independently audit the supplied frozen proposal; no edits. The host binds input, packet, baseline and response provenance. Submit engineering content only; packet_sha256, baseline_sha256 and repair-target hashes are reserved host metadata and must be omitted.\n'+context(sources)
    if document is not None:prompt+='\nDESIGN\n'+json.dumps(document,ensure_ascii=False)
    if checks is not None:prompt+='\nDETERMINISTIC CHECKS (limited scope)\n'+json.dumps(checks,ensure_ascii=False)
    if previous_audit:prompt+='\nPRIOR AUDIT (verify against new evidence)\n'+json.dumps(previous_audit,ensure_ascii=False)
    if identity_check is not None:prompt+='\nPROGRAM IDENTITY CHECK (distinct hash domains)\n'+json.dumps(identity_check,ensure_ascii=False)
    entries=history or []
    tools=[x for x in entries if x.get('kind')=='tool_result' or ('request' in x and 'result' in x)]
    repairs=[x for x in entries if x not in tools]
    prior_ids=[f['id'] for f in (previous_audit or {}).get('findings',[]) if f.get('severity')=='blocking' and f.get('disposition')=='open']
    prompt+='\nEXECUTED AUDIT TOOLS\n'+json.dumps([tool_view(entry) for entry in tools],ensure_ascii=False)
    valid=[];valid_checks=[];failed=[]
    for entry in tools:
        receipt=entry.get('result',{});verified=entry.get('executed_values')
        if verified is not None:
            valid.extend({'request_id':receipt['request_id'],'calculation_id':cid,'actual':verified['values'][cid]} for cid in verified.get('calculation_ids',[]))
            valid_checks.extend({'request_id':receipt['request_id'],'receipt_id':child['id'],'check_id':check['id'],'status':check['status']}
                for child in verified.get('subreceipts',[]) for check in child.get('checks',[]))
            if verified.get('errors'):failed.append({'request_id':receipt.get('request_id'),'errors':verified['errors']})
        if not receipt.get('ok'):failed.append({'request_id':receipt.get('request_id'),'errors':receipt.get('errors',[])})
    prompt+='\nVALID EXECUTED CALCULATION REFERENCES (copy exact ids and values): '+json.dumps(valid,ensure_ascii=False)
    prompt+='\nBINDING-VERIFIED EXECUTED CHECK REFERENCES (copy exact identities): '+json.dumps(valid_checks,ensure_ascii=False)+'. A nonnumeric failed check can be cited as {request_id,receipt_id,check_id}; review its actual check details and disposition without inventing a number. Check references are evidence, not numeric observations. observations[] still requires an actual calculation_id and matching numeric actual from the separate calculation list.'
    if failed:prompt+='\nFAILED DECLARATIONS/UNVERIFIED RECEIPTS: '+json.dumps(failed,ensure_ascii=False)+'. Only separately listed binding-verified executed values may be observed; failed rows supply no observations. Correct a declaration only when it affects the structural verdict. An unrelated failed declaration does not erase valid evidence; every binding-verified failed candidate check still needs an explicit disposition. Report insufficient_evidence when a decisive architecture question cannot be judged.'
    if 'audit-baseline.json' in sources:
        baseline=json.loads(sources['audit-baseline.json'])['baseline']
        prompt+='\nEXACT INDEPENDENT BASELINE IDS FOR requirements[].baseline_ids: '+json.dumps([x['id'] for x in baseline['obligations']])+'. Do not rename or invent these ids; match their semantics against original inputs.'
    prompt+='\nCURRENT RESPONSE FEEDBACK (not prior architecture findings)\n'+json.dumps(repairs,ensure_ascii=False)
    prompt+='\nEXACT PRIOR BLOCKING FINDING IDS FOR revision_checks: '+json.dumps(prior_ids)+'; empty means revision_checks=[]. Never use IDs from a rejected current report.'
    prompt+='\nUse model-selected original/proposal evidence and actual independently executed check_proposal observations. Judge whether the resource organization and major budget argument support RTL exploration without known critical contradictions. Exhaustive profile/operand receipts and implementation-level schedule proof are not acceptance prerequisites; explain estimates, assumptions and deferred verification honestly.'
    if 'design.json' in sources:
        proposal=json.loads(sources['design.json'])
        prompt+='\nEXACT EXTRACTED CONSTRAINT IDS TO MAP (also add independently found omissions): '+json.dumps([c['id'] for c in proposal.get('requirements',{}).get('constraints',[])])
        selected=next((c for c in proposal.get('candidates',[]) if c.get('id')==proposal.get('selected')), {})
        prompt+='\nACTUAL PROPOSAL IDENTIFIERS (inventory only, choose semantic correspondence yourself): '+json.dumps({'selected':proposal.get('selected'),'workload_profiles':proposal.get('work_model',{}).get('workloads',[]),'work_ids':[w['id'] for w in proposal.get('work_model',{}).get('items',[])],'cost_groups':[{'id':g['id'],'workload':g['workload'],'model':g['model']} for g in selected.get('cost_groups',[])],'windows':[{'id':w['id'],'workload':w['workload']} for w in selected.get('schedule_windows',[])]},ensure_ascii=False)
    if 'source-analysis.json' in sources:
        analysis=json.loads(sources['source-analysis.json'])
        verified=(source_execution or {}).get('verified_data',{})
        prompt+='\nACTUAL INDEPENDENT SOURCE PROFILES: '+json.dumps(verified.get('workloads',{}) if source_execution is not None else analysis.get('workloads',{}),ensure_ascii=False)+'. Profile roots may count elements or tasks differently; compare complete-task effects and explain boundary correspondence, not equal root numbers.'
        if source_execution is not None:
            summary={'coverage_complete':source_execution.get('coverage_complete'),
                     'successful_transactions':len(source_execution.get('subreceipts',[])),
                     'verification_errors':source_execution.get('errors',[])}
            prompt+='\nHOST-VERIFIED SOURCE TRANSACTIONS (availability only): '+json.dumps(summary,ensure_ascii=False)+'. These source transaction IDs are not audit observation references. Use check_proposal for an independently selected correspondence, or recompute with original-source facts, to obtain actual audit calculation references. The host supplies the unchanged full source record and its binding; the model does not provide source_declaration, execution_dependencies or receipt identity.'
        if analysis.get('ok') is not True or (source_execution is not None and source_execution.get('coverage_complete') is not True):
            prompt+='\nSOURCE QUANTITATIVE COVERAGE INCOMPLETE: accepted source citations/structure do not validate failed declarations. Accept and baseline-cache reuse are unavailable. Binding-verified successful source transactions may support a local check_proposal even while an unrelated source declaration failed; choose only corresponding available paths/profiles and preserve incomplete coverage. Re-read originals independently. A necessary proposal change supported by original/proposal evidence and successful local calculation may justify revise; otherwise report insufficient_evidence. Failed or unbound source values are unavailable, and source-model errors are not chip defects. Preserve failed preparation evidence; no constraint/design edits or additional allowance.'
    if report_repair_context is not None:
        prompt+='\nREPORT REPAIR: packet and calculations are frozen; the host binds the current report version. Do not submit a target hash or identity fields.'
        prompt+='\nUse Frozen evidence and local repairs in the supplied review-contract. This turn repairs references/format only; existing engineering judgments and calculations remain unchanged. The host supplies current diagnostics and the actual repair target.'
        prompt+='\nCURRENT STEP: return ONLY <<<AUDIT_REPAIR>>> followed by {reference_replacements:optional_array,checks:optional_complete_rows,requirements:optional_complete_rows,reference_updates:optional_existing_reference_updates}. Use the supplied review-contract for source-index or observation calculation-reference updates. Omit unused arrays; do not target the same row through multiple operations.'
    elif tool_limit:
        prompt+='\nCURRENT STEP: return ONLY <<<AUDIT_REPORT>>> followed by the complete evidenced review JSON. No tool requests remain; disclose unresolved evidence and do not invent results.'
    else:
        prompt+='\nCURRENT STEP: return ONLY <<<AUDIT_TOOL_REQUEST>>> followed by request JSON, or <<<AUDIT_REPORT>>> followed by the complete review JSON. Use actual tool results when provided.'
    prompt+='\nNo narration, Markdown fences or native tool tags. A format failure is not a design finding.'
    return prompt


def parse_design(text):
    markers=['<<<ARCHITECTURE_DATA>>>','<<<ARCHITECTURE_MD>>>']
    found=re.findall(r'<<<[A-Z_]+>>>',text)
    if found!=markers:
        raise ValueError('design must have exactly the data and unified Markdown markers in order')
    positions=[text.index(m) for m in markers]
    data=text[positions[0]+len(markers[0]):positions[1]].strip()
    result=parse_json(data)
    if set(result)!={'evaluated_design_sha256'} or not isinstance(result['evaluated_design_sha256'],str):
        raise ValueError('final data must contain only evaluated_design_sha256; design data belongs in the calculation request')
    result['architecture_md']=text[positions[1]+len(markers[1]):].strip()
    return result
