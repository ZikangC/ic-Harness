"""Compact bindings preserve source coverage and expose executed counts across inputs."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_architecture import fixture
from test_audit import engine


class CompactSelectorTests(unittest.TestCase):
    def test_unit_selector_uses_counts_without_editing_proposal(self):
        document,sources=fixture()
        for candidate in document['candidates']:
            candidate['cost_groups'][0]['work_ids']={'unit':'ops'}
        before=copy.deepcopy(document);report=engine.evaluate(document,sources)
        self.assertTrue(report['ok'],report['errors']);self.assertEqual(document,before)
        row=report['candidates']['small']['ledger']['cost_groups'][0]
        self.assertEqual(row['work_ids'],['arithmetic']);self.assertEqual(row['operations'],80)
        self.assertEqual(row['binding'],{'declaration':{'unit':'ops'},'resolved_work_ids':['arithmetic']})
        self.assertEqual(report['candidates']['small']['work_trace'],[{'workload':'primary','work_id':'arithmetic','cost_group':'compute'}])

    def test_exclude_can_partition_costs_while_resources_are_shared(self):
        document,sources=fixture();second=copy.deepcopy(document['work_model']['items'][0])
        second.update(id='finishing',dependencies=['arithmetic']);second['count']['operations']=7
        document['work_model']['items'].append(second)
        for candidate in document['candidates']:
            first=candidate['cost_groups'][0];first['work_ids']={'unit':'ops','exclude':['finishing']}
            other=copy.deepcopy(first);other.update(id='finish',work_ids=['finishing'])
            candidate['cost_groups'].append(other)
            candidate['workloads'][0]['components'].append({'id':'finish','composition':'serial','references':['finish'],'basis':'same resource reused after prior whole-task stages'})
        report=engine.evaluate(document,sources);self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(report['candidates']['small']['workloads']['primary']['task_cycles'],127)
        self.assertEqual([x['work_id'] for x in report['candidates']['small']['work_trace']],['arithmetic','finishing'])

    def test_unknown_empty_and_ambiguous_selectors_are_rejected(self):
        declarations=[{}, {'unit':''}, {'unit':None}, {'unit':'missing-unit'}, {'category':'missing-category'},
                      {'unit':'ops','category':'compute'}, {'unit':'ops','remaining':True}, {'remaining':True},
                      {'category':'movement'}, {'unit':'ops','exclude':['arithmetic']}]
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                document,sources=fixture();document['candidates'][0]['cost_groups'][0]['work_ids']=declaration
                report=engine.evaluate(document,sources);self.assertFalse(report['ok'],report)
                self.assertTrue(any('work_ids selector' in error for error in report['errors']),report['errors'])

    def test_exclude_must_name_selected_active_source_work_once(self):
        for excluded in ['arithmetic', ['arithmetic','arithmetic'], ['ghost'], [None], [1], [['arithmetic']]]:
            with self.subTest(excluded=excluded):
                document,sources=fixture();document['candidates'][0]['cost_groups'][0]['work_ids']={'unit':'ops','exclude':excluded}
                self.assertFalse(engine.evaluate(document,sources)['ok'])
        for row_kind in ('inactive','nonmatching'):
            document,sources=fixture();extra=copy.deepcopy(document['work_model']['items'][0]);extra['id']='other'
            if row_kind=='inactive':extra['scopes']['primary']={'kind':'element','axis':'sample','multiplicity':0}
            else:extra['count']['unit']='different-op'
            document['work_model']['items'].append(extra)
            document['candidates'][0]['cost_groups'][0]['work_ids']={'unit':'ops','exclude':['other']}
            report=engine.evaluate(document,sources);self.assertFalse(report['ok'])
            self.assertTrue(any('nonmatching work_ids' in error for error in report['errors']),report['errors'])

    def test_selector_overlap_and_missing_coverage_remain_errors(self):
        for change in ('overlap','missing'):
            document,sources=fixture();second=copy.deepcopy(document['work_model']['items'][0]);second['id']='second'
            document['work_model']['items'].append(second);candidate=document['candidates'][0]
            candidate['cost_groups'][0]['work_ids']={'unit':'ops'}
            if change=='overlap':
                extra=copy.deepcopy(candidate['cost_groups'][0]);extra.update(id='second-cost',work_ids={'category':'compute'})
                candidate['cost_groups'].append(extra)
            else:candidate['cost_groups'][0]['work_ids']['exclude']=['second']
            report=engine.evaluate(document,sources);self.assertFalse(report['ok'])
            self.assertTrue(any(('duplicate work cost' if change=='overlap' else 'unmapped active work') in error for error in report['errors']),report['errors'])

    def test_explicit_empty_list_is_only_an_explicit_overhead(self):
        document,sources=fixture();self.assertTrue(engine.evaluate(document,sources)['ok'])
        document['candidates'][0]['cost_groups'][1]['work_ids']={'category':'control'}
        self.assertFalse(engine.evaluate(document,sources)['ok'])
        document,sources=fixture();document['candidates'][0]['cost_groups'][0]['work_ids']=[]
        self.assertFalse(engine.evaluate(document,sources)['ok'])

    def source_work(self,unit,quantities):
        sources={'spec.md':'two workload quantities are declared','reference.py':'compute, transfer, commit and complete the task'}
        profiles=[{'id':name,'quantity':quantity,'unit':'records','basis':'independent synthetic workload'} for name,quantity in quantities.items()]
        def item(wid,category,count,scopes=None):
            return {'id':wid,'category':category,'semantics':'independent synthetic source obligation','condition':'profile-specific occurrence declaration',
                    'state_lifetime':'task-local','status':'complete','dependencies':[],
                    'count':count,'scopes':scopes or {p:{'kind':'element','axis':'record','multiplicity':1} for p in quantities},
                    'references':[{'file':'reference.py','quote':'compute, transfer, commit and complete the task'}]}
        rows=[item('product','compute',{'kind':'matmul','m':2,'n':3,'k':4,'batch':1,'unit':unit}),
              item('extra','compute',{'kind':'reduction','elements':6,'groups':1,'unit':unit},{p:({'kind':'element','axis':'record','multiplicity':0} if i==0 else {'kind':'task','axis':'task'}) for i,p in enumerate(quantities)}),
              item('load','movement',{'kind':'none','reason':'transfer amount is not arithmetic'}),
              item('store','movement',{'kind':'none','reason':'transfer amount is not arithmetic'}),
              item('commit','state',{'kind':'none','reason':'state commit is separately scheduled'}),
              item('complete','control',{'kind':'none','reason':'completion handshake is separately scheduled'})]
        return {'workloads':profiles,'items':rows},sources

    def test_profile_local_selectors_support_distinct_input_units_and_shared_groups(self):
        for unit,quantities in [('primitive-X',{'short':3,'long':11}),('accumulate-Z',{'packet':7,'batch':2})]:
            with self.subTest(unit=unit):
                model,sources=self.source_work(unit,quantities);work=engine._work.validate(model,{},sources,engine.expression)
                self.assertTrue(work['ok'],work['errors']);groups=[]
                for profile in quantities:
                    groups.append({'id':profile+'-compute','workload':profile,'work_ids':{'unit':unit},'resources':['shared-core'],'model':'compute','units':2,'operations_per_unit_cycle':3,'overhead_cycles':4,'basis':'declared shared primitive resource'})
                    for category in ('movement','state','control'):
                        groups.append({'id':profile+'-'+category,'workload':profile,'work_ids':{'category':category},'resources':['shared-core'],'model':'cycles','cycles':2,'basis':'explicit serial time-sharing obligations'})
                candidate={'id':'reused','macros':[],'resource_checks':[],'area_components':[{'id':'shared-core'}],'cost_groups':groups}
                before=copy.deepcopy(candidate);bound,errors=engine._work.bind_candidate(candidate,work,{},engine.expression)
                self.assertEqual(errors,[]);self.assertEqual(candidate,before)
                self.assertEqual(bound['cost_groups'][0]['work_ids'],['product'])
                second=bound['cost_groups'][4]
                self.assertEqual(second['work_ids'],['product','extra'])
                self.assertEqual(second['operations'],24*list(quantities.values())[1]+5)
                self.assertEqual(len(bound['_work_trace']),11)
                self.assertTrue(all(row['resources']==['shared-core'] for row in bound['cost_groups']))

    def test_heterogeneous_category_needs_explicit_transformed_cost(self):
        document,sources=fixture();other=copy.deepcopy(document['work_model']['items'][0]);other['id']='different'
        other['count'].update(unit='different-op',operations=9);document['work_model']['items'].append(other)
        for candidate in document['candidates']:candidate['cost_groups'][0]['work_ids']={'category':'compute'}
        self.assertFalse(engine.evaluate(document,sources)['ok'])
        for candidate in document['candidates']:
            candidate['cost_groups'][0].update(realization='transformed',count={'kind':'custom','operations':89,'unit':'shared-primitive','basis':'80 original primitives and 9 different primitives are both executed'},equivalence='all original operations retained by the shared primitive implementation')
        report=engine.evaluate(document,sources);self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(report['candidates']['small']['ledger']['cost_groups'][0]['operations'],89)

    def test_compact_summary_is_aggregation_of_executed_counts(self):
        model,sources=self.source_work('multiply',{'first':3,'second':11})
        report=engine.evaluate_work_model(model,{'facts':[],'constraints':[],'runtime_parameters':[]},sources)
        self.assertTrue(report['ok'],report['errors']);first,second=report['compact_summary']['profiles']
        self.assertEqual((first['quantity'],first['unit']),(3,'records'))
        self.assertEqual(first['by_unit'][0],{'unit':'multiply','work_ids':['product'],'operations':72,'occurrences':3,'unknown_work_ids':[],'work_ids_selector':{'unit':'multiply'}})
        self.assertEqual(second['by_unit'][0]['operations'],269)
        self.assertEqual(second['by_unit'][0]['occurrences'],12)
        self.assertEqual(first['by_unit'][1]['operations'],0)
        self.assertEqual(first['by_category'][1]['work_ids'],['load','store'])
        self.assertNotIn('extra',first['active_work_ids']);self.assertIn('extra',second['active_work_ids'])
        self.assertIn('no implementation cost',report['compact_summary']['scope'])

    def test_summary_selectors_match_active_profile_work_and_preserve_unknowns(self):
        model,sources=self.source_work('primitive-X',{'short':3,'long':11})
        unknown=copy.deepcopy(model['items'][0]);unknown.update(id='special',status='unresolved',
            count={'kind':'unsupported','unit':'unknown-Z','reason':'no source count available'})
        model['items'].append(unknown);before=copy.deepcopy(model)
        work=engine._work.validate(model,{},sources,engine.expression)
        self.assertTrue(work['ok'],work['errors']);self.assertEqual(model,before)
        for profile in work['compact_summary']['profiles']:
            for row in profile['by_unit']:
                if row['unit'] is None:
                    self.assertNotIn('work_ids_selector',row);continue
                self.assertEqual(engine._work.resolve_work_ids(row['work_ids_selector'],profile['id'],work),row['work_ids'])
                if row['unit']=='unknown-Z':
                    self.assertIsNone(row['operations']);self.assertEqual(row['unknown_work_ids'],['special'])
            candidate={'id':'candidate','macros':[],'resource_checks':[],'area_components':[{'id':'shared-core'}],
                'cost_groups':[{'id':'unresolved','workload':profile['id'],'work_ids':{'unit':'unknown-Z'},
                    'model':'compute','resources':['shared-core'],'units':2,'operations_per_unit_cycle':1,
                    'overhead_cycles':3,'basis':'unknown source cost remains unresolved'}]}
            bound,errors=engine._work.bind_candidate(candidate,work,{},engine.expression)
            self.assertIn('unresolved',bound['_unknown_cost']);self.assertIsNone(bound['cost_groups'][0]['operations'])
            self.assertTrue(errors) # Other active work is still unbound, never silently released.

    def test_mixed_unit_diagnostic_contains_selected_ids_and_task_totals(self):
        document,sources=fixture();other=copy.deepcopy(document['work_model']['items'][0])
        other.update(id='different');other['count'].update(unit='different-op',operations=9)
        document['work_model']['items'].append(other)
        for candidate in document['candidates']:candidate['cost_groups'][0]['work_ids']={'category':'compute'}
        before=copy.deepcopy(document);report=engine.evaluate(document,sources)
        self.assertFalse(report['ok']);self.assertEqual(document,before)
        errors=[error for error in report['errors'] if 'commensurate operation units' in error]
        self.assertEqual(len(errors),2)
        for error in errors:
            detail=json.loads(error.split('unit_breakdown=',1)[1]);self.assertEqual(detail['workload'],'primary')
            self.assertEqual(detail['by_unit'],[{'unit':'ops','work_ids':['arithmetic'],'operations':80,'unknown_work_ids':[]},
                {'unit':'different-op','work_ids':['different'],'operations':9,'unknown_work_ids':[]}])
            self.assertEqual(detail['non_counted_work_ids'],[])

    def test_counted_cycles_diagnostic_separates_nonarithmetic_and_unknown_work(self):
        model,sources=self.source_work('primitive-X',{'short':3,'long':11})
        unknown=copy.deepcopy(model['items'][0]);unknown.update(id='special',status='unresolved',
            count={'kind':'unsupported','reason':'unknown primitive cost'})
        model['items'].append(unknown);work=engine._work.validate(model,{},sources,engine.expression)
        candidate={'id':'candidate','macros':[],'resource_checks':[],'area_components':[{'id':'shared-core'}],
            'cost_groups':[{'id':'invalid','workload':'short','work_ids':['product','load','special'],
                'model':'cycles','cycles':1,'resources':['shared-core'],'basis':'deliberately invalid arithmetic pricing'}]}
        before=copy.deepcopy(candidate);bound,errors=engine._work.bind_candidate(candidate,work,{},engine.expression)
        self.assertEqual(candidate,before)
        error=next(error for error in errors if 'counted work requires compute cost' in error)
        detail=json.loads(error.split('unit_breakdown=',1)[1])
        self.assertEqual(detail['non_counted_work_ids'],['load'])
        self.assertEqual(detail['by_unit'][0],{'unit':'primitive-X','work_ids':['product'],'operations':72,'unknown_work_ids':[]})
        self.assertEqual(detail['by_unit'][1],{'unit':None,'work_ids':['special'],'operations':None,'unknown_work_ids':['special']})
        self.assertNotIn('scheduled_cycles',bound['cost_groups'][0])

    def test_unknown_profile_diagnostic_lists_actual_profile_ids(self):
        document,sources=fixture();document['candidates'][0]['cost_groups'][0]['workload']='invented-profile'
        before=copy.deepcopy(document);report=engine.evaluate(document,sources)
        self.assertFalse(report['ok']);self.assertEqual(document,before)
        error=next(error for error in report['errors'] if 'unknown workload invented-profile' in error)
        self.assertEqual(json.loads(error.split('available_workloads=',1)[1]),['primary'])

    def test_copyable_unknown_selector_cannot_make_complete_budget_pass(self):
        document,sources=fixture();extra=copy.deepcopy(document['work_model']['items'][0])
        extra.update(id='special',status='unresolved',count={'kind':'unsupported','unit':'special-op','reason':'source cost unavailable'})
        document['work_model']['items'].append(extra)
        summary=engine.evaluate_work_model(document['work_model'],document['requirements'],sources)['compact_summary']
        group=next(row for row in summary['profiles'][0]['by_unit'] if row['unit']=='special-op')
        self.assertIsNone(group['operations']);self.assertEqual(group['unknown_work_ids'],['special'])
        for candidate in document['candidates']:
            cost=copy.deepcopy(candidate['cost_groups'][0]);cost.update(id='special-cost',work_ids=group['work_ids_selector'])
            candidate['cost_groups'].append(cost)
            candidate['workloads'][0]['components'].append({'id':'special-stage','composition':'serial',
                'references':['special-cost'],'basis':'declared unknown cost remains a separate bound obligation'})
        before=copy.deepcopy(document);report=engine.evaluate(document,sources)
        self.assertTrue(report['ok'],report['errors']);self.assertEqual(document,before)
        self.assertEqual(report['gate']['status'],'unknown')
        self.assertIn('primary/special',report['gate']['unsupported_models'])

    def test_unknown_summary_does_not_invent_counts_or_merge_units(self):
        model,sources=self.source_work('multiply',{'first':3,'second':11})
        unknown=copy.deepcopy(model['items'][0]);unknown.update(id='special',status='unresolved',count={'kind':'unsupported','unit':'special-op','reason':'cost count unavailable'})
        model['items'].append(unknown)
        model['items'][0]['status']='unresolved'
        report=engine.evaluate_work_model(model,{'facts':[],'constraints':[],'runtime_parameters':[]},sources)
        self.assertTrue(report['ok'],report['errors']);first=report['compact_summary']['profiles'][0]
        groups={row['unit']:row for row in first['by_unit']}
        self.assertEqual(groups['multiply']['operations'],72)
        self.assertEqual(groups['multiply']['unknown_work_ids'],['product'])
        self.assertIsNone(groups['special-op']['operations'])
        self.assertEqual(first['unknown_work_ids'],['product','special'])
        self.assertNotIn('operations',first)

    def test_units_are_source_strings_before_summary_or_selector(self):
        for unit in (1,True,[],{},'', '   '):
            with self.subTest(unit=unit):
                document,sources=fixture();document['work_model']['items'][0]['count']['unit']=unit
                report=engine.evaluate_work_model(document['work_model'],document['requirements'],sources)
                self.assertFalse(report['ok']);self.assertTrue(any('string unit' in e or 'unit must be nonempty string' in e for e in report['errors']),report['errors'])

    def test_standalone_bundle_expands_selectors_without_harness(self):
        document,sources=fixture()
        for candidate in document['candidates']:candidate['cost_groups'][0]['work_ids']={'unit':'ops'}
        directory=Path(tempfile.mkdtemp());scripts=Path(engine.__file__).parent
        for script in scripts.glob('*.py'):shutil.copy2(script,directory/script.name)
        proc=subprocess.run([sys.executable,'-B',str(directory/'evaluate.py')],cwd=directory,input=json.dumps({'document':document,'sources':sources}),text=True,capture_output=True,check=True)
        report=json.loads(proc.stdout);self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(report['candidates']['small']['ledger']['cost_groups'][0]['binding']['resolved_work_ids'],['arithmetic'])


if __name__=='__main__':unittest.main()
