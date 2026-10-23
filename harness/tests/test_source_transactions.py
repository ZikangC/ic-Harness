"""Regression checks for actually executed independent source transactions."""
import copy
import hashlib
from pathlib import Path
import unittest

from harness.tests.test_audit_source_model import engine, source, witness


class SourceTransactionTests(unittest.TestCase):
    def test_unused_bad_fact_does_not_erase_independent_source_outputs(self):
        model, texts = witness()
        model['facts'].append({'id': 'unused', 'source': {'file': 'spec.md', 'line': 1, 'number': 1}})
        before = copy.deepcopy((model, texts))
        result = source.analyze(model, texts, engine)
        self.assertFalse(result['ok']); self.assertFalse(result['coverage_complete'])
        self.assertEqual(result['execution_status'], 'partial')
        self.assertNotIn('unused', result['fact_values'])
        self.assertEqual(result['computations']['projection']['totals']['p'], 60)
        self.assertEqual(result['flows']['out']['totals']['p']['bytes'], 80)
        self.assertEqual(result['states']['cache']['profiles']['p']['elements'], 20)
        self.assertEqual((model, texts), before)

    def test_bad_fact_and_its_transitive_dependents_are_never_used(self):
        model, texts = witness()
        # The explicit dtype already supplies bits; a conflicting byte unit
        # remains a real bad declaration whose dependents must stay unusable.
        model['facts'][-1]['unit'] = 'byte'
        refs = [{'file': 'spec.md', 'start': 5, 'end': 5}]
        model['facts'].append({'id': 'converted_width', 'formula': 'external_bits * 2',
                               'unit': 'bits', 'citations': refs})
        model['flows'][0]['fields'][0]['element_bits'] = 'converted_width'
        result = source.analyze(model, texts, engine)
        self.assertFalse(result['ok'])
        self.assertNotIn('external_bits', result['fact_values'])
        self.assertNotIn('converted_width', result['fact_values'])
        self.assertNotIn('out', result['flows'])
        self.assertEqual(result['computations']['projection']['totals']['p'], 60)
        self.assertEqual(result['states']['cache']['profiles']['p']['elements'], 20)
        failed = [row for row in result['subreceipts'] if row['status'] == 'declaration_failed']
        self.assertTrue(any('depends on failed or unresolved' in error for row in failed for error in row['errors']))
        self.assertTrue(all(row['values'] == {} and row['calculation_ids'] == [] and 'output' not in row for row in failed))

    def test_duplicate_facts_cannot_select_one_usable_version(self):
        model, texts = witness()
        model['facts'].append(copy.deepcopy(model['facts'][0]))
        result = source.analyze(model, texts, engine)
        self.assertFalse(result['ok']); self.assertNotIn('rows', result['fact_values'])
        self.assertNotIn('projection', result['computations']); self.assertNotIn('out', result['flows'])
        self.assertEqual(result['states']['cache']['profiles']['p']['elements'], 20)
        self.assertEqual(sum(row['section'] == 'fact' and row['applicability']['declaration_id'] == 'rows'
                             and row['status'] == 'declaration_failed' for row in result['subreceipts']), 2)

    def test_workload_failure_keeps_other_profile_but_blocks_full_coverage(self):
        model, texts = witness(); bad = copy.deepcopy(model['workloads'][0])
        bad.update(id='invalid-profile', quantity=0); model['workloads'].append(bad)
        result = source.analyze(model, texts, engine)
        self.assertEqual(result['workloads'], {'p': 5}); self.assertEqual(result['workload_units'], {'p': 'tokens'})
        self.assertEqual(result['computations']['projection']['totals'], {'p': 60})
        self.assertFalse(result['ok']); self.assertFalse(result['coverage_complete'])
        self.assertTrue(any(row['key'] == 'invalid-profile' for row in result['missing_declarations']))
        self.assertEqual({row['collection'] for row in result['missing_declarations'] if row.get('workload') == 'invalid-profile'},
                         {'computation', 'state', 'flow'})

    def test_one_profile_computation_error_keeps_successful_profile_and_flows(self):
        model, texts = witness(); small = copy.deepcopy(model['workloads'][0])
        small.update(id='one', quantity=1); model['workloads'].append(small)
        model['computations'][0].update(scope={'kind': 'task', 'axis': 'run'},
                                       steps=[{'id': 'dot', 'kind': 'dot', 'width': 'workload_elements - 1'}])
        result = source.analyze(model, texts, engine)
        self.assertFalse(result['ok'])
        self.assertEqual(result['computations']['projection']['totals'], {'p': 4})
        self.assertEqual(result['flows']['out']['totals']['one']['bytes'], 16)
        self.assertTrue(any(row['collection'] == 'computation' and row.get('workload') == 'one'
                            for row in result['missing_declarations']))

    def test_state_error_is_independent_per_profile(self):
        model, texts = witness(); small = copy.deepcopy(model['workloads'][0])
        small.update(id='one', quantity=1); model['workloads'].append(small)
        model['states'][0]['shape'] = ['workload_elements - 1', 'inner']
        result = source.analyze(model, texts, engine)
        self.assertFalse(result['ok'])
        self.assertEqual(result['states']['cache']['profiles'], {'p': {'shape': [4, 4], 'elements': 16}})
        self.assertEqual(result['computations']['projection']['totals']['one'], 12)
        self.assertEqual(result['flows']['out']['totals']['one']['bytes'], 16)
        self.assertTrue(any(row['collection'] == 'state' and row.get('workload') == 'one'
                            for row in result['missing_declarations']))

    def test_task_flow_shape_uses_actual_profile_workload_elements(self):
        model, texts = witness(); small = copy.deepcopy(model['workloads'][0])
        small.update(id='one', quantity=1); model['workloads'].append(small)
        model['flows'][0].update(scope={'kind': 'task', 'axis': 'run'},
                                fields=[{'shape': ['workload_elements - 1'], 'element_bits': 'external_bits'}])
        result = source.analyze(model, texts, engine)
        self.assertFalse(result['ok'])
        self.assertEqual(result['flows']['out']['totals'], {'p': {'bits': 128, 'bytes': 16}})
        self.assertEqual(result['computations']['projection']['totals']['one'], 12)
        self.assertTrue(any(row['collection'] == 'flow' and row.get('workload') == 'one'
                            for row in result['missing_declarations']))

    def test_unsupported_computation_does_not_erase_state_and_fixed_flow(self):
        model, texts = witness()
        model['computations'][0]['steps'][0] = {'id': 'unsupported', 'kind': 'custom', 'operations': 1}
        result = source.analyze(model, texts, engine)
        self.assertFalse(result['ok']); self.assertEqual(result['computations'], {})
        self.assertEqual(result['states']['cache']['profiles']['p']['elements'], 20)
        self.assertEqual(result['flows']['out']['totals']['p']['bytes'], 80)

    def test_bad_limits_are_not_a_whole_analysis_early_return(self):
        model, texts = witness(); model['limits'] = []
        result = source.analyze(model, texts, engine)
        self.assertFalse(result['ok']); self.assertFalse(result['coverage_complete'])
        self.assertEqual(result['flows']['out']['totals']['p']['bytes'], 80)
        self.assertTrue(any(row['collection'] == 'limits' for row in result['missing_declarations']))

    def test_receipts_bind_complete_source_and_script_context_and_exact_outputs(self):
        model, texts = witness(); result = source.analyze(model, texts, engine)
        self.assertTrue(result['ok'], result['errors']); self.assertTrue(result['coverage_complete'])
        self.assertEqual(result['binding']['model_sha256'], source.fingerprint(model))
        self.assertEqual(result['binding']['source_sha256s'], {key: hashlib.sha256(text.encode()).hexdigest() for key, text in texts.items()})
        for kind, directory in (('calculator', Path(engine.__file__).parent), ('checker', Path(source.__file__).parent)):
            files = source._source_script_files(directory)
            self.assertEqual(result['binding'][kind + '_files_sha256'], files)
            self.assertEqual(result['binding'][kind + '_bundle_sha256'], source.fingerprint(files))
        combined = {}
        for receipt in result['subreceipts']:
            self.assertEqual(receipt['status'], 'executed')
            self.assertEqual(receipt['binding'], result['binding'])
            self.assertEqual(receipt['receipt_sha256'], source.fingerprint({key: value for key, value in receipt.items() if key != 'receipt_sha256'}))
            self.assertEqual(set(receipt['values']), set(receipt['calculation_ids']))
            self.assertTrue(all(name.startswith(receipt['id'] + '::') for name in receipt['calculation_ids']))
            self.assertFalse(set(combined) & set(receipt['values'])); combined.update(receipt['values'])
            self.assertIsInstance(receipt['output']['data'], dict)
        self.assertEqual(result['values'], combined); self.assertEqual(set(result['calculation_ids']), set(combined))
        flow = next(row for row in result['subreceipts'] if row['section'] == 'flow')['output']
        self.assertEqual(flow, {'collection': 'flows', 'key': 'out', 'workload': 'p',
                               'data': {'direction': 'write', 'encoding': 'fixed_external',
                                        'total': {'bits': 640, 'bytes': 80}, 'per_occurrence_bits': 128,
                                        'scope': {'kind': 'element', 'axes': ['token'], 'quantity': 5, 'multiplicity': 1, 'factor': 5, 'total': 5}}})

    def test_successful_profile_receipts_track_numeric_fact_dependencies(self):
        model, texts = witness()
        result = source.analyze(model, texts, engine)
        by_section = {row['section']: row for row in result['subreceipts'] if row['section'] in ('computation', 'state', 'flow')}
        self.assertEqual(by_section['computation']['applicability']['dependencies'], ['inner', 'rows'])
        self.assertEqual(by_section['state']['applicability']['dependencies'], ['inner'])
        self.assertEqual(by_section['flow']['applicability']['dependencies'], ['external_bits', 'rows'])
        model['computations'][0]['scope'] = {'kind': 'prefix', 'axis': 'token', 'prefix_axis': 'history',
                                            'first': 1, 'step': 1, 'multiplicity': 'heads'}
        result = source.analyze(model, texts, engine)
        self.assertTrue(result['ok'], result['errors'])
        computation = next(row for row in result['subreceipts'] if row['section'] == 'computation')
        self.assertEqual(computation['applicability']['dependencies'], ['heads', 'inner', 'rows'])

    def test_malformed_numeric_rows_fail_locally_without_crashing(self):
        for bad in (None, 'not-a-step', {'id': 'broken', 'kind': 'matmul', 'lhs': None, 'rhs': [1]}):
            model, texts = witness(); model['computations'][0]['steps'] = [bad]
            result = source.analyze(model, texts, engine)
            self.assertFalse(result['ok']); self.assertEqual(result['computations'], {})
            self.assertEqual(result['flows']['out']['totals']['p']['bytes'], 80)
            self.assertEqual(result['states']['cache']['profiles']['p']['elements'], 20)

    def test_formula_dependency_order_and_fixed_conversion_use_shared_engine(self):
        model, texts = witness()
        model['facts'].insert(0, {'id': 'twice_inner', 'formula': 'ceil(inner) * 2', 'unit': 'count',
                                  'citations': [{'file': 'spec.md', 'start': 2, 'end': 2}]})
        model['states'][0]['shape'] = ['workload_elements', 'twice_inner']
        result = source.analyze(model, texts, engine)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['fact_values']['twice_inner'], 8)
        self.assertEqual(result['states']['cache']['profiles']['p']['elements'], 40)


if __name__ == '__main__': unittest.main()
