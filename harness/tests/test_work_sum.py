"""Fixed primitive aggregation preserves one source scope and the budget gates."""
import copy
import json
import re
from pathlib import Path
import unittest

from harness.tests.test_scoped_work import engine, fixture
from harness.tests.test_architecture import fixture as budget_fixture


def consecutive_gemvs():
    return {'kind':'sum','unit':'MAC','terms':[
        {'kind':'matmul','m':3,'n':1,'k':5,'batch':1,'unit':'MAC'},
        {'kind':'matmul','m':2,'n':1,'k':3,'batch':1,'unit':'MAC'}]}


class WorkSumTests(unittest.TestCase):
    def evaluate(self,recipe,scopes=None):
        model,requirements,sources=fixture(scopes,recipe)
        return engine.evaluate_work_model(model,requirements,sources)

    def test_document_example_is_executable_fixed_addition(self):
        doc=(Path(__file__).resolve().parents[1]/'skills/microarchitecture-design/references/work-model.md').read_text()
        recipe=json.loads(re.search(r'```json\n(.*?)\n```',doc,re.S).group(1))
        per,trace=engine._work.scoped_count(recipe,{'kind':'element','axis':'record','multiplicity':1},4,{},engine.expression)
        self.assertEqual(per,15);self.assertEqual(trace['total'],4)
        self.assertEqual(per*trace['total'],60)

    def test_element_and_prefix_apply_parent_scope_once(self):
        for scope in ({'kind':'element','axis':'record','multiplicity':2},
                      {'kind':'prefix','axis':'record','prefix_axis':'prior','first':1,'step':1,'multiplicity':2}):
            with self.subTest(scope=scope):
                model,req,src=fixture({p:scope for p in ('small','large')},consecutive_gemvs())
                before=copy.deepcopy(model);r=engine.evaluate_work_model(model,req,src)
                self.assertTrue(r['ok'],r['errors']);self.assertEqual(model,before)
                for profile,n in (('small',3),('large',5)):
                    repetitions=2*n if scope['kind']=='element' else 2*sum(range(1,n+1))
                    row=r['counts'][profile]['operation']
                    self.assertEqual(row['operations_per_occurrence'],21)
                    self.assertEqual(row['repetitions'],repetitions)
                    self.assertEqual(row['operations'],21*repetitions)
                    summary=next(p for p in r['compact_summary']['profiles'] if p['id']==profile)
                    self.assertEqual(summary['by_unit'][0]['operations'],21*repetitions)

    def test_task_terms_may_use_complete_workload_extent(self):
        recipe=consecutive_gemvs();recipe['terms'][0]['batch']='workload_elements'
        scopes={p:{'kind':'task','axis':'entire_task'} for p in ('small','large')}
        r=self.evaluate(recipe,scopes);self.assertTrue(r['ok'],r['errors'])
        for profile,n in (('small',3),('large',5)):
            row=r['counts'][profile]['operation']
            self.assertEqual(row['repetitions'],1);self.assertEqual(row['operations'],15*n+6)

    def test_all_supported_primitive_formulas_add_with_one_declared_unit(self):
        recipe={'kind':'sum','unit':'primitive','terms':[
            {'kind':'matmul','m':1,'n':2,'k':3,'batch':1,'unit':'primitive'},
            {'kind':'elementwise','elements':4,'operations_per_element':2,'unit':'primitive'},
            {'kind':'reduction','elements':4,'groups':2,'unit':'primitive'}]}
        self.assertEqual(engine._work.count(recipe,{},engine.expression),6+8+6)

    def test_final_candidate_ledger_and_budget_use_aggregated_work(self):
        d,s=budget_fixture()
        d['work_model']['items'][0]['count']={'kind':'sum','unit':'ops','terms':[
            {'kind':'elementwise','elements':40,'operations_per_element':1,'unit':'ops'},
            {'kind':'elementwise','elements':40,'operations_per_element':1,'unit':'ops'}]}
        before=copy.deepcopy(d);r=engine.evaluate(d,s)
        self.assertTrue(r['ok'],r['errors']);self.assertEqual(d,before)
        candidate=r['candidates']['small'];compute=next(g for g in candidate['ledger']['cost_groups'] if g['id']=='compute')
        self.assertEqual(compute['operations'],80);self.assertEqual(compute['scheduled_cycles'],80)
        self.assertEqual(candidate['workloads']['primary']['task_cycles'],120)
        self.assertEqual(candidate['workloads']['primary']['items_per_second'],8000000)
        d['work_model']['items'][0]['count']['terms'][-1]['unit']='MAC'
        rejected=engine.evaluate(d,s);self.assertFalse(rejected['ok'])
        self.assertTrue(any('unit must match' in error for error in rejected['errors']))

    def test_sum_parent_and_terms_have_exact_fields(self):
        for field,value in (('operations',21),('scope',{'kind':'task','axis':'run'}),('repeat',2),('basis','free total')):
            with self.subTest(parent_field=field):
                recipe=consecutive_gemvs();recipe[field]=value
                self.assertFalse(self.evaluate(recipe)['ok'])
            with self.subTest(term_field=field):
                recipe=consecutive_gemvs();recipe['terms'][0][field]=value
                self.assertFalse(self.evaluate(recipe)['ok'])
        for field in ('unit','terms'):
            recipe=consecutive_gemvs();recipe.pop(field)
            self.assertFalse(self.evaluate(recipe)['ok'])

    def test_sum_terms_cannot_use_custom_unknown_or_nested_counts(self):
        forbidden=[{'kind':'custom','operations':21,'unit':'MAC','basis':'arbitrary total'},
                   {'kind':'none','reason':'omit actual arithmetic'},
                   {'kind':'unsupported','reason':'unknown','unit':'MAC'},consecutive_gemvs()]
        for term in forbidden:
            with self.subTest(term=term):
                recipe=consecutive_gemvs();recipe['terms'][0]=term
                self.assertFalse(self.evaluate(recipe)['ok'])

    def test_sum_unit_and_each_term_shape_are_validated(self):
        cases=[('unit','ops'),('m',0),('k',-1),('batch',0.5)]
        for field,value in cases:
            with self.subTest(field=field,value=value):
                recipe=consecutive_gemvs();recipe['terms'][-1][field]=value
                self.assertFalse(self.evaluate(recipe)['ok'])
        recipe=consecutive_gemvs();recipe['unit']=' '
        self.assertFalse(self.evaluate(recipe)['ok'])

    def test_hidden_workload_axis_is_checked_in_every_non_task_term(self):
        scope_cases=[{'kind':'element','axis':'record','multiplicity':1},
                     {'kind':'prefix','axis':'record','prefix_axis':'prior','first':1,'step':1,'multiplicity':1}]
        for scope in scope_cases:
            for index in (0,1):
                with self.subTest(scope=scope,index=index):
                    recipe=consecutive_gemvs();recipe['terms'][index]['batch']='max(1,workload_elements)'
                    r=self.evaluate(recipe,{p:scope for p in ('small','large')})
                    self.assertFalse(r['ok'])
                    self.assertTrue(any('cannot use workload_elements' in error for error in r['errors']),r['errors'])

    def test_empty_and_oversized_term_lists_are_execution_errors(self):
        for terms in ([],[consecutive_gemvs()['terms'][0]]*(engine._work.MAX_SUM_TERMS+1)):
            with self.subTest(size=len(terms)):
                recipe={'kind':'sum','unit':'MAC','terms':terms};r=self.evaluate(recipe)
                self.assertFalse(r['ok'])
                self.assertTrue(any('execution-size limit' in error for error in r['errors']))


if __name__=='__main__':unittest.main()
