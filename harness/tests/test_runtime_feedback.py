"""Feedback must separate failed declarations from valid negative evidence."""
import copy
import json
import unittest
from unittest.mock import Mock

import test_architecture as base
import test_audit as audit
from harness.runtime import design_session as design
from harness.runtime import audit_session


class ProgressSignatureTests(unittest.TestCase):
    def test_work_counts_quantities_status_and_dependencies_are_progress(self):
        document,_=base.fixture()
        changes=[lambda d:d['work_model']['workloads'][0].update(quantity=24),
                 lambda d:d['work_model']['items'][0]['count'].update(operations=81),
                 lambda d:d['work_model']['items'][0]['scopes'].update(primary={'kind':'element','axis':'sample','multiplicity':2}),
                 lambda d:d['work_model']['items'][0].update(status='unresolved'),
                 lambda d:d['work_model']['items'][0].update(dependencies=['lifecycle'])]
        for change in changes:
            revised=copy.deepcopy(document);change(revised)
            self.assertNotEqual(design.progress_signature(document),design.progress_signature(revised))

    def test_work_wording_and_citation_placement_are_not_machine_progress(self):
        document,_=base.fixture();revised=copy.deepcopy(document)
        row=revised['work_model']['items'][0]
        row.update(semantics='same task, clearer words',condition='same condition, clearer words',state_lifetime='same lifetime, clearer words')
        row['references'][0].update(start=900,end=901)
        revised['work_model']['workloads'][0]['basis']='clearer workload explanation'
        self.assertEqual(design.progress_signature(document),design.progress_signature(revised))

    def test_resource_precision_and_schedule_links_remain_sensitive(self):
        document,_=base.fixture()
        for change in [lambda d:d['candidates'][0]['macros'][0].update(width='16'),
                       lambda d:d['candidates'][0]['assumptions'][0].update(unit='logic_precision_bits'),
                       lambda d:d['candidates'][0]['workloads'][0]['components'][0].update(references=['compute'])]:
            revised=copy.deepcopy(document);change(revised)
            self.assertNotEqual(design.progress_signature(document),design.progress_signature(revised))


class CalculationFeedbackTests(unittest.TestCase):
    def test_json_syntax_and_protocol_schema_are_distinct(self):
        syntax=design.protocol_feedback(json.JSONDecodeError('Expecting comma','{}',1))
        schema=design.protocol_feedback(ValueError('evaluate_design requires one document'))
        self.assertEqual(syntax['kind'],'json_syntax');self.assertEqual(schema['kind'],'protocol_structure')
        self.assertIn('transport only',syntax['scope'])

    def test_real_service_contradiction_does_not_mean_global_infeasibility(self):
        document,sources=base.fixture();document['candidates'][0]['resource_checks'][0]['demand_per_cycle']='64'
        report=audit.engine.evaluate(document,sources);before=copy.deepcopy(report)
        result=design.calculation_feedback(report)
        issue=next(x for x in result['issues'] if x['kind']=='declared_resource_contradiction')
        self.assertIn('declared demand exceeds service',issue['diagnostics'][0])
        self.assertIn('not all possible architectures',issue['action']);self.assertEqual(report,before)

    def test_undefined_resource_expression_is_a_declaration_error(self):
        document,sources=base.fixture();document['candidates'][0]['resource_checks'][0]['demand_per_cycle']='undefined_rate'
        report=audit.engine.evaluate(document,sources)
        result=design.calculation_feedback(report)
        self.assertFalse(any(x['kind']=='declared_resource_contradiction' for x in result['issues']))
        self.assertTrue(any(x['kind']=='expression_or_unit_declaration' for x in result['issues']))

    def test_budget_failure_requires_valid_complete_data(self):
        document,sources=base.fixture();document['selected']='large'
        report=audit.engine.evaluate(document,sources);self.assertTrue(report['ok'],report['errors'])
        result=design.calculation_feedback(report)
        self.assertTrue(any(x['kind']=='hard_budget_exceeded' for x in result['issues']))
        document['candidates'][0]['resource_checks'][0]['demand_per_cycle']='undefined_rate'
        report=audit.engine.evaluate(document,sources);self.assertFalse(report['ok'])
        result=design.calculation_feedback(report)
        self.assertFalse(any(x['kind']=='hard_budget_exceeded' for x in result['issues']))
        self.assertTrue(result['provisional_budget_ids']);self.assertIn('partial totals',result['budget_action'])

    def test_unknown_cost_is_evidence_limit_not_service_failure(self):
        document,sources=base.fixture()
        document['work_model']['items'][0].update(status='unresolved',count={'kind':'unsupported','reason':'not expressed by this cost model','unit':'ops'})
        report=audit.engine.evaluate(document,sources);self.assertTrue(report['ok'],report['errors'])
        result=design.calculation_feedback(report)
        self.assertEqual([x['kind'] for x in result['issues']],['unresolved_design_evidence'])
        self.assertIn('not proof',result['issues'][0]['action'])


class RuntimeFeedbackTests(unittest.TestCase):
    def test_initial_work_parse_failure_feedback_is_visible_and_resume_reuses_it(self):
        document,sources=base.fixture();out=base.SkillToolTests().setup_run();(out/'work-model.current.json').unlink()
        first=Mock(side_effect=[json.JSONDecodeError('bad comma','{}',1),RuntimeError('interrupted')])
        with self.assertRaises(RuntimeError):design.prepare_work_model(out,'source','',document['requirements'],sources,first,2)
        self.assertIn('json_syntax',first.call_args.args[2])
        self.assertTrue((out/'source.work-0.feedback.json').exists())
        resumed=Mock(return_value=document['work_model'])
        result=design.prepare_work_model(out,'source','',document['requirements'],sources,resumed,2)
        self.assertEqual(result,document['work_model']);self.assertEqual(resumed.call_count,1)
        self.assertEqual(resumed.call_args.args[0],'source.work-1')

    def test_calculator_resource_feedback_preserves_original_diagnostic(self):
        document,sources=base.fixture();out=base.SkillToolTests().setup_run();bad=copy.deepcopy(document)
        bad['candidates'][0]['resource_checks'][0]['demand_per_cycle']='64'
        invoke=Mock(side_effect=[{'tool_request':{'tool':'evaluate_design','document':bad}},RuntimeError('inspect')])
        with self.assertRaises(RuntimeError):design.generate(out,'resource','',document['requirements'],sources,invoke,2)
        prompt=invoke.call_args.args[2]
        self.assertIn('declared_resource_contradiction',prompt);self.assertIn('declared demand exceeds service',prompt)
        self.assertIn('not all possible architectures',prompt)
        self.assertFalse(list(out.glob('*.architecture.md')))

    def test_protocol_feedback_does_not_spend_calculation_requests(self):
        document,sources=base.fixture();out=base.SkillToolTests().setup_run()
        final={'evaluated_design_sha256':design.identity(document),'architecture_md':document['architecture_md']}
        invoke=Mock(side_effect=[json.JSONDecodeError('bad comma','{}',1),{'tool_request':{'tool':'evaluate_design','document':copy.deepcopy(document)}},{'final':final}])
        result=design.generate(out,'protocol','',document['requirements'],sources,invoke,1)
        self.assertEqual(result['candidates'],document['candidates'])
        self.assertIn('json_syntax',invoke.call_args_list[1].args[2])
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),1)

    def test_baseline_parse_failure_feedback_is_source_only_and_recoverable(self):
        _,sources=base.fixture();out=audit.snapshot();(out/'audit-baseline.current.json').unlink()
        def malformed(name,role,prompt,parser):return parser('<<<AUDIT_BASELINE>>>\n{}\n{"second":true}')
        first=Mock(side_effect=lambda *args: malformed(*args) if first.call_count==1 else (_ for _ in ()).throw(RuntimeError('interrupted')))
        with self.assertRaises(RuntimeError):audit_session.prepare_baseline(out,'blind',sources,first,1)
        prompt=first.call_args.args[2]
        self.assertIn('protocol_structure',prompt);self.assertIn('ambiguous suffix rejected',prompt)
        self.assertNotIn('FILE design.json',prompt);self.assertNotIn('FILE architecture.md',prompt)
        resumed=audit.wire_provider(return_value=audit.baseline_fixture(sources))
        record=audit_session.prepare_baseline(out,'blind',sources,resumed,1)
        self.assertTrue(record['baseline_sha256']);self.assertEqual(resumed.call_count,1)
        self.assertEqual(resumed.call_args.args[0],'blind.baseline-1')

    def test_successful_audit_comparison_mismatch_is_not_tool_failure(self):
        request=audit.arithmetic_request();request['calculations'][0]['expected']='128'
        result=audit.validator.recompute(request,{},audit.engine);self.assertTrue(result['ok'])
        feedback=audit_session.tool_feedback(request,result)
        self.assertEqual(feedback['kind'],'audit_executed_evidence')
        self.assertEqual(feedback['comparison_mismatches'],['reconstructed_bits'])
        request['calculations'][0]['formula']='undefined_symbol'
        failed=audit.validator.recompute(request,{},audit.engine)
        feedback=audit_session.tool_feedback(request,failed)
        self.assertEqual(feedback['kind'],'audit_evidence_tool_declaration')
        self.assertIn('not observation evidence',feedback['action'])

    def test_report_validation_feedback_is_not_architecture_finding(self):
        ctx=audit.context_fixture();report=audit.report_fixture(ctx);report['observations'][0]['actual']=257
        validation=audit.validator.validate(report,ctx);self.assertFalse(validation['ok'])
        feedback=audit_session.report_feedback(report,validation)
        self.assertEqual(feedback['diagnostics'],validation['errors'])
        self.assertIn('not architecture findings',feedback['action'])

    def test_audit_report_is_unchanged_and_semantic_feedback_is_separate(self):
        document,sources=base.fixture();out=audit.snapshot();budget=audit.engine.evaluate(document,sources,True)
        ctx=audit.packet(sources,document,budget);result=audit.validator.recompute(audit.arithmetic_request(),ctx['sources'],audit.engine)
        result['request_id']='review.step-0';ctx['calculations']=[result]
        report=audit.report_content_fixture(ctx);report['verdict']='insufficient_evidence';original=copy.deepcopy(report)
        invoke=audit.wire_provider(side_effect=[{'tool_request':audit.arithmetic_request()},{'final':report}])
        returned,check=audit_session.run(out,'review',sources,document,budget,invoke,2)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(returned,dict(original,packet_sha256=ctx['packet_sha256'],baseline_sha256=ctx['baseline']['baseline_sha256']))
        feedback=json.loads((out/'review.feedback.json').read_text())
        self.assertEqual(feedback['kind'],'audit_semantic_judgment');self.assertIn('cannot override',feedback['scope'])


if __name__=='__main__':unittest.main()
