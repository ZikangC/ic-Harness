"""Task-profile compatibility is a declaration check, not a hardware result."""
import copy
import unittest

from test_audit_candidate_checks import audit, engine, example, execute, source_result, check_rows


def mismatched_example():
    model, baseline, proposal, sources = example()
    baseline['workloads'][0]['quantity'] = 24
    baseline['computations'][0]['steps'][0]['shape'] = ['workload_elements * 20 / 3']
    baseline['flows'][0]['fields'][0]['shape'] = ['workload_elements * 16 / 3']
    return model, baseline, proposal, sources


class ProfileMetadataTests(unittest.TestCase):
    def test_same_unit_extent_mismatch_is_not_a_count_or_flow_hardware_failure(self):
        args = mismatched_example()
        before = copy.deepcopy(args)
        result = execute(*args)
        self.assertFalse(result['ok'])
        self.assertEqual(args, before)
        self.assertEqual(result['execution_status'], 'partial')
        profile = next(row for row in result['subreceipts'] if row['section'] == 'profile')
        self.assertEqual(profile['status'], 'declaration_failed')
        self.assertIn('source 24 samples, candidate 12 samples', profile['errors'][0])
        self.assertEqual(profile['values'], {})
        for prefix in ('count__', 'flow__', 'flow_inventory__', 'service_window__'):
            self.assertEqual(check_rows(result, prefix), [])
        self.assertFalse(any(row['status'] == 'fail' for row in result['checks']))
        self.assertIn({'side': 'source', 'workload': 'task'}, result['missing_profiles'])
        self.assertIn({'side': 'candidate', 'workload': 'primary'}, result['missing_profiles'])
        self.assertEqual(result['work_result']['observations'], [])

    def test_mismatch_does_not_erase_independent_abi_counterexample(self):
        model, baseline, proposal, sources = mismatched_example()
        proposal['architecture_md'] = 'region extent = 28\n'
        references = [{'file': 'architecture.md', 'quote': 'region extent = 28'}]
        model['abi_checks'] = [{
            'id': 'layout',
            'geometry': {'id': 'image', 'kind': 'region', 'beat_bytes': 8, 'fields': [
                {'id': name, 'payload_bytes': size, 'alignment_bytes': 8}
                for name, size in (('head', 5), ('body', 10), ('end', 5))]},
            'claims': [{'id': 'extent', 'result': 'extent_bytes', 'declared': 28,
                        'relation': '==', 'references': references}],
            'references': [{'file': 'spec.md', 'quote': 'items 12'}] + references,
        }]
        result = execute(model, baseline, proposal, sources)
        self.assertEqual(result['execution_status'], 'partial')
        checks = check_rows(result, 'abi__')
        self.assertEqual(len(checks), 1)
        self.assertEqual((checks[0]['expected'], checks[0]['actual'], checks[0]['status']), (29, 28, 'fail'))
        receipt = next(row for row in result['subreceipts'] if row['section'] == 'abi')
        self.assertEqual(receipt['status'], 'executed')
        self.assertTrue(receipt['values'])
        self.assertTrue(set(receipt['calculation_ids']) <= set(result['values']))
        self.assertTrue(any(row['section'] == 'input' and row['status'] == 'executed'
                            for row in result['subreceipts']))

    def test_direct_compare_work_applies_the_same_metadata_guard(self):
        model, declaration, proposal, sources = mismatched_example()
        original = {key: sources[key] for key in ('spec.md', 'reference.py')}
        baseline = audit._source.source_view(source_result(declaration, sources), declaration, original, engine)
        result = audit._source.compare_work(model, baseline, proposal, sources, engine)
        self.assertFalse(result['ok'])
        self.assertEqual(result['execution_status'], 'partial')
        self.assertTrue(any('source 24 samples, candidate 12 samples' in error for error in result['errors']))
        self.assertEqual(result['observations'], [])
        self.assertEqual(result['values'], {})
        self.assertEqual(result['subreceipts'][0]['status'], 'declaration_failed')

    def test_unit_matching_strips_only_and_does_not_guess_aliases(self):
        helper = audit._source.profile_mapping_error
        self.assertIsNotNone(helper(24, ' samples ', 12, 'samples'))
        self.assertIsNone(helper(12, ' samples ', 12, 'samples'))
        for source_unit, candidate_unit in (('sample', 'samples'), ('B', 'b'), ('batch', 'samples')):
            self.assertIsNone(helper(24, source_unit, 12, candidate_unit))
        model, baseline, proposal, sources = example()
        baseline['workloads'][0].update(quantity=24, unit='batch')
        result = execute(model, baseline, proposal, sources)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(check_rows(result, 'count__')[0]['status'], 'pass')

    def test_rejected_pair_does_not_erase_other_valid_partial_mapping(self):
        model, baseline, proposal, sources = example()
        baseline['workloads'].append(dict(baseline['workloads'][0], id='larger', quantity=24))
        model['workload_map']['larger'] = 'primary'
        model['count_checks'].append(dict(model['count_checks'][0], id='larger_count', workload='larger'))
        result = execute(model, baseline, proposal, sources)
        self.assertFalse(result['ok'])
        counts = check_rows(result, 'count__')
        self.assertEqual(len(counts), 1)
        self.assertEqual((counts[0]['expected'], counts[0]['actual'], counts[0]['status']), (80, 80, 'pass'))
        self.assertIn({'side': 'source', 'workload': 'larger'}, result['missing_profiles'])
        self.assertNotIn({'side': 'candidate', 'workload': 'primary'}, result['missing_profiles'])
        self.assertFalse(result['coverage_complete'])
        self.assertTrue(result['values'])

    def test_unmapped_extra_profile_remains_a_coverage_gap(self):
        model, baseline, proposal, sources = example()
        baseline['workloads'].append(dict(baseline['workloads'][0], id='larger', quantity=24))
        result = execute(model, baseline, proposal, sources)
        self.assertTrue(result['ok'], result['errors'])
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(check_rows(result, 'count__')[0]['status'], 'pass')
        self.assertFalse(any(row['section'] == 'profile' for row in result['subreceipts']))

    def test_equal_logical_extent_allows_wire_padding(self):
        model, baseline, proposal, sources = example()
        proposal['candidates'][0]['traffic'][0]['alignment_bytes'] = 16
        result = execute(model, baseline, proposal, sources)
        self.assertTrue(result['ok'], result['errors'])
        flow = check_rows(result, 'flow__')[0]
        self.assertEqual((flow['expected'], flow['actual'], flow['status']), (64, 64, 'pass'))
        fresh = engine.evaluate(proposal, {key: sources[key] for key in ('spec.md', 'reference.py')})
        self.assertEqual(fresh['candidates']['small']['ledger']['traffic'][0]['wire_bytes'], 128)
        self.assertFalse(any(row['section'] == 'profile' for row in result['subreceipts']))

    def test_equal_extent_fused_native_cost_is_not_forced_to_source_count(self):
        model, baseline, proposal, sources = example()
        baseline['computations'][0]['steps'][0]['shape'] = [40]
        baseline['computations'].append(dict(baseline['computations'][0], id='second'))
        proposal['work_model']['items'][0]['count']['operations'] = 40
        model['service_windows'][0]['intervals'][0]['end'] = 80
        model['count_checks'][0].update(
            source_paths=['path', 'second'], realization='transformed',
            equivalence_evidence='Synthetic fusion is a hypothesis requiring independent equivalence review.',
            physical_realization={'basis': 'selected fused scalar implementation', 'groups': [{
                'cost_group': 'compute', 'steps': [{'id': 'fused', 'kind': 'elementwise',
                                                  'shape': [40], 'ops_per_element': 1}],
                'scope': {'kind': 'task', 'axis': 'run'}, 'basis': 'native implementation effects',
                'references': [{'file': 'design.json', 'quote': '"selected": "small"'}]}]})
        result = execute(model, baseline, proposal, sources)
        self.assertTrue(result['ok'], result['errors'])
        count = check_rows(result, 'count__count')[0]
        self.assertEqual((count['expected'], count['actual'], count['status']), (80, 40, 'unverified'))
        physical = check_rows(result, 'count__count__physical__compute')
        self.assertTrue(physical)
        self.assertTrue(all(row['status'] == 'pass' for row in physical))
        self.assertFalse(any(row['section'] == 'profile' for row in result['subreceipts']))


if __name__ == '__main__':
    unittest.main()
