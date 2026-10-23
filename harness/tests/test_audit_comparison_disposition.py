"""Failed ordinary comparisons need a supported disposition, not an automatic chip verdict."""
import copy
import json
from pathlib import Path
import shutil
import unittest

import test_audit as f
from harness.runtime import audit_session


def fixture(guard=None):
    document,sources=f.fixture()
    if guard is not None:
        document['boundary_guard']={'minimum_denominator':guard}
        document['architecture_md']+='\nVirtual boundary policy: after quantization denominator=max(value, minimum_denominator).\n'
    sources['support/hypothesis.md']='Payload = 8\nCopies = 1\nUnrelated = 9\nTiny = 0.000001\nFractional bits = 24\n'
    context=f.packet(sources,document,f.engine.evaluate(document,sources,True))
    context['calculations']=[]
    proposal=f.executed_proposal_check(context)
    context['calculations'].append(proposal);context['proposal_checks']=[proposal]
    return context,f.report_fixture(context)


def request(copies=2,expected=8):
    return {'tool':'recompute','facts':[{'id':'payload','source':{'file':'support/hypothesis.md','line':1,'number':1},'unit':'count'}],
            'assumptions':[{'id':'copies','value':copies,'unit':'count','basis':'auditor hypothesis, not a chip requirement'}],
            'calculations':[{'id':'demand','formula':'payload*copies','unit':'count','basis':'virtual selected payload inventory hypothesis','expected':expected}]}


def add(context,raw,name):
    result=f.validator.recompute(raw,context['sources'],f.engine)
    assert result['ok'],result['errors']
    result['request_id']=name;context['calculations'].append(result)
    return result


def disposition(context,**changes):
    row={'request_id':'failed','receipt_id':'recompute','check_id':'demand','disposition':'unresolved',
         'unverified_topic':'supply_hypothesis','reason':'The source-to-proposal interpretation has not been established.',
         'references':[{'file':'support/hypothesis.md','start':1,'end':2},{'file':'design.json','quote':'"id": "small"'}]}
    row.update(changes);return row


class ComparisonDispositionTests(unittest.TestCase):
    def test_computed_mismatch_is_valid_but_cannot_be_accepted_or_omitted(self):
        context,report=fixture();result=add(context,request(),'failed')
        self.assertTrue(result['ok']);self.assertFalse(result['comparisons']['demand']['matches_expected'])
        report['observations'].append({'request_id':'failed','calculation_id':'demand','actual':16,'interpretation':'conditional non-reuse hypothesis, not automatic infeasibility'})
        report['unverified'].append({'kind':'implementation_evidence','topic':'later_reuse','obligation':'Potential future reuse',
                                    'reason':'Negative test: a label must not close an executed comparison.','references':disposition(context)['references']})
        for verdict in ('accept','insufficient_evidence'):
            report['verdict']=verdict
            checked=f.validator.validate(report,context)
            self.assertFalse(checked['ok']);self.assertTrue(any('counterexample requires explicit adjudication' in error for error in checked['errors']))

    def test_unresolved_and_design_defect_have_different_honest_outcomes(self):
        context,report=fixture();add(context,request(),'failed')
        report['verdict']='insufficient_evidence';report['counterexample_reviews']=[disposition(context)]
        report['unverified'].append({'kind':'audit_evidence','topic':'supply_hypothesis','obligation':'Determine the selected inventory mapping',
                                    'reason':'The auditor has not established whether repetition is needed.','references':disposition(context)['references']})
        checked=f.validator.validate(report,context);self.assertTrue(checked['ok'],checked['errors'])
        report['verdict']='accept';self.assertFalse(f.validator.validate(report,context)['ok'])
        report['verdict']='revise';report['unverified']=[]
        report['counterexample_reviews']=[disposition(context,disposition='design_defect',finding_id='payload_deficit')]
        report['findings']=[{'id':'payload_deficit','category':'design','severity':'blocking','disposition':'open','location':'virtual selected inventory',
                             'issue':'Selected inventory contradicts an independently established requirement.','evidence':'Protocol fixture:16 versus8.',
                             'required_change':'Correct the selected inventory and execute the revised budget.','references':disposition(context)['references']}]
        checked=f.validator.validate(report,context);self.assertTrue(checked['ok'],checked['errors'])

    def test_actual_related_auditor_correction_can_close_without_chip_revision(self):
        context,report=fixture();add(context,request(),'failed')
        corrected=request();corrected['assumptions']=[]
        corrected['facts'].append({'id':'copies','source':{'file':'support/hypothesis.md','line':2,'number':1},'unit':'count'})
        result=add(context,corrected,'fixed');self.assertTrue(result['comparisons']['demand']['matches_expected'])
        report['counterexample_reviews']=[disposition(context,disposition='audit_declaration_error',
            reason='The first audit assumed two copies; original evidence declares one. The same comparison now executes with that source fact.',
            correction_check={'request_id':'fixed','receipt_id':'recompute','check_id':'demand'})]
        checked=f.validator.validate(report,context);self.assertTrue(checked['ok'],checked['errors'])

    def test_changed_expectation_or_unrelated_pass_cannot_close(self):
        for kind in ('expected','unrelated'):
            context,report=fixture();add(context,request(),'failed')
            corrected=request(expected=16) if kind=='expected' else request()
            if kind=='unrelated':
                corrected['facts']=[{'id':'other','source':{'file':'support/hypothesis.md','line':3,'number':1},'unit':'count'}]
                corrected['calculations'][0]['formula']='other-other+8'
            add(context,corrected,'fixed')
            report['counterexample_reviews']=[disposition(context,disposition='audit_declaration_error',
                correction_check={'request_id':'fixed','receipt_id':'recompute','check_id':'demand'})]
            checked=f.validator.validate(report,context)
            self.assertFalse(checked['ok']);self.assertTrue(any('related actual passing check' in error for error in checked['errors']))

    def test_failed_numeric_assertion_uses_same_disposition_and_actual_correction(self):
        context,report=fixture()
        raw={'tool':'recompute','facts':[{'id':'tiny','source':{'file':'support/hypothesis.md','line':4,'number':1},'unit':'dimensionless'}],
             'assumptions':[{'id':'frac','value':4,'unit':'count','basis':'incorrect auditor format hypothesis'}],'calculations':[],
             'numeric_probes':[{'id':'small','operation':'identity','input':'tiny','format':{'bits':32,'fractional_bits':'frac','signed':True},
                 'rounding':'nearest_even','overflow':'saturate','assertions':[{'metric':'quantized','relation':'>','expected':0}],
                 'basis':'virtual destination-format check, no reachable-range or full accuracy claim'}]}
        add(context,raw,'failed');checked=f.validator.validate(report,context)
        self.assertFalse(checked['ok']);self.assertTrue(any('small__assertion_0' in error for error in checked['errors']))
        fixed=copy.deepcopy(raw);fixed['assumptions']=[]
        fixed['facts'].append({'id':'frac','source':{'file':'support/hypothesis.md','line':5,'number':1},'unit':'count'})
        add(context,fixed,'fixed')
        report['counterexample_reviews']=[disposition(context,check_id='small__assertion_0',disposition='audit_declaration_error',
            correction_check={'request_id':'fixed','receipt_id':'recompute','check_id':'small__assertion_0'},reason='The auditor used the wrong declared fractional precision; corrected source-bound precision executes the same point.')]
        checked=f.validator.validate(report,context);self.assertTrue(checked['ok'],checked['errors'])

    def test_existing_guard_can_rebut_a_wrong_positive_only_audit_premise(self):
        for variant in ('real_guard','different_point','constant_guard','unrelated_pass'):
            context,report=fixture(guard=1/32)
            tiny={'id':'tiny','source':{'file':'support/hypothesis.md','line':4,'number':1},'unit':'dimensionless'}
            point={'id':'small','operation':'identity','input':'tiny','format':{'bits':16,'fractional_bits':4,'signed':False},
                   'rounding':'nearest_even','overflow':'saturate','assertions':[{'metric':'quantized','relation':'>','expected':0}],
                   'basis':'Auditor hypothesis that the rounded intermediate must itself stay positive.'}
            raw={'tool':'recompute','facts':[tiny],'assumptions':[],'calculations':[],'numeric_probes':[point]}
            add(context,raw,'failed')
            corrected=copy.deepcopy(raw);corrected['numeric_probes'][0]['assertions']=[]
            if variant=='different_point':corrected['numeric_probes'][0]['input']='tiny*2'
            guard_line=next(i for i,line in enumerate(context['sources']['design.json'].splitlines(),1) if '"minimum_denominator"' in line)
            corrected['facts'].append({'id':'guard','source':{'file':'design.json','line':guard_line,'number':1},'unit':'dimensionless'})
            guarded_input='max(small__quantized,guard)'
            if variant=='constant_guard':guarded_input='max(small__quantized,1/32)'
            if variant=='unrelated_pass':guarded_input='guard'
            corrected['numeric_probes'].append({'id':'guarded','operation':'identity','input':guarded_input,
                'format':{'bits':32,'fractional_bits':24,'signed':False},'rounding':'nearest_even','overflow':'saturate',
                'assertions':[{'metric':'quantized','relation':'>','expected':0}],
                'basis':'Rebuild the same rounded point, then execute the frozen proposal minimum-denominator policy.'})
            result=add(context,corrected,'fixed');self.assertTrue(result['comparisons']['guarded__assertion_0']['matches_expected'])
            report['counterexample_reviews']=[disposition(context,check_id='small__assertion_0',disposition='audit_declaration_error',
                correction_check={'request_id':'fixed','receipt_id':'recompute','check_id':'guarded__assertion_0'},
                reason='The first assertion imposed positivity before the existing frozen guard; this related execution rebuilds that value and applies the already chosen guard.')]
            checked=f.validator.validate(report,context)
            if variant=='real_guard':self.assertTrue(checked['ok'],checked['errors'])
            else:
                self.assertFalse(checked['ok'],variant)
                self.assertTrue(any('related actual passing check' in error for error in checked['errors']))

    def test_source_interpretation_correction_is_explicit_and_cannot_enable_accept(self):
        context,report=fixture();raw=request()
        raw['facts'][0]['source']={'file':'spec.md','line':1,'number':1}
        add(context,raw,'failed')
        report['verdict']='insufficient_evidence'
        report['counterexample_reviews']=[disposition(context,disposition='source_interpretation_error',baseline_obligation_id='independent-work')]
        report['baseline_corrections']=[{'obligation_id':'independent-work','correction':'Reconstruct the independently declared scalar source grouping.',
            'reason':'The source hypothesis requires review; this is not evidence that the chip is wrong.',
            'analysis_targets':[{'section':'computations','id':'scalar_work','workload':'primary'}],
            'references':[{'file':'spec.md','start':1,'end':1}]}]
        checked=f.validator.validate(report,context);self.assertTrue(checked['ok'],checked['errors'])
        report['verdict']='accept';self.assertFalse(f.validator.validate(report,context)['ok'])

    def test_comparison_cannot_move_to_a_different_frozen_packet(self):
        context,report=fixture();result=add(context,request(),'failed')
        changed=dict(context['sources']);changed['architecture.md']+='\nchanged proposal\n'
        selected=f.validator.executed_values(result,changed,context['source_analysis'],context['document'],context['budget'],context['execution_dependencies'])
        self.assertTrue(selected['comparison_evidence_errors']);self.assertFalse(selected['subreceipts'])

    def test_actual_comparison_ids_are_exposed_in_existing_prompt_and_runtime_rejects_accept(self):
        sources=f.fixture()[1];out=f.snapshot(sources);self.addCleanup(shutil.rmtree,out)
        document=f.fixture()[0];budget=f.engine.evaluate(document,sources,True)
        failed=f.arithmetic_request();failed['calculations'][0]['expected']=128
        seen=[]
        def reply(name,role,prompt,parser):
            seen.append(name)
            if name.endswith('step-0'):return f.wire_reply({'tool_request':f.proposal_request()},parser)
            if name.endswith('step-1'):return f.wire_reply({'tool_request':failed},parser)
            self.assertIn('"receipt_id": "recompute", "check_id": "reconstructed_bits", "status": "fail"',prompt)
            context=json.loads((out/'review.packet.json').read_text())
            receipts=[]
            for label in ('review.step-0','review.step-1'):
                row=json.loads((out/(label+'.tool.result.json')).read_text());row['request_id']=label;receipts.append(row)
            context['calculations']=receipts;context['proposal_checks']=[receipts[0]]
            return f.wire_reply({'final':f.report_content_fixture(context)},parser)
        with self.assertRaises(audit_session.AuditSessionLimit):
            audit_session.run(out,'review',sources,document,budget,reply,2,max_report_repairs=0)
        self.assertGreaterEqual(len(seen),3)
        self.assertFalse((out/'review.json').exists())
        check=json.loads((out/'review.step-2.report-check.result.json').read_text())
        self.assertFalse(check['ok'])
        self.assertTrue(any('counterexample requires explicit adjudication' in error for error in check['errors']))


if __name__=='__main__':unittest.main()
