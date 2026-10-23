"""Whole-candidate transport and executed work summaries preserve full checks."""
import copy
import json
import unittest
from unittest.mock import Mock

import test_architecture as base
from harness.runtime import design_session as design


def replacement(document,candidates=(),**updates):
    return {'tool':'replace_candidates','base_design_sha256':design.identity(document),
            'candidates':copy.deepcopy(list(candidates)),**copy.deepcopy(updates)}


def executed_summary(prompt):
    return json.loads(prompt.split('\nEXECUTED DECLARED-WORK SUMMARY\n',1)[1].split('\nFEEDBACK SCOPE:',1)[0])


class CandidateTransportTests(unittest.TestCase):
    def test_design_views_preserve_current_evidence_and_all_historical_failures(self):
        document,sources=base.fixture();old={'ok':False,'errors':['a deficit','bad declaration'],
            'gate':{'status':'unknown','unsupported_models':['unpriced']},'design_sha256':'earlier',
            'unchecked':['tail schedule'],'scope':'declared only','candidates':{'old':{'huge_ledger':[1,2,3]}}}
        current={'ok':True,'errors':[],'gate':{'status':'unknown','unsupported_models':['unpriced']},
                 'design_sha256':'current','candidates':{'small':{'values':{'actual':7}}}}
        history=[{'request':{'document':document},'result':old,'feedback':{'issues':['repair']}},
                 {'format_error':'provider failed'}, {'result':current,'feedback':{'issues':['unknown remains']}}]
        before=copy.deepcopy(history);view=design.design_history_view(history)
        self.assertEqual(history,before);self.assertEqual(view[-1]['result'],current)
        for key in ('errors','gate','design_sha256','unchecked','scope'):self.assertEqual(view[0]['result'][key],old[key])
        self.assertEqual(view[0]['feedback'],history[0]['feedback']);self.assertNotIn('candidates',view[0]['result'])
        self.assertEqual(view[1],history[1]);self.assertNotIn('feedback',view[-1]);self.assertNotIn('request',view[0])
        original=copy.deepcopy(document);data=design.design_data_view(document,document['requirements'],document['work_model'])
        self.assertNotIn('requirements',data);self.assertNotIn('work_model',data)
        self.assertEqual(data['candidates'],document['candidates']);self.assertEqual(document,original)
        different=copy.deepcopy(document);different['requirements']={'conflicts':['do not hide']}
        different['work_model']={'items':['new interpretation']}
        self.assertEqual(design.design_data_view(different,document['requirements'],document['work_model']),different)

    def test_generated_prompt_has_one_current_result_and_no_duplicate_data(self):
        document,sources=base.fixture();out=base.SkillToolTests().setup_run();seen=[]
        def invoke(name,role,prompt,parser):
            seen.append(prompt)
            if name.endswith('step-0'):return {'tool_request':{'tool':'evaluate_design','document':copy.deepcopy(document)}}
            history=json.loads(prompt.split('\nSKILL TOOL HISTORY (current data shown once)\n')[1].split('\nEXECUTED DECLARED-WORK SUMMARY\n')[0])
            report=json.loads((out/'packed.step-0.tool.result.json').read_text())
            self.assertEqual(history[0]['result'],report)
            data=json.loads(prompt.split('\nCURRENT EVALUATED DATA\n')[1].split('\nVIEW NOTE:')[0])
            self.assertEqual(data['candidates'],document['candidates']);self.assertNotIn('requirements',data);self.assertNotIn('work_model',data)
            self.assertIn(report['design_sha256'],prompt);self.assertIn('CURRENT CALCULATION FEEDBACK',prompt)
            return {'final':{'evaluated_design_sha256':design.identity(document),'architecture_md':document['architecture_md']}}
        result=design.generate(out,'packed','PROMPT',document['requirements'],sources,invoke,1)
        self.assertEqual(design.identity(result),design.identity(document));self.assertEqual(len(seen),2)

    def test_whole_replacement_preserves_siblings_sources_and_caller_values(self):
        document,sources=base.fixture();before=copy.deepcopy(document);source_before=copy.deepcopy(sources)
        candidate=copy.deepcopy(document['candidates'][0]);candidate['assumptions'][0]['value']=1.75
        request=replacement(document,[candidate]);request_before=copy.deepcopy(request)
        result=design.replace_candidates(document,request)
        self.assertEqual(result['candidates'][0],candidate)
        self.assertEqual(result['candidates'][1],document['candidates'][1])
        for key in ('requirements','work_model','selected','architecture_md'):self.assertEqual(result[key],document[key])
        self.assertEqual(document,before);self.assertEqual(sources,source_before);self.assertEqual(request,request_before)
        request['candidates'][0]['assumptions'][0]['value']=99
        self.assertEqual(result['candidates'][0]['assumptions'][0]['value'],1.75)

    def test_omitted_candidate_fields_are_not_deep_merged(self):
        document,sources=base.fixture();request=replacement(document,[{'id':'small','assumptions':[]}])
        result=design.replace_candidates(document,request)
        self.assertEqual(result['candidates'][0],{'id':'small','assumptions':[]})
        self.assertNotIn('macros',result['candidates'][0]);self.assertEqual(result['candidates'][1],document['candidates'][1])
        out=base.SkillToolTests().setup_run();report=design.execute_tool(out,'omitted',result,sources)
        self.assertFalse(report['ok']);self.assertEqual(report['gate']['status'],'fail')

    def test_stale_duplicate_unknown_and_invalid_candidate_ids_are_rejected(self):
        document,_=base.fixture();candidate=copy.deepcopy(document['candidates'][0]);candidate['assumptions'][0]['value']=1.75
        cases=[replacement(document,[candidate],base_design_sha256='stale'),
               replacement(document,[candidate,candidate]),
               replacement(document,[dict(candidate,id='unknown')]),
               replacement(document,[dict(candidate,id='')]),
               replacement(document,[dict(candidate,id=7)]),
               replacement(document,[None])]
        before=copy.deepcopy(document)
        for request in cases:
            with self.subTest(request=request):
                with self.assertRaises(ValueError):design.replace_candidates(document,request)
                self.assertEqual(document,before)

    def test_source_receipt_and_removed_pointer_protocol_are_not_editable(self):
        document,_=base.fixture();candidate=copy.deepcopy(document['candidates'][0]);candidate['assumptions'][0]['value']=1.75
        for key,value in [('requirements',{}),('sources',{}),('architecture_md','changed'),('tool_receipt',{}),('changes',[])]:
            request=replacement(document,[candidate],**{key:value})
            with self.subTest(key=key):
                with self.assertRaises(ValueError):design.replace_candidates(document,request)
                with self.assertRaises(ValueError):design.parse_turn('<<<BUDGET_REQUEST>>>\n'+json.dumps(request))
        old={'tool':'revise_design','base_design_sha256':design.identity(document),'changes':[{'path':'/candidates/0/assumptions/0/value','value':1.75}]}
        with self.assertRaises(ValueError):design.parse_turn('<<<BUDGET_REQUEST>>>\n'+json.dumps(old))

    def test_noop_replacements_are_rejected_and_selection_only_is_explicit(self):
        document,sources=base.fixture()
        for request in [replacement(document),replacement(document,[document['candidates'][0]]),replacement(document,selected='small')]:
            with self.subTest(request=request):
                with self.assertRaises(ValueError):design.replace_candidates(document,request)
        request=replacement(document,selected='large');changed=design.replace_candidates(document,request)
        self.assertEqual(changed['candidates'],document['candidates']);self.assertEqual(document['selected'],'small')
        self.assertEqual(changed['selected'],'large')
        out=base.SkillToolTests().setup_run();report=design.execute_tool(out,'selection',changed,sources)
        self.assertTrue(report['ok'],report['errors']);self.assertEqual(report['gate']['status'],'fail')
        with self.assertRaises(ValueError):design.replace_candidates(document,replacement(document,selected='unknown'))

    def test_first_candidate_prompt_contains_actual_script_summary(self):
        document,sources=base.fixture();out=base.SkillToolTests().setup_run();seen=[]
        def invoke(name,role,prompt,parser):
            seen.append(name)
            summary=executed_summary(prompt)
            self.assertEqual(summary['profiles'][0]['by_unit'][0]['operations'],80)
            saved=json.loads((out/'summary.work-summary.result.json').read_text())
            self.assertEqual(summary,saved['compact_summary'])
            execution=json.loads((out/'summary.work-summary.execution.json').read_text())
            self.assertEqual(execution['exit_code'],0)
            self.assertEqual(json.loads((out/'summary.work-summary.request.json').read_text())['work_model'],document['work_model'])
            if name.endswith('step-0'):return {'tool_request':{'tool':'evaluate_design','document':copy.deepcopy(document)}}
            return {'final':{'evaluated_design_sha256':design.identity(document),'architecture_md':document['architecture_md']}}
        result=design.generate(out,'summary','PROMPT',document['requirements'],sources,invoke,1)
        self.assertEqual(seen,['summary.step-0','summary.step-1']);self.assertIn('tool_receipt',result)

    def revised_work(self,document):
        work=copy.deepcopy(document['work_model']);work['items'][0]['count']['operations']=160
        request=replacement(document,work_model=work,work_model_revision={'reason':'synthetic source-backed counting correction; semantics remain an independent audit obligation','references':[{'file':'spec.md','quote':'items 12'}]})
        return request,design.replace_candidates(document,request)

    def test_work_revision_refreshes_summary_from_executed_calculation(self):
        document,sources=base.fixture();before=copy.deepcopy(sources);out=base.SkillToolTests().setup_run()
        request,changed=self.revised_work(document);summaries=[]
        def invoke(name,role,prompt,parser):
            summaries.append(executed_summary(prompt))
            if name.endswith('step-0'):return {'tool_request':{'tool':'evaluate_design','document':copy.deepcopy(document)}}
            if name.endswith('step-1'):return {'tool_request':copy.deepcopy(request)}
            report=json.loads((out/'revision.step-1.tool.result.json').read_text())
            self.assertTrue(report['ok'],report['errors']);self.assertEqual(summaries[-1],report['work_model']['compact_summary'])
            self.assertEqual(report['candidates']['small']['ledger']['cost_groups'][0]['operations'],160)
            return {'final':{'evaluated_design_sha256':design.identity(changed),'architecture_md':document['architecture_md']}}
        result=design.generate(out,'revision','PROMPT',document['requirements'],sources,invoke,2)
        self.assertEqual([row['profiles'][0]['by_unit'][0]['operations'] for row in summaries],[80,80,160])
        self.assertEqual(result['work_model'],changed['work_model']);self.assertEqual(sources,before)
        self.assertEqual(json.loads((out/'work-model.current.json').read_text())['work_model'],changed['work_model'])

    def test_work_replacement_cannot_reuse_an_earlier_revision_declaration(self):
        document,sources=base.fixture();out=base.SkillToolTests().setup_run()
        document['work_model_revision']={'reason':'earlier independent source interpretation correction','references':[{'file':'spec.md','quote':'items 12'}]}
        report=design.execute_tool(out,'previous-revision',document,sources)
        self.assertTrue(report['ok'],report['errors']);self.assertTrue(report['work_model']['revision_ok'])
        work=copy.deepcopy(document['work_model']);work['items'][0]['count']['operations']=160
        request=replacement(document,work_model=work);before=copy.deepcopy(document)
        with self.assertRaisesRegex(ValueError,'its own work_model_revision'):design.replace_candidates(document,request)
        self.assertEqual(document,before)
        request['work_model_revision']={'reason':'new source interpretation requires this separate repetition correction','references':[{'file':'spec.md','quote':'items 12'}]}
        changed=design.replace_candidates(document,request)
        report=design.execute_tool(out,'new-revision',changed,sources)
        self.assertTrue(report['ok'],report['errors']);self.assertTrue(report['work_model']['revision_ok'])
        self.assertEqual(report['work_model']['counts']['primary']['arithmetic']['operations'],160)
        self.assertEqual(changed['work_model_revision'],request['work_model_revision']);self.assertEqual(document,before)
        request['work_model_revision']['references'][0]['quote']='invented original input'
        bad=design.replace_candidates(document,request)
        checked=design.execute_tool(out,'bad-new-reference',bad,sources)
        self.assertFalse(checked['ok']);self.assertFalse(checked['work_model']['revision_ok'])

    def test_resume_replays_work_revision_without_repeating_completed_model_turns(self):
        document,sources=base.fixture();out=base.SkillToolTests().setup_run();request,changed=self.revised_work(document)
        first=Mock(side_effect=[{'tool_request':{'tool':'evaluate_design','document':copy.deepcopy(document)}},
                                {'tool_request':request},RuntimeError('provider interrupted before final')])
        with self.assertRaises(RuntimeError):design.generate(out,'resume','PROMPT',document['requirements'],sources,first,2)
        self.assertEqual(first.call_count,3)
        self.assertTrue((out/'resume.step-0.tool.result.json').exists());self.assertTrue((out/'resume.step-1.tool.result.json').exists())
        self.assertEqual(json.loads((out/'work-model.current.json').read_text())['work_model'],changed['work_model'])
        def invoke(name,role,prompt,parser):
            self.assertEqual(name,'resume.step-2')
            self.assertEqual(executed_summary(prompt)['profiles'][0]['by_unit'][0]['operations'],160)
            return {'final':{'evaluated_design_sha256':design.identity(changed),'architecture_md':document['architecture_md']}}
        resumed=Mock(side_effect=invoke);result=design.generate(out,'resume','PROMPT',document['requirements'],sources,resumed,2)
        self.assertEqual(resumed.call_count,1);self.assertEqual(design.identity(result),design.identity(changed))

    def test_candidate_replacement_always_recalculates_invalid_siblings(self):
        document,sources=base.fixture();document['candidates'][1]['resource_checks'][0]['demand_per_cycle']='64'
        candidate=copy.deepcopy(document['candidates'][0]);candidate['assumptions'][0]['value']=1.75
        request=replacement(document,[candidate]);out=base.SkillToolTests().setup_run()
        invoke=Mock(side_effect=[{'tool_request':{'tool':'evaluate_design','document':document}},{'tool_request':request}])
        with self.assertRaisesRegex(design.DesignToolLimit,'invalid data'):design.generate(out,'siblings','PROMPT',document['requirements'],sources,invoke,2)
        report=json.loads((out/'siblings.step-1.tool.result.json').read_text())
        self.assertFalse(report['ok']);self.assertEqual(set(report['candidates']),{'small','large'})
        self.assertTrue(any('large' in error and 'declared demand exceeds service' in error for error in report['errors']))
        self.assertEqual(invoke.call_count,2)

    def test_invalid_candidate_count_cannot_bypass_full_set_checks(self):
        for number in (1,5):
            with self.subTest(number=number):
                document,sources=base.fixture();document['candidates']=[dict(copy.deepcopy(document['candidates'][0]),id='choice'+str(i)) for i in range(number)];document['selected']='choice0'
                candidate=copy.deepcopy(document['candidates'][0]);candidate['assumptions'][0]['value']=1.75
                request=replacement(document,[candidate]);out=base.SkillToolTests().setup_run()
                invoke=Mock(side_effect=[{'tool_request':{'tool':'evaluate_design','document':document}},{'tool_request':request}])
                with self.assertRaisesRegex(design.DesignToolLimit,'invalid data'):design.generate(out,'count','PROMPT',document['requirements'],sources,invoke,2)
                report=json.loads((out/'count.step-1.tool.result.json').read_text())
                self.assertFalse(report['ok']);self.assertIn('need 2-4 distinct candidates',report['errors'])
                self.assertEqual(len(report['candidates']),number);self.assertEqual(invoke.call_count,2)


if __name__=='__main__':unittest.main()
