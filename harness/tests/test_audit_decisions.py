"""Engineering revision is distinct from optional improvement or failed audit."""
import copy
import json
import shutil
import unittest
import test_audit as fixtures
from test_audit import context_fixture,report_fixture,validator
from harness.runtime import audit_session
from harness.runtime.audit_session import parse_turn

class AuditDecisionTests(unittest.TestCase):
    def test_revise_only_conditions_or_suggestions_is_rejected(self):
        ctx=context_fixture();report=report_fixture(ctx)
        report['verdict']='revise'
        result=validator.validate(report,ctx)
        self.assertFalse(result['ok']);self.assertTrue(any('revise requires an open blocking' in x for x in result['errors']))
        report['verdict']='accept';self.assertTrue(validator.validate(report,ctx)['ok'])

    def test_failed_audit_can_report_insufficient_without_asking_for_design_change(self):
        ctx=context_fixture();report=report_fixture(ctx);report.update(verdict='insufficient_evidence',observations=[])
        self.assertTrue(validator.validate(report,ctx)['ok'])

    def audit_gap(self,report):
        row=copy.deepcopy(report['unverified'][0])
        row.update(kind='audit_evidence',topic='operand_mapping',
            obligation='Independently reconstruct the selected window operand correspondence.',
            reason='The required auditor reconstruction has not yet been performed; no proposal defect is established.')
        report['unverified'].append(row)

    def test_audit_gap_is_valid_insufficient_evidence_without_design_revision(self):
        ctx=context_fixture();report=report_fixture(ctx);self.audit_gap(report)
        before=copy.deepcopy(report)
        report['verdict']='insufficient_evidence'
        result=validator.validate(report,ctx)
        self.assertTrue(result['ok'],result['errors'])
        self.assertEqual(report['findings'],[])
        self.assertEqual(report['unverified'],before['unverified'])

    def test_accept_cannot_leave_a_declared_audit_gap(self):
        ctx=context_fixture();report=report_fixture(ctx)
        self.assertTrue(validator.validate(report,ctx)['ok'])
        self.audit_gap(report)
        result=validator.validate(report,ctx)
        self.assertFalse(result['ok'])
        self.assertEqual(result['errors'],['accept lacks audited evidence: operand_mapping'])
        self.assertNotIn('unverified kind',str(result['errors']))

    def design_gap(self,report):
        row=copy.deepcopy(report['unverified'][0])
        row.update(kind='design_decision',topic='input_region_extent',
            obligation='Specify the allocated input region and final transfer boundary.',
            reason='The proposal leaves this necessary interface choice unspecified.')
        report['unverified'].append(row)

    def necessary_finding(self,report):
        return {'id':'missing_extent','category':'design','severity':'blocking','disposition':'open',
            'location':'selected interface contract','issue':'The allocated input region is unspecified.',
            'evidence':'Synthetic input/proposal correspondence; the necessary boundary is undecided.',
            'required_change':'Define the allocation and show each legal final transfer remains in its region.',
            'references':copy.deepcopy(report['checks'][0]['references'])}

    def test_declared_missing_design_choice_cannot_end_as_accept_or_insufficient(self):
        ctx=context_fixture()
        for verdict in ('accept','insufficient_evidence'):
            with self.subTest(verdict=verdict):
                report=report_fixture(ctx);report['verdict']=verdict;self.design_gap(report)
                before=copy.deepcopy(report);checked=validator.validate(report,ctx)
                self.assertFalse(checked['ok'])
                self.assertIn('unverified design decision requires revise and an open blocking design finding: input_region_extent',checked['errors'])
                self.assertEqual(report,before)

    def test_declared_design_choice_needs_an_actionable_finding_not_only_revise(self):
        ctx=context_fixture();report=report_fixture(ctx);report['verdict']='revise';self.design_gap(report)
        checked=validator.validate(report,ctx)
        self.assertFalse(checked['ok'])
        self.assertTrue(any(error.startswith('unverified design decision requires revise and an open blocking design finding:') for error in checked['errors']))
        report['findings']=[self.necessary_finding(report)]
        checked=validator.validate(report,ctx)
        self.assertTrue(checked['ok'],checked['errors'])

    def test_necessary_revision_can_retain_unknown_audit_and_implementation_work(self):
        ctx=context_fixture();report=report_fixture(ctx);report['verdict']='revise'
        self.design_gap(report);self.audit_gap(report);report['findings']=[self.necessary_finding(report)]
        checked=validator.validate(report,ctx)
        self.assertTrue(checked['ok'],checked['errors'])
        self.assertEqual({row['kind'] for row in report['unverified']},
                         {'design_decision','audit_evidence','implementation_evidence'})

    def run_design_gap_reports(self,close):
        document,sources=fixtures.fixture();budget=fixtures.engine.evaluate(document,sources,True)
        original=copy.deepcopy(document);out=fixtures.snapshot(sources);self.addCleanup(shutil.rmtree,out)
        label='decision';sent=[]
        def invoke(name,role,prompt,parser):
            sent.append(prompt)
            if name.endswith('step-0'):return {'tool_request':fixtures.proposal_request()}
            ctx=fixtures.packet(sources,document,budget)
            receipt=json.loads((out/(label+'.step-0.tool.result.json')).read_text())
            receipt['request_id']=label+'.step-0';ctx['calculations']=[receipt];ctx['proposal_checks']=[receipt]
            report=fixtures.report_content_fixture(ctx);self.design_gap(report)
            report['verdict']='insufficient_evidence'
            if not name.endswith('step-1'):
                self.assertIn('unverified design decision requires revise and an open blocking design finding:',prompt)
                self.assertIn('audit_evidence_reopen',prompt)
                self.assertNotIn('CURRENT STEP: return ONLY <<<AUDIT_REPAIR>>>',prompt)
                if close:
                    report['verdict']='revise';report['findings']=[self.necessary_finding(report)]
            return {'final':report}
        provider=fixtures.wire_provider(callback=invoke)
        if close:
            report,checked=audit_session.run(out,label,sources,document,budget,provider,1,max_report_repairs=0)
            self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(report['verdict'],'revise')
            self.assertEqual(report['findings'][0]['id'],'missing_extent')
            self.assertEqual(provider.call_count,3)
        else:
            with self.assertRaisesRegex(audit_session.AuditSessionLimit,'bounded audit responses exhausted'):
                audit_session.run(out,label,sources,document,budget,provider,1,max_report_repairs=0)
            self.assertEqual(provider.call_count,4)
            self.assertFalse((out/(label+'.json')).exists())
        preserved=json.loads((out/(label+'.step-1.canonical.json')).read_text())
        self.assertEqual(preserved['verdict'],'insufficient_evidence');self.assertEqual(preserved['findings'],[])
        self.assertEqual(preserved['unverified'][-1]['kind'],'design_decision')
        self.assertEqual(len(list(out.glob(label+'.step-*.tool.execution.json'))),1)
        self.assertEqual(document,original)

    def test_design_gap_reopens_model_judgment_without_reference_repair_or_host_finding(self):
        self.run_design_gap_reports(close=True)

    def test_unrepaired_design_gap_stops_at_existing_allowance_without_acceptance(self):
        self.run_design_gap_reports(close=False)

    def test_evidenced_revision_can_retain_an_unfinished_independent_audit(self):
        ctx=context_fixture();report=report_fixture(ctx);self.audit_gap(report)
        report['verdict']='revise'
        report['findings']=[{'id':'necessary_choice','category':'design','severity':'blocking','disposition':'open',
            'location':'synthetic selected organization','issue':'Necessary operand source is unspecified.',
            'evidence':'Mechanical classification fixture; an explicit missing choice is distinct from an unfinished audit.',
            'required_change':'Specify the necessary source and its resource/timing contract.',
            'references':copy.deepcopy(report['checks'][0]['references'])}]
        result=validator.validate(report,ctx)
        self.assertTrue(result['ok'],result['errors'])
        report['verdict']='insufficient_evidence'
        self.assertTrue(any('open blocking design finding requires revise' in error
                            for error in validator.validate(report,ctx)['errors']))

    def test_audit_gap_does_not_expand_kind_to_arbitrary_labels_or_remove_refs(self):
        ctx=context_fixture();report=report_fixture(ctx);self.audit_gap(report)
        report['verdict']='insufficient_evidence';report['unverified'][-1]['kind']='audit_maybe'
        self.assertTrue(any('unverified kind' in error for error in validator.validate(report,ctx)['errors']))
        report['unverified'][-1]['kind']='audit_evidence';report['unverified'][-1]['references']=[]
        self.assertFalse(validator.validate(report,ctx)['ok'])

    def test_unresolved_count_correspondence_can_be_audit_evidence(self):
        document,sources=fixtures.fixture();document['work_model']['items'][0]['count']['operations']=79
        budget=fixtures.engine.evaluate(document,sources)
        ctx=fixtures.packet(sources,document,budget)
        result=fixtures.executed_proposal_check(ctx)
        ctx['calculations']=[result];ctx['proposal_checks']=[result]
        report=report_fixture(ctx);report['verdict']='insufficient_evidence';self.audit_gap(report)
        report['counterexample_reviews']=[{
            'request_id':result['request_id'],'receipt_id':receipt['id'],'check_id':check['id'],
            'disposition':'unresolved','unverified_topic':'operand_mapping',
            'reason':'Independent interpretation of the count correspondence remains unresolved.',
            'references':copy.deepcopy(report['checks'][0]['references'])}
            for receipt in result['subreceipts'] for check in receipt['checks'] if check['status']=='fail']
        self.assertTrue(report['counterexample_reviews'])
        checked=validator.validate(report,ctx)
        self.assertTrue(checked['ok'],checked['errors'])

    def test_audit_gap_cannot_defer_a_complete_inventory_deficit(self):
        from test_audit_counterexample_judgment import fixture,failed_rows
        ctx,report,result=fixture();self.audit_gap(report)
        report['counterexample_reviews']=failed_rows(result)
        for row in report['counterexample_reviews']:
            row.update(disposition='unresolved',unverified_topic='operand_mapping')
        checked=validator.validate(report,ctx)
        self.assertFalse(checked['ok'])
        self.assertTrue(any('complete candidate inventory deficit cannot be deferred' in error for error in checked['errors']))

    def test_unexpected_request_field_is_identified_without_dropping_it(self):
        raw='<<<AUDIT_TOOL_REQUEST>>>\n{"tool":"recompute","request_id":null,"facts":[],"assumptions":[],"calculations":[]}'
        with self.assertRaises(ValueError) as error:parse_turn(raw)
        self.assertIn('unexpected fields',str(error.exception));self.assertIn('request_id',str(error.exception));self.assertIn('omit',str(error.exception))
        result=parse_turn('<<<AUDIT_TOOL_REQUEST>>>\n{"tool":"recompute","facts":[],"assumptions":[],"calculations":[]}')
        self.assertEqual(set(result['tool_request']),{'tool','facts','assumptions','calculations'})

if __name__=='__main__':unittest.main()
