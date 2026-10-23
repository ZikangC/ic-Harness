"""Known work-count/mapping failures and blind baseline orchestration."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock
import tempfile
import shutil
import test_architecture as base
import test_audit as audit
from harness.runtime import audit_session,design_session
from harness.runtime.run_store import dump,digest

engine=audit.engine


class WorkEvidenceTests(unittest.TestCase):
    def test_original_source_span_binds_actual_indented_text_without_model_edits(self):
        d,s=base.fixture();s['reference.py']='def transform(value):\n    updated = value + 3\n    return updated\n'
        span={'file':'reference.py','start':2,'end':3}
        d['work_model']['items'][0]['references']=[span];original=copy.deepcopy(d)
        result=engine.evaluate_work_model(d['work_model'],d['requirements'],s)
        self.assertTrue(result['ok'],result['errors']);self.assertEqual(d,original)
        self.assertEqual(result['counts']['primary']['arithmetic']['operations'],80)
        binding=result['citation_bindings'][0]
        self.assertEqual(binding['reference'],span)
        self.assertEqual(binding['resolved']['quote'],'    updated = value + 3\n    return updated')
        self.assertEqual(binding['resolved']['source_sha256'],digest(s['reference.py'].encode()))
        self.assertEqual((binding['resolved']['start'],binding['resolved']['end']),(2,3))
        s['reference.py']+='\n# another source version\n'
        changed=engine.evaluate_work_model(d['work_model'],d['requirements'],s)
        self.assertTrue(changed['ok']);self.assertNotEqual(changed['citation_bindings'][0]['resolved']['source_sha256'],binding['resolved']['source_sha256'])

    def test_inexact_quote_is_not_ignored_when_its_span_is_valid(self):
        d,s=base.fixture();s['reference.py']='def transform(value):\n    return value + 3\n'
        item=d['work_model']['items'][0]
        item['references']=[{'file':'reference.py','start':1,'end':2,'quote':'    return value + 3'}]
        self.assertTrue(engine.evaluate_work_model(d['work_model'],d['requirements'],s)['ok'])
        item['references'][0]['quote']='     return value + 3';before=copy.deepcopy(d['work_model'])
        result=engine.evaluate_work_model(d['work_model'],d['requirements'],s)
        self.assertFalse(result['ok']);self.assertEqual(result['citation_bindings'],[])
        self.assertEqual(d['work_model'],before)
        self.assertTrue(any('exact original-input quote required' in error for error in result['errors']))

    def test_source_span_bounds_scope_and_supplied_fingerprint_are_checked(self):
        d,s=base.fixture();s['reference.py']='line one\nline two\n'
        for ref in ({'file':'reference.py','start':True,'end':2},
                    {'file':'reference.py','start':0,'end':1},
                    {'file':'reference.py','start':2,'end':1},
                    {'file':'reference.py','start':1,'end':3},
                    {'file':'design.json','start':1,'end':1},
                    {'file':'reference.py','start':1,'end':1,'quote':'line two'},
                    {'file':'reference.py','start':1,'end':1,'source_sha256':'stale'}):
            with self.subTest(ref=ref):
                d['work_model']['items'][0]['references']=[ref]
                result=engine.evaluate_work_model(d['work_model'],d['requirements'],s)
                self.assertFalse(result['ok']);self.assertEqual(result['citation_bindings'],[])

    def test_exact_quote_without_bounds_and_ambiguous_bound_quote_remain_valid(self):
        d,s=base.fixture();s['reference.py']='first\nrepeat\nrepeat\nlast\n'
        d['work_model']['items'][0]['references']=[{'file':'reference.py','quote':'first\n'}]
        result=engine.evaluate_work_model(d['work_model'],d['requirements'],s)
        self.assertTrue(result['ok']);self.assertEqual(result['citation_bindings'][0]['resolved']['end'],1)
        d['work_model']['items'][0]['references']=[{'file':'reference.py','quote':'repeat'}]
        self.assertFalse(engine.evaluate_work_model(d['work_model'],d['requirements'],s)['ok'])
        d['work_model']['items'][0]['references'][0].update(start=3,end=3)
        result=engine.evaluate_work_model(d['work_model'],d['requirements'],s)
        self.assertTrue(result['ok']);self.assertEqual(result['citation_bindings'][0]['resolved']['quote'],'repeat')
        self.assertEqual(result['citation_bindings'][0]['resolved']['start'],3)

    def test_fixed_shape_whole_task_batch_count(self):
        d,s=base.fixture();item=d['work_model']['items'][0]
        item['count']={'kind':'matmul','m':2,'n':3,'k':5,'batch':4,'unit':'MAC'}
        item['scopes']['primary']={'kind':'task','axis':'task'}
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors'])
        self.assertEqual(r['work_model']['counts']['primary']['arithmetic']['operations'],120)
        self.assertEqual(r['candidates']['small']['ledger']['cost_groups'][0]['operations'],120)
        self.assertEqual(r['candidates']['small']['workloads']['primary']['task_cycles'],160)
        self.assertNotIn('operations',d['candidates'][0]['cost_groups'][0])

    def test_missing_duplicate_and_unknown_cost_relation(self):
        for change in [lambda c:c['cost_groups'][0].update(work_ids=[]),lambda c:c['cost_groups'].append(dict(c['cost_groups'][0],id='second')),lambda c:c['cost_groups'][0].update(work_ids=['absent']),lambda c:c['cost_groups'][0].update(resources=['ghost']),lambda c:c['workloads'][0]['components'][1].update(references=['ghost'])]:
            d,s=base.fixture();change(d['candidates'][0]);r=engine.evaluate(d,s)
            self.assertFalse(r['ok'],r)

    def test_work_cost_cannot_be_replaced_by_placeholder_operations(self):
        d,s=base.fixture();d['candidates'][0]['cost_groups'][0]['operations']=1
        r=engine.evaluate(d,s);self.assertFalse(r['ok'])
        self.assertTrue(any('operations is program-derived' in x for x in r['errors']))
        d,s=base.fixture();d['work_model']['items'][0]['status']='placeholder'
        self.assertFalse(engine.evaluate(d,s)['ok'])

    def test_unused_count_and_local_interval_cannot_claim_whole_task(self):
        d,s=base.fixture();c=d['candidates'][0];c['workloads'][0]['components'][1].update(composition='overlap',cycles=1)
        r=engine.evaluate(d,s);self.assertFalse(r['ok']);self.assertTrue(any('below whole-task cost' in x for x in r['errors']))
        d,s=base.fixture();c=d['candidates'][0];new=copy.deepcopy(c['cost_groups'][1]);new['id']='unmapped';c['cost_groups'].append(new)
        self.assertFalse(engine.evaluate(d,s)['ok'])

    def test_duplicate_whole_task_cost_is_not_extra_work(self):
        d,s=base.fixture();c=d['candidates'][0];x=copy.deepcopy(c['workloads'][0]['components'][1]);x['id']='again';c['workloads'][0]['components'].append(x)
        self.assertFalse(engine.evaluate(d,s)['ok'])

    def test_source_graph_cycles_and_invented_citations_fail(self):
        for change in [lambda item:item.update(dependencies=['missing']),lambda item:item.update(dependencies=['arithmetic']),lambda item:item['references'][0].update(quote='not a source excerpt')]:
            d,s=base.fixture();change(d['work_model']['items'][0]);self.assertFalse(engine.evaluate(d,s)['ok'])

    def test_unknown_count_is_explicit_unknown_not_hardware_impossibility(self):
        d,s=base.fixture();d['work_model']['items'][0].update(status='unresolved',count={'kind':'unsupported','reason':'specialized cost not expressed','unit':'ops'})
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors']);self.assertEqual(r['gate']['status'],'unknown')
        self.assertIn('primary/arithmetic',r['gate']['unsupported_models'])

    def test_inactive_branch_does_not_require_cost_mapping(self):
        d,s=base.fixture();row=copy.deepcopy(d['work_model']['items'][0]);row.update(id='inactive',condition='branch is inactive in this profile',scopes={'primary':{'kind':'element','axis':'sample','multiplicity':0}})
        d['work_model']['items'].append(row)
        self.assertTrue(engine.evaluate(d,s)['ok'])

    def test_fusion_can_change_counts_with_explicit_equivalence(self):
        d,s=base.fixture();model=d['work_model'];row=model['items'][0];row['count']={'kind':'elementwise','elements':40,'operations_per_element':1,'unit':'ops'}
        other=copy.deepcopy(row);other.update(id='second',dependencies=['arithmetic']);model['items'].append(other)
        for c in d['candidates']:
            bound=c['cost_groups'][0];bound.update(work_ids=['arithmetic','second'],realization='transformed',count={'kind':'elementwise','elements':40,'operations_per_element':1,'unit':'ops'},equivalence='two consecutive increments combine into one add of two with identical modular result')
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors']);self.assertEqual(r['candidates']['small']['ledger']['cost_groups'][0]['operations'],40)
        d['candidates'][0]['cost_groups'][0].pop('equivalence');self.assertFalse(engine.evaluate(d,s)['ok'])
        # Mechanical acceptance does not prove the declared transformation's source semantics.

    def test_work_model_change_needs_source_evidence_and_preserves_input(self):
        d,s=base.fixture();out=base.SkillToolTests().setup_run();new=copy.deepcopy(d);new['work_model']['items'][0]['count']['operations']=90
        request={'tool_request':{'tool':'evaluate_design','document':new}}
        invoke=Mock(side_effect=[request,RuntimeError('stop')])
        with self.assertRaises(RuntimeError):design_session.generate(out,'change','',d['requirements'],s,invoke,2)
        self.assertIn('work_model_revision',invoke.call_args.args[2]);self.assertFalse(list(out.glob('*.tool.execution.json')))
        new['work_model_revision']={'reason':'source-backed correction','references':[{'file':'spec.md','quote':'items 12'}]}
        self.assertTrue(engine.evaluate(new,s)['ok']);self.assertEqual(d['work_model']['items'][0]['count']['operations'],80)

    def test_initial_work_model_precedes_candidates_and_recovery_reuses_it(self):
        d,s=base.fixture();out=base.SkillToolTests().setup_run();(out/'work-model.current.json').unlink();calls=[]
        def invoke(name,role,prompt,parser):
            calls.append(name)
            if '.work-' in name:
                self.assertIn('BEFORE any candidate',prompt);self.assertNotIn('STRUCTURED CANDIDATE DATA SCHEMA',prompt);return copy.deepcopy(d['work_model'])
            if name.endswith('step-0'):return {'tool_request':{'tool':'evaluate_design','document':{k:v for k,v in d.items() if k!='work_model'}}}
            raise RuntimeError('interruption')
        with self.assertRaises(RuntimeError):design_session.generate(out,'initial','',d['requirements'],s,invoke,2)
        self.assertEqual(calls[:2],['initial.work-0','initial.step-0'])
        final=Mock(return_value={'final':{'evaluated_design_sha256':design_session.identity(d),'architecture_md':d['architecture_md']}})
        design_session.generate(out,'initial','',d['requirements'],s,final,2);self.assertEqual(final.call_count,1)


class BaselineEvidenceTests(unittest.TestCase):
    def fresh(self,sources=None):
        out=audit.snapshot(sources);(out/'audit-baseline.current.json').unlink();return out

    def test_blind_call_has_no_proposal_or_designer_history(self):
        d,s=base.fixture();out=self.fresh();seen=[]
        def invoke(name,role,prompt,parser):
            seen.append(prompt);self.assertNotIn('TOP_SECRET_PROPOSAL',prompt);self.assertNotIn('TOP_SECRET_PREVIOUS',prompt)
            self.assertIn('FILE spec.md',prompt);self.assertIn('FILE reference.py',prompt)
            return audit.wire_reply(audit.baseline_fixture(s),parser)
        record=audit_session.prepare_baseline(out,'blind',dict(s,**{'design.json':'TOP_SECRET_PROPOSAL'}),invoke,0)
        self.assertEqual(len(seen),1);self.assertTrue(record['baseline_sha256'])

    def test_baseline_reuse_and_source_change(self):
        d,s=base.fixture();out=self.fresh()
        changed=dict(s,**{'reference.py':s['reference.py']+'# new source\n'})
        invoke=audit.wire_provider(side_effect=[audit.baseline_fixture(s),audit.baseline_fixture(changed)])
        first=audit_session.prepare_baseline(out,'first',s,invoke,0);second=audit_session.prepare_baseline(out,'next',s,invoke,0)
        self.assertEqual(first,second);self.assertEqual(invoke.call_count,1)
        with self.assertRaises(audit_session.TransportBindingError):
            audit_session.prepare_baseline(out,'changed',changed,invoke,0)
        self.assertEqual(invoke.call_count,1)
        # A new frozen-input version is a new run; the original snapshot stays immutable.
        changed_out=self.fresh(changed)
        third=audit_session.prepare_baseline(changed_out,'changed',changed,invoke,0)
        self.assertNotEqual(first['identity'],third['identity']);self.assertEqual(invoke.call_count,2)

    def test_baseline_correction_invalidates_reuse_and_is_source_only(self):
        d,s=base.fixture();out=self.fresh()
        revised=audit.baseline_fixture(s);revised['source_revision']={'reason':'source correction independently reconsidered against original inputs','references':[{'file':'spec.md','quote':'area 3'}]}
        invoke=audit.wire_provider(side_effect=[audit.baseline_fixture(s),revised])
        first=audit_session.prepare_baseline(out,'first',s,invoke,0)
        correction={'obligation_id':'independent-work','correction':'correct source interpretation','reason':'original-source evidence','references':[{'file':'spec.md','quote':'area 3'}]}
        dump(out/'audit-baseline.invalidated.json',{'baseline_sha256':first['baseline_sha256'],'corrections':[correction]})
        second=audit_session.prepare_baseline(out,'rebuild',s,invoke,0)
        self.assertEqual(invoke.call_count,2);self.assertIn('source_corrections',invoke.call_args.args[2])
        self.assertEqual(second['baseline'],revised)

    def test_baseline_ids_are_independent_and_all_must_be_mapped(self):
        ctx=audit.context_fixture();r=audit.report_fixture(ctx)
        self.assertTrue(audit.validator.validate(r,ctx)['ok'])
        for row in r['requirements']:row['baseline_ids']=[]
        checked=audit.validator.validate(r,ctx);self.assertFalse(checked['ok']);self.assertTrue(any('independent baseline coverage' in x for x in checked['errors']))
        r=audit.report_fixture(ctx);r['baseline_sha256']='forged';self.assertFalse(audit.validator.validate(r,ctx)['ok'])

    def test_baseline_disagreement_can_be_corrected_without_rejecting_design(self):
        ctx=audit.context_fixture();r=audit.report_fixture(ctx)
        r['baseline_corrections']=[{'obligation_id':'independent-work','correction':'source correction','reason':'source interpretation error','references':[{'file':'spec.md','quote':'area 3'}]}]
        self.assertTrue(audit.validator.validate(r,ctx)['ok'])
        r['baseline_corrections'][0]['references']=[{'file':'design.json','quote':'"id": "small"'}]
        self.assertFalse(audit.validator.validate(r,ctx)['ok'])

    def test_raw_baseline_never_substitutes_for_original_evidence(self):
        ctx=audit.context_fixture();r=audit.report_fixture(ctx);r['checks'][0]['references'][0]={'file':'audit-baseline.json','quote':'"id": "independent-work"'}
        self.assertFalse(audit.validator.validate(r,ctx)['ok'])

    def test_bounded_baseline_errors_and_resume_no_repeated_calls(self):
        d,s=base.fixture();out=self.fresh();bad=audit.wire_provider(return_value={'scope':'bad'})
        with self.assertRaises(audit_session.AuditSessionLimit):audit_session.prepare_baseline(out,'bad',s,bad,1)
        self.assertEqual(bad.call_count,2)
        again=Mock(side_effect=AssertionError('saved replies must be reused'))
        with self.assertRaises(audit_session.AuditSessionLimit):audit_session.prepare_baseline(out,'bad',s,again,1)
        self.assertEqual(again.call_count,0)

    def test_prompt_exposes_only_successful_calculation_references(self):
        from harness.prompts.architecture import audit as prompt
        ctx=audit.context_fixture();receipt=dict(ctx['calculations'][0]);verified=audit.validator.executed_values(receipt,ctx['sources'],ctx['source_analysis'],ctx['document'],ctx['budget'],ctx['execution_dependencies']);history=[{'kind':'tool_result','result':receipt,'executed_values':verified},{'kind':'tool_result','result':{'request_id':'failed','ok':False,'errors':['bad fact'],'calculation_ids':['partial'],'values':{'partial':3}}}]
        text=prompt('skill',ctx['sources'],history=history)
        table=text.split('VALID EXECUTED CALCULATION REFERENCES (copy exact ids and values): ',1)[1].split('\n',1)[0]
        self.assertIn('reconstructed_bits',table);self.assertNotIn('partial',table)
        self.assertIn('EXACT INDEPENDENT BASELINE IDS',text);self.assertIn('insufficient_evidence',text)

    def test_standalone_baseline_validation_without_design_skill(self):
        import subprocess,sys
        d,s=base.fixture();out=Path(tempfile.mkdtemp());script=out/'validate.py';shutil.copy2(audit.BASE/'skills/microarchitecture-audit/scripts/validate_review.py',script)
        proc=subprocess.run([sys.executable,'-B',str(script)],input=json.dumps({'mode':'baseline','baseline':audit.baseline_fixture(s),'sources':s}),text=True,capture_output=True,check=True,cwd=out)
        self.assertTrue(json.loads(proc.stdout)['ok'])

if __name__=='__main__':unittest.main()
