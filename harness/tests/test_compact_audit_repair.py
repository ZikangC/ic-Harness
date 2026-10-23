"""Only one fully checked text-field repair omits the engineering packet."""
import copy
import json
import unittest
from unittest.mock import Mock

import test_audit as fixtures
from harness.runtime import audit_session as session
from harness.prompts.architecture import compact_audit_repair


class CompactAuditRepairTests(unittest.TestCase):
    def setUp(self):
        self.context=fixtures.context_fixture();self.good=fixtures.report_fixture(self.context)
        self.reference=(fixtures.BASE/'skills/microarchitecture-audit/references/review-contract.md').read_text()
        self.bad=copy.deepcopy(self.good)
        row=next(row for row in self.bad['checks'] if row['topic']=='abi')
        row['explanation']=row.pop('evidence')
        receipt=self.context['calculations'][0]
        verified=fixtures.validator.executed_values(receipt,self.context['sources'],self.context['source_analysis'],
            self.context['document'],self.context['budget'],self.context['execution_dependencies'])
        self.history=[{'kind':'tool_result','result':receipt,'executed_values':verified}]

    def checked(self,report=None):return fixtures.validator.validate(self.bad if report is None else report,self.context)

    def compact(self,report=None,validation=None,reference=None):
        return compact_audit_repair(self.reference if reference is None else reference,
            self.bad if report is None else report,self.checked() if validation is None else validation,self.history)

    def test_missing_and_all_nonstring_evidence_types_remain_invalid_and_complete_validation_does_not_raise(self):
        for value in (None,0,True,{},[],42.5,'','   '):
            with self.subTest(value=value):
                report=copy.deepcopy(self.bad);row=next(row for row in report['checks'] if row['topic']=='abi');row['evidence']=value
                checked=self.checked(report)
                self.assertFalse(checked['ok']);self.assertEqual(len(checked['errors']),1);self.assertIn('scope',checked)
                self.assertTrue(checked['bindings']);self.assertIsNotNone(self.compact(report,checked))
        checked=self.checked();self.assertFalse(checked['ok']);self.assertEqual(len(checked['errors']),1)

    def test_actual_target_and_selected_bound_references_preserved_without_original_packet_or_auto_rename(self):
        checked=self.checked();before=copy.deepcopy(self.bad);message=self.compact(validation=checked)
        self.assertIn('LOCAL STRUCTURAL REPORT REPAIR ONLY',message)
        self.assertNotIn('FILE spec.md\n',message);self.assertNotIn('FILE design.json\n',message)
        self.assertNotIn('EXECUTED AUDIT TOOLS',message);self.assertNotIn('SOURCE QUANTITY SELECTORS',message)
        view=json.loads(message.split('\nCURRENT RESPONSE FEEDBACK\n',1)[1].split('\nCURRENT STEP:',1)[0])
        self.assertEqual(view['target_report'],before)
        row=next(row for row in view['target_report']['checks'] if row['topic']=='abi')
        self.assertNotIn('evidence',row);self.assertIn('explanation',row)
        self.assertEqual(view['current_check_reference_bindings'],[r for r in checked['bindings'] if r['location']=='abi'])
        self.assertEqual(view['referenced_bound_calculations'][0]['actual'],256)
        self.assertEqual(self.bad,before)

    def test_hidden_numeric_reference_and_source_errors_are_discovered_and_prevent_compact(self):
        for failure in ('calculation','source'):
            report=copy.deepcopy(self.bad)
            if failure=='calculation':report['observations'][0]['calculation_id']='not-executed'
            else:next(row for row in report['checks'] if row['topic']=='abi')['references'][0]['quote']='invented original text'
            with self.subTest(failure=failure):
                checked=self.checked(report)
                self.assertFalse(checked['ok']);self.assertGreater(len(checked['errors']),1)
                self.assertIsNone(self.compact(report,checked))

    def test_mixed_multiple_unknown_reference_and_engineering_errors_do_not_compact(self):
        cases=[]
        report=copy.deepcopy(self.bad);report['checks'][1]['evidence']=None;cases.append(report)
        report=copy.deepcopy(self.bad);report['findings']=[{'id':'required','category':'design','severity':'blocking','disposition':'open','location':'x','issue':'x','evidence':'x','required_change':'x'}];cases.append(report)
        report=copy.deepcopy(self.bad);report['checks'][0]['status']='UNKNOWN';cases.append(report)
        report=copy.deepcopy(self.good);report['checks'][0]['references'][0]['quote']='invalid source reference';cases.append(report)
        for report in cases:
            with self.subTest(report=report):self.assertIsNone(self.compact(report,self.checked(report)))
        self.assertIsNone(self.compact(validation={'ok':False,'errors':['new unknown diagnostic'],'scope':'complete'}))
        self.assertIsNone(self.compact(validation={'ok':False,'errors':self.checked()['errors']}))
        self.assertIsNone(self.compact(report=self.good,validation=self.checked()))
        self.assertIsNone(self.compact(reference='missing the loaded repair contract'))
        self.assertIsNone(self.compact(report=self.good,validation=self.checked(self.good)))
        partial=copy.deepcopy(self.bad);partial.pop('observations')
        self.assertIsNone(self.compact(report=partial,validation=self.checked()))

    def session_case(self,label):
        document,sources=fixtures.fixture();out=fixtures.snapshot();budget=fixtures.engine.evaluate(document,sources,True)
        context=fixtures.packet(sources,document,budget)
        result=fixtures.validator.recompute(fixtures.arithmetic_request(),context['sources'],fixtures.engine)
        result['request_id']=label+'.step-0';context['calculations']=[result]
        good=fixtures.report_content_fixture(context);good['verdict']='insufficient_evidence'
        bad=copy.deepcopy(good);row=next(row for row in bad['checks'] if row['topic']=='abi');row['explanation']=row.pop('evidence')
        repair={'checks':[next(row for row in good['checks'] if row['topic']=='abi')]}
        return out,document,sources,budget,good,bad,repair

    def test_real_runtime_uses_original_protocol_bound_target_and_full_validator_without_extra_tools(self):
        out,document,sources,budget,good,bad,repair=self.session_case('compact');messages=[]
        def callback(name,role,prompt,parser):
            messages.append(prompt)
            if name.endswith('step-0'):
                self.assertIn('FILE spec.md\n',prompt);return {'tool_request':fixtures.arithmetic_request()}
            if name.endswith('step-1'):
                self.assertIn('FILE reference.py\n',prompt);return {'final':bad}
            self.assertIn('LOCAL STRUCTURAL REPORT REPAIR ONLY',prompt)
            self.assertNotIn('FILE spec.md\n',prompt);return {'report_repair':repair}
        invoke=fixtures.wire_provider(callback=callback)
        report,checked=session.run(out,'compact',sources,document,budget,invoke,1,max_report_repairs=1)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(invoke.call_count,3)
        self.assertEqual(report['checks'],good['checks']);self.assertEqual(report['observations'],bad['observations'])
        prior=json.loads((out/'compact.step-1.canonical.json').read_text())
        binding=json.loads((out/'compact.step-2.binding.json').read_text())
        self.assertEqual(binding['context']['target_sha256'],session.report_identity(prior))
        self.assertEqual(json.loads((out/'compact.step-2.model.json').read_text()),{'report_repair':repair})
        self.assertEqual(len(list(out.glob('compact*.tool.execution.json'))),1)
        self.assertIn('mode',json.loads((out/'compact.step-2.report-check.request.json').read_text()))

    def test_failed_compact_repair_falls_back_once_to_full_packet_and_resume_preserves_caps(self):
        out,document,sources,budget,good,bad,repair=self.session_case('fallback')
        partial=copy.deepcopy(repair);partial['checks'][0]['evidence']=[]
        first=fixtures.wire_provider(side_effect=[{'tool_request':fixtures.arithmetic_request()},{'final':bad},{'report_repair':partial},RuntimeError('interrupt before full fallback')])
        with self.assertRaises(RuntimeError):session.run(out,'fallback',sources,document,budget,first,1,max_report_repairs=2)
        self.assertIn('LOCAL STRUCTURAL REPORT REPAIR ONLY',first.call_args_list[2].args[2])
        self.assertNotIn('LOCAL STRUCTURAL REPORT REPAIR ONLY',first.call_args_list[3].args[2])
        self.assertIn('FILE spec.md\n',first.call_args_list[3].args[2])
        self.assertIn('EXECUTED AUDIT TOOLS',first.call_args_list[3].args[2])
        resumed=fixtures.wire_provider(return_value={'report_repair':repair})
        report,checked=session.run(out,'fallback',sources,document,budget,resumed,1,max_report_repairs=2)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(resumed.call_count,1)
        self.assertEqual(resumed.call_args.args[0],'fallback.step-3');self.assertIn('FILE reference.py\n',resumed.call_args.args[2])
        self.assertEqual(len(list(out.glob('fallback*.tool.execution.json'))),1)
        self.assertEqual(report['observations'],bad['observations'])

    def test_failed_compact_cannot_reset_or_exceed_report_repair_allowance(self):
        out,document,sources,budget,good,bad,repair=self.session_case('cap')
        partial=copy.deepcopy(repair);partial['checks'][0]['evidence']=[]
        invoke=fixtures.wire_provider(side_effect=[{'tool_request':fixtures.arithmetic_request()},{'final':bad},{'report_repair':partial}])
        with self.assertRaisesRegex(session.AuditSessionLimit,'report repair limit'):
            session.run(out,'cap',sources,document,budget,invoke,1,max_report_repairs=1)
        self.assertEqual(invoke.call_count,3)


if __name__=='__main__':unittest.main()
