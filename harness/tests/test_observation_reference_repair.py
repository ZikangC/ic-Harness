"""Reference-only observation repair never revises numeric or engineering content."""
import copy
import json
import unittest
from unittest.mock import Mock

import test_audit as fixtures
from harness.runtime import audit_session as session
from harness.prompts.architecture import audit as audit_prompt


def repair(old,new):
    return {'reference_updates':[{'collection':'observations','request_id':old['request_id'],
            'calculation_id':old['calculation_id'],'reference':{key:new[key] for key in ('request_id','calculation_id','actual')}}]}


class ObservationReferenceRepairTests(unittest.TestCase):
    def setUp(self):
        self.context=fixtures.context_fixture()
        self.good=fixtures.report_fixture(self.context)
        self.bad=copy.deepcopy(self.good)
        self.bad['observations'][0]['calculation_id']='duplicated_prefix__reconstructed_bits'
        self.request=repair(self.bad['observations'][0],self.good['observations'][0])

    def apply(self,request=None,previous=None):
        return session.repair_report(self.bad if previous is None else previous,self.request if request is None else request,
                                     self.context['citations'],self.context['sources'])

    def test_exact_reference_repair_preserves_number_interpretation_and_all_engineering_content(self):
        previous=copy.deepcopy(self.bad);request=copy.deepcopy(self.request)
        checked=fixtures.validator.validate(self.bad,self.context)
        self.assertTrue(any('reference must name an executed bound calculation' in e for e in checked['errors']))
        result=self.apply()
        self.assertEqual(result,self.good)
        self.assertEqual(self.bad,previous);self.assertEqual(self.request,request)
        self.assertTrue(fixtures.validator.validate(result,self.context)['ok'])

    def test_nonexecuted_or_wrong_request_destination_does_not_gain_valid_evidence(self):
        for field,value in (('request_id','unexecuted-request'),('calculation_id','unexecuted-result')):
            with self.subTest(field=field):
                request=copy.deepcopy(self.request);request['reference_updates'][0]['reference'][field]=value
                result=self.apply(request)
                self.assertEqual(result['observations'][0]['actual'],self.bad['observations'][0]['actual'])
                checked=fixtures.validator.validate(result,self.context)
                self.assertFalse(checked['ok'])
                self.assertTrue(any('observation calculation reference:' in e for e in checked['errors']))

    def test_changed_number_numeric_type_or_explanation_is_rejected_without_mutating_prior_report(self):
        for value in (255,256.0,True,'256'):
            with self.subTest(actual=value):
                request=copy.deepcopy(self.request);request['reference_updates'][0]['reference']['actual']=value
                with self.assertRaisesRegex(ValueError,'exact existing numeric actual'):self.apply(request)
        for container in ('reference','target'):
            request=copy.deepcopy(self.request);update=request['reference_updates'][0]
            (update['reference'] if container=='reference' else update)['interpretation']='new engineering claim'
            with self.assertRaises(ValueError):self.apply(request)
        self.assertEqual(self.bad['observations'][0]['actual'],256)

    def test_ambiguous_unknown_and_duplicate_old_targets_are_rejected(self):
        previous=copy.deepcopy(self.bad);previous['observations'].append(copy.deepcopy(previous['observations'][0]))
        with self.assertRaisesRegex(ValueError,'unique existing'):self.apply(previous=previous)
        request=copy.deepcopy(self.request);request['reference_updates'][0]['request_id']='not-the-existing-target'
        with self.assertRaisesRegex(ValueError,'unique existing'):self.apply(request)
        request=copy.deepcopy(self.request);request['reference_updates'].append(copy.deepcopy(request['reference_updates'][0]))
        with self.assertRaisesRegex(ValueError,'duplicate observation'):self.apply(request)

    def test_whole_observation_edits_and_host_identity_edits_remain_rejected(self):
        for request in ({'observations':self.good['observations']},dict(self.request,verdict='revise'),dict(self.request,packet_sha256='fake')):
            with self.subTest(request=request),self.assertRaises(ValueError):self.apply(request)

    def test_diagnostic_names_the_bad_reference_and_only_bound_request_ids(self):
        checked=fixtures.validator.validate(self.bad,self.context)
        diagnostic=next(e for e in checked['errors'] if e.startswith('observation calculation reference:'))
        self.assertIn('duplicated_prefix__reconstructed_bits',diagnostic)
        self.assertIn('audit.step-0',diagnostic)
        ids=json.loads(diagnostic.split('; bound_calculation_ids=',1)[1])
        verified=fixtures.validator.executed_values(self.context['calculations'][0],self.context['sources'],
            self.context['source_analysis'],self.context['document'],self.context['budget'],self.context['execution_dependencies'])
        self.assertEqual(ids,verified['calculation_ids']);self.assertNotIn('duplicated_prefix__reconstructed_bits',ids)

    def test_actual_runtime_preserves_raw_target_and_uses_no_new_calculation_allowance(self):
        document,sources=fixtures.fixture();out=fixtures.snapshot();budget=fixtures.engine.evaluate(document,sources,True)
        context=fixtures.packet(sources,document,budget)
        receipt=fixtures.validator.recompute(fixtures.arithmetic_request(),context['sources'],fixtures.engine)
        receipt['request_id']='repair.step-0';context['calculations']=[receipt]
        good=fixtures.report_content_fixture(context);good['verdict']='insufficient_evidence'
        bad=copy.deepcopy(good);bad['observations'][0]['calculation_id']='duplicated_prefix__reconstructed_bits'
        request=repair(bad['observations'][0],good['observations'][0])
        invoke=fixtures.wire_provider(side_effect=[{'tool_request':fixtures.arithmetic_request()},{'final':bad},{'report_repair':request}])
        report,checked=session.run(out,'repair',sources,document,budget,invoke,1,max_report_repairs=1)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(invoke.call_count,3)
        prior=json.loads((out/'repair.step-1.canonical.json').read_text())
        expected=copy.deepcopy(prior);expected['observations']=good['observations'];self.assertEqual(report,expected)
        binding=json.loads((out/'repair.step-2.binding.json').read_text())
        self.assertEqual(binding['context']['target_sha256'],session.report_identity(prior))
        self.assertEqual(json.loads((out/'repair.step-2.model.json').read_text()),{'report_repair':request})
        self.assertEqual(json.loads((out/'repair.step-1.model.json').read_text()),{'final':bad})
        self.assertEqual(len(list(out.glob('repair*.tool.execution.json'))),1)
        replay=Mock(side_effect=AssertionError('saved bound frames must replay without provider calls'))
        self.assertEqual(session.run(out,'repair',sources,document,budget,replay,1,max_report_repairs=1)[0],report)
        replay.assert_not_called()

    def test_forged_numeric_claim_stays_invalid_after_reference_only_update(self):
        report=copy.deepcopy(self.bad);report['observations'][0]['actual']=255
        request=repair(report['observations'][0],dict(self.good['observations'][0],actual=255))
        result=self.apply(request,report)
        checked=fixtures.validator.validate(result,self.context)
        self.assertFalse(checked['ok']);self.assertTrue(any('reported value differs' in e for e in checked['errors']))


if __name__=='__main__':unittest.main()
