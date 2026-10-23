"""Zero activity checks use actual source transactions, never a trusted flag."""
import copy
import unittest

from harness.tests.test_audit_candidate_checks import (
    check_rows, example, execute, source_result,
)


def zero_history_case():
    model, declaration, proposal, sources = example()
    sources['spec.md'] += 'History entries before the first item are empty.\n'
    sources['reference.py'] += 'def history_before_first(items):\n    return items[:0]\n'
    refs = [{'file': key, 'quote': text.strip()}
            for key, text in sources.items() if key in ('spec.md', 'reference.py')]
    declaration['workloads'][0].update(quantity=1, references=copy.deepcopy(refs))
    proposal['work_model']['workloads'][0]['quantity'] = 1
    declaration['computations'][0]['references'] = copy.deepcopy(refs)
    declaration['flows'] = [{
        'id': 'past', 'direction': 'read', 'encoding': 'fixed_external',
        'fields': [{'shape': [1], 'element_bits': 8}],
        'scope': {'kind': 'prefix', 'axis': 'item', 'prefix_axis': 'prior_item',
                  'first': 0, 'step': 1, 'multiplicity': 1},
        'basis': 'No prior entries for the single-item source profile.',
        'references': copy.deepcopy(refs),
    }]
    candidate = proposal['candidates'][0]
    candidate['traffic'] = []
    candidate['coverage']['traffic'] = {'status': 'not_applicable',
        'basis': 'This selected inventory declares no external transactions.'}
    model.update(count_checks=[], service_windows=[], operand_checks=[],
        flow_bindings=[{'id': 'past_history', 'flow_ids': ['past'], 'workload': 'task',
                        'traffic_ids': [], 'basis': 'There is no history payload to transport.'}])
    return model, declaration, proposal, sources


class ZeroActivityFlowTests(unittest.TestCase):
    def test_zero_prefix_total_and_empty_actual_supply_execute(self):
        args = zero_history_case()
        source = source_result(args[1], args[3])
        self.assertTrue(source['ok'], source['errors'])
        self.assertEqual(source['flows']['past']['totals']['task'], {'bits': 0, 'bytes': 0})
        result = execute(*args)
        self.assertTrue(result['ok'], result['errors'])
        bound = check_rows(result, 'flow__')[0]
        inventory = check_rows(result, 'flow_inventory__')[0]
        self.assertEqual((bound['status'], bound['required_bytes'], bound['available_bytes']), ('pass', 0, 0))
        self.assertEqual((inventory['status'], inventory['required_bytes'], inventory['available_bytes']), ('pass', 0, 0))
        self.assertEqual(inventory['traffic_ids'], [])
        self.assertTrue(inventory['coverage_complete'])
        # This successful local zero check does not supply omitted whole-task
        # count/supply witnesses or claim architecture acceptance.
        self.assertFalse(result['coverage_complete'])

    def test_nonzero_source_and_explicit_empty_supply_produce_deficit(self):
        model, declaration, proposal, sources = zero_history_case()
        declaration['flows'][0]['scope']['first'] = 1
        result = execute(model, declaration, proposal, sources)
        self.assertTrue(result['ok'], result['errors'])
        for check in check_rows(result, 'flow__') + check_rows(result, 'flow_inventory__'):
            self.assertEqual((check['status'], check['required_bytes'], check['available_bytes'], check['deficit_bytes']), ('fail', 1, 0, 1))

    def test_nonempty_inventory_cannot_claim_not_applicable(self):
        model, declaration, proposal, sources = zero_history_case()
        declaration['flows'][0]['scope']['first'] = 1
        proposal['candidates'][0]['traffic'] = [{
            'id': 'claimed_read', 'model': 'continuous_reserved',
            'payload_bytes': 1, 'transfers': 1, 'alignment_bytes': 1,
            'transaction_bytes': 1, 'latency_cycles': 1,
            'transactions_per_cycle': 1, 'inflight': 1, 'response_buffer_bytes': 1,
            'basis': 'Nonempty actual declaration with inconsistent coverage.',
        }]
        result = execute(model, declaration, proposal, sources)
        inventory = check_rows(result, 'flow_inventory__')[0]
        self.assertEqual(inventory['status'], 'unverified')
        self.assertFalse(inventory['coverage_complete'])

    def test_empty_inventory_with_incomplete_coverage_stays_unknown(self):
        for status in ('covered', 'unknown'):
            with self.subTest(status=status):
                model, declaration, proposal, sources = zero_history_case()
                declaration['flows'][0]['scope']['first'] = 1
                proposal['candidates'][0]['coverage']['traffic']['status'] = status
                inventory = check_rows(execute(model, declaration, proposal, sources), 'flow_inventory__')[0]
                self.assertEqual(inventory['status'], 'unverified')

    def test_missing_coverage_basis_does_not_prove_empty_inventory_complete(self):
        model, declaration, proposal, sources = zero_history_case()
        proposal['candidates'][0]['coverage']['traffic']['basis'] = ''
        inventory = check_rows(execute(model, declaration, proposal, sources), 'flow_inventory__')[0]
        self.assertEqual(inventory['status'], 'unverified')
        self.assertFalse(inventory['coverage_complete'])


if __name__ == '__main__':
    unittest.main()
