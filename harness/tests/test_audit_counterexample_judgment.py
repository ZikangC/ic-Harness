"""Executed deficits must be judged, without equating auditor errors to chip defects."""
import copy
import importlib.util
import unittest
import test_audit as f

spec=importlib.util.spec_from_file_location('counterexample_candidates',f.BASE/'skills/microarchitecture-audit/scripts/candidate_checks.py')
checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)
spec=importlib.util.spec_from_file_location('counterexample_source',f.BASE/'skills/microarchitecture-audit/scripts/source_model.py')
source=importlib.util.module_from_spec(spec);spec.loader.exec_module(source)


def fixture():
    document,sources=f.fixture();sources['spec.md']+='\nfixed input payload = 80 bytes\n'
    baseline=f.baseline_fixture(sources)
    baseline['analysis']['facts']=[{'id':'input_bytes','source':{'file':'spec.md','line':2,'number':1},'unit':'bytes'}]
    baseline['analysis']['flows']=[{'id':'input','direction':'read','encoding':'fixed_external',
        'fields':[{'shape':['input_bytes'],'element_bits':8}], 'scope':{'kind':'task','axis':'run'},
        'basis':'normative synthetic whole-task payload','references':[{'file':'spec.md','start':2,'end':2}]}]
    baseline['analysis']['states']=[{'id':'bad_state','shape':[-1],'lifecycle':'synthetic task',
        'basis':'independent malformed declaration','references':[{'file':'reference.py','start':1,'end':1}]}]
    raw=source.analyze(baseline['analysis'],sources,f.engine)
    record={'baseline':baseline,'baseline_sha256':f.audit_session.report_identity(baseline),'analysis_result':raw,
        'audit_script_bundle_sha256':raw['binding']['checker_bundle_sha256'],
        'calculator_bundle_sha256':raw['binding']['calculator_bundle_sha256'],
        'source_execution':f.validator.executed_source_values(raw,sources,baseline['analysis'],
            {'checker_bundle_sha256':raw['binding']['checker_bundle_sha256'],
             'calculator_bundle_sha256':raw['binding']['calculator_bundle_sha256']})}
    budget=f.engine.evaluate(document,sources,True)
    context=f.audit_session.packet(sources,document,budget,None,record)
    model=f.proposal_request()['model']
    model['flow_bindings']=[{'id':'inputs','flow_ids':['input'],'workload':'primary','traffic_ids':['reads'],
                             'basis':'whole selected read payload inventory'}]
    result=checker.check(model,raw,document,budget,context['sources'],f.engine,
        source_declaration=baseline['analysis'],execution_dependencies=context['execution_dependencies'])
    assert result['ok'],result['errors'];assert not result['coverage_complete']
    result['request_id']='actual';context['calculations']=[result];context['proposal_checks']=[result]
    report=f.report_fixture(context);report['verdict']='insufficient_evidence'
    return context,report,result


def failed_rows(result):
    refs=[{'file':'spec.md','start':2,'end':2},{'file':'design.json','quote':'"id": "small"'}]
    return [dict(request_id=result['request_id'],receipt_id=receipt['id'],check_id=check['id'],
        disposition='design_defect',finding_id='missing_input',reason='Declared complete inventory is 16 bytes below the normative input payload.',
        references=copy.deepcopy(refs)+[{'request_id':result['request_id'],
          'calculation_id':receipt['id']+'::'+check['id']+'__actual','actual':check['actual']}])
        for receipt in result['subreceipts'] for check in receipt['checks'] if check['status']=='fail']


class CounterexampleJudgmentTests(unittest.TestCase):
    def test_partial_source_cannot_silently_drop_an_executed_inventory_deficit(self):
        context,report,result=fixture()
        self.assertFalse(context['source_analysis']['ok'])
        inventory=next(row for row in result['checks'] if row.get('kind')=='candidate_inventory_bound')
        self.assertEqual((inventory['required_bytes'],inventory['available_bytes'],inventory['deficit_bytes']), (80,64,16))
        checked=f.validator.validate(report,context)
        self.assertFalse(checked['ok']);self.assertTrue(any('counterexample requires explicit adjudication:' in error for error in checked['errors']))

    def test_actual_design_defect_requires_revision_even_when_other_source_work_failed(self):
        context,report,result=fixture();report['counterexample_reviews']=failed_rows(result)
        report['findings']=[{'id':'missing_input','category':'design','severity':'blocking','disposition':'open',
            'location':'selected read traffic','issue':'16 input bytes are missing','evidence':'80 required bytes versus 64 available bytes',
            'required_change':'Provide all input payload and recalculate transfer service.',
            'references':copy.deepcopy(report['counterexample_reviews'][0]['references'])}]
        checked=f.validator.validate(report,context)
        self.assertTrue(any('open blocking design finding requires revise' in error for error in checked['errors']))
        report['verdict']='revise';checked=f.validator.validate(report,context)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(len(checked['counterexamples']),2)

    def test_inventory_deficit_cannot_be_closed_as_downstream_implementation_evidence(self):
        context,report,result=fixture();report['counterexample_reviews']=failed_rows(result)
        for row in report['counterexample_reviews']:
            row.update(disposition='unresolved',unverified_topic='input_mapping')
        report['unverified'].append({'kind':'design_decision','topic':'input_mapping','obligation':'Determine source-to-transfer mapping',
            'reason':'Synthetic undecided source mapping','references':report['checks'][0]['references']})
        checked=f.validator.validate(report,context)
        self.assertTrue(any('complete candidate inventory deficit cannot be deferred' in error for error in checked['errors']))

    def test_wrong_source_interpretation_has_explicit_correction_path_but_does_not_enable_accept(self):
        context,report,result=fixture();report['counterexample_reviews']=failed_rows(result)
        report['baseline_corrections']=[{'obligation_id':'independent-work',
            'correction':'Synthetic disputable grouping must be rebuilt from the original source.',
            'reason':'Correction route test; no new demand or passing receipt is invented.',
            'analysis_targets':[{'section':'flows','id':'input','workload':'primary'}],
            'references':[{'file':'spec.md','start':2,'end':2}]}]
        for row in report['counterexample_reviews']:row.update(disposition='source_interpretation_error',baseline_obligation_id='independent-work')
        checked=f.validator.validate(report,context);self.assertTrue(checked['ok'],checked['errors'])
        report['verdict']='accept';self.assertFalse(f.validator.validate(report,context)['ok'])

    def test_irrelevant_pass_cannot_rebut_a_complete_inventory_deficit(self):
        context,report,result=fixture();report['counterexample_reviews']=failed_rows(result)
        passed=next((receipt,check) for receipt in result['subreceipts'] for check in receipt['checks'] if check['status']=='pass')
        for row in report['counterexample_reviews']:
            row.update(disposition='audit_declaration_error',correction_check={'request_id':'actual','receipt_id':passed[0]['id'],'check_id':passed[1]['id']})
        checked=f.validator.validate(report,context)
        self.assertTrue(any('requires a related actual passing check' in error for error in checked['errors']))

    def test_unrelated_source_correction_cannot_rebut_inventory_failure(self):
        context,report,result=fixture();report['counterexample_reviews']=failed_rows(result)
        report['baseline_corrections']=[{'obligation_id':'independent-work','correction':'Unrelated count correction',
            'reason':'Negative test: no relation to failed input fields',
            'analysis_targets':[{'section':'computations','id':'scalar_work','workload':'primary'}],
            'references':[{'file':'reference.py','start':1,'end':1}]}]
        for row in report['counterexample_reviews']:row.update(disposition='source_interpretation_error',baseline_obligation_id='independent-work')
        checked=f.validator.validate(report,context)
        self.assertTrue(any('must target its affected source declarations' in error for error in checked['errors']))

    def test_unknown_or_duplicate_failure_ids_are_rejected(self):
        context,report,result=fixture();report['counterexample_reviews']=failed_rows(result)
        report['counterexample_reviews'][0]['check_id']='invented'
        checked=f.validator.validate(report,context);self.assertTrue(any('actual binding-verified failed check' in error for error in checked['errors']))

    def test_nonnumeric_proposal_contradiction_uses_actual_check_reference(self):
        document,sources=f.fixture();document['candidates'][0]['abi_fields'][0]['width']=40
        budget=f.engine.evaluate(document,sources,True);context=f.packet(sources,document,budget)
        result=checker.check(f.proposal_request()['model'],context['source_analysis'],document,budget,context['sources'],f.engine,
            source_declaration=context['baseline']['baseline']['analysis'],execution_dependencies=context['execution_dependencies'])
        self.assertTrue(result['ok'],result['errors']);result['request_id']='abi'
        context['calculations']=[result];context['proposal_checks']=[result]
        report=f.report_fixture(context);report['verdict']='revise'
        rows=[]
        for receipt in result['subreceipts']:
            for check in receipt['checks']:
                if check['status']!='fail':continue
                self.assertEqual(check['id'],'proposal_calculation')
                check_ref={'request_id':'abi','receipt_id':receipt['id'],'check_id':check['id']}
                rows.append(dict(check_ref,disposition='design_defect',finding_id='abi_width',reason='Actual selected field exceeds its declared carrier.',references=[*report['checks'][0]['references'],check_ref]))
        self.assertTrue(rows);report['counterexample_reviews']=rows
        report['findings']=[{'id':'abi_width','category':'design','severity':'blocking','disposition':'open',
            'location':'ABI field','issue':'40-bit field cannot fit 32-bit declared carrier',
            'evidence':'Executed shared calculator reports field does not fit carrier.',
            'required_change':'Define a legal ABI field/carrier relationship and recompute.',
            'references':copy.deepcopy(rows[0]['references'])}]
        checked=f.validator.validate(report,context);self.assertTrue(checked['ok'],checked['errors'])
        report['counterexample_reviews'][0]['references'][-1]['check_id']='not_executed'
        self.assertFalse(f.validator.validate(report,context)['ok'])

    def test_malformed_disposition_reference_ids_produce_diagnostics(self):
        for field in ('obligation','calculation','correction'):
            context,report,result=fixture();report['counterexample_reviews']=failed_rows(result)
            if field=='obligation':report['baseline_corrections']=[{'obligation_id':[],'correction':'invalid','reason':'negative fixture'}]
            elif field=='calculation':report['counterexample_reviews'][0]['references'][-1]['calculation_id']=[]
            else:report['counterexample_reviews'][0].update(disposition='audit_declaration_error',correction_check={'request_id':[],'receipt_id':'actual','check_id':'actual'})
            checked=f.validator.validate(report,context)
            self.assertFalse(checked['ok']);self.assertTrue(checked['errors'])

    def test_actual_failure_identity_needs_no_duplicate_number_id_copy(self):
        context,report,result=fixture();report['counterexample_reviews']=failed_rows(result)
        report['baseline_corrections']=[{'obligation_id':'independent-work','correction':'Rebuild the disputed input-flow interpretation',
            'reason':'Protocol fixture only; no revised source result is fabricated',
            'analysis_targets':[{'section':'flows','id':'input','workload':'primary'}],
            'references':[{'file':'spec.md','start':2,'end':2}]}]
        for row in report['counterexample_reviews']:
            row.update(disposition='source_interpretation_error',baseline_obligation_id='independent-work')
            row['references']=[ref for ref in row['references'] if 'request_id' not in ref]
        checked=f.validator.validate(report,context)
        self.assertTrue(checked['ok'],checked['errors'])
        self.assertEqual(checked['counterexamples'][0]['check']['required_bytes'],80)
        self.assertEqual(checked['counterexamples'][0]['check']['available_bytes'],64)

    def test_partial_negative_mapping_is_reported_but_never_enables_accept(self):
        context=f.context_fixture();report=f.report_fixture(context)
        report.update(verdict='insufficient_evidence',scope='Partial independent review; mappings have not been established.')
        report['requirements']=[]
        checked=f.validator.validate(report,context)
        self.assertTrue(checked['ok'],checked['errors']);self.assertFalse(checked['coverage']['release_mapping_complete'])
        self.assertEqual(checked['coverage']['missing_baseline_ids'],['independent-work'])
        self.assertTrue(checked['coverage']['missing_constraint_ids'])
        report['verdict']='accept';checked=f.validator.validate(report,context)
        self.assertFalse(checked['ok']);self.assertTrue(any('missing independent baseline coverage' in error for error in checked['errors']))


if __name__=='__main__':unittest.main()
