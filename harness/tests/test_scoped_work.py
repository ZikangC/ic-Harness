"""Declared scope axes derive counts without claiming arbitrary Python correspondence."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPTS=Path(__file__).resolve().parents[1]/'skills/microarchitecture-design/scripts'
spec=importlib.util.spec_from_file_location('scoped_work_evaluator',SCRIPTS/'evaluate.py')
engine=importlib.util.module_from_spec(spec);spec.loader.exec_module(engine)
work=engine._work


def fixture(scopes=None,recipe=None):
    sources={'spec.md':'Small workload contains 3 records. Large workload contains 5 records. Independent dimension is 3. Multiplicity is 2.\n',
             'reference.py':'for record in records:\n    for previous in prefix(record):\n        consume(record, previous)\n'}
    facts=[{'id':name,'value':value,'unit':'count','citations':[{'file':'spec.md','quote':quote,'start':1}]}
           for name,value,quote in [('small_count',3,'Small workload contains 3 records.'),
                                   ('large_count',5,'Large workload contains 5 records.'),
                                   ('independent_dimension',3,'Independent dimension is 3.'),
                                   ('copies',2,'Multiplicity is 2.')]]
    requirements={'facts':facts,'constraints':[],'runtime_parameters':[],'storage_area_model':{'status':'unspecified','reason':'independent synthetic source does not specify density'}}
    profiles=[{'id':'small','quantity':'small_count','unit':'records','basis':'small source workload'},
              {'id':'large','quantity':'large_count','unit':'records','basis':'large source workload'}]
    if scopes is None:scopes={profile:{'kind':'element','axis':'record','multiplicity':'copies'} for profile in ('small','large')}
    item={'id':'operation','category':'compute','semantics':'declared primitive arithmetic for each scoped source obligation',
          'condition':'active when the derived scope has members','state_lifetime':'task-local','status':'complete','dependencies':[],
          'count':recipe or {'kind':'elementwise','elements':2,'operations_per_element':1,'unit':'primitive'},
          'scopes':copy.deepcopy(scopes),'references':[{'file':'reference.py','quote':'consume(record, previous)'}]}
    return {'workloads':profiles,'items':[item]},requirements,sources


class ScopedWorkTests(unittest.TestCase):
    def check(self,model,requirements,sources):
        return engine.evaluate_work_model(model,requirements,sources)

    def test_element_occurrences_and_two_profile_totals_are_derived(self):
        model,requirements,sources=fixture();before=copy.deepcopy(model)
        report=self.check(model,requirements,sources);self.assertTrue(report['ok'],report['errors'])
        small=report['counts']['small']['operation'];large=report['counts']['large']['operation']
        self.assertEqual((small['repetitions'],small['operations_per_occurrence'],small['operations']),(6,2,12))
        self.assertEqual((large['repetitions'],large['operations']),(10,20))
        self.assertEqual(small['scope_trace'],{'kind':'element','axes':['record'],'quantity':3,'multiplicity':2,'factor':3,'total':6})
        self.assertEqual(model,before);self.assertNotIn('repetitions',model['items'][0])
        summary=report['compact_summary']['profiles'][0]
        self.assertEqual(summary['by_unit'][0]['occurrences'],6)
        self.assertIn('program-derived scope totals',report['compact_summary']['scope'])

    def test_prefix_first_zero_and_one_match_independent_enumerated_lengths(self):
        for first in (0,1):
            scopes={profile:{'kind':'prefix','axis':'record','prefix_axis':'prior_record','first':first,'step':1,'multiplicity':'copies'} for profile in ('small','large')}
            model,requirements,sources=fixture(scopes);report=self.check(model,requirements,sources)
            self.assertTrue(report['ok'],report['errors'])
            for profile,quantity in [('small',3),('large',5)]:
                values=report['counts'][profile]['operation'];lengths=list(range(first,first+quantity))
                self.assertEqual(values['scope_trace']['factor'],sum(lengths))
                self.assertEqual(values['repetitions'],sum(lengths)*2)
                self.assertEqual(values['operations'],sum(lengths)*4)
                self.assertEqual(values['scope_trace']['axes'],['record','prior_record'])

    def test_prefix_general_integer_step_is_not_a_triangular_only_shortcut(self):
        scopes={profile:{'kind':'prefix','axis':'record','prefix_axis':'prior_record','first':2,'step':3,'multiplicity':2} for profile in ('small','large')}
        model,requirements,sources=fixture(scopes);report=self.check(model,requirements,sources)
        self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(report['counts']['small']['operation']['scope_trace']['factor'],2+5+8)
        self.assertEqual(report['counts']['large']['operation']['scope_trace']['factor'],2+5+8+11+14)

    def test_zero_prefix_and_zero_multiplicity_are_inactive_without_empty_run(self):
        cases=[{'kind':'prefix','axis':'record','prefix_axis':'prior','first':0,'step':0,'multiplicity':1},
               {'kind':'prefix','axis':'record','prefix_axis':'prior','first':1,'step':1,'multiplicity':0},
               {'kind':'element','axis':'record','multiplicity':0}]
        for scope in cases:
            model,requirements,sources=fixture({profile:scope for profile in ('small','large')})
            report=self.check(model,requirements,sources);self.assertTrue(report['ok'],report['errors'])
            self.assertFalse(report['counts']['small']['operation']['active'])
            self.assertEqual(report['counts']['small']['operation']['operations'],0)
            self.assertEqual(report['compact_summary']['profiles'][0]['active_work_ids'],[])
        trace=work.scope_count({'kind':'prefix','axis':'record','prefix_axis':'prior','first':0,'step':1,'multiplicity':1},1,{},engine.expression)
        self.assertEqual(trace['factor'],0);self.assertEqual(trace['total'],0)
        model,requirements,sources=fixture();model['workloads'][0]['quantity']=0
        self.assertFalse(self.check(model,requirements,sources)['ok'])

    def test_task_can_express_legitimate_quadratic_work_without_extra_repeat(self):
        scopes={profile:{'kind':'task','axis':'entire_task'} for profile in ('small','large')}
        model,requirements,sources=fixture(scopes,{'kind':'custom','operations':'workload_elements**2','unit':'primitive','basis':'complete source task contains all ordered pairs'})
        report=self.check(model,requirements,sources);self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(report['counts']['small']['operation']['operations'],9)
        self.assertEqual(report['counts']['large']['operation']['operations'],25)
        self.assertEqual(report['counts']['small']['operation']['scope_trace'],{'kind':'task','axes':['entire_task'],'quantity':3,'factor':1,'total':1})
        self.assertEqual(report['counts']['small']['operation']['repetitions'],1)

    def test_equal_numeric_dimension_and_workload_are_not_guessed_as_duplicate_axes(self):
        model,requirements,sources=fixture(recipe={'kind':'matmul','m':'independent_dimension','n':2,'k':4,'batch':1,'unit':'MAC'})
        report=self.check(model,requirements,sources);self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(report['counts']['small']['operation']['operations_per_occurrence'],24)
        self.assertEqual(report['counts']['small']['operation']['operations'],144)
        self.assertEqual(report['counts']['large']['operation']['operations'],240)
        # The independent dimension equals the short quantity; only source audit
        # can prove that they denote distinct semantic axes.

    def test_old_repetitions_rejected_even_with_valid_new_scopes(self):
        model,requirements,sources=fixture();model['items'][0]['repetitions']={'small':6,'large':10}
        before=copy.deepcopy(model);report=self.check(model,requirements,sources)
        self.assertFalse(report['ok']);self.assertTrue(any('repetitions was removed' in error for error in report['errors']))
        self.assertEqual(model,before)
        del model['items'][0]['scopes'];self.assertFalse(self.check(model,requirements,sources)['ok'])

    def test_scope_fields_and_prefix_axes_are_strict(self):
        cases=[{'kind':'task','axis':'entire_task','multiplicity':1},
               {'kind':'element','axis':'record','multiplicity':1,'quantity':3},
               {'kind':'element','axis':'record'},
               {'kind':'element','axis':'','multiplicity':1},
               {'kind':'element','axis':None,'multiplicity':1},
               {'kind':'prefix','axis':'record','prefix_axis':' record ','first':0,'step':1,'multiplicity':1},
               {'kind':'prefix','axis':'record','prefix_axis':'','first':0,'step':1,'multiplicity':1},
               {'kind':'prefix','axis':'record','prefix_axis':'prior','first':0,'step':1,'multiplicity':1,'repetitions':3},
               {'kind':'fixed','axis':'record','multiplicity':1}]
        for scope in cases:
            with self.subTest(scope=scope):
                model,requirements,sources=fixture({profile:scope for profile in ('small','large')})
                self.assertFalse(self.check(model,requirements,sources)['ok'])
        model,requirements,sources=fixture();model['items'][0]['scopes'].pop('large')
        self.assertFalse(self.check(model,requirements,sources)['ok'])
        model,requirements,sources=fixture();model['items'][0]['scopes']['unknown']={'kind':'task','axis':'task'}
        self.assertFalse(self.check(model,requirements,sources)['ok'])

    def test_nonnegative_integer_scope_parameters_and_no_workload_repeats(self):
        for field in ('first','step','multiplicity'):
            for value in (-1,0.5,'workload_elements','max(1,workload_elements)',True):
                scope={'kind':'prefix','axis':'record','prefix_axis':'prior','first':0,'step':1,'multiplicity':1};scope[field]=value
                with self.subTest(field=field,value=value):
                    model,requirements,sources=fixture({profile:scope for profile in ('small','large')})
                    self.assertFalse(self.check(model,requirements,sources)['ok'])
        model,requirements,sources=fixture();model['items'][0]['scopes']['small']['multiplicity']='workload_elements'
        self.assertFalse(self.check(model,requirements,sources)['ok'])

    def test_element_and_prefix_recipes_cannot_repeat_the_workload_axis(self):
        recipes=[{'kind':'custom','operations':'workload_elements*2','unit':'primitive','basis':'invalid duplicate axis'},
                 {'kind':'elementwise','elements':'workload_elements','operations_per_element':1,'unit':'primitive'},
                 {'kind':'reduction','elements':'workload_elements','groups':2,'unit':'primitive'},
                 {'kind':'matmul','m':1,'n':3,'k':4,'batch':'workload_elements','unit':'MAC'}]
        scopes=[{'kind':'element','axis':'record','multiplicity':1},
                {'kind':'prefix','axis':'record','prefix_axis':'prior','first':1,'step':1,'multiplicity':1}]
        for scope in scopes:
            for recipe in recipes:
                with self.subTest(scope=scope,recipe=recipe):
                    model,requirements,sources=fixture({profile:scope for profile in ('small','large')},recipe)
                    report=self.check(model,requirements,sources);self.assertFalse(report['ok'])
                    self.assertTrue(any('cannot use workload_elements' in error for error in report['errors']),report['errors'])

    def test_none_and_unsupported_use_scopes_and_preserve_unknown_activity(self):
        scopes={profile:{'kind':'prefix','axis':'record','prefix_axis':'prior','first':0,'step':1,'multiplicity':1} for profile in ('small','large')}
        model,requirements,sources=fixture(scopes,{'kind':'none','reason':'completion events have no arithmetic'})
        model['items'][0]['category']='control';report=self.check(model,requirements,sources)
        self.assertTrue(report['ok'],report['errors']);self.assertEqual(report['counts']['small']['operation']['repetitions'],3)
        self.assertEqual(report['counts']['small']['operation']['operations'],0);self.assertEqual(report['unknown'],[])
        model['items'][0].update(category='compute',status='unresolved',count={'kind':'unsupported','reason':'source arithmetic not established','unit':'primitive'})
        model['items'][0]['scopes']['small']['multiplicity']=0
        report=self.check(model,requirements,sources);self.assertTrue(report['ok'],report['errors'])
        self.assertFalse(report['counts']['small']['operation']['active']);self.assertEqual(report['unknown'],['large/operation'])
        self.assertIsNone(report['counts']['large']['operation']['operations'])

    def test_inactive_compute_none_is_allowed_only_when_every_profile_has_zero_occurrences(self):
        scopes={profile:{'kind':'element','axis':'record','multiplicity':0} for profile in ('small','large')}
        model,requirements,sources=fixture(scopes,{'kind':'none','reason':'this source branch is not executed in the declared profiles'})
        before=copy.deepcopy(model);report=self.check(model,requirements,sources)
        self.assertTrue(report['ok'],report['errors']);self.assertEqual(model,before)
        self.assertFalse(report['counts']['small']['operation']['active'])
        self.assertFalse(report['counts']['large']['operation']['active'])
        self.assertEqual(report['counts']['large']['operation']['operations'],0)
        model['items'][0]['scopes']['large']['multiplicity']=1
        report=self.check(model,requirements,sources)
        self.assertFalse(report['ok']);self.assertTrue(any('active compute' in e and 'large' in e for e in report['errors']))

    def test_invalid_inactive_count_recipe_is_still_checked(self):
        scopes={profile:{'kind':'element','axis':'record','multiplicity':0} for profile in ('small','large')}
        model,requirements,sources=fixture(scopes,{'kind':'matmul','m':1,'n':1,'k':0,'batch':1,'unit':'MAC'})
        report=self.check(model,requirements,sources)
        self.assertFalse(report['ok']);self.assertTrue(any('k needs integer' in e for e in report['errors']))

    def test_selector_mapping_consumes_derived_counts_and_keeps_source_immutable(self):
        model,requirements,sources=fixture();source_before=copy.deepcopy(sources);model_before=copy.deepcopy(model)
        report=self.check(model,requirements,sources);self.assertTrue(report['ok'],report['errors'])
        report['items']={row['id']:row for row in model['items']}
        candidate={'id':'shared','macros':[],'resource_checks':[],'area_components':[{'id':'one_resource'}],
                   'cost_groups':[{'id':profile+'-work','workload':profile,'work_ids':{'unit':'primitive'},'resources':['one_resource'],
                                   'model':'compute','units':1,'operations_per_unit_cycle':2,'overhead_cycles':1,'basis':'declared serial resource service'}
                                  for profile in ('small','large')]}
        candidate_before=copy.deepcopy(candidate)
        bound,errors=work.bind_candidate(candidate,report,{},engine.expression)
        self.assertEqual(errors,[]);self.assertEqual([row['operations'] for row in bound['cost_groups']],[12,20])
        self.assertEqual([row['scheduled_cycles'] for row in bound['cost_groups']],[7,11])
        self.assertTrue(all(row['_work_binding']['resolved_work_ids']==['operation'] for row in bound['cost_groups']))
        self.assertEqual(candidate,candidate_before);self.assertEqual(model,model_before);self.assertEqual(sources,source_before)

    def test_transformed_recipe_is_whole_task_and_does_not_inherit_source_scope(self):
        model,requirements,sources=fixture();report=self.check(model,requirements,sources)
        candidate={'id':'transformed','macros':[],'resource_checks':[],'area_components':[{'id':'one_resource'}],
                   'cost_groups':[{'id':profile,'workload':profile,'work_ids':['operation'],'resources':['one_resource'],'model':'compute',
                                   'realization':'transformed','count':{'kind':'custom','operations':'workload_elements**2','unit':'implemented_op','basis':'declared full-task implementation count'},
                                   'equivalence':'independent audit must verify changed implementation count','units':1,'operations_per_unit_cycle':1,'overhead_cycles':0,'basis':'whole-task implementation'}
                                  for profile in ('small','large')]}
        bound,errors=work.bind_candidate(candidate,report,{},engine.expression)
        self.assertEqual(errors,[]);self.assertEqual([row['operations'] for row in bound['cost_groups']],[9,25])

    def test_real_standalone_script_returns_scope_evidence_without_harness(self):
        scopes={profile:{'kind':'prefix','axis':'record','prefix_axis':'prior','first':1,'step':1,'multiplicity':2} for profile in ('small','large')}
        model,requirements,sources=fixture(scopes);directory=Path(tempfile.mkdtemp())
        shutil.copytree(SCRIPTS,directory/'scripts',ignore=shutil.ignore_patterns('__pycache__'))
        request={'work_model':model,'requirements':requirements,'sources':sources};before=json.dumps(request,sort_keys=True)
        proc=subprocess.run([sys.executable,'-B',str(directory/'scripts/evaluate.py'),'--work-model-only'],cwd=directory,
                            input=json.dumps(request),text=True,capture_output=True,check=True)
        report=json.loads(proc.stdout);self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(report['counts']['small']['operation']['scope_trace']['factor'],6)
        self.assertEqual(report['counts']['large']['operation']['operations'],60)
        self.assertEqual(json.dumps(request,sort_keys=True),before)


if __name__=='__main__':unittest.main()
