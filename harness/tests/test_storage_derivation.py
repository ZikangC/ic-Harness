"""Storage arithmetic, exact capacity-domain projection and aliasing witnesses."""
import copy
import importlib.util
from pathlib import Path
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'microarchitecture-design' / 'scripts'


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


storage = load('storage_model')
expression = load('evaluate').expression


def obj(identifier='values', shape=None, **updates):
    row = {'id': identifier, 'shape': [8] if shape is None else shape,
           'element_bits': 8, 'copies': 1, 'alignment_bits': 1, 'metadata_bits': 0,
           'live_in': ['work'], 'basis': 'synthetic object representation'}
    row.update(updates)
    return row


def buffer(identifier='scratch', objects=None, **updates):
    row = {'id': identifier, 'kind': 'SRAM', 'count': 1, 'depth': 8, 'width': 8,
           'storage_reference': 'memory', 'objects': [obj()] if objects is None else objects,
           'basis': 'synthetic storage allocation'}
    row.update(updates)
    return row


def fixture():
    candidate = {'storage_phases': [{'id': 'work', 'basis': 'objects simultaneously live'}],
                 'buffers': [buffer()], 'macros': [{'name': 'memory', 'kind': 'SRAM'}]}
    values = {'lower': 1, 'upper': 5}
    requirements = {'facts': [{'id': key} for key in values], 'runtime_parameters': []}
    inventory = {'memory': {'bits': 64, 'kind': 'SRAM'}}
    return candidate, values, requirements, inventory


def parameter(identifier='length', domain=None):
    return {'id': identifier,
            'domain': {'kind': 'range', 'lower': 'lower', 'upper': 'upper'} if domain is None else domain,
            'basis': 'all synthetic legal integer configurations',
            'citations': [{'file': 'spec.md', 'quote': 'synthetic parameter domain'}]}


class StorageDerivationTests(unittest.TestCase):
    def derive(self, data):
        candidate, values, requirements, inventory = data
        return storage.derive(candidate, values, requirements, expression, inventory)

    def test_empty_extent_has_no_payload_but_retains_metadata(self):
        c,v,r,inventory=fixture();c['buffers'][0]['objects']=[obj(shape=[0,8],metadata_bits=9,alignment_bits=8,copies=2)]
        result=storage.derive(c,v,r,expression,inventory)
        self.assertTrue(result['ok'],result['errors']);self.assertEqual(result['buffers'][0]['required_bits'],32)
        self.assertEqual(result['buffers'][0]['object_sizes'][0]['payload_bits'],0)
        c['buffers'][0]['objects'][0]['shape']=[-1]
        self.assertFalse(storage.derive(c,v,r,expression,inventory)['ok'])

    def test_fixed_shape_capacity_and_source_immutability(self):
        data = fixture(); before = copy.deepcopy(data)
        report = self.derive(data)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(data, before)
        self.assertEqual(report['scenarios_checked'], 1)
        self.assertEqual(report['buffers'][0]['required_bits'], 64)
        self.assertEqual(report['macro_live_peaks'][0]['required_bits'], 64)
        self.assertIn('does not prove', report['scope'])

    def test_padding_metadata_and_replicas_are_per_copy(self):
        data = fixture(); c, values, req, inventory = data
        c['buffers'][0].update(depth=32, objects=[obj(shape=[3, 5], element_bits=5, copies=3,
                                                    alignment_bits=16, metadata_bits=3)])
        inventory['memory']['bits'] = 256
        report = self.derive(data); self.assertTrue(report['ok'], report['errors'])
        sizes = report['buffers'][0]['object_sizes'][0]
        self.assertEqual(sizes['payload_bits'], 75)
        self.assertEqual(sizes['padded_bits_per_copy'], 80)
        self.assertEqual(sizes['bits'], 240)

    def test_empty_shape_is_one_scalar(self):
        data = fixture(); data[0]['buffers'][0]['objects'] = [obj(shape=[], element_bits=7,
                                                                 copies=2, alignment_bits=8, metadata_bits=2)]
        report = self.derive(data)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['buffers'][0]['required_bits'], 32)

    def test_actual_shape_detects_underallocation(self):
        data = fixture(); data[0]['buffers'][0]['objects'] = [obj(shape=[5, 2])]
        report = self.derive(data)
        self.assertFalse(report['ok'])
        self.assertEqual(report['buffers'][0]['required_bits'], 80)
        self.assertTrue(any('maximum simultaneous live data 80' in e for e in report['errors']))

    def test_objects_in_one_buffer_sum_when_simultaneously_live(self):
        data = fixture(); data[0]['buffers'][0]['objects'] = [obj('state', [5]), obj('cache', [4])]
        report = self.derive(data)
        self.assertFalse(report['ok'])
        self.assertEqual(report['buffers'][0]['required_bits'], 72)

    def test_disjoint_object_lifetimes_use_max_not_sum(self):
        data = fixture(); c = data[0]
        c['storage_phases'].append({'id': 'finish', 'basis': 'work object retired'})
        c['buffers'][0]['objects'] = [obj('state', [8]), obj('result', [8], live_in=['finish'])]
        report = self.derive(data)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['buffers'][0]['required_bits'], 64)
        self.assertEqual([o['live_at_peak'] for o in report['buffers'][0]['object_sizes']], [True, False])

    def test_disjoint_buffer_aliases_do_not_sum_allocations(self):
        data = fixture(); c = data[0]
        c['storage_phases'].append({'id': 'finish', 'basis': 'first allocation retired'})
        c['buffers'].append(buffer('result', [obj('output', live_in=['finish'])]))
        report = self.derive(data)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(sum(b['allocated_bits'] for b in report['buffers']), 128)
        self.assertEqual(report['macro_live_peaks'][0]['required_bits'], 64)

    def test_simultaneously_live_buffer_aliases_exceed_physical_macro(self):
        data = fixture(); data[0]['buffers'].append(buffer('state', [obj('state_values')]))
        report = self.derive(data)
        self.assertFalse(report['ok'])
        self.assertTrue(all(b['required_bits'] <= b['allocated_bits'] for b in report['buffers']))
        self.assertEqual(report['macro_live_peaks'][0]['required_bits'], 128)
        self.assertTrue(any('simultaneous aliased buffer demand 128' in e for e in report['errors']))

    def test_macro_peak_does_not_add_peaks_from_different_parameter_values(self):
        data = fixture(); c, values, req, inventory = data
        req['runtime_parameters'] = [parameter()]
        c['buffers'][0]['objects'] = [obj('growing', ['length'])]
        c['buffers'].append(buffer('other', [obj('shrinking', ['6-length'])]))
        report = self.derive(data)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual([b['required_bits'] for b in report['buffers']], [40, 40])
        self.assertEqual(report['macro_live_peaks'][0]['required_bits'], 48)

    def test_full_domain_checks_nonmonotone_interior_peak(self):
        data = fixture(); c, values, req, inventory = data
        req['runtime_parameters'] = [parameter()]
        c['buffers'][0].update(depth=7, objects=[obj(shape=['10-(length-3)**2'])])
        report = self.derive(data)
        self.assertFalse(report['ok'])
        self.assertEqual(report['scenarios_checked'], 5)
        self.assertEqual(report['buffers'][0]['required_bits'], 80)
        self.assertEqual(report['buffers'][0]['peak_parameters'], {'length': 3})

    def test_parameter_set_fact_ids_need_not_be_sorted(self):
        data = fixture(); c, values, req, inventory = data
        req['runtime_parameters'] = [parameter(domain={'kind': 'set', 'values': ['upper', 'lower']})]
        c['buffers'][0]['objects'] = [obj(shape=['length'])]
        report = self.derive(data)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['scenarios_checked'], 2)
        self.assertEqual(report['buffers'][0]['peak_parameters'], {'length': 5})

    def test_domain_cartesian_product_checks_all_values(self):
        data = fixture(); c, values, req, inventory = data
        req['runtime_parameters'] = [parameter('rows'), parameter('columns')]
        c['buffers'][0].update(depth=25, objects=[obj(shape=['rows', 'columns'])])
        inventory['memory']['bits'] = 200
        report = self.derive(data)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['scenarios_checked'], 25)
        self.assertEqual(report['buffers'][0]['required_bits'], 200)

    def test_oversized_domain_is_unknown_not_capacity_pass(self):
        data = fixture(); c, values, req, inventory = data
        values['upper'] = 5000
        req['runtime_parameters'] = [parameter()]
        c['buffers'][0]['objects'] = [obj(shape=['length'])]
        report = self.derive(data)
        self.assertTrue(report['ok'], report['errors'])
        self.assertTrue(report['unknown'])
        self.assertEqual(report['scenarios_checked'], 0)
        self.assertIsNone(report['buffers'][0]['required_bits'])

    def test_enumeration_limit_is_inclusive(self):
        data = fixture(); data[1]['upper'] = storage.MAX_SCENARIOS
        data[2]['runtime_parameters'] = [parameter()]
        data[0]['buffers'][0]['objects'][0]['shape'] = ['0*length']
        report = self.derive(data)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['unknown'], [])
        self.assertEqual(report['scenarios_checked'], storage.MAX_SCENARIOS)

    def test_unused_large_domain_does_not_hide_exact_relevant_capacity(self):
        data=fixture();c,values,req,inventory=data
        values['large_upper']=7000;req['facts'].append({'id':'large_upper'})
        req['runtime_parameters']=[parameter(),parameter('event_id',{'kind':'range','lower':'lower','upper':'large_upper'})]
        c['buffers'][0]['objects']=[obj(shape=['length'])]
        before=copy.deepcopy(data);report=self.derive(data)
        self.assertTrue(report['ok'],report['errors']);self.assertEqual(report['unknown'],[])
        self.assertEqual(report['scenarios_checked'],5);self.assertEqual(report['buffers'][0]['required_bits'],40)
        self.assertEqual(report['parameter_projection'],{'declared_domains':{'length':5,'event_id':7000},
            'relevant_domains':{'length':5},'omitted_domains':{'event_id':7000},
            'declared_scenario_count':35000,'scenario_count':5})
        self.assertEqual(data,before)
        c['buffers'][0]['objects'][0]['shape']=['event_id']
        report=self.derive(data);self.assertTrue(report['unknown']);self.assertEqual(report['scenarios_checked'],0)
        self.assertEqual(report['parameter_projection']['relevant_domains'],{'event_id':7000})

    def test_invalid_unused_domain_is_still_rejected(self):
        for domain in ({'kind':'range','lower':'upper','upper':'lower'},
                       {'kind':'set','values':['upper','upper']},
                       {'kind':'range','lower':'lower','upper':'absent'}):
            data=fixture();data[2]['runtime_parameters']=[parameter('unused',domain)]
            self.assertFalse(self.derive(data)['ok'])

    def test_all_numeric_capacity_fields_keep_their_parameter_domains(self):
        data=fixture();c,values,req,inventory=data;values['upper']=2
        req['runtime_parameters']=[parameter(name) for name in ('extent','precision','replicas','alignment','metadata')]
        c['buffers'][0]['objects']=[obj(shape=['extent'],element_bits='precision',copies='replicas',
            alignment_bits='alignment',metadata_bits='metadata')]
        report=self.derive(data);self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(report['scenarios_checked'],32);self.assertEqual(report['buffers'][0]['required_bits'],12)
        self.assertEqual(set(report['parameter_projection']['relevant_domains']),{'extent','precision','replicas','alignment','metadata'})
        self.assertEqual(report['parameter_projection']['omitted_domains'],{})

    def test_projection_still_finds_nonmonotone_capacity_failure(self):
        data=fixture();c,values,req,inventory=data
        values['large_upper']=9000;req['facts'].append({'id':'large_upper'})
        req['runtime_parameters']=[parameter(),parameter('request_address',{'kind':'range','lower':'lower','upper':'large_upper'})]
        c['buffers'][0].update(depth=7,objects=[obj(shape=['10-(length-3)**2'])])
        report=self.derive(data);self.assertFalse(report['ok']);self.assertEqual(report['unknown'],[])
        self.assertEqual(report['scenarios_checked'],5);self.assertEqual(report['buffers'][0]['required_bits'],80)
        self.assertEqual(report['buffers'][0]['peak_parameters'],{'length':3})

    def test_unresolved_or_unparseable_dependencies_are_not_pruned(self):
        for shape in (['unknown_extent'],['length+'],['custom(length)']):
            data=fixture();data[2]['runtime_parameters']=[parameter()]
            data[0]['buffers'][0]['objects'][0]['shape']=shape
            report=self.derive(data);self.assertFalse(report['ok']);self.assertEqual(report['scenarios_checked'],0)
            self.assertTrue(any('capacity dependencies' in error for error in report['errors']))

    def test_static_phase_aliasing_remains_exact_after_projection(self):
        data=fixture();c,values,req,inventory=data
        values['upper']=8000;req['runtime_parameters']=[parameter('event_id')]
        c['storage_phases'].append({'id':'finish','basis':'first object retired'})
        c['buffers'][0]['objects']=[obj('first',[8]),obj('second',[8],live_in=['finish'])]
        report=self.derive(data);self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(report['scenarios_checked'],1);self.assertEqual(report['buffers'][0]['required_bits'],64)
        c['buffers'][0]['objects'][1]['live_in']=['work']
        report=self.derive(data);self.assertFalse(report['ok']);self.assertEqual(report['buffers'][0]['required_bits'],128)

    def test_parameter_cannot_overwrite_fact_or_candidate_choice(self):
        for identifier in ('upper', 'lanes'):
            data = fixture(); data[1]['lanes'] = 2
            data[2]['runtime_parameters'] = [parameter(identifier)]
            report = self.derive(data)
            self.assertFalse(report['ok'])
            self.assertTrue(any('overwrites a fact/choice' in e for e in report['errors']))

    def test_domain_bounds_must_be_cited_fact_ids(self):
        for domain in ({'kind': 'range', 'lower': 1, 'upper': 5},
                       {'kind': 'set', 'values': ['lower', 'lower']},
                       {'kind': 'range', 'lower': 'upper', 'upper': 'lower'}):
            data = fixture(); data[2]['runtime_parameters'] = [parameter(domain=domain)]
            self.assertFalse(self.derive(data)['ok'])

    def test_domain_requires_integral_values_and_source_citations(self):
        data = fixture(); data[1]['upper'] = 2.5; data[2]['runtime_parameters'] = [parameter()]
        self.assertFalse(self.derive(data)['ok'])
        data = fixture(); data[2]['runtime_parameters'] = [parameter()]
        data[2]['runtime_parameters'][0]['citations'] = []
        self.assertFalse(self.derive(data)['ok'])

    def test_physical_allocation_cannot_resize_with_runtime_parameter(self):
        data = fixture(); data[2]['runtime_parameters'] = [parameter()]
        data[0]['buffers'][0]['depth'] = 'length'
        self.assertFalse(self.derive(data)['ok'])

    def test_old_claimed_requirement_and_allocation_fields_are_rejected(self):
        for field in ('required_bits', 'allocated_bits'):
            data = fixture(); data[0]['buffers'][0][field] = 64
            report = self.derive(data)
            self.assertFalse(report['ok'])
            self.assertTrue(any('program outputs' in e for e in report['errors']))

    def test_lifetime_ids_and_object_ids_are_checked(self):
        data = fixture(); data[0]['buffers'][0]['objects'][0]['live_in'] = ['missing']
        self.assertFalse(self.derive(data)['ok'])
        data = fixture(); data[0]['buffers'][0]['objects'].append(obj())
        self.assertFalse(self.derive(data)['ok'])
        data = fixture(); data[0]['storage_phases'].append(copy.deepcopy(data[0]['storage_phases'][0]))
        self.assertFalse(self.derive(data)['ok'])

    def test_element_copy_alignment_positive_shape_metadata_nonnegative(self):
        for updates in ({'element_bits': 0}, {'copies': 0}, {'alignment_bits': 0},
                        {'shape': [-1]}, {'shape': [1.5]}, {'metadata_bits': -1}):
            data = fixture(); data[0]['buffers'][0]['objects'][0].update(updates)
            self.assertFalse(self.derive(data)['ok'])

    def test_register_buffer_needs_no_macro_and_preserves_area_link_id(self):
        data = fixture(); data[0]['buffers'] = [buffer('holding_register', kind='register', storage_reference=None)]
        data[3].clear()
        report = self.derive(data)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['buffers'][0]['id'], 'holding_register')
        self.assertEqual(report['macro_live_peaks'], [])

    def test_macro_kind_and_individual_physical_allocation_checked(self):
        data = fixture(); data[3]['memory']['kind'] = 'ROM'
        self.assertFalse(self.derive(data)['ok'])
        data = fixture(); data[0]['buffers'][0]['depth'] = 9
        self.assertFalse(self.derive(data)['ok'])


if __name__ == '__main__':
    unittest.main()
