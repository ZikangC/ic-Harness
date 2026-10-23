import copy
import unittest

from harness.checks.architecture import _engine as engine
from test_architecture import fixture


def representative():
    document, sources = fixture()
    candidate = document['candidates'][0]
    candidate['execution_templates'] = [{
        'id': 'path', 'workload': 'primary', 'basis': 'read and consume',
        'repetition_basis': 'one complete arithmetic operation per path',
        'phases': [{'id': 'body', 'cycles': 2, 'cost_groups': ['compute'],
            'basis': 'synchronous read then consume',
            'reads': [{'id': 'word', 'storage_reference': 'banks', 'instance': 0,
                       'word_address': 0, 'cycle': 0}],
            'uses': [{'cost_group': 'compute', 'read_id': 'word', 'cycle': 1,
                      'lane': 0, 'operand': 'value', 'bit_offset': 0, 'width': 8}],
            'issues': [{'cost_group': 'compute', 'cycle': 1, 'lanes': [0],
                        'operands': ['value'], 'basis': 'one real lane issue'}]}]}]
    candidate['workloads'][0]['components'][1] = {
        'id': 'compute', 'composition': 'execution', 'references': ['compute'],
        'execution': {'template': 'path', 'component': 'whole'}, 'basis': 'inclusive path'}
    return document, sources


class ExecutionBudgetTests(unittest.TestCase):
    def test_real_load_wait_tail_cycles_change_authoritative_throughput(self):
        document, sources = representative()
        result = engine.evaluate(document, sources)
        self.assertTrue(result['ok'], result['errors'])
        task = result['candidates']['small']['workloads']['primary']
        self.assertEqual(task['task_cycles'], 200)  # setup20 + 80*(read+consume) + output20
        self.assertEqual(task['components'][1]['execution']['repetitions'], 80)
        self.assertEqual(task['components'][1]['execution']['count_witness']['compute']['issued_operations'], 1)
        self.assertEqual(task['items_per_second'], 4800000)
        document['candidates'][0]['execution_templates'][0]['phases'][0]['cycles'] = 3
        newer = engine.evaluate(document, sources)
        self.assertTrue(newer['ok'], newer['errors'])
        self.assertEqual(newer['candidates']['small']['workloads']['primary']['task_cycles'], 280)

    def test_manual_cycles_and_wrong_repetitions_are_rejected(self):
        for key, value in [('cycles', 1), ('repetitions', 1)]:
            document, sources = representative()
            component = document['candidates'][0]['workloads'][0]['components'][1]
            if key == 'cycles': component[key] = value
            else: component['execution'][key] = value
            result = engine.evaluate(document, sources)
            self.assertFalse(result['ok'])

    def test_partly_priced_template_cannot_silently_drop_a_known_wait_phase(self):
        document, sources = representative()
        candidate = document['candidates'][0]
        candidate['execution_templates'][0]['phases'].append({
            'id': 'drain', 'cycles': 9, 'cost_groups': [], 'basis': 'drain before completion'})
        candidate['workloads'][0]['components'][1]['execution']['component'] = 'body'
        result = engine.evaluate(document, sources)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['gate']['status'], 'unknown')
        self.assertTrue(any('unpriced_phases' in row for row in result['gate']['unsupported_models']))
        document['requirements']['storage_rules'][0].pop('read_latency_cycles')
        result = engine.evaluate(document, sources)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['gate']['status'], 'unknown')
        self.assertTrue(any('unpriced_phases' in row for row in result['gate']['unsupported_models']))

    def test_unchecked_numeric_evidence_does_not_claim_numeric_pass_or_block_budget(self):
        for probes, expected in [([], 'not_checked'), (None, 'computed_only')]:
            document, sources = representative()
            if probes is None: document['candidates'][0]['numeric_probes'][0]['assertions'] = []
            else: document['candidates'][0]['numeric_probes'] = probes
            result = engine.evaluate(document, sources)
            self.assertTrue(result['ok'], result['errors'])
            self.assertEqual(result['candidates']['small']['numeric_probes']['coverage_status'], expected)
            self.assertEqual(result['gate']['status'], 'pass')

    def test_missing_read_capability_preserves_unverified_execution_and_cost(self):
        document, sources = representative()
        document['requirements']['storage_rules'][0].pop('read_latency_cycles')
        result = engine.evaluate(document, sources)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['gate']['status'], 'pass')
        self.assertEqual(result['candidates']['small']['workloads']['primary']['task_cycles'], 200)
        template = result['candidates']['small']['ledger']['execution_derivation']['templates'][0]
        self.assertEqual(template['status'], 'unverified')
        self.assertTrue(any('read latency capability absent' in row for row in result['candidates']['small']['ledger']['evidence_gaps']))

    def test_multiple_groups_need_same_complete_scope_ratio(self):
        groups = {'a': {'model': 'compute', 'workload': 'p', 'operations': 8,
                       'scheduled_cycles': 4, 'operation_unit': 'multiply'},
                  'b': {'model': 'compute', 'workload': 'p', 'operations': 8,
                       'scheduled_cycles': 4, 'operation_unit': 'add'}}
        template = {'id': 'path', 'workload': 'p', 'status': 'pass', 'cycles': 10,
                    'components': [{'id': 'path', 'cycles': 10, 'cost_groups': ['a','b'],
                                    'issued_operations': {'a':4,'b':4}}],
                    'issued_operations': {'a':4,'b':4}, 'peak_buffer_bits': {'reg':24}}
        component = {'references':['a','b'], 'execution':{'template':'path','component':'whole'}}
        charge = engine._helpers['execution_budget'].charge
        cost, evidence = charge(component, groups, [template], {}, engine.expression, set())
        self.assertEqual(cost, 20)
        self.assertEqual(evidence['repetitions'], 2)
        groups['b']['operations'] = 12
        with self.assertRaisesRegex(ValueError, 'different execution repetitions'):
            charge(component, groups, [template], {}, engine.expression, set())

    def test_double_phase_charge_and_fractional_scope_are_rejected(self):
        document, sources = representative()
        result = engine.evaluate(document, sources)
        candidate = result['candidates']['small']
        template = candidate['ledger']['execution_derivation']['templates']
        groups = {g['id']:g for g in candidate['ledger']['cost_groups']}
        component = document['candidates'][0]['workloads'][0]['components'][1]
        charge = engine._helpers['execution_budget'].charge
        used=set();charge(component, groups, template, {}, engine.expression, used)
        with self.assertRaisesRegex(ValueError, 'charged more than once'):
            charge(component, groups, template, {}, engine.expression, used)
        groups['compute']['operations']=0.5
        with self.assertRaisesRegex(ValueError, 'positive integer multiple'):
            charge(component, groups, template, {}, engine.expression, set())


if __name__ == '__main__': unittest.main()
