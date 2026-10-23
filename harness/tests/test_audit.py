import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock
from test_architecture import fixture
from harness.runtime import audit_session
from harness.runtime.skill_tools import execute_script
from harness.checks.architecture import delivery_decision

BASE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('audit_validator',BASE/'skills/microarchitecture-audit/scripts/validate_review.py')
validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
math_spec=importlib.util.spec_from_file_location('math_engine',BASE/'skills/microarchitecture-design/scripts/evaluate.py')
engine=importlib.util.module_from_spec(math_spec);math_spec.loader.exec_module(engine)


def baseline_fixture(sources):
    refs=[{'file':n,'quote':t.strip()} for n,t in sources.items() if n in ('spec.md','reference.py')]
    analysis={'facts':[], 'workloads':[{'id':'primary','quantity':12,'unit':'samples','basis':'isolated protocol workload','references':refs}],
              'computations':[{'id':'scalar_work','semantics':'synthetic scalar effects for mechanical protocol tests',
                 'steps':[{'id':'effects','kind':'elementwise','shape':[80],'ops_per_element':1}],
                 'scope':{'kind':'task','axis':'run'},'basis':'isolated fixture, not actual source completeness','references':refs}],
              'states':[],'flows':[],'limits':'Mechanical fixture only; no substantive chip audit claims.'}
    return {'scope':'synthetic source-only baseline for protocol tests','obligations':[{'id':'independent-work','category':'computation','requirement':'synthetic source obligation','references':refs}],'analysis':analysis,'ambiguities':[],'limits':'mechanical protocol fixture, not proof of semantic completeness'}


def baseline_record(sources):
    b=baseline_fixture(sources)
    spec=importlib.util.spec_from_file_location('test_source_analysis',BASE/'skills/microarchitecture-audit/scripts/source_model.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    result=module.analyze(b['analysis'],sources,engine)
    assert result['ok'],result['errors']
    request={'mode':'source','model':b['analysis'],'sources':sources}
    record={'baseline':b,'baseline_sha256':audit_session.report_identity(b),'analysis_result':result,
            'analysis_request_sha256':audit_session.report_identity(request),'analysis_result_sha256':audit_session.report_identity(result),
            'audit_script_bundle_sha256':audit_session.script_bundle_hash(BASE/'skills/microarchitecture-audit/scripts'),
            'calculator_bundle_sha256':audit_session.script_bundle_hash(BASE/'skills/microarchitecture-design/scripts')}
    record['source_execution']=validator.executed_source_values(result,sources,b['analysis'],
        {'checker_bundle_sha256':record['audit_script_bundle_sha256'],'calculator_bundle_sha256':record['calculator_bundle_sha256']})
    assert record['source_execution']['coverage_complete'],record['source_execution']['errors']
    return record


def packet(sources,document,budget,previous_audit=None):
    return audit_session.packet(sources,document,budget,previous_audit,baseline_record(sources))


def arithmetic_request():
    return {'tool':'recompute','facts':[],'assumptions':[],'calculations':[{'id':'reconstructed_bits','formula':'2*16*8','unit':'bits','basis':'independent synthetic instance inventory'}]}


def proposal_request():
    return {'tool':'check_proposal','model':{'candidate':'small','workload_map':{'primary':'primary'},
       'count_checks':[{'id':'direct_work','workload':'primary','source_paths':['scalar_work'],'candidate_work_ids':['arithmetic'],
           'realization':'direct','basis':'isolated fixture correspondence','references':[{'file':'spec.md','quote':'items 12'}]}],
       'flow_bindings':[],'service_windows':[{'id':'external','workload':'primary','intervals':[{'start':0,'end':'task_cycles'}],
          'resource':{'kind':'external_requests','capacity_fact':'unspecified_external_rate'},'demands':[{'traffic_id':'reads'}],
          'basis':'mechanical fixture external service capability absent from sources','references':[{'file':'spec.md','quote':'items 12'},{'file':'design.json','quote':'"id": "small"'}]}],
       'operand_checks':[{'window_id':'load','cost_group':'compute','issues':[{'cycle':1,'lanes':[0],'operands':['weight']}],'basis':'isolated scalar-word protocol fixture'}]}}


def executed_proposal_check(context):
    spec=importlib.util.spec_from_file_location('test_candidate_check',BASE/'skills/microarchitecture-audit/scripts/candidate_checks.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    result=module.check(proposal_request()['model'],context['source_analysis'],context['document'],context['budget'],context['sources'],engine,
        source_declaration=context['baseline']['baseline']['analysis'],execution_dependencies=context['execution_dependencies'])
    assert result['ok'],result['errors']
    result['request_id']='audit.proposal-check'
    return result


def report_fixture(context):
    input_ref={'file':'spec.md','quote':'area 3'}
    design_ref={'file':'design.json','quote':'"id": "small"'}
    refs=[input_ref,design_ref]
    result=context['calculations'][0]
    name='reconstructed_bits' if 'reconstructed_bits' in result['values'] else next(cid for cid in result['calculation_ids'] if cid.endswith('direct_work__source'))
    obs={'request_id':result['request_id'],'calculation_id':name,'actual':result['values'][name],'interpretation':'synthetic executed quantity correspondence'}
    return {'packet_sha256':context['packet_sha256'],'baseline_sha256':context['baseline']['baseline_sha256'],'scope':'synthetic declaration test; semantic/physical correctness is not asserted','verdict':'accept',
        'requirements':[{'baseline_ids':['independent-work'],'id':c['id'],'requirement':c['id'],'status':'covered','explanation':'synthetic mapping','references':copy.deepcopy(refs)} for c in context['document']['requirements']['constraints']],
        'checks':[{'topic':t,'status':'conditional','evidence':'synthetic evidence with implementation obligations','references':copy.deepcopy(refs)} for t in sorted(validator.TOPICS)],
        'observations':[obs],'findings':[],'unverified':[{'kind':'implementation_evidence','topic':'numerics','obligation':'execution remains downstream','reason':'only declared design available','references':copy.deepcopy(refs)},
           {'kind':'implementation_evidence','topic':'resource_service','obligation':'verify unspecified synthetic external service','reason':'isolated source fixture provides no external capacity','references':copy.deepcopy(refs)}],'revision_checks':[]}


def report_content_fixture(context):
    report=report_fixture(context)
    for key in ('packet_sha256','baseline_sha256'):report.pop(key)
    return report


def wire_reply(reply,parser):
    if isinstance(reply,dict) and set(reply)=={'final'}:marker='<<<AUDIT_REPORT>>>';content=reply['final']
    elif isinstance(reply,dict) and set(reply)=={'tool_request'}:marker='<<<AUDIT_TOOL_REQUEST>>>';content=reply['tool_request']
    elif isinstance(reply,dict) and set(reply)=={'report_repair'}:marker='<<<AUDIT_REPAIR>>>';content=reply['report_repair']
    elif isinstance(reply,dict) and set(reply)=={'baseline_repair'}:marker='<<<BASELINE_REPAIR>>>';content=reply['baseline_repair']
    elif isinstance(reply,dict) and set(reply)=={'analysis_repair'}:marker='<<<BASELINE_ANALYSIS_REPAIR>>>';content=reply['analysis_repair']
    else:marker='<<<AUDIT_BASELINE>>>';content=reply
    return parser(marker+'\n'+json.dumps(content,ensure_ascii=False))


def wire_provider(callback=None,return_value=None,side_effect=None):
    inner=Mock(side_effect=callback if callback is not None else side_effect,return_value=return_value)
    def invoke(name,role,prompt,parser):return wire_reply(inner(name,role,prompt,parser),parser)
    return Mock(side_effect=invoke)


def freeze_snapshot(out,sources):
    for name in ('spec.md','reference.py'):
        path=out/'snapshot/input'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(sources[name])
    hashes={str(p.relative_to(out/'snapshot')):audit_session.digest(p.read_bytes()) for p in (out/'snapshot').rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc' and p.name!='.DS_Store'}
    (out/'manifest.json').write_text(json.dumps({'sha256':hashes},sort_keys=True))


def context_fixture():
    d,s=fixture();context=packet(s,d,engine.evaluate(d,s,True))
    result=validator.recompute(arithmetic_request(),context['sources'],engine)
    result['request_id']='audit.step-0';context['calculations']=[result];context['proposal_checks']=[executed_proposal_check(context)]
    return context


def snapshot(sources=None):
    out=Path(tempfile.mkdtemp(prefix='audit-session-test-'))
    for name in ('microarchitecture-design','microarchitecture-audit'):
        shutil.copytree(BASE/'skills'/name,out/'snapshot/harness/skills'/name,ignore=shutil.ignore_patterns('__pycache__','.DS_Store'))
    sources=fixture()[1] if sources is None else sources;freeze_snapshot(out,sources)
    audit_session.prepare_baseline(out,'fixture-source',sources,wire_provider(return_value=baseline_fixture(sources)),0)
    return out


class AuditEvidenceTests(unittest.TestCase):
    def test_proposal_span_is_bound_once_and_receipt_id_is_reusable(self):
        ctx=context_fixture();request=proposal_request()['model']
        request['count_checks'][0]['references']=[{'file':'spec.md','start':1,'end':1}]
        request['service_windows'][0]['references']=[{'file':'spec.md','start':1,'end':1},{'file':'design.json','start':1,'end':3}]
        spec=importlib.util.spec_from_file_location('span_candidate_check',BASE/'skills/microarchitecture-audit/scripts/candidate_checks.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        before=copy.deepcopy(request)
        result=module.check(request,ctx['source_analysis'],ctx['document'],ctx['budget'],ctx['sources'],engine,ctx['citations'],
            source_declaration=ctx['baseline']['baseline']['analysis'],execution_dependencies=ctx['execution_dependencies'])
        self.assertTrue(result['ok'],result['errors']);self.assertEqual(request,before)
        ref=next(x['resolved'] for x in result['citation_bindings'] if 'resolved' in x)
        request['count_checks'][0]['references']=[{'evidence_id':ref['evidence_id']}]
        result=module.check(request,ctx['source_analysis'],ctx['document'],ctx['budget'],ctx['sources'],engine,[ref],
            source_declaration=ctx['baseline']['baseline']['analysis'],execution_dependencies=ctx['execution_dependencies'])
        self.assertTrue(result['ok'],result['errors'])

    def test_whole_task_effects_can_match_with_different_root_quantity(self):
        ctx=context_fixture();model=ctx['baseline']['baseline']['analysis']
        model['workloads'][0]['quantity']=1;model['workloads'][0]['unit']='tasks'
        spec=importlib.util.spec_from_file_location('task_source',BASE/'skills/microarchitecture-audit/scripts/source_model.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        result=module.analyze(model,{k:v for k,v in ctx['sources'].items() if k in ('spec.md','reference.py')},engine)
        source_view=module.source_view(result,model,ctx['sources'],engine,ctx['execution_dependencies'])
        comparison=module.compare_work(proposal_request()['model'],source_view,ctx['document'],ctx['sources'],engine)
        self.assertTrue(comparison['ok'],comparison['errors']);obs=comparison['observations'][0]
        self.assertEqual(obs['source_root_quantity'],1);self.assertEqual(obs['candidate_root_quantity'],12)
        self.assertTrue(obs['matches']);self.assertEqual(obs['actual'],80)

    def test_mixed_primitive_fusion_remains_unverified_not_arithmetic_failure(self):
        ctx=context_fixture();model=ctx['baseline']['baseline']['analysis']
        extra=copy.deepcopy(model['computations'][0]);extra['id']='matrix-part'
        extra['steps']=[{'id':'products','kind':'matmul','lhs':[2,3],'rhs':[3]}]
        model['computations'].append(extra)
        spec=importlib.util.spec_from_file_location('fusion_source',BASE/'skills/microarchitecture-audit/scripts/source_model.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        baseline=module.analyze(model,{k:v for k,v in ctx['sources'].items() if k in ('spec.md','reference.py')},engine)
        baseline=module.source_view(baseline,model,ctx['sources'],engine,ctx['execution_dependencies'])
        request=proposal_request()['model'];row=request['count_checks'][0]
        row.update(source_paths=['scalar_work','matrix-part'],realization='transformed',equivalence_evidence='Synthetic correspondence protocol only; semantic equivalence remains independently unproved.')
        result=module.compare_work(request,baseline,ctx['document'],ctx['sources'],engine)
        self.assertTrue(result['ok'],result['errors']);self.assertIsNone(result['observations'][0]['matches'])
        self.assertEqual(result['observations'][0]['source_by_unit'],{'ops':80,'MAC':6})
        row['realization']='direct';self.assertFalse(module.compare_work(request,baseline,ctx['document'],ctx['sources'],engine)['ok'])

    def test_arbitrary_formula_and_absent_source_witness_cannot_release(self):
        ctx=context_fixture();report=report_fixture(ctx)
        ctx['proposal_checks']=[]
        self.assertFalse(validator.validate(report,ctx)['ok'])
        ctx=context_fixture();ctx['source_analysis']['model_sha256']='unbound'
        self.assertFalse(validator.validate(report_fixture(ctx),ctx)['ok'])

    def test_executed_contradiction_blocks_but_missing_service_receipt_does_not(self):
        ctx=context_fixture();report=report_fixture(ctx)
        ctx['proposal_checks'][0]['checks'][0]['status']='fail'
        self.assertFalse(validator.validate(report,ctx)['ok'])
        ctx=context_fixture();ctx['proposal_checks'][0]['missing_service_declarations']=['reads']
        self.assertTrue(validator.validate(report_fixture(ctx),ctx)['ok'])

    def test_partial_operand_coverage_can_support_a_conditional_draft(self):
        ctx=context_fixture();model=proposal_request()['model'];model['operand_checks']=[]
        path=BASE/'skills/microarchitecture-audit/scripts/candidate_checks.py'
        spec=importlib.util.spec_from_file_location('partial_draft_check',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        result=module.check(model,ctx['source_analysis'],ctx['document'],ctx['budget'],ctx['sources'],engine,
            source_declaration=ctx['baseline']['baseline']['analysis'],execution_dependencies=ctx['execution_dependencies'])
        result['request_id']='partial-draft';ctx['calculations']=[result];ctx['proposal_checks']=[result]
        self.assertTrue(result['ok'],result['errors']);self.assertFalse(result['coverage_complete'])
        self.assertTrue(result['missing_operand_checks'])
        checked=validator.validate(report_fixture(ctx),ctx)
        self.assertTrue(checked['ok'],checked['errors'])

    def test_valid_conditional_report_and_quote_binding(self):
        ctx=context_fixture();result=validator.validate(report_fixture(ctx),ctx)
        self.assertTrue(result['ok'],result['errors']);self.assertTrue(result['bindings'])

    def test_service_disclosure_diagnostic_identifies_only_the_missing_field(self):
        ctx=context_fixture()
        for target in ('check_status','unverified_topic','unverified_kind'):
            with self.subTest(target=target):
                report=report_fixture(ctx)
                check=next(x for x in report['checks'] if x['topic']=='resource_service')
                condition=next(x for x in report['unverified'] if x['topic']=='resource_service')
                if target=='check_status':check['status']='pass'
                elif target=='unverified_topic':condition['topic']='SRAM port and latency conditions'
                else:condition['kind']='audit_evidence'
                before=copy.deepcopy(report);result=validator.validate(report,ctx)
                self.assertFalse(result['ok']);self.assertEqual(report,before)
                errors=[e for e in result['errors'] if e.startswith('accept must disclose unverified executed service capability')]
                self.assertEqual(len(errors),1)
                field='checks[topic="resource_service"].status' if target=='check_status' else 'unverified[*]'
                self.assertIn(field,errors[0])
                if target!='check_status':
                    self.assertIn('topic="resource_service"',errors[0])
                    self.assertIn('obligation/reason',errors[0])
                check['status']='conditional';condition.update(topic='resource_service',kind='implementation_evidence')
                result=validator.validate(report,ctx)
                self.assertTrue(result['ok'],result['errors'])

    def test_unique_excerpt_relocation_does_not_rewrite_report(self):
        ctx=context_fixture();report=report_fixture(ctx)
        report['checks'][0]['references'][0]['start']=999
        old=copy.deepcopy(report);checked=validator.validate(report,ctx)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(report,old)
        self.assertTrue(any(b.get('original_start')==999 for b in checked['bindings']))
        report['checks'][0]['references'][0]['quote']='Area 3'
        self.assertFalse(validator.validate(report,ctx)['ok'])

    def test_missing_reasoning_diagnostic_identifies_exact_field(self):
        ctx=context_fixture();report=report_fixture(ctx);report['checks'][0].pop('evidence')
        checked=validator.validate(report,ctx)
        self.assertFalse(checked['ok']);self.assertTrue(any('.evidence must be' in e for e in checked['errors']))

    def test_bad_quote_identity_forged_math_and_missing_coverage_rejected(self):
        ctx=context_fixture()
        for mutate in [lambda r:r.update(packet_sha256='old'),lambda r:r['checks'][0]['references'][0].update(quote='not in any input'),lambda r:r['observations'][0].update(actual=257),lambda r:r['requirements'].pop(),lambda r:r.update(observations=[])]:
            report=report_fixture(ctx);mutate(report);self.assertFalse(validator.validate(report,ctx)['ok'])

    def test_condition_cannot_defer_hard_budget_or_hide_failure(self):
        ctx=context_fixture();report=report_fixture(ctx);report['requirements'][0]['status']='deferred'
        self.assertFalse(validator.validate(report,ctx)['ok'])
        report=report_fixture(ctx);report['checks'][0]['status']='fail'
        self.assertFalse(validator.validate(report,ctx)['ok'])

    def test_prior_issue_reaudited_with_actual_evidence(self):
        ctx=context_fixture();ctx['previous_audit']={'findings':[{'id':'prior-port','severity':'blocking','disposition':'open'}]}
        report=report_fixture(ctx);self.assertFalse(validator.validate(report,ctx)['ok'])
        report['revision_checks']=[{'finding_id':'prior-port','status':'resolved','evidence':'new supply definition','references':report['checks'][0]['references']}]
        self.assertTrue(validator.validate(report,ctx)['ok'])
        report['revision_checks'][0]['status']='open';self.assertFalse(validator.validate(report,ctx)['ok'])

    def test_independent_math_mismatch_is_evidence_not_execution_failure(self):
        req=arithmetic_request();req['calculations'][0]['expected']='128'
        report=validator.recompute(req,{},engine)
        self.assertTrue(report['ok']);self.assertFalse(report['comparisons']['reconstructed_bits']['matches_expected'])
        req['calculations'][0]['formula']='__import__("os")';self.assertFalse(validator.recompute(req,{},engine)['ok'])
        req=arithmetic_request();req['facts']=[{'id':'invented','value':4,'unit':'units','citations':[{'file':'spec.md','start':1,'quote':'area 3'}]}]
        self.assertFalse(validator.recompute(req,{'spec.md':'area 3'},engine)['ok'])

    def test_observation_cannot_use_fact_as_reconstruction(self):
        ctx=context_fixture();ctx['calculations'][0]['values']['literal']=256
        report=report_fixture(ctx);report['observations'][0]['calculation_id']='literal'
        self.assertFalse(validator.validate(report,ctx)['ok'])

    def test_lookup_receipt_references_and_fact_citations(self):
        ctx=context_fixture();result=validator.lookup({'tool':'lookup_source','queries':[{'file':'spec.md','query':'area 3'},{'file':'design.json','query':'"id": "small"'}]},ctx['sources'])
        self.assertTrue(result['ok']);ctx['citations']=result['evidence']
        report=report_fixture(ctx)
        report['checks'][0]['references']=[{'evidence_id':x['evidence_id']} for x in result['evidence']]
        self.assertTrue(validator.validate(report,ctx)['ok'])
        req=arithmetic_request();req['facts']=[{'id':'raw_area','source':{'file':'spec.md','line':1,'number':1},'unit':'mm2'},
                                            {'id':'input_area','formula':'raw_area','unit':'mm2','citations':[{'evidence_id':result['evidence'][0]['evidence_id']}]}]
        original=copy.deepcopy(req);checked=validator.recompute(req,ctx['sources'],engine,ctx['citations'])
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(checked['values']['input_area'],3)
        self.assertEqual(req,original)
        self.assertEqual(checked['citation_bindings'][0]['resolved']['evidence_id'],result['evidence'][0]['evidence_id'])
        bad=copy.deepcopy(req);bad['facts'][1]['citations'][0]['evidence_id']='forged'
        self.assertFalse(validator.recompute(bad,ctx['sources'],engine,ctx['citations'])['ok'])
        report['checks'][0]['references'][0]['evidence_id']='forged'
        self.assertFalse(validator.validate(report,ctx)['ok'])
        self.assertFalse(validator.lookup({'tool':'lookup_source','queries':[{'file':'/etc/passwd','query':'root'}]},ctx['sources'])['ok'])

    def test_budget_quote_requires_matching_proposal_fingerprint(self):
        ctx=context_fixture();report=report_fixture(ctx)
        report['checks'][0]['references'][1]={'file':'budget.json','quote':'"macro_inventory_bits": 256'}
        # Duplicate per-candidate value requires an actual bound range.
        source=ctx['sources']['budget.json'];pos=source.index('"macro_inventory_bits": 256');line=source[:pos].count('\n')+1
        report['checks'][0]['references'][1].update(start=line,end=line)
        self.assertTrue(validator.validate(report,ctx)['ok'])
        ctx['budget']['design_sha256']='other'
        self.assertFalse(validator.validate(report,ctx)['ok'])

    def test_report_check_is_required_for_release(self):
        ctx=context_fixture();review=report_fixture(ctx)
        self.assertFalse(delivery_decision(ctx['document'],ctx['budget'],review)['deliverable'])
        checked=validator.validate(review,ctx)
        self.assertTrue(delivery_decision(ctx['document'],ctx['budget'],review,checked)['deliverable'])


class AuditSessionTests(unittest.TestCase):
    def test_baseline_coverage_error_requires_new_report_not_reference_patch(self):
        for mutate in (lambda r:r['requirements'][0].update(baseline_ids=[]),):
            d,s=fixture();out=snapshot();budget=engine.evaluate(d,s,True)
            def invoke(name,role,prompt,parser):
                if name.endswith('step-0'):return {'tool_request':proposal_request()}
                receipt=json.loads((out/'resubmit.step-0.tool.result.json').read_text())
                receipt['request_id']='resubmit.step-0';ctx=packet(s,d,budget)
                ctx['calculations']=[receipt];ctx['proposal_checks']=[receipt]
                report=report_content_fixture(ctx)
                if name.endswith('step-1'):
                    mutate(report)
                    for row in report['requirements']:row['baseline_ids']=[]
                else:
                    self.assertIn('audit_evidence_reopen',prompt)
                    self.assertNotIn('CURRENT STEP: return ONLY <<<AUDIT_REPAIR>>>',prompt)
                return {'final':report}
            called=wire_provider(callback=invoke)
            report,check=audit_session.run(out,'resubmit',s,d,budget,called,1)
            self.assertTrue(check['ok'],check['errors']);self.assertEqual(called.call_count,3)
            self.assertTrue((out/'resubmit.step-1.report-check.result.json').exists())
            self.assertEqual(len(list(out.glob('*.tool.execution.json'))),1)

    def test_extracted_constraint_mapping_gap_reopens_same_packet_without_reference_repair(self):
        d,s=fixture();original=copy.deepcopy(d);out=snapshot();budget=engine.evaluate(d,s,True)
        self.addCleanup(shutil.rmtree,out)
        def invoke(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':proposal_request()}
            receipt=json.loads((out/'mapping.step-0.tool.result.json').read_text())
            receipt['request_id']='mapping.step-0';ctx=packet(s,d,budget)
            ctx['calculations']=[receipt];ctx['proposal_checks']=[receipt]
            report=report_content_fixture(ctx)
            if name.endswith('step-1'):
                for row in report['requirements']:row['id']='independent_'+row['id']
            else:
                self.assertIn('missing extracted constraint mappings:',prompt)
                self.assertIn('audit_evidence_reopen',prompt)
                self.assertNotIn('CURRENT STEP: return ONLY <<<AUDIT_REPAIR>>>',prompt)
            return {'final':report}
        called=wire_provider(callback=invoke)
        report,checked=audit_session.run(out,'mapping',s,d,budget,called,1,max_report_repairs=0)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(called.call_count,3)
        rejected=json.loads((out/'mapping.step-1.canonical.json').read_text())
        failure=json.loads((out/'mapping.step-1.report-check.result.json').read_text())
        self.assertEqual(failure['errors'],['missing extracted constraint mappings: area,capacity,throughput'])
        self.assertEqual(failure['coverage']['missing_baseline_ids'],[])
        self.assertTrue(all(row['id'].startswith('independent_') for row in rejected['requirements']))
        self.assertEqual([row['baseline_ids'] for row in rejected['requirements']],
                         [row['baseline_ids'] for row in report['requirements']])
        before=json.loads((out/'mapping.step-1.report-check.request.json').read_text())['context']
        after=json.loads((out/'mapping.step-2.report-check.request.json').read_text())['context']
        for key in ('packet_sha256','calculations','proposal_checks'):
            self.assertEqual(before[key],after[key])
        frame=json.loads((out/'mapping.step-2.binding.json').read_text())['context']
        self.assertIsNone(frame['target_sha256']);self.assertEqual(frame['packet_sha256'],report['packet_sha256'])
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),1)
        self.assertEqual(d,original)
        preserved=copy.deepcopy(rejected)
        with self.assertRaisesRegex(ValueError,'unique existing id'):
            audit_session.repair_report(rejected,{'requirements':report['requirements']},before['citations'],before['sources'])
        self.assertEqual(rejected,preserved)

    def test_unknown_service_disclosure_reopens_judgment_without_extra_tools(self):
        d,s=fixture();out=snapshot();budget=engine.evaluate(d,s,True)
        def invoke(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':proposal_request()}
            receipt=json.loads((out/'condition.step-0.tool.result.json').read_text())
            receipt['request_id']='condition.step-0';ctx=packet(s,d,budget)
            ctx['calculations']=[receipt];ctx['proposal_checks']=[receipt]
            report=report_content_fixture(ctx)
            if name.endswith('step-1'):
                next(x for x in report['checks'] if x['topic']=='resource_service')['status']='pass'
                report['unverified']=[x for x in report['unverified'] if x['topic']!='resource_service']
            else:
                self.assertIn('audit_evidence_reopen',prompt)
                self.assertNotIn('CURRENT STEP: return ONLY <<<AUDIT_REPAIR>>>',prompt)
                self.assertIn('checks[topic=\\"resource_service\\"].status',prompt)
                self.assertIn('unverified[*]',prompt)
            return {'final':report}
        called=wire_provider(callback=invoke)
        report,check=audit_session.run(out,'condition',s,d,budget,called,1)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(called.call_count,3)
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),1)
        self.assertEqual(next(x for x in report['checks'] if x['topic']=='resource_service')['status'],'conditional')

    def test_descriptive_service_topic_can_be_corrected_without_new_tools_or_design_changes(self):
        d,s=fixture();out=snapshot();budget=engine.evaluate(d,s,True)
        original=copy.deepcopy(d)
        def invoke(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':proposal_request()}
            receipt=json.loads((out/'topic.step-0.tool.result.json').read_text())
            receipt['request_id']='topic.step-0';ctx=packet(s,d,budget)
            ctx['calculations']=[receipt];ctx['proposal_checks']=[receipt]
            report=report_content_fixture(ctx)
            if name.endswith('step-1'):
                next(x for x in report['unverified'] if x['topic']=='resource_service')['topic']='SRAM port and latency conditions'
            else:
                self.assertIn('audit_evidence_reopen',prompt)
                self.assertIn('unverified[*]',prompt)
                self.assertIn('obligation/reason',prompt)
            return {'final':report}
        called=wire_provider(callback=invoke)
        report,checked=audit_session.run(out,'topic',s,d,budget,called,1,max_report_repairs=0)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(called.call_count,3)
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),1);self.assertEqual(d,original)
        rejected=json.loads((out/'topic.step-1.canonical.json').read_text())
        next(x for x in rejected['unverified'] if x['topic']=='SRAM port and latency conditions')['topic']='resource_service'
        self.assertEqual(report,rejected)

    def test_late_evidence_reopen_can_report_within_original_response_cap(self):
        d,s=fixture();out=snapshot();budget=engine.evaluate(d,s,True)
        incomplete=arithmetic_request()
        current=None
        def invoke(name,role,prompt,parser):
            nonlocal current
            step=int(name.rsplit('-',1)[1])
            if step==0:return {'tool_request':incomplete}
            if step==5:return {'tool_request':proposal_request()}
            receipt=json.loads((out/'late.step-0.tool.result.json').read_text())
            receipt['request_id']='late.step-0';ctx=packet(s,d,budget);ctx['calculations']=[receipt]
            good=report_content_fixture(ctx)
            if step==1:
                current=copy.deepcopy(good)
                for row in current['checks'][:3]:row['evidence']=[]
                return {'final':copy.deepcopy(current)}
            if step in (2,3,4):
                repair={'checks':[good['checks'][step-2]]}
                current['checks'][step-2]=copy.deepcopy(good['checks'][step-2])
                return {'report_repair':repair}
            self.assertEqual(step,6);return {'final':good}
        called=wire_provider(callback=invoke)
        report,check=audit_session.run(out,'late',s,d,budget,called,2,max_report_repairs=3)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(called.call_count,7)
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),2)
        self.assertEqual(report['verdict'],'accept')

    def test_missing_accept_evidence_reopens_bounded_audit_not_reference_repair(self):
        d,s=fixture();out=snapshot();budget=engine.evaluate(d,s,True)
        incomplete=proposal_request();incomplete['model']['service_windows']=[]
        def invoke(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':incomplete}
            if name.endswith('step-2'):
                self.assertIn('audit_evidence_reopen',prompt);self.assertNotIn('CURRENT STEP: return ONLY <<<AUDIT_REPAIR>>>',prompt)
                return {'tool_request':proposal_request()}
            ctx=packet(s,d,budget);receipt=json.loads((out/'reopen.step-0.tool.result.json').read_text())
            receipt['request_id']='reopen.step-0';ctx['calculations']=[receipt]
            report=report_content_fixture(ctx)
            if name.endswith('step-1'):
                report['unverified'].append({'kind':'design_decision','topic':'aggregate_external_service_binding',
                    'obligation':'bind actual external transfers','reason':'source/candidate mapping not yet executed',
                    'references':copy.deepcopy(report['checks'][0]['references'])})
            return {'final':report}
        called=wire_provider(callback=invoke)
        report,check=audit_session.run(out,'reopen',s,d,budget,called,2)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(called.call_count,4)
        self.assertTrue((out/'reopen.step-1.report-check.result.json').exists())
        self.assertTrue((out/'reopen.step-2.tool.execution.json').exists())
        self.assertEqual(report['verdict'],'accept')

    def test_real_script_receipt_resume_and_no_proposal_mutation(self):
        d,s=fixture();original=copy.deepcopy(d);out=snapshot();budget=engine.evaluate(d,s,True)
        invoke=wire_provider(side_effect=[{'tool_request':proposal_request()},RuntimeError('provider interruption')])
        with self.assertRaises(RuntimeError):audit_session.run(out,'audit',s,d,budget,invoke,2)
        receipt=json.loads((out/'audit.step-0.tool.result.json').read_text());receipt['request_id']='audit.step-0'
        ctx=packet(s,d,budget);ctx['calculations']=[receipt];ctx['proposal_checks']=[receipt]
        invoke=wire_provider(return_value={'final':report_content_fixture(ctx)})
        review,checked=audit_session.run(out,'audit',s,d,budget,invoke,2)
        self.assertTrue(checked['ok']);self.assertEqual(invoke.call_count,1);self.assertEqual(d,original)
        self.assertIn('80',invoke.call_args.args[2]);self.assertTrue((out/'audit.step-0.tool.execution.json').exists())

    def test_real_lookup_session_retains_bound_evidence_ids(self):
        d,s=fixture();out=snapshot();budget=engine.evaluate(d,s,True)
        request={'tool':'lookup_source','queries':[{'file':'spec.md','query':'area 3'},{'file':'design.json','query':'"id": "small"'}]}
        def invoke(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':request}
            ctx=packet(s,d,budget)
            fake=context_fixture()['calculations'];ctx['calculations']=fake
            report=report_content_fixture(ctx);report['verdict']='insufficient_evidence';report['observations']=[]
            receipt=json.loads((out/'lookup.step-0.tool.result.json').read_text())
            refs=[{'evidence_id':x['evidence_id']} for x in receipt['evidence']]
            for check in report['checks']:check['references']=refs
            return {'final':report}
        result,checked=audit_session.run(out,'lookup',s,d,budget,wire_provider(callback=invoke),2)
        self.assertTrue(checked['ok']);self.assertTrue((out/'lookup.step-0.tool.execution.json').exists())

    def test_bad_final_feedback_repairs_without_new_math(self):
        d,s=fixture();out=snapshot();budget=engine.evaluate(d,s,True);ctx=packet(s,d,budget)
        result=executed_proposal_check(ctx);result['request_id']='audit.step-0';ctx['calculations']=[result];ctx['proposal_checks']=[result]
        good=report_content_fixture(ctx);bad=copy.deepcopy(good);bad['checks'][0]['evidence']=[]
        repair={'checks':[good['checks'][0]]}
        invoke=wire_provider(side_effect=[{'tool_request':proposal_request()},{'final':bad},{'report_repair':repair}])
        review,checked=audit_session.run(out,'audit',s,d,budget,invoke,2)
        self.assertTrue(checked['ok']);self.assertIn('.evidence must be',invoke.call_args.args[2])
        self.assertEqual(review['observations'],bad['observations']);self.assertEqual(review['verdict'],bad['verdict'])
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),1)

    def test_bad_observation_cannot_be_rewritten_through_report_repair(self):
        d,s=fixture();out=snapshot();budget=engine.evaluate(d,s,True);ctx=packet(s,d,budget)
        receipt=validator.recompute(arithmetic_request(),ctx['sources'],engine);receipt['request_id']='protected.step-0';ctx['calculations']=[receipt]
        report=report_content_fixture(ctx);report['observations'][0]['actual']=255
        repair={'observations':[dict(report['observations'][0],actual=256)]}
        invoke=wire_provider(side_effect=[{'tool_request':arithmetic_request()},{'final':report},{'report_repair':repair}])
        with self.assertRaisesRegex(audit_session.AuditSessionLimit,'report repair limit'):
            audit_session.run(out,'protected',s,d,budget,invoke,2,max_report_repairs=1)
        self.assertEqual(invoke.call_count,3)
        self.assertEqual(json.loads((out/'protected.step-1.json').read_text())['final']['observations'][0]['actual'],255)
        self.assertFalse((out/'protected.step-2.report-check.result.json').exists())

    def test_unique_marker_with_format_wrapper_and_ambiguous_suffix(self):
        raw='Narration\n```\n<<<AUDIT_TOOL_REQUEST>>>\n'+json.dumps(arithmetic_request())+'\n```'
        self.assertIn('tool_request',audit_session.parse_turn(raw))
        with self.assertRaises(ValueError):audit_session.parse_turn(raw+'\n'+json.dumps(arithmetic_request()))
        self.assertEqual(audit_session.parse_turn('<<<AUDIT_REPORT>>>\n{"verdict":"revise"}')['final']['verdict'],'revise')

    def test_bounded_failures_and_restricted_requests(self):
        d,s=fixture();out=snapshot()
        invoke=Mock(side_effect=lambda name,role,prompt,parser:parser('bad response'))
        with self.assertRaises(audit_session.AuditSessionLimit):audit_session.run(out,'audit',s,d,engine.evaluate(d,s,True),invoke,1)
        self.assertEqual(invoke.call_count,4)
        with self.assertRaises(ValueError):audit_session.parse_turn('<<<AUDIT_TOOL_REQUEST>>>\n'+json.dumps({'tool':'shell','command':'anything'}))
        with self.assertRaises(ValueError):audit_session.parse_turn('<<<AUDIT_TOOL_REQUEST>>>\n'+json.dumps({'tool':'recompute','path':'elsewhere'}))

    def test_standalone_validator_copied_without_harness(self):
        out=Path(tempfile.mkdtemp(prefix='audit-standalone-'));ctx=context_fixture()
        shutil.copy2(BASE/'skills/microarchitecture-audit/scripts/validate_review.py',out/'validator.py')
        import subprocess,sys
        result=subprocess.run([sys.executable,'-B',str(out/'validator.py')],input=json.dumps({'mode':'validate','review':report_fixture(ctx),'context':ctx}),capture_output=True,text=True,check=True,cwd=out)
        self.assertTrue(json.loads(result.stdout)['ok'])

if __name__=='__main__':unittest.main()
