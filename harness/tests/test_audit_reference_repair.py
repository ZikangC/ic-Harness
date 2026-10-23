"""Stable evidence repairs and executed source-only baseline transport."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock

import test_audit as fixtures
from harness.prompts.architecture import audit as audit_prompt
from harness.runtime import audit_session as session


class ReferenceRepairTests(unittest.TestCase):
    def setUp(self):
        self.context=fixtures.context_fixture()
        self.report=fixtures.report_fixture(self.context)
        self.refs=[{'file':'spec.md','start':1,'end':1},{'file':'architecture.md','start':1,'end':1}]

    def repair(self,rows,previous=None,**extra):
        previous=self.report if previous is None else previous
        return session.repair_report(previous,dict(reference_replacements=rows,**extra),self.context['citations'],self.context['sources'])

    def test_missing_or_empty_references_replaced_without_changing_judgment(self):
        for initial in (None,[]):
            report=copy.deepcopy(self.report)
            row=report['checks'][0]
            if initial is None:row.pop('references')
            else:row['references']=initial
            before=copy.deepcopy(report)
            replacement={'collection':'checks','topic':row['topic'],'references':self.refs}
            repaired=self.repair([replacement],report)
            self.assertEqual(report,before)
            expected=copy.deepcopy(before);expected['checks'][0]['references']=self.refs
            self.assertEqual(repaired,expected)
            checked=fixtures.validator.validate(repaired,self.context)
            self.assertTrue(checked['ok'],checked['errors'])

    def test_requirement_id_replacement_preserves_siblings_and_frozen_fields(self):
        row=self.report['requirements'][0];before=copy.deepcopy(self.report)
        repaired=self.repair([{'collection':'requirements','id':row['id'],'references':self.refs}])
        expected=copy.deepcopy(before);expected['requirements'][0]['references']=self.refs
        self.assertEqual(repaired,expected);self.assertEqual(self.report,before)

    def test_invalid_or_overlapping_targets_and_judgment_fields_rejected(self):
        row=self.report['checks'][0]
        base={'collection':'checks','topic':row['topic'],'references':self.refs}
        cases=[[dict(base,status='pass')],[dict(base,topic='absent')],[base,base],
               [{'collection':'findings','id':'absent','references':self.refs}],
               [dict(base,references=[])],[dict(base,references=[{'file':'spec.md','start':999,'end':999}])],
               [dict(base,references=[{'evidence_id':'invented'}])]]
        for rows in cases:
            with self.subTest(rows=rows),self.assertRaises(ValueError):self.repair(rows)
        with self.assertRaises(ValueError):self.repair([base],checks=[copy.deepcopy(row)])
        with self.assertRaises(ValueError):self.repair([base],reference_updates=[{'collection':'checks','row_index':0,'reference_index':0,'reference':self.refs[0]}])
        with self.assertRaises(ValueError):session.repair_report(self.report,{'base_report_sha256':'stale','reference_replacements':[base]},self.context['citations'],self.context['sources'])

    def test_realization_review_reference_repair_preserves_review_judgment(self):
        report=copy.deepcopy(self.report)
        report['realization_reviews']=[{'count_check_id':'changed_path','status':'unverified','evidence':'Semantic equivalence not established','references':[]}]
        original=copy.deepcopy(report)
        replacement={'collection':'realization_reviews','count_check_id':'changed_path','references':self.refs}
        repaired=session.repair_report(report,{'reference_replacements':[replacement]},self.context['citations'],self.context['sources'])
        expected=copy.deepcopy(original);expected['realization_reviews'][0]['references']=self.refs
        self.assertEqual(repaired,expected);self.assertEqual(report,original)
        for field,value in (('status','reviewed'),('evidence','new engineering conclusion')):
            with self.subTest(field=field),self.assertRaises(ValueError):
                session.repair_report(report,{'reference_replacements':[dict(replacement,**{field:value})]},self.context['citations'],self.context['sources'])
        with self.assertRaises(ValueError):
            session.repair_report(report,{'realization_reviews':[dict(report['realization_reviews'][0],status='reviewed')]},self.context['citations'],self.context['sources'])

    def test_roles_still_require_actual_input_and_proposal_evidence(self):
        target=self.report['checks'][0]['topic']
        repaired=self.repair([{'collection':'checks','topic':target,'references':[self.refs[0]]}])
        checked=fixtures.validator.validate(repaired,self.context)
        self.assertFalse(checked['ok'])
        diagnostic=next(row for row in checked['reference_diagnostics'] if row.get('topic')==target)
        self.assertEqual(diagnostic['collection'],'checks');self.assertEqual(diagnostic['reference_count'],1)
        self.assertEqual(diagnostic['missing_roles'],['proposal'])
        self.assertEqual(set(diagnostic['required_roles']),{'input','proposal'})
        self.assertEqual(session.report_feedback(repaired,checked)['reference_diagnostics'],checked['reference_diagnostics'])

    def test_current_step_has_one_protocol_and_format_error_is_not_report_mode(self):
        args=dict(skill='',sources={},history=[{'kind':'report_format_repair','errors':['JSON typo']}])
        prompt=audit_prompt(**args)
        self.assertEqual(prompt.count('CURRENT STEP:'),1);self.assertIn('CURRENT STEP: return ONLY <<<AUDIT_TOOL_REQUEST>>>',prompt)
        self.assertNotIn('REPORT REPAIR:',prompt)
        prompt=audit_prompt(**args,tool_limit=True)
        self.assertEqual(prompt.count('CURRENT STEP:'),1);self.assertIn('CURRENT STEP: return ONLY <<<AUDIT_REPORT>>>',prompt)
        prompt=audit_prompt(**args,tool_limit=True,report_repair_context={})
        self.assertEqual(prompt.count('CURRENT STEP:'),1);self.assertIn('CURRENT STEP: return ONLY <<<AUDIT_REPAIR>>>',prompt)
        self.assertNotIn('CURRENT STEP: return ONLY <<<AUDIT_REPORT>>>',prompt)
        self.assertIn('reference_replacements',prompt)


class QuantitativeBaselineTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup);self.out=Path(self.directory.name)
        for name in ('microarchitecture-design','microarchitecture-audit'):
            shutil.copytree(fixtures.BASE/'skills'/name,self.out/'snapshot/harness/skills'/name,ignore=shutil.ignore_patterns('__pycache__'))
        self.base=self.out/'snapshot/harness/skills/microarchitecture-audit'
        from test_audit_source_model import witness
        model,self.sources=witness()
        self.sources['spec.md']='rows=7\ninner=5\ntokens=2\nheads=1\noutput int32\n'
        self.sources['architecture.md']='PROPOSAL_SENTINEL'
        self.baseline=fixtures.baseline_fixture(self.sources)
        self.baseline['analysis']=model
        fixtures.freeze_snapshot(self.out,self.sources)

    def test_same_response_analysis_executes_original_only_and_reuses_exact_evidence(self):
        original=copy.deepcopy(self.baseline);seen=[]
        def invoke(name,role,prompt,parser):
            seen.append(prompt);self.assertEqual(role,'review');self.assertNotIn('PROPOSAL_SENTINEL',prompt)
            self.assertIn('SOURCE QUANTITY SELECTORS',prompt)
            return fixtures.wire_reply(copy.deepcopy(self.baseline),parser)
        record=session.prepare_baseline(self.out,'source',self.sources,invoke,1)
        self.assertEqual(len(seen),1);self.assertEqual(self.baseline,original);self.assertEqual(record['baseline'],original)
        self.assertTrue(record['analysis_result']['ok'])
        self.assertEqual(record['analysis_result']['workloads'],{'p':2})
        self.assertTrue(record['source_execution']['coverage_complete']);self.assertEqual(record['source_execution']['errors'],[])
        request_paths=list(self.out.glob('source.baseline-0.analysis-*.request.json'));self.assertEqual(len(request_paths),1)
        request=json.loads(request_paths[0].read_text());self.assertEqual(request['model'],original['analysis'])
        self.assertEqual(set(request['sources']),{'spec.md','reference.py'})
        self.assertEqual(record['analysis_request_sha256'],session.report_identity(request))
        self.assertTrue(request_paths[0].with_name(request_paths[0].name.replace('.request.json','.execution.json')).exists())
        forbid=Mock(side_effect=AssertionError('cache reuse must not call a model'))
        self.assertEqual(session.prepare_baseline(self.out,'next',self.sources,forbid,1),record);forbid.assert_not_called()
        document,sources=fixtures.fixture();packet=session.packet(sources,document,fixtures.engine.evaluate(document,sources),baseline=record)
        self.assertEqual(packet['source_analysis'],record['analysis_result'])
        self.assertEqual(packet['source_execution'],record['source_execution'])
        compact=json.loads(packet['sources']['source-analysis.json'])
        self.assertEqual(compact['kind'],'host_verified_source_view')
        self.assertEqual(compact['verified_data'],record['source_execution']['verified_data'])
        self.assertNotIn('subreceipts',compact);self.assertNotIn('binding',compact)

    def test_failed_analysis_requires_full_source_revision_with_same_call_limit(self):
        bad=copy.deepcopy(self.baseline);bad['analysis']['computations'][0]['steps'][0]['rhs']=[99]
        fixed=copy.deepcopy(self.baseline);fixed['source_revision']={'reason':'repair independent source declaration','references':[{'file':'spec.md','start':1,'end':1}]}
        prompts=[]
        def invoke(name,role,prompt,parser):
            prompts.append(prompt)
            return fixtures.wire_reply(copy.deepcopy(bad if len(prompts)==1 else fixed),parser)
        record=session.prepare_baseline(self.out,'source',self.sources,invoke,1)
        self.assertEqual(len(prompts),2);self.assertIn('computation/projection',prompts[1])
        self.assertIn('source_revision',prompts[1]);self.assertEqual(record['baseline'],fixed)
        self.assertEqual(json.loads((self.out/'source.baseline-0.json').read_text()),bad)

    def test_missing_analysis_is_structured_failure_without_inventing_model(self):
        missing=copy.deepcopy(self.baseline);missing.pop('analysis');invoke=fixtures.wire_provider(return_value=missing)
        with self.assertRaises(session.AuditSessionLimit):session.prepare_baseline(self.out,'source',self.sources,invoke,0)
        self.assertEqual(invoke.call_count,1);self.assertFalse((self.out/'audit-baseline.current.json').exists())
        self.assertEqual(json.loads((self.out/'source.baseline-0.json').read_text()),missing)
        diagnostic=json.loads((self.out/'source.baseline-0.analysis.declaration-check.json').read_text())
        self.assertFalse(diagnostic['ok']);self.assertIn('baseline.analysis',diagnostic['errors'][0])

    def test_reference_repair_cannot_hide_failed_analysis(self):
        bad=copy.deepcopy(self.baseline);bad['analysis']['computations'][0]['steps'][0]['rhs']=[99]
        repair={'baseline_repair':{'reference_updates':[{'obligation_id':'independent-work','reference_index':0,'reference':{'file':'spec.md','start':1,'end':1}}]}}
        invoke=fixtures.wire_provider(side_effect=[bad,repair])
        with self.assertRaises(session.AuditSessionLimit):session.prepare_baseline(self.out,'source',self.sources,invoke,1)
        self.assertEqual(invoke.call_count,2)
        feedback=json.loads((self.out/'source.baseline-1.feedback.json').read_text())
        self.assertIn('full baseline',feedback['diagnostics'][0])

    def test_cache_identity_covers_script_and_calculator_closure_and_result_tampering(self):
        original={k:v for k,v in self.sources.items() if k in ('spec.md','reference.py')}
        identity=session.baseline_identity(self.base,original)
        helper=self.base.parent/'microarchitecture-design/scripts/budget_units.py';helper.write_text(helper.read_text()+'\n# changed dependency\n')
        changed=session.baseline_identity(self.base,original);self.assertNotEqual(changed,identity)
        audit_helper=self.base/'scripts/extra_helper.py';audit_helper.write_text('# extra source dependency\n')
        self.assertNotEqual(session.baseline_identity(self.base,original),changed)
        fixtures.freeze_snapshot(self.out,self.sources)
        record=session.prepare_baseline(self.out,'source',self.sources,fixtures.wire_provider(return_value=self.baseline),1)
        record['analysis_result']['values']['tampered']=99
        (self.out/'audit-baseline.current.json').write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError,'quantitative baseline evidence'):
            session.prepare_baseline(self.out,'source',self.sources,Mock(),1)

    def test_real_source_script_executes_shape_count_through_frozen_calculator(self):
        self.sources['reference.py']='def operation(xs, weights):\n    return [sum(x*w for x,w in zip(row, weights)) for row in xs]\n'
        baseline=fixtures.baseline_fixture(self.sources)
        baseline['analysis']={'facts':[{'id':'rows','source':{'file':'spec.md','line':1,'number':1},'unit':'count'},
                                       {'id':'inner','source':{'file':'spec.md','line':2,'number':1},'unit':'count'}],
            'workloads':[{'id':'profile','quantity':'rows','unit':'items','basis':'synthetic source row count','references':[{'file':'spec.md','start':1,'end':1}]}],
            'computations':[{'id':'dot','steps':[{'id':'products','kind':'matmul','lhs':[1,'inner'],'rhs':['inner',1]}],
                'scope':{'kind':'element','axis':'row','multiplicity':1},'semantics':'one inner-dimension dot per row',
                'basis':'source dot products and explicit dimensions','references':[{'file':'spec.md','start':1,'end':2},{'file':'reference.py','start':1,'end':2}]}],
            'states':[],'flows':[],'limits':'shape-count example; interpretation remains fallible'}
        fixtures.freeze_snapshot(self.out,self.sources)
        record=session.prepare_baseline(self.out,'real',self.sources,fixtures.wire_provider(return_value=baseline),1)
        self.assertEqual(record['analysis_result']['computations']['dot']['totals']['profile'],35)
        self.assertEqual(record['analysis_result']['model_sha256'],session.report_identity(baseline['analysis']))
        self.assertEqual(record['baseline'],baseline)

    def test_cache_rejects_altered_host_verified_transaction_view(self):
        record=session.prepare_baseline(self.out,'source',self.sources,fixtures.wire_provider(return_value=self.baseline),0)
        record['source_execution']['coverage_complete']=False
        (self.out/'audit-baseline.current.json').write_text(json.dumps(record))
        with self.assertRaisesRegex(session.TransportBindingError,'source transaction evidence'):
            session.prepare_baseline(self.out,'next',self.sources,Mock(),0)


if __name__=='__main__':unittest.main()
