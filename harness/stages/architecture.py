"""Architecture controller: orchestrate calls/checks, revisions and termination."""
import json
from harness.adapters.omp import call_model
from harness.checks.architecture import delivery_decision
from harness.prompts import architecture as prompts
from harness.runtime import run_store as store
from harness.runtime.design_session import generate, execute_tool, DesignToolLimit, progress_signature
from harness.runtime import audit_session, requirement_session
from harness.runtime.call_budget import CallBudget, LimitReached



def execute(out, sources, models, root, settings, calls):
    design_skill=(out/'snapshot/harness/skills/microarchitecture-design/SKILL.md').read_text()
    budget_reference=(out/'snapshot/harness/skills/microarchitecture-design/references/budget-tools.md').read_text()
    def channel(purpose):
        def invoke(label,role,prompt,parser=None):
            return call_model(out,label,role,prompt,models,root,calls.timeout(purpose),settings['thinking'],parser=parser,before_call=lambda:calls.reserve(purpose),record_observed=lambda count:calls.record_observed(count,purpose))
        return invoke
    invoke=channel('design'); audit_invoke=channel('audit')
    if (out/'snapshot/audit/design.json').exists():return audit_session.diagnose(out,sources,audit_invoke,settings)
    prepared_baseline=audit_session.prepare_baseline(out,'source',sources,channel('audit_baseline'),settings['max_audit_baseline_repairs'])
    design_references='\n'.join((out/'snapshot/harness/skills/microarchitecture-design/references'/name).read_text() for name in ('storage-tools.md','execution-tools.md','numeric-tools.md','work-model.md'))
    feedback_path=out/'snapshot/feedback.json'
    feedback=json.loads(feedback_path.read_text()) if feedback_path.exists() else None
    requirements,errors=requirement_session.prepare(out,sources,invoke,settings['max_extraction_repairs'],revision_feedback=feedback)
    if requirements is None:return {'status':'needs_requirement_revision','errors':errors}
    seed=feedback.get('native_revision_seed') if isinstance(feedback,dict) else None
    document=seed['proposal'] if seed is not None else None
    findings=None; last_signature=None; stagnant=0
    for attempt in range(settings['max_design_revisions']+1):
        label=f'design-{attempt}'; path=out/(label+'.json')
        if path.exists():
            document=json.loads(path.read_text())
        else:
            document=generate(out,label,prompts.design(design_skill+'\n'+budget_reference+'\n'+design_references,sources,requirements,document,findings,feedback),requirements,sources,invoke,settings['max_design_tool_calls'],max_format_repairs=settings.get('max_design_format_repairs',2),revision_base=document)
            if document.get('requirements',requirements)!=requirements:
                return {'status':'needs_requirement_revision','errors':['design attempted to silently replace frozen requirement extraction']}
            document['requirements']=requirements
            store.dump(path,document)
        proposed=[c for c in document.get('requirement_corrections',[]) if isinstance(c,dict) and (c.get('requires_change') is True or c.get('corrected_requirement') is not None)]
        if proposed:
            store.save_draft(out,label,document)
            return {'status':'needs_requirement_revision','corrections':proposed,
                    'reason':'explicit correction required; original input unchanged; no automatic constraint replacement'}
        signature=progress_signature(document)
        stagnant=stagnant+1 if signature==last_signature else 0
        last_signature=signature
        try:
            checks=execute_tool(out,label+'.final-gate',document,sources,require_document=True)
        except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError,OverflowError) as e:
            checks={'ok':False,'errors':[type(e).__name__+': '+str(e)]}
        store.dump(out/(label+'.budget.json'),checks)
        store.save_draft(out,label,document,checks)
        # Unusable data cannot support meaningful model audit. Budget overrun is
        # valid data and IS audited for bottlenecks, but cannot be released.
        if not checks['ok']:
            findings={'deterministic':checks,'decision':{'deliverable':False}}
        else:
            review,report_check=audit_session.run(out,f'review-{attempt}',sources,document,checks,audit_invoke,settings['max_audit_tool_calls'],(findings or {}).get('audit'),max_baseline_repairs=settings['max_audit_baseline_repairs'],max_report_repairs=settings.get('max_audit_report_repairs',3),prepared_baseline=prepared_baseline)
            decision=delivery_decision(document,checks,review,report_check)
            store.dump(out/f'decision-{attempt}.json',decision)
            if review['verdict']=='insufficient_evidence':
                return {'status':'stopped_audit_evidence','audit':review,'report_check':report_check,'reason':'audit did not establish an actionable architecture revision; draft preserved'}
            findings={'deterministic':checks,'audit':review,'report_check':report_check,'decision':decision}
            if decision['deliverable']:
                for name,data in [('requirements',requirements),('design',document),('budgets',checks),('review',review),('decision',decision)]:
                    store.dump(out/(name+'.json'),data)
                (out/'architecture.md').write_text(store.architecture_text(document,checks))
                selected=checks['candidates'][document['selected']]['checks']
                return {'status':'accepted_conditional_design','selected':document['selected'],'estimate_checks':selected,
                        'unmet_targets':[c for c in selected if c.get('kind')=='target' and c.get('estimate_pass') is False],
                        'physical_timing':'unverified','numeric_execution':'unverified','cross_project_model_run':'not_claimed'}
        store.dump(out/f'feedback-{attempt}.json',findings)
        if stagnant >= settings['max_stagnant_revisions']:
            return {'status':'stopped_no_progress','findings':findings,'criterion':'same numeric/resource identity despite revisions'}
    # Keep last drafts under versioned names; no canonical accepted documents.
    return {'status':'stopped_revision_limit','findings':findings,'reason':'search did not establish a deliverable design; not proof of impossibility'}


def run(args):
    root,models,settings,sources,files,hashes,input_check=store.load(args)
    required=('max_audit_baseline_repairs','max_audit_tool_calls','max_design_tool_calls','max_design_revisions','max_extraction_repairs','max_model_calls','max_stagnant_revisions','request_timeout_seconds','total_active_seconds','max_design_format_repairs','max_audit_report_repairs','reserved_audit_calls','reserved_audit_seconds')
    if any(type(settings.get(k)) is not int or settings[k]<0 for k in required) or settings['request_timeout_seconds']<=5 or settings['max_stagnant_revisions']<1 or settings['max_design_tool_calls']<1 or settings['max_audit_tool_calls']<1:
        raise ValueError('invalid architecture execution limits')
    if settings['reserved_audit_calls']>=settings['max_model_calls'] or settings['reserved_audit_seconds']>=settings['total_active_seconds']:
        raise ValueError('audit reservation must fit inside total execution budget')
    if args.validate_inputs:
        print(json.dumps(input_check,ensure_ascii=False,indent=2));return 0
    out,lock=store.open_run(args,root,models,settings,files,hashes,input_check)
    calls=CallBudget(out,settings)
    state={'status':'running'};store.dump(out/'status.json',state)
    try:
        state=execute(out,sources,models,root,settings,calls)
    except audit_session.AuditReportRejected as e:
        state={'status':'needs_audit_revision','reason':str(e),'recovery':'current proposal is not changed by report-format repair'}
    except audit_session.AuditSessionLimit as e:
        state={'status':'stopped_audit_response_limit','reason':str(e),'recovery':'audit checkpoints preserved; new run needed to change limits'}
    except DesignToolLimit as e:
        state={'status':'stopped_design_tool_limit','reason':str(e),'recovery':'completed requests/results preserved; new run needed to increase limits'}
    except LimitReached as e:
        state={'status':'stopped_execution_limit','reason':str(e)}
    except Exception as e:
        state={'status':'infrastructure_or_format_failure','error':type(e).__name__+': '+str(e),
               'recovery':'resolve cause, then --resume '+out.name+' with identical inputs/code/config/feedback'}
    finally:
        calls.save();store.close_run(out,lock,state)
    print(json.dumps({'output':str(out),**state},ensure_ascii=False,indent=2))
    return 0 if state['status'] in ('accepted_conditional_design','audit_completed') else 1
