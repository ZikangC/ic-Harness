import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

from harness.tests.test_architecture import fixture


BASE = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load('candidate_audit_tests', BASE / 'skills/microarchitecture-audit/scripts/candidate_checks.py')
engine = load('candidate_shared_math_tests', BASE / 'skills/microarchitecture-design/scripts/evaluate.py')


def example():
    proposal, sources = fixture()
    sources['spec.md'] += '\nrequest_peak = 1\n'
    originals = {key: sources[key] for key in ('spec.md', 'reference.py')}
    refs = [{'file': key, 'quote': text.strip()} for key, text in originals.items()]
    baseline = {
        'facts': [{'id': 'request_peak', 'source': {'file': 'spec.md', 'line': 2, 'number': 1},
                   'unit': 'requests/cycle'}],
        'workloads': [{'id': 'task', 'quantity': 12, 'unit': 'samples',
                       'basis': 'isolated synthetic source workload', 'references': refs}],
        'computations': [{'id': 'path', 'semantics': 'mechanical scalar work',
                          'steps': [{'id': 'effects', 'kind': 'elementwise', 'shape': [80], 'ops_per_element': 1}],
                          'scope': {'kind': 'task', 'axis': 'run'}, 'basis': 'synthetic fixed effects', 'references': refs}],
        'flows': [{'id': 'input', 'direction': 'read', 'encoding': 'fixed_external',
                   'fields': [{'shape': [64], 'element_bits': 8}],
                   'scope': {'kind': 'task', 'axis': 'run'}, 'basis': 'synthetic fixed payload', 'references': refs}],
        'states': [], 'limits': 'Mechanical witness fixture only; does not prove semantic source completeness.',
    }
    sources['design.json'] = json.dumps(proposal, ensure_ascii=False, sort_keys=True)
    sources['architecture.md'] = proposal['architecture_md']
    refs = [{'file': 'design.json', 'quote': '"selected": "small"'}]
    model = {
        'candidate': 'small', 'workload_map': {'task': 'primary'},
        'count_checks': [{'id': 'count', 'workload': 'task', 'source_paths': ['path'],
                          'candidate_work_ids': ['arithmetic'], 'realization': 'direct',
                          'basis': 'same synthetic elementwise path',
                          'references': [{'file': 'spec.md', 'quote': 'items 12'}]}],
        'flow_bindings': [{'id': 'input_transport', 'flow_ids': ['input'], 'workload': 'task', 'traffic_ids': ['reads'],
                           'basis': 'one whole input payload per traffic transfer'}],
        'service_windows': [{'id': 'request_slots', 'workload': 'task',
                             'intervals': [{'start': 0, 'end': 120}],
                             'resource': {'kind': 'external_requests', 'capacity_fact': 'request_peak'},
                             'demands': [{'traffic_id': 'reads'}],
                             'basis': 'requests may overlap scalar computation', 'references': refs}],
        'operand_checks': [{'window_id': 'load', 'cost_group': 'compute',
                            'issues': [{'cycle': 1, 'lanes': [0], 'operands': ['weight']}],
                            'basis': 'complete active lanes and roles of one finite scalar issue'}],
    }
    return model, baseline, proposal, sources


def execute(model, baseline, proposal, sources):
    sources = dict(sources, **{'design.json': json.dumps(proposal, ensure_ascii=False, sort_keys=True),
                              'architecture.md': proposal['architecture_md']})
    budget = engine.evaluate(proposal, {key: sources[key] for key in ('spec.md', 'reference.py')})
    return audit.check(model, source_result(baseline, sources), proposal, budget, sources, engine, source_declaration=baseline)


def source_result(declaration, sources):
    originals = {key: sources[key] for key in ('spec.md', 'reference.py')}
    return audit._source.analyze(declaration, originals, engine)


def check_rows(result, prefix):
    return [row for row in result['checks'] if row['id'].startswith(prefix)]


class CandidateWitnessTests(unittest.TestCase):
    def test_service_mapping_error_names_current_request_profiles_without_inheriting_an_earlier_map(self):
        model,baseline,proposal,sources=example()
        second=copy.deepcopy(baseline['workloads'][0]);second['id']='second_task'
        baseline['workloads'].append(second)
        model['service_windows'][0]['workload']='second_task'
        earlier=copy.deepcopy(model);earlier['workload_map']['second_task']='primary'
        accepted=execute(earlier,baseline,proposal,sources)
        self.assertTrue(accepted['ok'],accepted['errors'])
        self.assertEqual(check_rows(accepted,'service__')[0]['actual'],8)
        before=copy.deepcopy((model,baseline,proposal,sources))
        rejected=execute(model,baseline,proposal,sources)
        self.assertFalse(rejected['ok']);self.assertEqual((model,baseline,proposal,sources),before)
        error=next(error for error in rejected['errors'] if 'service workload' in error)
        self.assertIn("'second_task' is not mapped in this request",error)
        self.assertIn("mapped workloads: ['task']",error)
        self.assertIn("independent source workloads: ['second_task', 'task']",error)
        failed=next(row for row in rejected['subreceipts'] if row['id']=='service/0')
        self.assertEqual((failed['status'],failed['checks'],failed['values']),('declaration_failed',[],{}))
        self.assertTrue(check_rows(rejected,'count__'));self.assertTrue(check_rows(rejected,'flow__'))
        self.assertFalse(any(row['status']=='fail' for row in rejected['checks']))
        restored=execute(earlier,baseline,proposal,sources)
        self.assertEqual(restored['values'],accepted['values'])
        self.assertEqual(restored['checks'],accepted['checks'])

    def test_external_capacity_selector_is_original_bound_and_not_a_model_value(self):
        m,b,p,s=example();s['spec.md']+='\nShared interface accepts 1 request per cycle.\n'
        m['service_windows'][0]['resource']={'kind':'external_requests','capacity_source':{'file':'spec.md','line':len(s['spec.md'].splitlines()),'number':1},'unit':'requests/cycle'}
        result=execute(m,b,p,s);self.assertTrue(result['ok'],result['errors'])
        self.assertTrue(any(x.get('fact')=='audit_external_capacity' for x in result['citation_bindings']))
        m['service_windows'][0]['resource']['capacity_fact']='request_peak'
        self.assertFalse(execute(m,b,p,s)['ok'])

    def test_explicit_chinese_and_english_capacity_source_needs_no_baseline_rate_fact(self):
        cases=[('峰值每周期接受三个请求、返回两个读 beat。',3),
               ('The interface accepts three requests per clock cycle.',3),
               ('The interface accepts 5 requests per cycle.',5)]
        for sentence,rate in cases:
            with self.subTest(sentence=sentence):
                m,b,p,s=example();b['facts']=[];s['spec.md']+='\n'+sentence+'\n'
                window=m['service_windows'][0]
                window['resource']={'kind':'external_requests','capacity_source':{'file':'spec.md','line':len(s['spec.md'].splitlines()),'number':1}}
                before=copy.deepcopy((m,b,p,s));result=execute(m,b,p,s)
                self.assertTrue(result['ok'],result['errors']);self.assertEqual((m,b,p,s),before)
                check=check_rows(result,'service__')[0]
                self.assertEqual((check['expected'],check['actual'],check['status']),(rate*120,8,'pass'))
                receipt=next(x for x in result['citation_bindings'] if x.get('fact')=='audit_external_capacity')
                self.assertEqual(receipt['materialized_fact']['unit'],'requests/cycle')
                self.assertIn(sentence,receipt['materialized_fact']['citations'][0]['quote'])
                self.assertIn('optimistic aggregate bound',check['basis'])
                self.assertIn('not bank/arbitration or full-schedule proof',check['basis'])

    def test_capacity_source_remains_peak_bound_and_cannot_override_multicycle_count(self):
        m,b,p,s=example();b['facts']=[]
        s['spec.md']+='\n峰值每周期接受两个请求。\n'
        window=m['service_windows'][0];window['intervals']=[{'start':0,'end':3}]
        window['resource']={'kind':'external_requests','capacity_source':{'file':'spec.md','line':len(s['spec.md'].splitlines()),'number':1}}
        result=execute(m,b,p,s);self.assertTrue(result['ok'],result['errors'])
        check=check_rows(result,'service__')[0]
        self.assertEqual((check['expected'],check['actual'],check['status']),(6,8,'fail'))
        # Change the actual normative literal, keeping the selector and declarations.
        s['spec.md']=s['spec.md'].replace('峰值每周期接受两个请求。','峰值每周期接受四个请求。')
        changed=execute(m,b,p,s);self.assertTrue(changed['ok'],changed['errors'])
        self.assertEqual(check_rows(changed,'service__')[0]['expected'],12)
        self.assertEqual(check_rows(changed,'service__')[0]['status'],'pass')
        s['spec.md']=s['spec.md'].replace('峰值每周期接受四个请求。','每三个周期接受四个请求。')
        window['resource']['capacity_source']['number']=2;window['resource']['unit']='requests/cycle'
        rejected=execute(m,b,p,s)
        self.assertFalse(rejected['ok']);self.assertTrue(any('override is not a conversion' in error for error in rejected['errors']),rejected['errors'])

    def test_generic_flow_service_and_scalar_issue_execute_without_input_mutation(self):
        args = example()
        before = copy.deepcopy(args)
        result = execute(*args)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(args, before)
        self.assertTrue(all(row['status'] == 'pass' for row in result['checks']), result['checks'])
        self.assertEqual(result['missing_flow_checks'], [])
        self.assertEqual(result['missing_service_checks'], [])
        self.assertEqual(result['missing_operand_checks'], [])
        self.assertIn('finite', result['scope'])

    def test_payload_padding_does_not_close_fixed_external_deficit(self):
        m, b, p, s = example()
        tr = p['candidates'][0]['traffic'][0]
        tr.update(payload_bytes=1, transfers=8, alignment_bytes=8)
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        flow = check_rows(r, 'flow__')[0]
        self.assertEqual((flow['expected'], flow['actual'], flow['status']), (64, 8, 'fail'))

    def test_service_declaration_coverage_is_distinct_from_unknown_capability(self):
        m, b, p, s = example()
        b['facts'] = []
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertTrue(r['coverage_complete'])
        self.assertEqual(r['missing_service_checks'], ['reads'])
        self.assertEqual(r['missing_service_declarations'], [])
        self.assertEqual(check_rows(r, 'service__')[0]['status'], 'unverified')
        m['service_windows'] = []
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertFalse(r['coverage_complete'])
        self.assertEqual(r['missing_service_declarations'], ['reads'])

    def test_count_mismatch_is_executed_negative_evidence(self):
        m, b, p, s = example()
        b['computations'][0]['steps'][0]['shape'] = [96]
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(check_rows(r, 'count__')[0]['status'], 'fail')

    def test_abi_dependent_flow_does_not_freeze_internal_encoding(self):
        m, b, p, s = example()
        b['flows'][0].update(encoding='abi_dependent', fields=[])
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(check_rows(r, 'flow__')[0]['status'], 'unverified')
        self.assertEqual(r['missing_flow_checks'], [])

    def test_packed_multiple_flows_share_one_payload_without_duplicate_credit(self):
        m, b, p, s = example()
        b['flows'].append(dict(copy.deepcopy(b['flows'][0]), id='other'))
        m['flow_bindings'][0]['flow_ids'].append('other')
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(check_rows(r, 'flow__')[0]['status'], 'fail')
        self.assertEqual(check_rows(r, 'flow__')[0]['expected'], 128)
        self.assertEqual(r['missing_flow_checks'], [])
        p['candidates'][0]['traffic'][0]['transfers'] = 16
        r = execute(m, b, p, s)
        self.assertEqual(check_rows(r, 'flow__')[0]['status'], 'pass')

    def test_packed_byte_aligned_flow_group_retains_program_quantities(self):
        m, b, p, s = example()
        original = b['flows'][0]
        b['flows'] = [dict(copy.deepcopy(original), id=fid, direction='write',
                          fields=[{'shape': [size], 'element_bits': 8}])
                      for fid, size in (('body', 32), ('tag', 4))]
        p['candidates'][0]['traffic'] = [{'id': 'packed', 'model': 'posted_write',
            'payload_bytes': 36, 'transfers': 1, 'alignment_bytes': 8,
            'transaction_bytes': 8, 'basis': 'two fixed fields packed once'}]
        m['flow_bindings'] = [{'id': 'result', 'flow_ids': ['body', 'tag'], 'workload': 'task',
            'traffic_ids': ['packed'], 'basis': 'whole fixed fields share a packed transfer'}]
        m['service_windows'][0]['demands'] = [{'traffic_id': 'packed'}]
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        flow = check_rows(r, 'flow__')[0]
        self.assertEqual((flow['required_bytes'], flow['available_bytes'], flow['deficit_bytes']), (36, 36, 0))
        self.assertEqual(flow['unit'], 'bytes')
        self.assertEqual(flow['flow_ids'], ['body', 'tag'])
        self.assertEqual(flow['candidate_workload'], 'primary')
        self.assertEqual(check_rows(r, 'flow_inventory__')[0]['status'], 'pass')
        p['candidates'][0]['traffic'][0]['payload_bytes'] = 32
        r = execute(m, b, p, s)
        self.assertEqual(check_rows(r, 'flow__')[0]['deficit_bytes'], 4)
        inventory = check_rows(r, 'flow_inventory__')[0]
        self.assertEqual((inventory['status'], inventory['required_bytes'], inventory['available_bytes']), ('fail', 36, 32))
        self.assertEqual(inventory['kind'], 'candidate_inventory_bound')
        self.assertTrue(inventory['coverage_complete'])
        # Wire padding remains 32 bytes here; even larger padding cannot replace
        # semantic bytes that were never included in the transfer payload.
        p['candidates'][0]['traffic'][0]['alignment_bytes'] = 40
        self.assertEqual(check_rows(execute(m, b, p, s), 'flow__')[0]['status'], 'fail')

    def test_non_byte_aligned_fields_use_only_task_bit_lower_bound(self):
        m, b, p, s = example()
        original = b['flows'][0]
        b['workloads'][0]['quantity'] = 3
        p['work_model']['workloads'][0]['quantity'] = 3
        b['flows'] = [dict(copy.deepcopy(original), id=fid,
                          fields=[{'shape': [1], 'element_bits': 4}],
                          scope={'kind': 'element', 'axis': 'nibble', 'multiplicity': 1})
                      for fid in ('a', 'b')]
        tr = p['candidates'][0]['traffic'][0]
        tr.update(payload_bytes=3, transfers=1)
        m['flow_bindings'][0]['flow_ids'] = ['a', 'b']
        flow = check_rows(execute(m, b, p, s), 'flow__')[0]
        self.assertEqual((flow['expected'], flow['actual'], flow['status']), (3, 3, 'pass'))
        self.assertFalse(flow['byte_aligned'])
        self.assertEqual(flow['required_bits'], 24)
        self.assertIn('necessary bound only', flow['basis'])

    def test_bound_subset_deficit_is_not_complete_inventory_deficit(self):
        m, b, p, s = example()
        p['candidates'][0]['traffic'][0]['payload_bytes'] = 4
        extra = copy.deepcopy(p['candidates'][0]['traffic'][0])
        extra.update(id='other_path', payload_bytes=4)
        p['candidates'][0]['traffic'].append(extra)
        r = execute(m, b, p, s)
        self.assertEqual(check_rows(r, 'flow__')[0]['status'], 'fail')
        inventory = check_rows(r, 'flow_inventory__')[0]
        self.assertEqual((inventory['status'], inventory['required_bytes'], inventory['available_bytes']), ('pass', 64, 64))
        self.assertEqual(inventory['traffic_ids'], ['reads', 'other_path'])
        self.assertEqual(inventory['bound_traffic_ids'], ['reads'])

    def test_unknown_direction_or_coverage_cannot_make_inventory_defect(self):
        m, b, p, s = example()
        p['candidates'][0]['traffic'][0]['payload_bytes'] = 1
        unknown = {'id': 'unknown_path', 'model': 'unsupported', 'payload_bytes': 1,
                   'transfers': 1, 'alignment_bytes': 1, 'transaction_bytes': 1,
                   'reason': 'direction and protocol undecided', 'basis': 'deliberately unknown transport'}
        p['candidates'][0]['traffic'].append(unknown)
        self.assertEqual(check_rows(execute(m, b, p, s), 'flow_inventory__')[0]['status'], 'unverified')
        p['candidates'][0]['traffic'].pop()
        p['candidates'][0]['coverage']['traffic']['status'] = 'unknown'
        self.assertEqual(check_rows(execute(m, b, p, s), 'flow_inventory__')[0]['status'], 'unverified')

    def test_inventory_includes_only_current_profile_and_direction(self):
        m, b, p, s = example()
        p['candidates'][0]['traffic'][0]['payload_bytes'] = 1
        p['candidates'][0]['traffic'].extend([
            {'id': 'opposite', 'model': 'posted_write', 'payload_bytes': 128, 'transfers': 1,
             'alignment_bytes': 1, 'transaction_bytes': 8, 'basis': 'opposite direction'},
            dict(p['candidates'][0]['traffic'][0], id='other_profile', payload_bytes=128, workload='unused')])
        inventory = check_rows(execute(m, b, p, s), 'flow_inventory__')[0]
        self.assertEqual((inventory['status'], inventory['available_bytes'], inventory['traffic_ids']), ('fail', 8, ['reads']))

    def test_cross_group_source_or_traffic_credit_is_rejected(self):
        m, b, p, s = example()
        b['flows'].append(dict(copy.deepcopy(b['flows'][0]), id='other'))
        other = dict(m['flow_bindings'][0], id='other_group', flow_ids=['other'])
        m['flow_bindings'].append(other)
        r = execute(m, b, p, s)
        self.assertFalse(r['ok'])
        self.assertEqual(check_rows(r, 'flow__'), [])
        self.assertTrue(any('duplicate whole traffic/profile credit' in error for error in r['errors']))
        other['traffic_ids'] = ['reads_elsewhere']
        p['candidates'][0]['traffic'].append(dict(p['candidates'][0]['traffic'][0], id='reads_elsewhere'))
        other['flow_ids'] = ['input']
        r = execute(m, b, p, s)
        self.assertFalse(r['ok'])
        self.assertTrue(any('duplicate whole flow/profile credit' in error for error in r['errors']))

    def test_bad_group_preserves_separate_valid_quantity_receipt(self):
        m, b, p, s = example()
        m['flow_bindings'].append(dict(m['flow_bindings'][0], id='bad', flow_ids=['absent']))
        r = execute(m, b, p, s)
        self.assertFalse(r['ok'])
        self.assertEqual(check_rows(r, 'flow__')[0]['status'], 'pass')
        self.assertTrue(any('flow__input_transport__task' in key for key in r['values']))
        m['flow_bindings'][1] = {'flow_id': 'input', 'workload': 'task',
                                  'traffic_ids': ['reads'], 'basis': 'old alias is not supported'}
        r = execute(m, b, p, s)
        self.assertFalse(r['ok'])
        self.assertEqual(check_rows(r, 'flow__')[0]['status'], 'pass')

    def test_mixed_flow_direction_group_is_not_a_byte_proof(self):
        m, b, p, s = example()
        b['flows'].append(dict(copy.deepcopy(b['flows'][0]), id='other'))
        b['flows'][-1]['direction'] = 'write'
        m['flow_bindings'][0]['flow_ids'].append('other')
        r = execute(m, b, p, s)
        self.assertFalse(r['ok'])
        self.assertEqual(check_rows(r, 'flow__'), [])

    def test_missing_external_port_capability_remains_unverified(self):
        m, b, p, s = example()
        b['facts'] = []
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(check_rows(r, 'service__')[0]['status'], 'unverified')
        self.assertEqual(r['missing_service_checks'], ['reads'])
        b['facts'] = [{'id': 'request_peak', 'source': {'file': 'spec.md', 'line': 2, 'number': 1}, 'unit': 'bytes/cycle'}]
        self.assertEqual(check_rows(execute(m, b, p, s), 'service__')[0]['status'], 'unverified')

    def test_external_read_and_write_share_capacity_and_union_of_windows(self):
        m, b, p, s = example()
        p['candidates'][0]['traffic'].append({'id': 'writes', 'model': 'posted_write',
            'payload_bytes': 8, 'transfers': 8, 'alignment_bytes': 8,
            'transaction_bytes': 8, 'basis': 'synthetic fixed write'})
        first = m['service_windows'][0]
        first['intervals'] = [{'start': 0, 'end': 8}]
        second = copy.deepcopy(first)
        second.update(id='write_slots', demands=[{'traffic_id': 'writes'}])
        m['service_windows'].append(second)
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        group = check_rows(r, 'service__')[0]
        self.assertEqual((group['expected'], group['actual'], group['status']), (8, 16, 'fail'))
        second['intervals'] = [{'start': 8, 'end': 16}]
        r = execute(m, b, p, s)
        group = check_rows(r, 'service__')[0]
        self.assertEqual((group['expected'], group['actual'], group['status']), (16, 16, 'pass'))

    def test_individual_window_cannot_borrow_later_unrelated_time(self):
        m, b, p, s = example()
        p['candidates'][0]['traffic'].append({'id': 'writes', 'model': 'posted_write',
            'payload_bytes': 8, 'transfers': 1, 'alignment_bytes': 8,
            'transaction_bytes': 8, 'basis': 'synthetic fixed write'})
        first = m['service_windows'][0]
        first['intervals'] = [{'start': 0, 'end': 4}]
        second = copy.deepcopy(first)
        second.update(id='later', intervals=[{'start': 4, 'end': 120}],
                      demands=[{'traffic_id': 'writes'}])
        m['service_windows'].append(second)
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        own = next(row for row in r['checks'] if row['id'] == 'service_window__request_slots__capacity')
        self.assertEqual(own['status'], 'fail')
        self.assertEqual(check_rows(r, 'service__')[0]['status'], 'pass')

    def test_service_interval_outside_derived_task_is_negative_evidence(self):
        m, b, p, s = example()
        m['service_windows'][0]['intervals'][0]['end'] = 121
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(check_rows(r, 'service_window__request_slots__bounds')[0]['status'], 'fail')

    def test_macro_expansion_uses_candidate_representation_and_can_overlap(self):
        m, b, p, s = example()
        macro = {'id': 'fill', 'workload': 'task', 'intervals': [{'start': 0, 'end': 80}],
                 'resource': {'kind': 'macro_writes', 'reference': 'banks'},
                 'demands': [{'traffic_id': 'reads', 'representation': {'input_bits': 4, 'stored_bits': 8}}],
                 'basis': 'each whole compressed payload expands into chosen cache representation',
                 'references': m['service_windows'][0]['references']}
        m['service_windows'].append(macro)
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        row = next(row for row in r['checks'] if '__macro_writes__' in row['id'])
        self.assertEqual((row['expected'], row['actual'], row['status']), (160, 128, 'pass'))
        macro['intervals'][0]['end'] = 32
        r = execute(m, b, p, s)
        row = next(row for row in r['checks'] if '__macro_writes__' in row['id'])
        self.assertEqual((row['expected'], row['actual'], row['status']), (64, 128, 'fail'))
        macro['demands'][0]['representation']['stored_bits'] = 4
        row = next(row for row in execute(m, b, p, s)['checks'] if '__macro_writes__' in row['id'])
        self.assertEqual((row['expected'], row['actual'], row['status']), (64, 64, 'pass'))

    def test_macro_representation_or_input_ports_missing_stay_unverified(self):
        m, b, p, s = example()
        m['service_windows'].append({'id': 'fill', 'workload': 'task',
            'intervals': [{'start': 0, 'end': 120}],
            'resource': {'kind': 'macro_writes', 'reference': 'banks'},
            'demands': [{'traffic_id': 'reads'}], 'basis': 'representation not yet resolved',
            'references': m['service_windows'][0]['references']})
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(next(row for row in r['checks'] if '__macro_writes__' in row['id'])['status'], 'unverified')
        m['service_windows'][-1]['demands'][0]['representation'] = {'input_bits': 8, 'stored_bits': 8}
        del p['requirements']['storage_rules'][0]['write_ports']
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(next(row for row in r['checks'] if '__macro_writes__' in row['id'])['status'], 'unverified')

    def test_declared_operand_missing_is_fail_but_partial_lane_issue_is_unknown(self):
        m, b, p, s = example()
        m['operand_checks'][0]['issues'][0]['operands'].append('activation')
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(check_rows(r, 'operands__')[0]['status'], 'fail')
        m['operand_checks'][0]['issues'][0]['operands'] = ['weight']
        p['candidates'][0]['cost_groups'][0]['units'] = 2
        m['service_windows'][0]['intervals'][0]['end'] = 80
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(check_rows(r, 'operands__')[0]['status'], 'unverified')
        self.assertEqual(r['missing_operand_checks'], ['compute'])

    def test_one_input_MAC_does_not_establish_complete_supply(self):
        m, b, p, s = example()
        p['work_model']['items'][0]['count']['unit'] = 'MAC'
        b['computations'][0]['steps'] = [{'id': 'effects', 'kind': 'dot', 'width': 80}]
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(check_rows(r, 'operands__')[0]['status'], 'unverified')
        self.assertEqual(r['missing_operand_checks'], ['compute'])

    def test_packed_words_with_register_broadcast_supply_all_MAC_lanes(self):
        m, b, p, s = example()
        p['work_model']['items'][0]['count']['unit'] = 'MAC'
        b['computations'][0]['steps'] = [{'id': 'effects', 'kind': 'dot', 'width': 80}]
        c = p['candidates'][0]
        c['cost_groups'][0]['units'] = 2
        c['macros'][0]['width'] = 16
        window = c['schedule_windows'][0]
        # This case independently declares the extra register role below.
        window.pop('issues', None)
        window['reads'] = [{'id': 'packed', 'storage_reference': 'banks', 'instance': 0,
                            'word_address': 0, 'cycle': 0}]
        window['uses'] = [{'cost_group': 'compute', 'read_id': 'packed', 'cycle': 1,
                           'lane': lane, 'operand': 'lhs', 'bit_offset': lane * 8, 'width': 8}
                          for lane in (0, 1)]
        c['buffers'].append({'id': 'broadcast', 'count': 1, 'depth': 1, 'width': 8,
            'kind': 'register', 'basis': 'candidate broadcast holding register',
            'objects': [{'id': 'held_input', 'shape': [], 'element_bits': 8, 'copies': 1,
                         'alignment_bits': 8, 'metadata_bits': 0, 'live_in': ['run'],
                         'basis': 'one retained scalar'}]})
        c['area_components'][0]['covers_buffers'] = ['broadcast']
        p['architecture_md'] += '\nThe broadcast scalar is captured at cycle zero and ready at cycle one.\n'
        m['service_windows'][0]['intervals'][0]['end'] = 80
        operand = m['operand_checks'][0]
        operand['issues'] = [{'cycle': 1, 'lanes': [0, 1], 'operands': ['lhs', 'rhs']}]
        operand['local_uses'] = [{'cycle': 1, 'lane': lane, 'operand': 'rhs',
            'kind': 'register', 'buffer': 'broadcast', 'bit_offset': 0, 'width': 8,
            'ready_cycle': 0, 'basis': 'one captured scalar broadcast to both active lanes',
            'references': [{'file': 'architecture.md', 'quote': 'The broadcast scalar is captured at cycle zero and ready at cycle one.'}]}
            for lane in (0, 1)]
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(check_rows(r, 'operands__')[0]['status'], 'pass', r['checks'])
        self.assertEqual(r['missing_operand_checks'], [])
        operand['local_uses'][0]['ready_cycle'] = 2
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(check_rows(r, 'operands__')[0]['status'], 'fail')

    def test_missing_read_latency_cannot_look_like_operand_supply_proof(self):
        m, b, p, s = example()
        del p['requirements']['storage_rules'][0]['read_latency_cycles']
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(check_rows(r, 'operands__')[0]['status'], 'unverified')
        self.assertEqual(r['missing_operand_checks'], ['compute'])

    def test_missing_read_port_and_unsupported_window_are_unverified(self):
        m, b, p, s = example()
        del p['requirements']['storage_rules'][0]['read_ports']
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(check_rows(r, 'operands__')[0]['status'], 'unverified')
        self.assertEqual(r['missing_operand_checks'], ['compute'])
        m, b, p, s = example()
        p['candidates'][0]['schedule_windows'][0].update(model='unsupported', reason='physical organization unresolved')
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(check_rows(r, 'operands__')[0]['status'], 'unverified')

    def test_review_cannot_substitute_unselected_candidate_or_skip_profile(self):
        m, b, p, s = example()
        m['candidate'] = 'large'
        r = execute(m, b, p, s)
        self.assertFalse(r['ok'])
        self.assertTrue(any('selected candidate' in error for error in r['errors']))
        m, b, p, s = example()
        m['workload_map'] = {}
        r = execute(m, b, p, s)
        self.assertFalse(r['ok'])
        self.assertFalse(r['coverage_complete'])
        self.assertIn({'side': 'candidate', 'workload': 'primary'}, r['missing_profiles'])
        self.assertTrue(any(row['status'] == 'executed' and row['section'] == 'operand' for row in r['subreceipts']))

    def test_empty_witnesses_are_missing_not_automatically_accepted(self):
        m, b, p, s = example()
        m.update(flow_bindings=[], service_windows=[], operand_checks=[])
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(r['missing_flow_checks'], [{'flow_id': 'input', 'workload': 'task'}])
        self.assertEqual(r['missing_service_checks'], ['reads'])
        self.assertEqual(r['missing_operand_checks'], ['compute'])

    def test_budget_identity_and_derived_ledger_tamper_rejected(self):
        m, b, p, s = example()
        budget = engine.evaluate(p, {key: s[key] for key in ('spec.md', 'reference.py')})
        budget['design_sha256'] = 'stale'
        self.assertFalse(audit.check(m, source_result(b, s), p, budget, s, engine, source_declaration=b)['ok'])
        budget = engine.evaluate(p, {key: s[key] for key in ('spec.md', 'reference.py')})
        budget['candidates']['small']['ledger']['traffic'][0]['transactions'] = 1
        r = audit.check(m, source_result(b, s), p, budget, s, engine, source_declaration=b)
        self.assertFalse(r['ok'])
        self.assertTrue(any('fresh calculation' in error for error in r['errors']))

    def test_source_parent_aggregate_tamper_cannot_become_candidate_evidence(self):
        m, declaration, p, s = example()
        raw = source_result(declaration, s)
        raw['flows']['input']['totals']['task']['bytes'] = 1
        budget = engine.evaluate(p, {key: s[key] for key in ('spec.md', 'reference.py')})
        result = audit.check(m, raw, p, budget, s, engine, source_declaration=declaration)
        self.assertFalse(result['ok']); self.assertFalse(result['coverage_complete'])
        self.assertEqual(result['values'], {})
        self.assertTrue(any('source transaction integrity failed' in error for error in result['errors']))

    def test_bad_operand_declaration_preserves_executed_flow_deficit(self):
        m, b, p, s = example()
        p['candidates'][0]['traffic'][0].update(payload_bytes=1, transfers=8)
        m['operand_checks'][0]['issues'][0]['lanes'] = [99]
        r = execute(m, b, p, s)
        self.assertFalse(r['ok'])
        self.assertFalse(r['coverage_complete'])
        flow = check_rows(r, 'flow__')[0]
        self.assertEqual((flow['expected'], flow['actual'], flow['status']), (64, 8, 'fail'))
        success = next(row for row in r['subreceipts'] if row['section'] == 'flow')
        failed = next(row for row in r['subreceipts'] if row['section'] == 'operand')
        self.assertEqual(success['status'], 'executed')
        self.assertEqual(failed['status'], 'declaration_failed')
        self.assertTrue(success['values'])
        self.assertEqual(failed['values'], {})
        self.assertEqual(failed['calculation_ids'], [])
        for receipt in r['subreceipts']:
            self.assertEqual(receipt['receipt_sha256'], audit._source.fingerprint({k: v for k, v in receipt.items() if k != 'receipt_sha256'}))
        self.assertTrue(any(value == 8 for value in r['values'].values()))

    def test_misplaced_physical_recipe_is_reported_without_erasing_valid_children(self):
        m, b, p, s = example()
        # Source copy events need not become numerical arithmetic in the
        # selected implementation. Its actual priced routing group is one
        # cycle with zero numerical operations.
        p['work_model']['items'][0].update(
            category='movement', count={'kind': 'none', 'reason': 'fixed byte routing'})
        for candidate in p['candidates']:
            candidate['cost_groups'][0] = {
                'id': 'compute', 'model': 'cycles', 'workload': 'primary',
                'work_ids': ['arithmetic'], 'resources': ['weights'],
                'cycles': 1, 'basis': 'one fixed routing capture'}
            candidate['schedule_windows'] = []
            candidate['coverage']['schedule_windows'] = {
                'status': 'not_applicable', 'basis': 'no numerical arithmetic issue'}
        m['service_windows'][0]['intervals'] = [{'start': 0, 'end': 41}]
        m['operand_checks'] = []
        m['count_checks'][0].update(
            realization='transformed',
            equivalence_evidence='Source copy events map to exact fixed routing; '
                                 'independent semantic review remains required.')
        recipe = {'basis': 'actual fixed routing cost', 'groups': [{
            'cost_group': 'compute', 'steps': [],
            'scope': {'kind': 'task', 'axis': 'run'},
            'basis': 'no numerical primitive for fixed byte routing',
            'references': [{'file': 'design.json', 'quote': '"selected": "small"'}]}]}
        m['physical_realization'] = recipe
        r = execute(m, b, p, s)
        self.assertFalse(r['ok'])
        self.assertFalse(r['coverage_complete'])
        self.assertTrue(any('count_checks[].physical_realization' in e for e in r['errors']))
        failed = next(row for row in r['subreceipts'] if row['section'] == 'model')
        self.assertEqual(failed['status'], 'declaration_failed')
        self.assertEqual(failed['values'], {})
        flow = next(row for row in r['subreceipts'] if row['section'] == 'flow')
        self.assertEqual(flow['status'], 'executed')
        self.assertEqual(check_rows(r, 'flow__')[0]['actual'], 64)
        self.assertTrue(flow['values'])
        self.assertTrue(all(r['values'][key] == value for key, value in flow['values'].items()))
        # Repair only the request declaration; no production compatibility
        # conversion, proposal edit or source arithmetic requirement is added.
        m['count_checks'][0]['physical_realization'] = m.pop('physical_realization')
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertTrue(r['coverage_complete'])
        observation = r['work_result']['observations'][0]
        self.assertEqual(observation['physical_status'], 'pass')
        self.assertEqual(observation['semantic_equivalence_status'], 'unverified')
        physical = check_rows(r, 'count__')[1:]
        self.assertTrue(physical)
        self.assertTrue(all(row['status'] == 'pass' for row in physical))
        self.assertTrue(any(row['expected'] == row['actual'] == 0 for row in physical))

    def test_local_profile_receipt_does_not_claim_complete_coverage(self):
        m, b, p, s = example()
        b['workloads'].append(dict(b['workloads'][0], id='other-task', quantity=24))
        b['computations'][0]['steps'][0]['shape'] = ['workload_elements * 20 / 3']
        b['flows'][0]['fields'][0]['shape'] = ['workload_elements * 16 / 3']
        p['candidates'][0]['traffic'][0]['payload_bytes'] = 1
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertFalse(r['coverage_complete'])
        self.assertIn({'side': 'source', 'workload': 'other-task'}, r['missing_profiles'])
        self.assertEqual(check_rows(r, 'flow__')[0]['status'], 'fail')
        self.assertTrue(r['missing_count_checks'])
        self.assertTrue(r['values'])

    def test_bad_count_row_preserves_other_count_and_flow_values(self):
        m, b, p, s = example()
        m['count_checks'].append(dict(m['count_checks'][0], id='broken', source_paths=['absent-path']))
        r = execute(m, b, p, s)
        self.assertFalse(r['ok'])
        self.assertEqual(check_rows(r, 'count__')[0]['status'], 'pass')
        self.assertEqual(check_rows(r, 'flow__')[0]['status'], 'pass')
        receipts = [row for row in r['subreceipts'] if row['section'] == 'count']
        self.assertEqual([row['status'] for row in receipts], ['executed', 'declaration_failed'])
        self.assertEqual(receipts[1]['values'], {})
        self.assertTrue(any(key.endswith('::count__source') for key in r['values']))
        self.assertFalse(any('broken' in key for key in r['values']))

    def test_bad_service_does_not_drop_valid_shared_resource_deficit(self):
        m, b, p, s = example()
        good = m['service_windows'][0]
        good['intervals'] = [{'start': 0, 'end': 4}]
        bad = copy.deepcopy(good)
        bad['id'] = 'broken'
        bad['resource']['kind'] = 'unsupported-port'
        m['service_windows'].append(bad)
        r = execute(m, b, p, s)
        self.assertFalse(r['ok'])
        self.assertEqual(check_rows(r, 'service__')[0]['status'], 'fail')
        self.assertEqual(len([row for row in r['subreceipts'] if row['section'] == 'service' and row['status'] == 'declaration_failed']), 1)

    def test_packed_credit_cannot_hide_an_independent_payload_lower_bound(self):
        m, b, p, s = example()
        b['flows'].append(dict(copy.deepcopy(b['flows'][0]), id='other'))
        m['flow_bindings'][0]['flow_ids'].append('other')
        p['candidates'][0]['traffic'][0]['payload_bytes'] = 1
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        self.assertTrue(all(row['status'] == 'fail' for row in check_rows(r, 'flow__')))

    def test_transformed_MAC_recipe_checks_actual_cost_not_relabelled_rows(self):
        m, b, p, s = example()
        p['work_model']['items'][0]['count'].update(operations=12, unit='MAC')
        b['computations'][0]['steps'] = [{'id': 'effects', 'kind': 'dot', 'width': 12}]
        group = p['candidates'][0]['cost_groups'][0]
        group.update(realization='transformed', count={'kind': 'custom', 'operations': 4, 'unit': 'MAC', 'basis': 'one row is claimed as one physical MAC'}, equivalence='row-count claim')
        m['service_windows'][0]['intervals'][0]['end'] = 44
        row = m['count_checks'][0]
        row.update(realization='transformed', equivalence_evidence='Source equivalence not independently proven.', physical_realization={'basis': 'chosen actual realization', 'groups': [{'cost_group': 'compute', 'steps': [{'id': 'matrix', 'kind': 'gemv', 'input_elements': 4, 'output_elements': 3}], 'scope': {'kind': 'task', 'axis': 'run'}, 'basis': 'actual scalar MACs for a matrix-vector step', 'references': [{'file': 'design.json', 'quote': '"selected": "small"'}]}]})
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        obs = r['work_result']['observations'][0]
        self.assertEqual(obs['physical_status'], 'fail')
        detail = obs['physical_realization']['groups'][0]
        self.assertEqual((detail['physical_operations'], detail['actual_cost_operations'], detail['required_cycles'], detail['scheduled_cycles']), (12, 4, 12, 4))
        self.assertTrue(any(row['status'] == 'fail' for row in r['checks'] if '__physical__' in row['id']))
        self.assertEqual(detail['work_ids'], ['arithmetic'])
        self.assertEqual(detail['schedule_references'], ['compute'])
        # A genuinely different four-MAC physical algorithm can fit its actual
        # group. This arithmetic does not prove equivalence to the source twelve.
        row['physical_realization']['groups'][0]['steps'] = [{'id': 'different-algorithm', 'kind': 'dot', 'width': 4}]
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        obs = r['work_result']['observations'][0]
        self.assertEqual(obs['physical_status'], 'pass')
        self.assertEqual(obs['semantic_equivalence_status'], 'unverified')
        self.assertEqual(r['unverified_transformations'], ['count'])
        self.assertFalse(any(row['status'] == 'fail' for row in r['checks'] if '__physical__' in row['id']))

    def test_direct_audit_label_cannot_hide_actual_transformed_cost_group(self):
        m, b, p, s = example()
        p['work_model']['items'][0]['count'].update(operations=12, unit='MAC')
        b['computations'][0]['steps'] = [{'id': 'effects', 'kind': 'dot', 'width': 12}]
        group = p['candidates'][0]['cost_groups'][0]
        group.update(realization='transformed', count={'kind': 'custom', 'operations': 4,
            'unit': 'MAC', 'basis': 'claimed row cost'}, equivalence='independent review still required')
        m['service_windows'][0]['intervals'][0]['end'] = 44
        # Nominal work_model still says twelve; the actual priced group says
        # four. The audit row may not bypass physical review by saying direct.
        self.assertEqual(m['count_checks'][0]['realization'], 'direct')
        r = execute(m, b, p, s)
        self.assertFalse(r['ok'])
        self.assertFalse(r['coverage_complete'])
        self.assertTrue(any('actual transformed cost groups' in error for error in r['errors']))
        count = next(row for row in r['subreceipts'] if row['section'] == 'count')
        self.assertEqual(count['status'], 'declaration_failed')
        self.assertEqual(count['values'], {})
        self.assertTrue(any(row['status'] == 'executed' and row['section'] == 'flow' for row in r['subreceipts']))
        self.assertFalse(check_rows(r, 'count__'))
        # A selector in the actual cost group must be expanded as well.
        group['work_ids'] = {'unit': 'MAC'}
        r = execute(m, b, p, s)
        self.assertFalse(r['ok'])
        self.assertTrue(any('actual transformed cost groups' in error for error in r['errors']))

    def test_binding_covers_full_calculator_and_checker_closures(self):
        m, b, p, s = example()
        r = execute(m, b, p, s)
        self.assertTrue(r['ok'], r['errors'])
        binding = r['binding']
        self.assertEqual(set(binding['calculator_files_sha256']), {'evaluate.py', 'budget_units.py', 'source_quantities.py', 'work_model.py', 'storage_model.py', 'execution_model.py', 'execution_templates.py', 'execution_budget.py', 'abi_geometry.py', 'numeric_model.py'})
        self.assertEqual(set(binding['checker_files_sha256']), {'candidate_checks.py', 'source_model.py', 'validate_review.py'})
        self.assertEqual(binding['calculator_bundle_sha256'], audit._source.fingerprint(binding['calculator_files_sha256']))
        self.assertEqual(binding['checker_bundle_sha256'], audit._source.fingerprint(binding['checker_files_sha256']))
        self.assertEqual(binding['source_analysis_sha256'], audit._source.fingerprint(source_result(b, s)))

    def test_cli_uses_same_calculator_and_host_contract(self):
        m, b, p, s = example()
        request = {'mode': 'proposal', 'model': m, 'baseline': source_result(b, s), 'source_declaration': b, 'proposal': p,
                   'budget': engine.evaluate(p, {key: s[key] for key in ('spec.md', 'reference.py')}),
                   'sources': s}
        completed = subprocess.run([sys.executable, str(BASE / 'skills/microarchitecture-audit/scripts/candidate_checks.py'),
                                    '--calculator', str(BASE / 'skills/microarchitecture-design/scripts/evaluate.py')],
                                   input=json.dumps(request), text=True, capture_output=True, check=True)
        r = json.loads(completed.stdout)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(r['missing_operand_checks'], [])


if __name__ == '__main__':
    unittest.main()
