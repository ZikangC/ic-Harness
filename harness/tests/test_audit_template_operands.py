"""Independent operand declarations can inspect expanded schedules and tails."""
import copy
import unittest
from test_audit_candidate_checks import example, execute, check_rows
from test_execution_templates import gemv


def template_example():
    model, baseline, proposal, sources = example()
    candidate = proposal['candidates'][0]
    proposal['work_model']['items'][0]['count']['unit'] = 'MAC'
    baseline['computations'][0]['steps'] = [{'id': 'effects', 'kind': 'dot', 'width': 80}]
    candidate['cost_groups'][0]['units'] = 2
    candidate['macros'][0]['width'] = 16
    candidate['schedule_windows'] = []
    candidate['buffers'].append({'id': 'broadcast', 'count': 1, 'depth': 1, 'width': 32,
        'kind': 'register', 'basis': 'priced broadcast and state holding register',
        'objects': [{'id': 'held_input', 'shape': [], 'element_bits': 8, 'copies': 1,
                     'alignment_bits': 8, 'metadata_bits': 0, 'live_in': ['run'],
                     'basis': 'one retained scalar'}]})
    candidate['area_components'][0]['covers_buffers'] = ['broadcast']
    template = gemv()['execution_templates'][0]
    template['workload'] = 'primary'
    phase = template['phases'][0]
    phase['cost_groups'] = ['compute']
    for row in phase['reads']: row['storage_reference'] = 'banks'
    for row in phase['retentions']: row['buffer'] = 'broadcast'
    for kind in ('uses', 'local_uses', 'issues'):
        for row in phase[kind]: row['cost_group'] = 'compute'
    candidate['execution_templates'] = [template]
    model['service_windows'][0]['intervals'][0]['end'] = 80
    model['operand_checks'] = [{'window_id': template['id'], 'cost_group': 'compute',
        'issues': [{'cycle': 1, 'lanes': [0, 1], 'operands': ['weight', 'input']},
                   {'cycle': 3, 'lanes': [0], 'operands': ['weight', 'input'],
                    'partial_issue_basis': 'last of three outputs uses one valid lane'}],
        'basis': 'independent full issue and final valid-lane issue, not a copied completeness verdict'}]
    return model, baseline, proposal, sources


class AuditTemplateOperandTests(unittest.TestCase):
    def test_template_windows_are_available_to_independent_full_and_tail_checks(self):
        args = template_example(); before = copy.deepcopy(args)
        result = execute(*args)
        self.assertTrue(result['ok'], result['errors'])
        check = check_rows(result, 'operands__')[0]
        self.assertEqual(check['status'], 'pass', result['checks'])
        self.assertEqual(check['actual']['declared_positions'], 6)
        self.assertEqual(check['actual']['supplied_positions'], 6)
        self.assertEqual(check['actual']['full_issue_cycles'], [1])
        self.assertEqual(check['actual']['unjustified_partial_issues'], [])
        self.assertEqual(result['missing_operand_checks'], [])
        self.assertEqual(args, before)

    def test_lane_zero_tail_alone_cannot_establish_full_resource_supply(self):
        model, baseline, proposal, sources = template_example()
        model['operand_checks'][0]['issues'] = model['operand_checks'][0]['issues'][1:]
        result = execute(model, baseline, proposal, sources)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(check_rows(result, 'operands__')[0]['status'], 'unverified')
        self.assertEqual(result['missing_operand_checks'], ['compute'])

    def test_partial_tail_needs_its_explicit_reason(self):
        model, baseline, proposal, sources = template_example()
        model['operand_checks'][0]['issues'][1].pop('partial_issue_basis')
        result = execute(model, baseline, proposal, sources)
        self.assertTrue(result['ok'], result['errors'])
        check = check_rows(result, 'operands__')[0]
        self.assertEqual(check['status'], 'unverified')
        self.assertEqual(len(check['actual']['unjustified_partial_issues']), 1)

    def test_independent_extra_role_finds_real_missing_supply(self):
        model, baseline, proposal, sources = template_example()
        model['operand_checks'][0]['issues'][0]['operands'].append('accumulator')
        result = execute(model, baseline, proposal, sources)
        self.assertTrue(result['ok'], result['errors'])
        check = check_rows(result, 'operands__')[0]
        self.assertEqual(check['status'], 'fail')
        self.assertEqual(check['actual']['missing_operand_positions'], [[1, 0, 'accumulator'], [1, 1, 'accumulator']])

    def test_fake_full_tail_lanes_are_not_justified_by_an_existing_full_issue(self):
        model, baseline, proposal, sources = template_example()
        model['operand_checks'][0]['issues'][1]['lanes'] = [0, 1]
        result = execute(model, baseline, proposal, sources)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(check_rows(result, 'operands__')[0]['status'], 'fail')

    def test_expansion_alone_is_not_an_independent_operand_check(self):
        model, baseline, proposal, sources = template_example()
        model['operand_checks'] = []
        result = execute(model, baseline, proposal, sources)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(check_rows(result, 'operands__'), [])
        self.assertEqual(result['missing_operand_checks'], ['compute'])

    def test_full_issue_plus_tail_can_use_legacy_concrete_window_too(self):
        model, baseline, proposal, sources = template_example()
        candidate = proposal['candidates'][0]
        from harness.checks.architecture import _engine as engine
        inventory = {row['name']: row for row in candidate['macros']}
        expanded = engine._helpers['execution_model']._templates.expand(candidate, {}, proposal['requirements'], engine.expression, inventory)
        self.assertTrue(expanded['ok'], expanded['errors'])
        candidate['schedule_windows'] = expanded['windows']
        candidate['execution_templates'] = []
        result = execute(model, baseline, proposal, sources)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(check_rows(result, 'operands__')[0]['status'], 'pass')

    def test_unsupported_template_and_colliding_template_names_do_not_create_a_window(self):
        model, baseline, proposal, sources = template_example()
        candidate = proposal['candidates'][0]
        candidate['execution_templates'] = [{'id': 'vector_path', 'model': 'unsupported',
            'basis': 'no executable mapping selected', 'reason': 'unknown mapping'}]
        result = execute(model, baseline, proposal, sources)
        self.assertFalse(result['ok'])
        self.assertEqual(check_rows(result, 'operands__'), [])
        model, baseline, proposal, sources = template_example()
        candidate = proposal['candidates'][0]
        candidate['execution_templates'].append(copy.deepcopy(candidate['execution_templates'][0]))
        result = execute(model, baseline, proposal, sources)
        self.assertFalse(result['ok'])
        self.assertEqual(check_rows(result, 'operands__'), [])


if __name__ == '__main__': unittest.main()
