"""Partial source evidence reaches local audit tools without enabling release."""
import copy
import json
import importlib.util
import unittest

import test_audit as fixtures
from harness.prompts.architecture import audit as audit_prompt, source_summary, baseline_feedback
from harness.runtime import audit_session as session


class SourceTransactionTransportTests(unittest.TestCase):
    def partial_baseline(self, sources):
        baseline=fixtures.baseline_fixture(sources)
        failed=copy.deepcopy(baseline['analysis']['computations'][0])
        failed.update(id='invalid_path',steps=[{'id':'bad_dot','kind':'dot','width':-1}])
        baseline['analysis']['computations'].append(failed)
        return baseline

    def test_successful_source_paths_are_available_despite_failed_independent_path(self):
        document,sources=fixtures.fixture();original=copy.deepcopy(document)
        out=fixtures.snapshot(sources);(out/'audit-baseline.current.json').unlink()
        baseline=self.partial_baseline(sources);budget=fixtures.engine.evaluate(document,sources,True)
        seen=[]
        def invoke(name,role,prompt,parser):
            seen.append(prompt)
            if name.endswith('baseline-0'):return fixtures.wire_reply(baseline,parser)
            self.assertIn('HOST-VERIFIED SOURCE TRANSACTIONS',prompt)
            self.assertIn('SOURCE QUANTITATIVE COVERAGE INCOMPLETE',prompt)
            self.assertNotIn('Do not call check_proposal with the failed baseline',prompt)
            if name.endswith('step-0'):
                return fixtures.wire_reply({'tool_request':fixtures.proposal_request()},parser)
            receipt=json.loads((out/'partial.step-0.tool.result.json').read_text())
            receipt['request_id']='partial.step-0'
            context=json.loads((out/'partial.packet.json').read_text())
            context['calculations']=[receipt];context['proposal_checks']=[receipt]
            report=fixtures.report_content_fixture(context)
            report.update(verdict='insufficient_evidence',scope='Successful local computation evidence retained; failed independent source path leaves full coverage unknown.')
            return fixtures.wire_reply({'final':report},parser)
        review,check=session.run(out,'partial',sources,document,budget,invoke,1,max_baseline_repairs=0)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(review['verdict'],'insufficient_evidence')
        self.assertEqual(len(seen),3);self.assertEqual(document,original)
        self.assertFalse((out/'audit-baseline.current.json').exists())
        context=json.loads((out/'partial.packet.json').read_text())
        self.assertFalse(context['source_analysis']['ok']);self.assertFalse(context['source_execution']['coverage_complete'])
        self.assertEqual(context['source_execution']['verified_data']['computations']['scalar_work']['totals']['primary'],80)
        request=json.loads((out/'partial.step-0.tool.request.json').read_text())
        self.assertEqual(request['source_declaration'],baseline['analysis'])
        self.assertEqual(request['execution_dependencies'],context['execution_dependencies'])
        receipt=json.loads((out/'partial.step-0.tool.result.json').read_text())
        self.assertFalse(receipt['coverage_complete'])
        self.assertTrue(any(key.endswith('direct_work__source') and value==80 for key,value in receipt['values'].items()))
        self.assertEqual(review['observations'][0]['actual'],80)

    def test_model_cannot_supply_host_source_execution_binding(self):
        for field in ('source_declaration','execution_dependencies','source_execution'):
            request=fixtures.proposal_request();request[field]={}
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'unexpected fields'):
                session.parse_turn('<<<AUDIT_TOOL_REQUEST>>>\n'+json.dumps(request))
            with self.subTest(report_field=field),self.assertRaisesRegex(ValueError,'reserved host metadata'):
                session.parse_turn('<<<AUDIT_REPORT>>>\n'+json.dumps({'verdict':'insufficient_evidence',field:{}}))
            with self.subTest(baseline_field=field),self.assertRaisesRegex(ValueError,'reserved host metadata'):
                session.parse_baseline_turn('<<<AUDIT_BASELINE>>>\n'+json.dumps({field:{}}))

    def test_source_summary_is_guidance_not_a_report_observation_receipt(self):
        sources={'source-analysis.json':json.dumps({'ok':False,'workloads':{'unverified':100}})}
        selected={'values':{'source/workload/0::quantity':4},'calculation_ids':['source/workload/0::quantity'],
                  'subreceipts':[], 'errors':[], 'applicability':[], 'coverage_complete':False,
                  'verified_data':{'workloads':{'verified':4}}}
        prompt=audit_prompt('',sources,source_execution=selected)
        self.assertIn('ACTUAL INDEPENDENT SOURCE PROFILES: {"verified": 4}',prompt)
        self.assertIn('source transaction IDs are not audit observation references',prompt)
        self.assertIn('Binding-verified successful source transactions may support a local check_proposal',prompt)
        self.assertIn('Accept and baseline-cache reuse are unavailable',prompt)

    def test_model_view_omits_repeated_execution_identity_but_preserves_verified_math(self):
        from test_audit_source_model import witness
        model,sources=witness()
        source_module=importlib.util.spec_from_file_location('compact_source_fixture',fixtures.BASE/'skills/microarchitecture-audit/scripts/source_model.py')
        module=importlib.util.module_from_spec(source_module);source_module.loader.exec_module(module)
        raw=module.analyze(model,sources,fixtures.engine)
        selected=fixtures.validator.executed_source_values(raw,sources,model,
            {'checker_bundle_sha256':session.script_bundle_hash(fixtures.BASE/'skills/microarchitecture-audit/scripts'),
             'calculator_bundle_sha256':session.script_bundle_hash(fixtures.BASE/'skills/microarchitecture-design/scripts')})
        original=copy.deepcopy((raw,selected));compact=source_summary(raw,selected)
        self.assertEqual(compact['verified_data'],selected['verified_data'])
        self.assertEqual(compact['verified_data']['flows']['out']['totals']['p']['bytes'],80)
        self.assertEqual(compact['verified_data']['computations']['projection']['totals']['p'],60)
        self.assertTrue(compact['coverage_complete']);self.assertIn('not original-source semantic evidence',compact['scope'])
        rendered=json.dumps(compact)
        for name in ('receipt_sha256','checker_files_sha256','calculator_files_sha256','source_sha256s','model_sha256'):
            self.assertNotIn(name,rendered)
        self.assertLess(len(rendered),len(json.dumps(raw))/2)
        self.assertEqual((raw,selected),original)

    def test_baseline_prompt_feedback_keeps_failures_without_repeating_receipts(self):
        document,sources=fixtures.fixture();out=fixtures.snapshot(sources);(out/'audit-baseline.current.json').unlink()
        baseline=self.partial_baseline(sources)
        record=session.prepare_baseline(out,'summary',sources,fixtures.wire_provider(return_value=baseline),0)
        diagnosis=json.loads((out/'summary.baseline-0.source-feedback.json').read_text());original=copy.deepcopy(diagnosis)
        compact=baseline_feedback(diagnosis)
        self.assertEqual(compact['diagnostics'],diagnosis['diagnostics'])
        self.assertEqual(compact['source_analysis']['verified_data'],record['source_execution']['verified_data'])
        self.assertFalse(compact['source_analysis']['coverage_complete'])
        self.assertTrue(any(row['status']=='declaration_failed' for row in compact['source_analysis']['applicability']))
        self.assertNotIn('analysis_result',compact);self.assertNotIn('source_execution',compact)
        self.assertNotIn('receipt_sha256',json.dumps(compact));self.assertEqual(diagnosis,original)

    def flow_partial_report(self, legacy_row=False):
        """Execute real frozen scripts; the synthetic inputs define the byte demand."""
        document,sources=fixtures.fixture();original=copy.deepcopy(document)
        sources['spec.md']+='\ninput_body=64 bytes\ninput_tag=16 bytes\n'
        out=fixtures.snapshot(sources);(out/'audit-baseline.current.json').unlink()
        baseline=fixtures.baseline_fixture(sources)
        refs=[{'file':'spec.md','start':2,'end':3},{'file':'reference.py','start':1,'end':len(sources['reference.py'].splitlines())}]
        baseline['analysis']['facts']=[{'id':'body_bytes','source':{'file':'spec.md','line':2,'number':1},'unit':'bytes'},
                                       {'id':'tag_bytes','source':{'file':'spec.md','line':3,'number':1},'unit':'bytes'}]
        baseline['analysis']['states']=[{'id':'broken_state','shape':[-1],'lifecycle':'synthetic task live state',
                                        'basis':'deliberately malformed independent state declaration','references':refs}]
        baseline['analysis']['flows']=[{'id':name,'direction':'read','encoding':'fixed_external',
            'fields':[{'shape':[fact],'element_bits':8}],'scope':{'kind':'task','axis':'run'},
            'basis':'test source fixes complete-task bytes','references':refs}
            for name,fact in (('body','body_bytes'),('tag','tag_bytes'))]
        request=fixtures.proposal_request()
        request['model']['flow_bindings']=[{'id':'all_input','flow_ids':['body','tag'],'workload':'primary','traffic_ids':['reads'],
            'basis':'all fixed complete-task source inputs use the entire selected read inventory'}]
        if legacy_row:
            request['model']['flow_bindings'].insert(0,{'flow_id':'body','workload':'primary','traffic_id':'reads',
                'basis':'deliberately unsupported old single-flow request'})
        budget=fixtures.engine.evaluate(document,sources,True);seen=[]
        def invoke(name,role,prompt,parser):
            seen.append(prompt)
            if name.endswith('baseline-0'):return fixtures.wire_reply(baseline,parser)
            if name.endswith('step-0'):return fixtures.wire_reply({'tool_request':request},parser)
            receipt=json.loads((out/'flowpartial.step-0.tool.result.json').read_text())
            receipt['request_id']='flowpartial.step-0'
            context=json.loads((out/'flowpartial.packet.json').read_text())
            context['calculations']=[receipt];context['proposal_checks']=[receipt]
            report=fixtures.report_content_fixture(context)
            inventory=next(row for row in receipt['checks'] if row['id'].startswith('flow_inventory__'))
            self.assertEqual((inventory['status'],inventory['required_bytes'],inventory['available_bytes']),('fail',80,64))
            successful=next(row for row in receipt['subreceipts'] if inventory['id'] in {check['id'] for check in row['checks']})
            calculation_refs=[{'request_id':receipt['request_id'],'calculation_id':successful['id']+'::'+inventory['id']+'__'+suffix,
                               'actual':inventory[suffix]} for suffix in ('expected','actual')]
            evidence=[{'file':'spec.md','start':2,'end':3},{'file':'design.json','quote':'"id": "small"'},*calculation_refs]
            report.update(verdict='revise',scope='Source state declaration failed, but successful fixed-flow transactions independently establish a complete selected inventory deficit.')
            report['observations']=[dict(row,interpretation='Entire selected read inventory supplies 64 of 80 required semantic bytes; padding is not additional input data.') for row in calculation_refs]
            next(row for row in report['checks'] if row['topic']=='abi').update(status='fail',evidence='80 fixed source bytes exceed the complete 64-byte read payload inventory.',references=evidence)
            report['findings']=[{'id':'input_deficit','category':'design','severity':'blocking','disposition':'open',
                'location':'selected candidate read traffic inventory','issue':'16 required source bytes are absent from every selected read transfer.',
                'evidence':'Successful source body/tag transactions derive 80 bytes. Complete selected read inventory derives 64 bytes. The bad source-state declaration does not invalidate those flow transactions.',
                'required_change':'Allocate and transfer all 80 semantic input bytes and recalculate traffic and service before consumption; close with complete inventory and address mapping evidence.',
                'references':evidence}]
            report['counterexample_reviews']=[]
            for child in receipt['subreceipts']:
                for failure in child['checks']:
                    if failure['status']!='fail':continue
                    cid=child['id']+'::'+failure['id']+'__actual'
                    report['counterexample_reviews'].append({'request_id':receipt['request_id'],'receipt_id':child['id'],
                        'check_id':failure['id'],'disposition':'design_defect','finding_id':'input_deficit',
                        'reason':'All fixed source flows are byte aligned and mapped to the complete selected read inventory; it supplies 16 fewer semantic bytes than required. This is independent of the malformed state declaration.',
                        'references':evidence[:2]+[{'request_id':receipt['request_id'],'calculation_id':cid,'actual':child['values'][cid]}]})
            return fixtures.wire_reply({'final':report},parser)
        review,checked=session.run(out,'flowpartial',sources,document,budget,invoke,1,max_baseline_repairs=0)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(review['verdict'],'revise')
        self.assertEqual(document,original);self.assertEqual(len(seen),3)
        context=json.loads((out/'flowpartial.packet.json').read_text())
        self.assertFalse(context['source_execution']['coverage_complete']);self.assertEqual(context['source_execution']['verified_data']['states'],{})
        self.assertFalse((out/'audit-baseline.current.json').exists())
        receipt=json.loads((out/'flowpartial.step-0.tool.result.json').read_text())
        self.assertFalse(receipt['coverage_complete'])
        return receipt

    def test_bad_source_state_does_not_hide_complete_fixed_flow_deficit(self):
        receipt=self.flow_partial_report()
        self.assertTrue(any(row['id'].startswith('flow_inventory__') and row['status']=='fail' for row in receipt['checks']))

    def test_old_single_flow_protocol_is_rejected_without_erasing_valid_group(self):
        receipt=self.flow_partial_report(legacy_row=True)
        failed=next(row for row in receipt['subreceipts'] if row['id']=='flow/0')
        self.assertEqual(failed['status'],'declaration_failed');self.assertEqual(failed['values'],{})
        self.assertTrue(any(row['id']=='flow/1' and row['status']=='executed' for row in receipt['subreceipts']))


if __name__=='__main__':unittest.main()
