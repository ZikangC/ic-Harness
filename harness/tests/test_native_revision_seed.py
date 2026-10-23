"""Optional native seeds replay source bindings before existing design revision."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

import test_architecture as base
from harness.prompts import architecture as prompts
from harness.runtime import design_session as design
from harness.runtime import requirement_session as requirements
from harness.runtime import run_store as store
from harness.stages import architecture as controller


class NativeRevisionSeedTests(unittest.TestCase):
    def setup_case(self):
        document,sources=base.fixture()
        out=Path(tempfile.mkdtemp(prefix='native-revision-seed-test-'));self.addCleanup(shutil.rmtree,out)
        for name in ('microarchitecture-design','microarchitecture-audit'):
            shutil.copytree(Path(__file__).resolve().parents[1]/'skills'/name,
                            out/'snapshot/harness/skills'/name,ignore=shutil.ignore_patterns('__pycache__'))
        raw=base.requirements_declaration(document['requirements'])
        canonical,_,errors=requirements._materialize(requirements._checker(out),raw,sources)
        self.assertEqual(errors,[]);document['requirements']=canonical
        document['tool_receipt']={'design_sha256':'old-receipt-is-not-authority','calculated_by':'historical fixture'}
        fingerprints={name:store.digest(text.encode()) for name,text in sources.items()}
        feedback={'input_sha256':fingerprints,'originating_run':'isolated-native-audit',
            'authority':'Original inputs remain authoritative; historical declarations are fallible.',
            'native_draft':{key:copy.deepcopy(document[key]) for key in ('selected','candidates','architecture_md')},
            'native_audit':{'verdict':'revise','findings':[{'id':'historical-necessary-change','evidence':'native historical judgment'}]},
            'native_executions':[{'request':'historical-execution-context','result':{'status':'not current evidence'}}],
            'native_revision_seed':{'proposal':copy.deepcopy(document),'requirement_declaration':raw,
                                    'input_sha256':copy.deepcopy(fingerprints)}}
        return out,document,sources,feedback

    def test_seed_replays_raw_source_binding_without_model_or_fabricated_model_turn(self):
        out,document,sources,feedback=self.setup_case();before=copy.deepcopy(feedback)
        invoke=Mock(side_effect=AssertionError('seed replay must not invoke extraction model'))
        result,errors=requirements.prepare(out,sources,invoke,2,revision_feedback=feedback)
        self.assertEqual(errors,[]);self.assertEqual(result,document['requirements']);invoke.assert_not_called()
        self.assertEqual(feedback,before)
        record=json.loads((out/'extraction.seed.json').read_text())
        self.assertTrue(record['ok']);self.assertTrue(record['bindings'])
        self.assertEqual(record['receipt']['source_sha256'],feedback['input_sha256'])
        self.assertEqual(record['receipt']['declaration_sha256'],requirements.identity(feedback['native_revision_seed']['requirement_declaration']))
        self.assertEqual(record['proposal_sha256'],requirements.identity(document))
        self.assertEqual(json.loads((out/'extraction.json').read_text()),result)
        self.assertEqual(json.loads((out/'extraction.declaration.json').read_text()),feedback['native_revision_seed']['requirement_declaration'])
        self.assertEqual(json.loads((out/'extraction.receipt.json').read_text()),record['receipt'])
        self.assertFalse(list(out.glob('extract-*.reply.json')))
        self.assertFalse(list(out.glob('*.tool.result.json')))

    def test_seed_replay_context_is_preserved_on_resume(self):
        out,document,sources,feedback=self.setup_case();invoke=Mock()
        self.assertEqual(requirements.prepare(out,sources,invoke,0,revision_feedback=feedback),
                         (document['requirements'],[]))
        before={p.name:p.read_bytes() for p in out.glob('extraction*')}
        self.assertEqual(requirements.prepare(out,sources,invoke,0,revision_feedback=feedback),
                         (document['requirements'],[]))
        self.assertEqual(before,{p.name:p.read_bytes() for p in out.glob('extraction*')});invoke.assert_not_called()
        changed=copy.deepcopy(feedback);changed['native_revision_seed']['proposal']['selected']='large'
        with self.assertRaisesRegex(ValueError,'checkpoint differs'):
            requirements.prepare(out,sources,invoke,0,revision_feedback=changed)
        self.assertEqual(before,{p.name:p.read_bytes() for p in out.glob('extraction*')})

    def test_changed_source_or_inconsistent_fingerprints_fail_before_model(self):
        out,_,sources,feedback=self.setup_case();invoke=Mock()
        changed=dict(sources,**{'reference.py':sources['reference.py']+'# changed\n'})
        with self.assertRaisesRegex(ValueError,'fingerprint'):
            requirements.prepare(out,changed,invoke,0,revision_feedback=feedback)
        wrong=copy.deepcopy(feedback);wrong['native_revision_seed']['input_sha256']['spec.md']='different'
        with self.assertRaisesRegex(ValueError,'fingerprints'):
            store.feedback_revision_seed(wrong,sources)
        missing=copy.deepcopy(feedback);missing.pop('input_sha256')
        with self.assertRaisesRegex(ValueError,'fingerprints'):
            store.feedback_revision_seed(missing,sources)
        invoke.assert_not_called();self.assertFalse((out/'extraction.json').exists())

    def test_seed_transport_rejects_unknown_missing_fields_and_incomplete_proposal(self):
        _,_,sources,feedback=self.setup_case()
        for mutation in ('unknown','missing_raw','null_raw','incomplete'):
            with self.subTest(mutation=mutation):
                wrong=copy.deepcopy(feedback);seed=wrong['native_revision_seed']
                if mutation=='unknown':seed['extra_authority']=True
                elif mutation=='missing_raw':seed.pop('requirement_declaration')
                elif mutation=='null_raw':seed['requirement_declaration']=None
                else:seed['proposal'].pop('work_model')
                with self.assertRaises(ValueError):store.feedback_revision_seed(wrong,sources)

    def test_raw_tampering_and_proposal_value_change_are_rejected_without_fallback(self):
        for mutation in ('source_selector','raw_materialized','proposal_value'):
            with self.subTest(mutation=mutation):
                out,_,sources,feedback=self.setup_case();seed=feedback['native_revision_seed'];invoke=Mock()
                if mutation=='source_selector':seed['requirement_declaration']['facts'][0]['source']['number']=2
                elif mutation=='raw_materialized':seed['requirement_declaration']['facts'][0]['value']=3
                else:seed['proposal']['requirements']['facts'][0]['value']=1000
                result,errors=requirements.prepare(out,sources,invoke,1,revision_feedback=feedback)
                self.assertIsNone(result);self.assertTrue(errors);invoke.assert_not_called()
                self.assertFalse((out/'extraction.json').exists())
                failed=json.loads((out/'extraction.seed.json').read_text())
                self.assertFalse(failed['ok']);self.assertTrue(failed['errors'])

    def test_feedback_without_seed_keeps_normal_extraction_and_supplemental_context(self):
        out,document,sources,feedback=self.setup_case();feedback.pop('native_revision_seed')
        raw=base.requirements_declaration(document['requirements']);invoke=Mock(return_value={'requirements':raw})
        result,errors=requirements.prepare(out,sources,invoke,0,revision_feedback=feedback)
        self.assertEqual(errors,[]);self.assertEqual(result,document['requirements']);self.assertEqual(invoke.call_count,1)
        self.assertFalse((out/'extraction.seed.json').exists());self.assertTrue((out/'extract-0.reply.json').exists())
        prompt=prompts.design('SKILL',sources,result,feedback=feedback)
        self.assertIn('historical-necessary-change',prompt);self.assertIn('historical-execution-context',prompt)

    def test_prompt_omits_only_exact_duplicate_seed_transport_and_preserves_native_evidence(self):
        _,document,sources,feedback=self.setup_case();before=copy.deepcopy(feedback)
        prompt=prompts.design('SKILL',sources,document['requirements'],document,None,feedback)
        self.assertNotIn('"native_revision_seed"',prompt);self.assertNotIn('"native_draft"',prompt)
        self.assertIn('historical-necessary-change',prompt);self.assertIn('historical-execution-context',prompt)
        self.assertIn('isolated-native-audit',prompt);self.assertIn(feedback['input_sha256']['spec.md'],prompt)
        self.assertEqual(prompt.count(document['architecture_md']),1)
        self.assertNotIn(json.dumps(document['candidates'],ensure_ascii=False),prompt)
        self.assertEqual(feedback,before)
        altered=copy.deepcopy(feedback);altered['native_draft']['architecture_md']='NONIDENTICAL HISTORICAL DETAIL'
        self.assertIn('NONIDENTICAL HISTORICAL DETAIL',prompts.design('SKILL',sources,document['requirements'],document,None,altered))
        current={'audit':{'verdict':'revise','findings':[{'id':'current-independent-audit'}]}}
        later=prompts.design('SKILL',sources,document['requirements'],document,current,feedback)
        self.assertIn('current-independent-audit',later);self.assertNotIn('historical-execution-context',later)

    def test_seeded_generate_rechecks_current_budget_and_rejects_direct_final(self):
        out,document,sources,feedback=self.setup_case()
        canonical,errors=requirements.prepare(out,sources,Mock(),0,revision_feedback=feedback);self.assertEqual(errors,[])
        request={'tool':'replace_candidates','base_design_sha256':design.identity(document),'candidates':[],
                 'revision_response':['MODEL-REQUESTED-UNCHANGED-RESOURCE-RECHECK']}
        changed=design.replace_candidates(document,request)
        prompt=prompts.design('SKILL',sources,canonical,document,None,feedback)
        def invoke(name,role,message,parser):
            if name.endswith('step-0'):
                self.assertEqual(message.count(json.dumps(document['candidates'],ensure_ascii=False)),1)
                self.assertEqual(message.count(json.dumps(document['work_model'],ensure_ascii=False)),1)
                fresh=json.loads((out/'seeded.revision-base.result.json').read_text())
                self.assertTrue(fresh['ok'],fresh['errors'])
                self.assertNotEqual(fresh['design_sha256'],document['tool_receipt']['design_sha256'])
                return {'final':{'evaluated_design_sha256':fresh['design_sha256'],'architecture_md':document['architecture_md']}}
            if name.endswith('step-1'):
                self.assertIn('model-requested calculation',message)
                return parser('<<<BUDGET_REQUEST>>>\n'+json.dumps(request))
            return {'final':{'evaluated_design_sha256':design.identity(changed),'architecture_md':document['architecture_md']}}
        provider=Mock(side_effect=invoke)
        result=design.generate(out,'seeded',prompt,canonical,sources,provider,1,revision_base=document)
        self.assertEqual(provider.call_count,3);self.assertEqual(result['candidates'],document['candidates'])
        self.assertEqual(result['work_model'],document['work_model'])
        self.assertFalse((out/'seeded.step-0.tool.execution.json').exists())
        self.assertTrue((out/'seeded.step-1.tool.execution.json').exists())
        self.assertEqual(len(list(out.glob('seeded.step-*.tool.execution.json'))),1)
        self.assertNotIn('old-receipt-is-not-authority',json.dumps(result['tool_receipt']))

    def test_controller_only_passes_validated_seed_and_keeps_old_audit_as_supplemental(self):
        out,document,sources,feedback=self.setup_case();store.dump(out/'snapshot/feedback.json',feedback)
        settings=base.LoopTests().settings();calls=controller.CallBudget(out,settings)
        report={'verdict':'insufficient_evidence','checks':[],'findings':[],'unverified':[]}
        with patch.object(controller.audit_session,'prepare_baseline',return_value={'origin':'controller fixture'}):
            with patch.object(controller,'generate',return_value=copy.deepcopy(document)) as generate:
                with patch.object(controller.audit_session,'run',return_value=(report,{'ok':True})) as audit:
                    with patch.object(controller,'call_model',side_effect=AssertionError('no extraction model call with a valid seed')):
                        state=controller.execute(out,sources,{},out,settings,calls)
        self.assertEqual(state['status'],'stopped_audit_evidence')
        self.assertEqual(generate.call_args.kwargs['revision_base'],document)
        self.assertIn('historical-necessary-change',generate.call_args.args[2])
        self.assertIn('historical-execution-context',generate.call_args.args[2])
        self.assertIsNone(audit.call_args.args[7])
        self.assertTrue(json.loads((out/'design-0.final-gate.result.json').read_text())['ok'])


if __name__=='__main__':unittest.main()
