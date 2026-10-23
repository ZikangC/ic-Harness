"""Cross-audit revisions use the same whole-value replacement and hard gate."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

import test_architecture as base
from harness.prompts import architecture as prompts
from harness.runtime import design_session as design
from harness.runtime import run_store as store
from harness.stages import architecture as controller


def work_replacement(document):
    work=copy.deepcopy(document['work_model'])
    work['items'][0]['count']['operations']=160
    return {'tool':'replace_candidates','base_design_sha256':design.identity(document),'candidates':[],
            'work_model':work,'work_model_revision':{'reason':'new independently checked source count',
            'references':[{'file':'spec.md','quote':'items 12'}]}}


class RevisionBaseTests(unittest.TestCase):
    def setup_case(self):
        document,sources=base.fixture()
        return document,sources,base.SkillToolTests().setup_run()

    def final(self,document):
        return {'final':{'evaluated_design_sha256':design.identity(document),'architecture_md':document['architecture_md']}}

    def test_first_turn_complete_work_value_replacement_preserves_candidates_and_recomputes_all(self):
        document,sources,out=self.setup_case();before=copy.deepcopy(document)
        request=work_replacement(document);changed=design.replace_candidates(document,request)
        prompt=prompts.design('DESIGN SKILL',sources,document['requirements'],document,{'audit':{'verdict':'revise'}})
        def invoke(name,role,message,parser):
            if name.endswith('step-0'):
                self.assertIn('empty candidates array',message)
                data=json.loads(message.split('\nCURRENT EVALUATED DATA\n',1)[1].split('\nVIEW NOTE:',1)[0])
                self.assertEqual(data['candidates'],document['candidates'])
                self.assertNotIn('architecture_md',data);self.assertNotIn('tool_receipt',data)
                self.assertEqual(message.count(json.dumps(document['candidates'],ensure_ascii=False)),1)
                self.assertEqual(message.count(document['architecture_md']),1)
                initial=json.loads((out/'revision.revision-base.result.json').read_text())
                self.assertEqual(initial['design_sha256'],design.identity(document))
                self.assertTrue(initial['ok'],initial['errors'])
                return parser('<<<BUDGET_REQUEST>>>\n'+json.dumps(request))
            report=json.loads((out/'revision.step-0.tool.result.json').read_text())
            self.assertTrue(report['ok'],report['errors'])
            self.assertEqual(set(report['candidates']),{'small','large'})
            for result in report['candidates'].values():
                self.assertEqual(result['ledger']['cost_groups'][0]['operations'],160)
            return self.final(dict(changed,architecture_md=document['architecture_md']))
        returned=design.generate(out,'revision',prompt,document['requirements'],sources,invoke,1,revision_base=document)
        self.assertEqual(returned['candidates'],document['candidates']);self.assertEqual(returned['work_model'],request['work_model'])
        self.assertEqual(document,before)
        self.assertEqual(len(list(out.glob('revision*.tool.execution.json'))),1)
        execution=json.loads((out/'revision.revision-base.execution.json').read_text())
        self.assertEqual(execution['request_sha256'],store.digest((out/'revision.revision-base.request.json').read_bytes()))
        self.assertEqual(execution['script_sha256'],store.digest((out/'snapshot/harness/skills/microarchitecture-design/scripts/evaluate.py').read_bytes()))

    def test_rechecked_base_cannot_deliver_old_markdown_without_new_request(self):
        document,sources,out=self.setup_case();request=work_replacement(document)
        changed=design.replace_candidates(document,request)
        invoke=Mock(side_effect=[self.final(document),{'tool_request':request},self.final(dict(changed,architecture_md=document['architecture_md']))])
        result=design.generate(out,'early','',document['requirements'],sources,invoke,1,revision_base=document)
        self.assertEqual(invoke.call_count,3)
        self.assertIn('model-requested calculation',invoke.call_args_list[1].args[2])
        self.assertFalse((out/'early.step-0.tool.result.json').exists())
        self.assertEqual(design.identity(result),design.identity(changed))

    def test_stale_hash_is_rejected_without_spending_a_calculation(self):
        document,sources,out=self.setup_case();request=work_replacement(document)
        stale=dict(request,base_design_sha256='not-the-bound-base')
        changed=design.replace_candidates(document,request)
        invoke=Mock(side_effect=[{'tool_request':stale},{'tool_request':request},self.final(dict(changed,architecture_md=document['architecture_md']))])
        result=design.generate(out,'stale','',document['requirements'],sources,invoke,1,revision_base=document)
        self.assertIn('revision base does not match',invoke.call_args_list[1].args[2])
        self.assertFalse((out/'stale.step-0.tool.execution.json').exists())
        self.assertEqual(len(list(out.glob('stale*.tool.execution.json'))),1)
        self.assertEqual(design.identity(result),design.identity(changed))

    def test_resume_replays_original_base_and_completed_replacement_without_extra_model_turns(self):
        document,sources,out=self.setup_case();request=work_replacement(document)
        changed=design.replace_candidates(document,request)
        first=Mock(side_effect=[{'tool_request':request},RuntimeError('interrupted before final')])
        with self.assertRaises(RuntimeError):
            design.generate(out,'resume','',document['requirements'],sources,first,1,revision_base=document)
        self.assertEqual(json.loads((out/'work-model.current.json').read_text())['work_model'],changed['work_model'])
        resumed=Mock(return_value=self.final(dict(changed,architecture_md=document['architecture_md'])))
        result=design.generate(out,'resume','',document['requirements'],sources,resumed,1,revision_base=document)
        self.assertEqual(resumed.call_count,1);self.assertEqual(resumed.call_args.args[0],'resume.step-1')
        self.assertIn('Tool-call limit reached',resumed.call_args.args[2])
        self.assertEqual(len(list(out.glob('resume*.tool.execution.json'))),1)
        self.assertEqual(design.identity(result),design.identity(changed))

    def test_resume_rejects_changed_or_omitted_base_and_sources_before_provider(self):
        document,sources,out=self.setup_case();invoke=Mock(side_effect=RuntimeError('stop before first model response'))
        with self.assertRaises(RuntimeError):
            design.generate(out,'bound','',document['requirements'],sources,invoke,1,revision_base=document)
        changed=copy.deepcopy(document);changed['selected']='large'
        contexts=[(changed,sources),(document,dict(sources,**{'reference.py':'changed original source'})),(None,sources)]
        for revision,context in contexts:
            with self.subTest(base=revision,context=context):
                resumed=Mock(side_effect=AssertionError('context mismatch must not invoke model'))
                with self.assertRaisesRegex(ValueError,'revision-base checkpoint'):
                    design.generate(out,'bound','',document['requirements'],context,resumed,1,revision_base=revision)
                resumed.assert_not_called()

    def test_frozen_requirements_are_not_replaced_by_revision_base(self):
        document,sources,out=self.setup_case();changed=copy.deepcopy(document);changed['requirements']['constraints']=[]
        invoke=Mock()
        with self.assertRaisesRegex(ValueError,'same frozen requirements'):
            design.generate(out,'requirements','',document['requirements'],sources,invoke,1,revision_base=changed)
        invoke.assert_not_called();self.assertFalse(list(out.glob('requirements*.execution.json')))

    def test_unchanged_invalid_sibling_is_rechecked_after_empty_candidate_update(self):
        document,sources,out=self.setup_case();document['candidates'][1]['resource_checks'][0]['demand_per_cycle']='64'
        request=work_replacement(document);invoke=Mock(return_value={'tool_request':request})
        with self.assertRaisesRegex(design.DesignToolLimit,'invalid data'):
            design.generate(out,'sibling','',document['requirements'],sources,invoke,1,revision_base=document)
        self.assertEqual(invoke.call_count,1)
        report=json.loads((out/'sibling.step-0.tool.result.json').read_text())
        self.assertFalse(report['ok']);self.assertEqual(set(report['candidates']),{'small','large'})
        self.assertTrue(any('large' in error and 'declared demand exceeds service' in error for error in report['errors']))

    def test_revised_session_does_not_gain_extra_calculation_requests(self):
        document,sources,out=self.setup_case();request=work_replacement(document)
        changed=design.replace_candidates(document,request)
        extra=dict(request,base_design_sha256=design.identity(changed),work_model_revision={'reason':'further wording','references':[{'file':'spec.md','quote':'items 12'}]})
        invoke=Mock(side_effect=[{'tool_request':request},{'tool_request':extra}])
        with self.assertRaisesRegex(design.DesignToolLimit,'calculation request limit'):
            design.generate(out,'cap','',document['requirements'],sources,invoke,1,revision_base=document)
        self.assertEqual(len(list(out.glob('cap*.tool.execution.json'))),1)

    def test_misnested_wrapper_reports_actual_extra_keys_without_relocating(self):
        request={'tool':'evaluate_design','document':{'candidates':[]},'work_model':{'items':[]},'work_model_revision':{'basis':'not a recognized field','citations':[]}}
        before=copy.deepcopy(request)
        with self.assertRaisesRegex(ValueError,"wrapper extra keys=\\['work_model', 'work_model_revision'\\]"):
            design.parse_turn('<<<BUDGET_REQUEST>>>\n'+json.dumps(request))
        self.assertEqual(request,before)
        wrong={'tool':'evaluate_design','document':[]}
        with self.assertRaisesRegex(ValueError,'one document object'):
            design.parse_turn('<<<BUDGET_REQUEST>>>\n'+json.dumps(wrong))


class RevisionPromptTests(unittest.TestCase):
    def test_current_audit_is_complete_and_older_supplemental_is_omitted(self):
        document,sources=base.fixture();findings={'audit':{'verdict':'revise','findings':[{'id':'native-new','evidence':'entire current native finding','required_change':'complete evidence'}]},'deterministic':{'errors':['all current checks']}}
        prompt=prompts.design('SKILL',sources,document['requirements'],document,findings,{'old':'OUTDATED_FEEDBACK'})
        self.assertNotIn('OUTDATED_FEEDBACK',prompt);self.assertNotIn('PREVIOUS DESIGN',prompt)
        self.assertEqual(json.loads(prompt.split('\nUNRESOLVED CHECKS/AUDIT\n',1)[1].split('\nPREVIOUS UNIFIED ARCHITECTURE DOCUMENT',1)[0]),findings)
        self.assertNotIn('"candidates"',prompt);self.assertIn('CURRENT EVALUATED DATA',prompt)
        self.assertEqual(prompt.count(document['architecture_md']),1)

    def test_supplemental_is_retained_when_no_current_audit_exists(self):
        document,sources=base.fixture();feedback={'native_review':'FIRST_RUN_FEEDBACK'}
        for previous,findings in ((None,None),(document,None),(document,{})):
            with self.subTest(previous=previous,findings=findings):
                prompt=prompts.design('SKILL',sources,document['requirements'],previous,findings,feedback)
                self.assertIn('FIRST_RUN_FEEDBACK',prompt)

    def test_controller_passes_exact_previous_document_only_to_revision_generate(self):
        document,sources=base.fixture();revise=base.review();revise['verdict']='revise'
        revise['findings']=[{'id':'native-revision','severity':'blocking','disposition':'open','category':'design','location':'synthetic ABI','issue':'missing implementation evidence','evidence':'original native report fixture','required_change':'complete actual contract'}]
        policy=base.LoopTests();out=policy.setup_run();self.addCleanup(policy.doCleanups);settings=policy.settings()
        with patch.object(controller,'generate',side_effect=[copy.deepcopy(document),copy.deepcopy(document)]) as generate:
            with patch.object(controller,'call_model',side_effect=[{'requirements':base.requirements_declaration(document['requirements'])},revise,base.review()]):
                result=controller.execute(out,sources,{},out,settings,controller.CallBudget(out,settings))
        self.assertEqual(result['status'],'accepted_conditional_design')
        self.assertIsNone(generate.call_args_list[0].kwargs['revision_base'])
        self.assertEqual(generate.call_args_list[1].kwargs['revision_base'],document)
        self.assertIn('native-revision',generate.call_args_list[1].args[2])


if __name__=='__main__':unittest.main()
