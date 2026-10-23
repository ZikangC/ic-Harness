"""Authoritative cost relation and whole-task scheduling known answers."""
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from test_architecture import fixture
from test_audit import engine

class CostGroupsTests(unittest.TestCase):
    def test_quantity_has_one_owner_and_named_element_unit(self):
        d,s=fixture();w=d['work_model'];w['items'][0]['count']={'kind':'elementwise','elements':2,'operations_per_element':1,'unit':'ops'};w['items'][0]['scopes']['primary']={'kind':'element','axis':'sample','multiplicity':1}
        before=copy.deepcopy(d);r=engine.evaluate(d,s)
        self.assertTrue(r['ok'],r['errors']);self.assertEqual(d,before)
        self.assertEqual(r['work_model']['counts']['primary']['arithmetic']['operations'],24)
        self.assertEqual(r['candidates']['small']['workloads']['primary']['quantity'],12)
        self.assertEqual(r['candidates']['small']['workloads']['primary']['unit'],'samples')
        self.assertEqual(r['candidates']['small']['work_trace'],[{'workload':'primary','work_id':'arithmetic','cost_group':'compute'}])
        d['candidates'][0]['workloads'][0]['quantity']=12
        self.assertFalse(engine.evaluate(d,s)['ok'])

    def test_removed_contract_rejected_without_conversion(self):
        for apply in [lambda d:d['candidates'][0].update(work_mapping=[]),lambda d:d['candidates'][0].update(compute_bounds=[]),lambda d:d['work_model']['workloads'][0].update(items=12),lambda d:d['work_model']['workloads'][0].pop('unit'),lambda d:d['work_model']['items'][0].update(repetitions={'primary':1})]:
            d,s=fixture();apply(d);before=copy.deepcopy(d);r=engine.evaluate(d,s)
            self.assertFalse(r['ok']);self.assertEqual(d,before)

    def test_nonarithmetic_work_can_share_compute_realization(self):
        d,s=fixture();row=copy.deepcopy(d['work_model']['items'][0]);row.update(id='state',category='state',count={'kind':'none','reason':'state stored as part of result commit'})
        d['work_model']['items'].append(row)
        for c in d['candidates']:c['cost_groups'][0]['work_ids'].append('state');c['cost_groups'][0]['overhead_cycles']=3
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors']);self.assertEqual(r['candidates']['small']['workloads']['primary']['task_cycles'],123)

    def test_counted_work_cannot_hide_in_arbitrary_cycles(self):
        d,s=fixture();d['candidates'][0]['cost_groups'][0].update(model='cycles',cycles=1)
        r=engine.evaluate(d,s);self.assertFalse(r['ok']);self.assertTrue(any('counted work requires compute' in e for e in r['errors']))

    def test_transformed_group_includes_unchanged_work(self):
        d,s=fixture();a=d['work_model']['items'][0];a['count']={'kind':'elementwise','elements':20,'operations_per_element':1,'unit':'add'}
        for wid,unit in [('second','add'),('unchanged','mul')]:
            row=copy.deepcopy(a);row['id']=wid;row['count']['unit']=unit;d['work_model']['items'].append(row)
        for c in d['candidates']:
            c['cost_groups'][0].update(work_ids=['arithmetic','second','unchanged'],realization='transformed',count={'kind':'custom','operations':40,'unit':'ALU_op','basis':'20 fused adds + 20 unchanged multiplies'},equivalence='two modular adds fuse; all 20 unchanged multiplies remain')
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors']);self.assertEqual(r['candidates']['small']['ledger']['cost_groups'][0]['operations'],40)
        # Counts/equivalence are evidence obligations, not semantically proven by this test.

    def test_parallel_is_max_of_disjoint_whole_task_groups(self):
        d,s=fixture()
        for c in d['candidates']:
            c['cost_groups'][1]['resources']=['banks'];c['workloads'][0]['components']=[{'id':'pipeline','composition':'parallel','references':['setup','compute'],'basis':'synthetic independent resources'},{'id':'output','composition':'serial','references':['output'],'basis':'output after both'}]
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors']);self.assertEqual(r['candidates']['small']['workloads']['primary']['task_cycles'],100)
        d['candidates'][0]['cost_groups'][1]['resources']=['weights'];self.assertFalse(engine.evaluate(d,s)['ok'])

    def test_overlap_needs_explicit_cost_at_least_each_group(self):
        d,s=fixture()
        for c in d['candidates']:c['workloads'][0]['components']=[{'id':'overlap','composition':'overlap','references':['setup','compute','output'],'cycles':90,'basis':'synthetic port/arbitration schedule; independent audit still required'}]
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors']);self.assertEqual(r['candidates']['small']['workloads']['primary']['task_cycles'],90)
        d['candidates'][0]['workloads'][0]['components'][0]['cycles']=79;self.assertFalse(engine.evaluate(d,s)['ok'])

    def test_serial_cost_cannot_be_overridden_with_local_cycles(self):
        d,s=fixture();d['candidates'][0]['workloads'][0]['components'][1]['cycles']=1
        self.assertFalse(engine.evaluate(d,s)['ok'])

    def test_unknown_cost_preserves_estimate_and_unknown_gate(self):
        d,s=fixture();d['candidates'][0]['cost_groups'][0].update(model='unsupported',cycles=75,reason='hardware primitive not represented by fixed cost recipes')
        d['candidates'][0]['schedule_windows']=[]
        d['candidates'][0]['coverage']['schedule_windows']={'status':'not_applicable','basis':'unsupported primitive has no validated concrete compute window; release stays unknown'}
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors']);self.assertEqual(r['gate']['status'],'unknown');self.assertEqual(r['candidates']['small']['workloads']['primary']['task_cycles'],115)

    def test_cost_reference_cannot_leak_between_workloads(self):
        d,s=fixture();d['candidates'][0]['cost_groups'][0]['workload']='ghost';self.assertFalse(engine.evaluate(d,s)['ok'])

    def test_source_facts_resolve_forward_dependencies_without_editing(self):
        source='frequency 31 MHz rate 12500 items/s rows 3'
        ref=[{'file':'spec.md','start':1,'quote':source}]
        req={'facts':[
            {'id':'task','formula':'cpt*rows','unit':'cycles','citations':ref},
            {'id':'cpt','formula':'hz/rate','unit':'cycles/item','citations':ref},
            {'id':'hz','convert':{'fact':'frequency','to_unit':'Hz'},'citations':ref},
            {'id':'rows','source':{'file':'spec.md','line':1,'number':3},'unit':'items'},
            {'id':'rate','source':{'file':'spec.md','line':1,'number':2},'unit':'items/s'},
            {'id':'frequency','source':{'file':'spec.md','line':1,'number':1}}], 'constraints':[], 'runtime_parameters':[]}
        before=copy.deepcopy(req);values,errors=engine.validate_requirements(req,{'spec.md':source})
        self.assertEqual(errors,[]);self.assertEqual(values['task'],7440);self.assertEqual(req,before)
        req['facts'][0]['formula']='hz*hz'
        self.assertTrue(any('dimension mismatch' in x for x in engine.validate_requirements(req,{'spec.md':source})[1]))

    def test_source_fact_cycle_and_missing_name_are_diagnostic_errors(self):
        source='literal 3';ref=[{'file':'spec.md','start':1,'quote':source}]
        req={'facts':[{'id':'a','formula':'b+1','unit':'cycles','citations':ref},{'id':'b','formula':'a+1','unit':'cycles','citations':ref}],'constraints':[],'runtime_parameters':[]}
        values,errors=engine.validate_requirements(req,{'spec.md':source});self.assertTrue(any('dependency cycle' in x for x in errors))
        req['facts'][1]['formula']='missing+1'
        self.assertTrue(any('undefined' in x for x in engine.validate_requirements(req,{'spec.md':source})[1]))

    def test_movement_amount_cannot_be_accepted_as_arithmetic_work(self):
        d,s=fixture();d['work_model']['items'][0].update(category='movement',count={'kind':'custom','operations':12,'unit':'bytes','basis':'input amount'})
        r=engine.evaluate_work_model(d['work_model'],d['requirements'],s)
        self.assertFalse(r['ok']);self.assertTrue(any('movement/control uses none' in x for x in r['errors']))
        d['work_model']['items'][0].update(category='state',count={'kind':'elementwise','elements':12,'operations_per_element':1,'unit':'add'})
        self.assertTrue(engine.evaluate_work_model(d['work_model'],d['requirements'],s)['ok'])

    def test_standalone_bundle_recomputes_without_harness(self):
        d,s=fixture();out=Path(tempfile.mkdtemp());src=Path(engine.__file__).parent
        for script in src.glob('*.py'):shutil.copy2(script,out/script.name)
        result=subprocess.run([sys.executable,'-B',str(out/'evaluate.py')],cwd=out,input=json.dumps({'document':d,'sources':s}),text=True,capture_output=True,check=True)
        r=json.loads(result.stdout);self.assertTrue(r['ok'],r['errors']);self.assertEqual(r['candidates']['small']['workloads']['primary']['task_cycles'],120)

if __name__=='__main__':unittest.main()
