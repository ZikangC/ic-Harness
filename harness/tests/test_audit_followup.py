"""One engineering follow-up uses existing evidence, wire binding and allowance."""
import copy
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import Mock

import test_audit as fixtures
from harness.runtime import audit_session as session


class AuditFollowupTests(unittest.TestCase):
    def setUp(self):
        self.document,self.sources=fixtures.fixture()
        self.budget=fixtures.engine.evaluate(self.document,self.sources,True)
        self.out=fixtures.snapshot(self.sources);self.addCleanup(shutil.rmtree,self.out)
        self.label='followup'
        self.partial=fixtures.proposal_request();self.partial['model']['operand_checks']=[]

    def report(self,verdict='insufficient_evidence'):
        ctx=json.loads((self.out/(self.label+'.packet.json')).read_text())
        ctx['calculations']=[];ctx['proposal_checks']=[]
        for p in sorted(self.out.glob(self.label+'.step-*.tool.result.json')):
            result=json.loads(p.read_text());result['request_id']=p.name.removesuffix('.tool.result.json')
            request=json.loads(p.with_name(p.name.replace('.result.json','.request.json')).read_text())
            if request['mode'] in ('proposal','recompute'):
                ctx['calculations'].append(result)
                if request['mode']=='proposal':ctx['proposal_checks'].append(result)
        usable=any(x.get('calculation_ids') for x in ctx['calculations'])
        report=fixtures.report_content_fixture(ctx if usable else fixtures.context_fixture())
        if not usable:report['observations']=[]
        report['verdict']=verdict
        return report

    def run_replies(self,callback,tools=3,repairs=0):
        provider=fixtures.wire_provider(callback=callback)
        result=session.run(self.out,self.label,self.sources,self.document,self.budget,provider,tools,max_report_repairs=repairs)
        return result,provider

    def test_valid_early_stop_preserved_then_one_engineering_execution(self):
        sent=[];original=copy.deepcopy(self.document)
        def reply(name,role,prompt,parser):
            sent.append(prompt)
            if name.endswith('step-0'):return {'tool_request':self.partial}
            if name.endswith('step-2'):
                self.assertIn('audit_evidence_reopen',prompt)
                self.assertNotIn('REPORT REPAIR:',prompt)
                return {'tool_request':fixtures.arithmetic_request()}
            return {'final':self.report()}
        (review,check),provider=self.run_replies(reply)
        self.assertEqual(provider.call_count,4);self.assertTrue(check['ok'],check['errors'])
        self.assertEqual(review['verdict'],'insufficient_evidence');self.assertEqual(self.document,original)
        preserved=json.loads((self.out/'followup.step-1.canonical.json').read_text())
        self.assertEqual(preserved['verdict'],'insufficient_evidence')
        self.assertTrue(json.loads((self.out/'followup.step-1.report-check.result.json').read_text())['ok'])
        self.assertTrue((self.out/'followup.step-1.model.response.txt').exists())
        self.assertEqual(len(list(self.out.glob('followup.step-*.tool.execution.json'))),2)
        frame=json.loads((self.out/'followup.step-2.binding.json').read_text())['context']
        self.assertIsNone(frame['target_sha256']);self.assertEqual(frame['packet_sha256'],review['packet_sha256'])

    def test_tool_limit_exhausted_returns_honest_report_without_followup(self):
        def reply(name,role,prompt,parser):
            return {'tool_request':self.partial} if name.endswith('step-0') else {'final':self.report()}
        (review,check),provider=self.run_replies(reply,tools=1)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(provider.call_count,2)
        self.assertEqual(review['verdict'],'insufficient_evidence')

    def test_existing_later_engineering_execution_already_satisfies_checkpoint(self):
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':self.partial}
            if name.endswith('step-1'):return {'tool_request':fixtures.arithmetic_request()}
            self.assertNotIn('audit_evidence_reopen',prompt)
            return {'final':self.report()}
        (review,check),provider=self.run_replies(reply)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(provider.call_count,3)

    def test_another_partial_check_does_not_force_third_engineering_request(self):
        def reply(name,role,prompt,parser):
            if name.endswith(('step-0','step-2')):return {'tool_request':self.partial}
            return {'final':self.report()}
        (review,check),provider=self.run_replies(reply)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(provider.call_count,4)
        self.assertEqual(review['verdict'],'insufficient_evidence')
        self.assertEqual(len(list(self.out.glob('followup.step-*.tool.execution.json'))),2)

    def test_failed_followup_preserves_failure_and_can_stop_without_new_values(self):
        bad=fixtures.arithmetic_request();bad['calculations'][0]['formula']='1/0'
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':self.partial}
            if name.endswith('step-2'):return {'tool_request':bad}
            return {'final':self.report()}
        (review,check),provider=self.run_replies(reply)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(provider.call_count,4)
        failed=json.loads((self.out/'followup.step-2.tool.result.json').read_text())
        self.assertFalse(failed['ok']);self.assertEqual(failed['values'],{})

    def test_final_report_and_lookup_cannot_bypass_pending_execution(self):
        lookup={'tool':'lookup_source','queries':[{'file':'spec.md','query':'area 3'}]}
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':self.partial}
            if name.endswith('step-2'):return {'final':self.report()}
            if name.endswith('step-3'):return {'tool_request':lookup}
            if name.endswith('step-4'):return {'tool_request':fixtures.arithmetic_request()}
            return {'final':self.report()}
        (review,check),provider=self.run_replies(reply)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(provider.call_count,6)
        self.assertEqual(len(list(self.out.glob('followup.step-*.tool.execution.json'))),3)
        self.assertTrue((self.out/'followup.step-3.tool.result.json').exists())
        self.assertTrue((self.out/'followup.step-2.canonical.json').exists())

    def test_repeated_early_reports_stop_when_response_allowance_cannot_fit_followup(self):
        def reply(name,role,prompt,parser):
            return {'tool_request':self.partial} if name.endswith('step-0') else {'final':self.report()}
        provider=fixtures.wire_provider(callback=reply)
        review,check=session.run(self.out,self.label,self.sources,self.document,self.budget,provider,2,max_report_repairs=0)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(review['verdict'],'insufficient_evidence')
        self.assertEqual(provider.call_count,5) # Existing response bound leaves no tool+report pair.
        self.assertEqual(len(list(self.out.glob('followup.step-*.tool.execution.json'))),1)
        self.assertTrue((self.out/'followup.json').exists())

    def test_valid_revise_is_not_reopened(self):
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':self.partial}
            report=self.report('revise')
            report['findings']=[{'id':'needed','category':'design','severity':'blocking','disposition':'open',
                'location':'fixture contract','issue':'Model-selected necessary fixture change.',
                'evidence':'Synthetic model judgment, not a semantic architecture evaluation.',
                'required_change':'Close the fixture issue.','references':copy.deepcopy(report['checks'][0]['references'])}]
            return {'final':report}
        (review,check),provider=self.run_replies(reply)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(provider.call_count,2)
        self.assertEqual(review['verdict'],'revise')

    def test_valid_necessary_revise_after_reopen_needs_no_artificial_tool(self):
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':self.partial}
            if name.endswith('step-1'):return {'final':self.report()}
            self.assertIn('audit_evidence_reopen',prompt)
            report=self.report('revise')
            report['findings']=[{'id':'needed','category':'design','severity':'blocking','disposition':'open',
                'location':'fixture contract','issue':'A necessary proposal decision is absent.',
                'evidence':'Synthetic original/proposal evidence; no extra arithmetic required.',
                'required_change':'Specify and implement the missing fixture decision.',
                'references':copy.deepcopy(report['checks'][0]['references'])}]
            return {'final':report}
        (review,check),provider=self.run_replies(reply)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(review['verdict'],'revise')
        self.assertEqual(provider.call_count,3)
        self.assertEqual(len(list(self.out.glob('followup.step-*.tool.execution.json'))),1)

    def test_pure_unknown_physical_service_does_not_trigger_checkpoint(self):
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':fixtures.proposal_request()}
            return {'final':self.report()}
        (review,check),provider=self.run_replies(reply)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(provider.call_count,2)
        receipt=json.loads((self.out/'followup.step-0.tool.result.json').read_text())
        self.assertTrue(receipt['missing_service_checks']);self.assertFalse(receipt['missing_service_declarations'])

    def test_no_candidate_child_evidence_does_not_trigger_checkpoint(self):
        empty=fixtures.proposal_request();empty['model'].update(count_checks=[],service_windows=[],operand_checks=[])
        def reply(name,role,prompt,parser):
            return {'tool_request':empty} if name.endswith('step-0') else {'final':self.report()}
        (review,check),provider=self.run_replies(reply)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(provider.call_count,2)

    def test_input_only_bound_children_do_not_trigger_checkpoint(self):
        history=[{'request':self.partial,'result':{'ok':True,'source_coverage_complete':True,
            'request_id':'bound-input-only','missing_operand_checks':['compute']},
            'executed_values':{'subreceipts':[{'section':'input','checks':[{'status':'pass'}]}]}}]
        self.assertIsNone(session.pending_engineering_followup(history,{'coverage_complete':True}))

    def test_lookup_spending_last_tool_credit_does_not_reset_or_satisfy_checkpoint(self):
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':self.partial}
            if name.endswith('step-2'):return {'tool_request':{'tool':'lookup_source','queries':[{'file':'spec.md','query':'area 3'}]}}
            return {'final':self.report()}
        provider=fixtures.wire_provider(callback=reply)
        review,check=session.run(self.out,self.label,self.sources,self.document,self.budget,provider,2,max_report_repairs=0)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(review['verdict'],'insufficient_evidence')
        self.assertEqual(provider.call_count,4)
        self.assertEqual(len(list(self.out.glob('followup.step-*.tool.execution.json'))),2)
        self.assertTrue((self.out/'followup.step-1.canonical.json').exists())
        self.assertTrue((self.out/'followup.json').exists())

    def test_failed_source_baseline_can_stop_without_candidate_followup(self):
        out=fixtures.snapshot(self.sources);self.addCleanup(shutil.rmtree,out)
        (out/'audit-baseline.current.json').unlink()
        bad=fixtures.baseline_fixture(self.sources);bad['analysis']['computations'][0]['steps'][0]['shape']=['missing']
        report=fixtures.report_content_fixture(fixtures.context_fixture());report.update(verdict='insufficient_evidence',observations=[])
        provider=fixtures.wire_provider(callback=lambda name,role,prompt,parser:bad if '.baseline-' in name else {'final':report})
        review,check=session.run(out,'source-failed',self.sources,self.document,self.budget,provider,3,max_baseline_repairs=0)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(review['verdict'],'insufficient_evidence')
        self.assertEqual(provider.call_count,2);self.assertFalse(list(out.glob('source-failed.step-*.tool.execution.json')))

    def test_resume_replays_once_checkpoint_and_receipts_without_new_tool(self):
        def first(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':self.partial}
            if name.endswith('step-1'):return {'final':self.report()}
            if name.endswith('step-2'):return {'tool_request':self.partial}
            raise RuntimeError('interrupted after the one followup')
        provider=fixtures.wire_provider(callback=first)
        with self.assertRaises(RuntimeError):
            session.run(self.out,self.label,self.sources,self.document,self.budget,provider,3,max_report_repairs=0)
        original={p.name:p.read_bytes() for p in self.out.glob('followup.step-[012].*.json')}
        resumed=fixtures.wire_provider(callback=lambda name,role,prompt,parser:{'final':self.report()})
        review,check=session.run(self.out,self.label,self.sources,self.document,self.budget,resumed,3,max_report_repairs=0)
        self.assertTrue(check['ok'],check['errors']);self.assertEqual(resumed.call_count,1)
        self.assertEqual(resumed.call_args.args[0],'followup.step-3')
        self.assertEqual(len(list(self.out.glob('followup.step-*.tool.execution.json'))),2)
        # Local validation may rerun with identical bytes; bound raw/canonical and
        # engineering script receipts stay tied to the original packet.
        for name,content in original.items():self.assertEqual((self.out/name).read_bytes(),content,name)


if __name__=='__main__':unittest.main()
