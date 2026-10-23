import copy
import importlib.util
import json
from pathlib import Path
import unittest

BASE = Path(__file__).resolve().parents[1]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module
source = load('independent_source_model', BASE/'skills/microarchitecture-audit/scripts/source_model.py')
engine = load('shared_source_math', BASE/'skills/microarchitecture-design/scripts/evaluate.py')


def witness():
    texts = {'spec.md': 'rows=3\ninner=4\ntokens=5\nheads=2\noutput int32\n',
             'reference.py': 'y = weights @ vector\nscore = q @ keys.T\ncontext = probabilities @ values\n'}
    refs = [{'file':'spec.md','start':1,'end':5}, {'file':'reference.py','start':1,'end':3}]
    facts = [{'id': n, 'source': {'file':'spec.md','line':i,'number':1}, 'unit':'count'} for i,n in enumerate(('rows','inner','tokens','heads'),1)]
    facts += [{'id':'external_bits','source':{'file':'spec.md','line':5,'number':1},'unit':'bits'}]
    scope = {'kind':'element','axis':'token','multiplicity':1}
    model = {'facts':facts,'workloads':[{'id':'p','quantity':'tokens','unit':'tokens','basis':'source task','references':refs}],
        'computations':[{'id':'projection','semantics':'per-token GEMV','steps':[{'id':'gemv','kind':'matmul','lhs':['rows','inner'],'rhs':['inner']}], 'scope':scope,'basis':'matrix operands','references':refs}],
        'states':[{'id':'cache','shape':['workload_elements','inner'],'lifecycle':'grows within task, fresh logical state per task','basis':'logical cache only','references':refs}],
        'flows':[{'id':'out','direction':'write','encoding':'fixed_external','fields':[{'shape':['rows'],'element_bits':'external_bits'},{'shape':[1],'element_bits':'external_bits'}],'scope':scope,'basis':'fixed external row plus index','references':refs}],
        'limits':'Does not select cache bitwidth or prove original-source interpretation completeness.'}
    return model, texts


def comparison_fixture(extra_profiles=(), fail_unused_fact=False):
    from harness.tests.test_architecture import fixture
    proposal, texts = fixture()
    originals = dict(texts)
    refs = [{'file': key, 'quote': text.strip()} for key, text in originals.items()]
    analysis = {'facts': [], 'workloads': [{'id': 'task', 'quantity': 12, 'unit': 'samples',
                                          'basis': 'mechanical source profile', 'references': refs}],
                'computations': [{'id': 'path', 'semantics': 'mechanical elementwise count',
                                  'steps': [{'id': 'effects', 'kind': 'elementwise',
                                             'shape': ['workload_elements * 20 / 3'], 'ops_per_element': 1}],
                                  'scope': {'kind': 'task', 'axis': 'run'}, 'basis': 'fixed synthetic test effects', 'references': refs}],
                'states': [], 'flows': [], 'limits': 'Mechanical correspondence test only, not semantic source completeness.'}
    for key, quantity in extra_profiles:
        analysis['workloads'].append(dict(analysis['workloads'][0], id=key, quantity=quantity))
    if fail_unused_fact:
        analysis['facts'].append({'id': 'unused', 'source': {'file': 'spec.md', 'line': 1, 'number': 1}})
    raw = source.analyze(analysis, originals, engine)
    baseline = source.source_view(raw, analysis, originals, engine)
    texts['design.json'] = json.dumps(proposal, ensure_ascii=False, sort_keys=True)
    texts['architecture.md'] = proposal['architecture_md']
    model = {'workload_map': {'task': 'primary'},
             'count_checks': [{'id': 'count', 'workload': 'task', 'source_paths': ['path'],
                               'candidate_work_ids': ['arithmetic'], 'realization': 'direct',
                               'basis': 'synthetic exact operation correspondence',
                               'references': [{'file': 'spec.md', 'quote': 'items 12'}]}]}
    return model, baseline, proposal, texts


class SourceWitnessTests(unittest.TestCase):
    def test_long_primitive_list_keeps_exact_scope_and_transaction_binding(self):
        m,s=witness()
        m['computations'][0]['steps']=[{'id':'projection-'+str(i),'kind':'gemv',
                                      'input_elements':'inner','output_elements':'rows'} for i in range(64)]
        before=copy.deepcopy(m);r=source.analyze(m,s,engine)
        self.assertTrue(r['ok'],r['errors']);self.assertEqual(m,before)
        row=r['computations']['projection']
        self.assertEqual(row['per_occurrence']['p'],64*3*4)
        self.assertEqual(row['totals']['p'],64*3*4*5)
        self.assertEqual(len(row['steps']['p']),64)
        verified=source.source_view(r,m,s,engine)
        self.assertTrue(verified['source_receipts_verified'])
        self.assertTrue(verified['source_coverage_complete'])
        self.assertEqual(verified['computations']['projection']['totals']['p'],64*3*4*5)

    def test_long_primitive_list_does_not_bypass_existing_validation(self):
        for failure in ('shape','unit','duplicate'):
            with self.subTest(failure=failure):
                m,s=witness()
                steps=[{'id':'projection-'+str(i),'kind':'gemv',
                        'input_elements':'inner','output_elements':'rows'} for i in range(64)]
                if failure=='shape':steps[-1]['output_elements']=0
                elif failure=='unit':steps[-1]={'id':'scalar','kind':'elementwise','shape':[1],'ops_per_element':1}
                else:steps[-1]['id']=steps[0]['id']
                m['computations'][0]['steps']=steps;r=source.analyze(m,s,engine)
                self.assertFalse(r['ok']);self.assertNotIn('projection',r['computations'])
                self.assertTrue(r['states']);self.assertTrue(r['flows'])
                expected={'shape':'positive integer','unit':'split MAC','duplicate':'unique nonempty'}[failure]
                self.assertTrue(any(expected in error for error in r['errors']),r['errors'])

    def test_primitive_execution_size_guard_is_separate_from_shape_validation(self):
        m,s=witness()
        m['computations'][0]['steps']=[{'id':'dot-'+str(i),'kind':'dot','width':'inner'}
                                      for i in range(source.MAX_PRIMITIVE_STEPS+1)]
        r=source.analyze(m,s,engine)
        self.assertFalse(r['ok']);self.assertNotIn('projection',r['computations'])
        self.assertTrue(any('execution-size limit' in error for error in r['errors']))

    def test_long_native_recipe_executes_without_claiming_equivalence(self):
        m,b,p,s=comparison_fixture()
        row=m['count_checks'][0]
        row.update(realization='transformed',equivalence_evidence='Synthetic mapping still needs independent semantic review.',
                   physical_realization={'basis':'actual scalar cost group','groups':[{
                       'cost_group':'compute','steps':[{'id':'scalar-'+str(i),'kind':'elementwise',
                                                       'shape':[1],'ops_per_element':1} for i in range(80)],
                       'scope':{'kind':'task','axis':'run'},'basis':'all declared native scalar effects',
                       'references':[{'file':'design.json','quote':'"selected": "small"'}]}]})
        before=copy.deepcopy(m);r=source.compare_work(m,b,p,s,engine)
        self.assertTrue(r['ok'],r['errors']);self.assertEqual(m,before)
        observed=r['observations'][0]
        self.assertEqual(observed['physical_status'],'pass')
        self.assertEqual(observed['semantic_equivalence_status'],'unverified')
        physical=observed['physical_realization']['groups'][0]
        self.assertEqual(len(physical['steps']),80)
        self.assertEqual(physical['physical_operations'],80)
        steps=row['physical_realization']['groups'][0]['steps']
        steps[-1]['shape']=[0]
        rejected=source.compare_work(m,b,p,s,engine)
        self.assertFalse(rejected['ok']);self.assertTrue(any('positive integers' in error for error in rejected['errors']))
        steps[-1]['shape']=[1];steps[-1]['id']=steps[0]['id']
        rejected=source.compare_work(m,b,p,s,engine)
        self.assertFalse(rejected['ok']);self.assertTrue(any('physical step ids must be unique' in error for error in rejected['errors']))

    def test_extra_annotations_are_recorded_without_affecting_math(self):
        m,s=witness();m['analysis_limits']=['Extra source uncertainty is preserved, not a second computation.']
        r=source.analyze(m,s,engine);self.assertTrue(r['ok'],r['errors'])
        self.assertEqual(r['ignored_annotation_fields'],['analysis_limits']);self.assertEqual(r['computations']['projection']['totals']['p'],60)

    def test_gemv_and_dot_roles_have_fixed_distinct_costs(self):
        m,s=witness();m['computations'][0]['steps']=[{'id':'gemv','kind':'gemv','input_elements':'inner','output_elements':'rows'}]
        r=source.analyze(m,s,engine);self.assertTrue(r['ok'],r['errors']);self.assertEqual(r['computations']['projection']['totals']['p'],60)
        m['computations'][0]['steps']=[{'id':'dot','kind':'dot','width':'inner'}]
        r=source.analyze(m,s,engine);self.assertTrue(r['ok'],r['errors']);self.assertEqual(r['computations']['projection']['totals']['p'],20)

    def test_textual_limit_list_and_source_fact_span_are_preserved(self):
        m,s=witness();m['limits']=['Unsupported scalar paths need independent review.']
        m['facts'].append({'id':'derived_rows','formula':'rows','unit':'count','citations':[{'file':'spec.md','start':1,'end':1}]})
        m['computations'][0]['steps'][0]['lhs'][0]='derived_rows'
        before=copy.deepcopy(m);r=source.analyze(m,s,engine)
        self.assertTrue(r['ok'],r['errors']);self.assertEqual(m,before)
        self.assertEqual(r['computations']['projection']['totals']['p'],60)
        self.assertTrue(any(x.get('location')=='fact/derived_rows/citations/0' for x in r['source_bindings']))

    def test_independent_declaration_errors_are_collected_in_one_response(self):
        m,s=witness();m['limits']=[];m['facts'][0].pop('unit')
        r=source.analyze(m,s,engine);self.assertFalse(r['ok'])
        self.assertTrue(any('limits' in x for x in r['errors']))
        self.assertTrue(any('no attached unit' in x for x in r['errors']))

    def test_non_expression_ids_are_free_strings_and_storage_unit_alias_preserves_value(self):
        m,s=witness();m['workloads'][0]['id']='profile-with-hyphen'
        m['computations'][0]['id']='compute-path'
        r=source.analyze(m,s,engine);self.assertTrue(r['ok'],r['errors'])
        self.assertEqual(r['computations']['compute-path']['totals']['profile-with-hyphen'],60)
        m['facts'][0]['id']='invalid-expression-name'
        self.assertFalse(source.analyze(m,s,engine)['ok'])

    def test_compare_work_commits_rows_independently(self):
        model, baseline, proposal, texts = comparison_fixture()
        model['count_checks'].append(dict(model['count_checks'][0], id='broken', source_paths=['absent']))
        before = copy.deepcopy((model, baseline, proposal, texts))
        result = source.compare_work(model, baseline, proposal, texts, engine)
        self.assertFalse(result['ok'])
        self.assertEqual([row['status'] for row in result['subreceipts']], ['executed', 'declaration_failed'])
        self.assertEqual(len(result['observations']), 1)
        self.assertTrue(result['observations'][0]['matches'])
        self.assertEqual(result['values']['count__source'], 80)
        self.assertEqual(result['subreceipts'][1]['values'], {})
        self.assertEqual(result['subreceipts'][1]['calculation_ids'], [])
        self.assertEqual((model, baseline, proposal, texts), before)

    def test_compare_work_profile_subset_retains_receipt_and_missing_paths(self):
        model, baseline, proposal, texts = comparison_fixture(extra_profiles=[('second', 24)])
        result = source.compare_work(model, baseline, proposal, texts, engine)
        self.assertTrue(result['ok'], result['errors'])
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(result['execution_status'], 'partial')
        self.assertIn({'side': 'source', 'workload': 'second'}, result['missing_profiles'])
        self.assertIn({'workload': 'second', 'source_path': 'path'}, result['missing_count_checks'])
        self.assertEqual(result['values']['count__source'], 80)
        model['count_checks'] = []
        result = source.compare_work(model, baseline, proposal, texts, engine)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['values'], {})
        self.assertEqual(len(result['missing_count_checks']), 2)

    def test_compare_work_bad_mapping_does_not_erase_valid_profile(self):
        model, baseline, proposal, texts = comparison_fixture()
        model['workload_map']['absent'] = ['malformed']
        result = source.compare_work(model, baseline, proposal, texts, engine)
        self.assertFalse(result['ok'])
        self.assertTrue(result['observations'][0]['matches'])
        self.assertEqual(result['values']['count__source'], 80)
        self.assertTrue(any('profile/absent' in error for error in result['errors']))

    def test_compare_work_uses_valid_source_transaction_when_unrelated_fact_fails(self):
        model, baseline, proposal, texts = comparison_fixture(fail_unused_fact=True)
        self.assertFalse(baseline['ok']); self.assertTrue(baseline['source_receipts_verified'])
        result = source.compare_work(model, baseline, proposal, texts, engine)
        self.assertTrue(result['ok'], result['errors'])
        self.assertTrue(result['observations'][0]['matches'])
        self.assertEqual(result['values']['count__source'], 80)
        self.assertFalse(result['source_coverage_complete']); self.assertFalse(result['coverage_complete'])
        self.assertTrue(result['missing_source_declarations'])

    def test_compare_work_rejects_raw_or_fields_only_source_baseline(self):
        model, baseline, proposal, texts = comparison_fixture()
        bare = {key: value for key, value in baseline.items() if not key.startswith('source_')}
        result = source.compare_work(model, bare, proposal, texts, engine)
        self.assertFalse(result['ok']); self.assertEqual(result['values'], {})
        self.assertTrue(any('transaction-verified' in error for error in result['errors']))

    def test_source_view_keeps_failed_declared_profile_visible_in_missing_count_coverage(self):
        model, baseline, proposal, texts = comparison_fixture(extra_profiles=[('failed-profile', 0)])
        self.assertEqual(baseline['declared_workloads'], ['task', 'failed-profile'])
        self.assertEqual(baseline['workloads'], {'task': 12})
        result = source.compare_work(model, baseline, proposal, texts, engine)
        self.assertTrue(result['ok'], result['errors'])
        self.assertFalse(result['coverage_complete'])
        self.assertIn({'side': 'source', 'workload': 'failed-profile'}, result['missing_profiles'])
        self.assertIn({'workload': 'failed-profile', 'source_path': 'path'}, result['missing_count_checks'])
        self.assertEqual(result['values']['count__source'], 80)

    def test_shapes_scopes_and_fixed_output(self):
        m,s = witness(); before=copy.deepcopy(m); r=source.analyze(m,s,engine)
        self.assertTrue(r['ok'],r['errors']); self.assertEqual(m,before)
        self.assertEqual(r['computations']['projection']['totals']['p'],60)
        self.assertEqual(r['states']['cache']['profiles']['p']['elements'],20)
        self.assertNotIn('bits',r['states']['cache']['profiles']['p'])
        self.assertEqual(r['flows']['out']['totals']['p']['bytes'],80)

    def test_causal_two_matmuls_add_and_prefix_once(self):
        m,s=witness(); c=m['computations'][0]
        c['steps']=[{'id':'score','kind':'matmul','lhs':['inner'],'rhs':['inner']}, {'id':'context','kind':'matmul','lhs':[1],'rhs':[1,'rows']}]
        c['scope']={'kind':'prefix','axis':'token','prefix_axis':'history','first':1,'step':1,'multiplicity':'heads'}
        r=source.analyze(m,s,engine); self.assertTrue(r['ok'],r['errors'])
        self.assertEqual(r['computations']['projection']['totals']['p'],210)
        c['steps'][0]['lhs']=['inner*tokens']; c['steps'][0]['rhs']=['inner*tokens']
        r=source.analyze(m,s,engine);self.assertFalse(r['ok']);self.assertTrue(any('workload-axis' in x for x in r['errors']))

    def test_invalid_dimensions_or_arbitrary_total_cannot_pass(self):
        m,s=witness();m['computations'][0]['steps'][0]['rhs']=[99]
        self.assertFalse(source.analyze(m,s,engine)['ok'])
        m,s=witness();m['computations'][0]['steps'][0]={'id':'shortcut','kind':'custom','operations':1}
        self.assertFalse(source.analyze(m,s,engine)['ok'])
        m,s=witness();m['facts'][0]['value']=900
        self.assertFalse(source.analyze(m,s,engine)['ok'])

    def test_abi_dependent_flow_and_source_state_dont_choose_internal_encoding(self):
        m,s=witness();m['flows'][0].update(encoding='abi_dependent',fields=[])
        r=source.analyze(m,s,engine);self.assertTrue(r['ok'],r['errors']);self.assertEqual(r['flows']['out']['totals'],{})
        m['states'][0]['element_bits']=32
        self.assertFalse(source.analyze(m,s,engine)['ok'])

    def test_proposal_source_or_wrong_repeated_quote_rejected(self):
        m,s=witness();s['design.json']='{}';self.assertFalse(source.analyze(m,s,engine)['ok'])
        m,s=witness();m['computations'][0]['references']=[{'file':'spec.md','quote':'='}]
        self.assertFalse(source.analyze(m,s,engine)['ok'])


if __name__=='__main__': unittest.main()
