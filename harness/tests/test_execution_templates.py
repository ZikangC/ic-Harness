"""Executable schedule rules check physical consequences, not source semantics."""
import copy
import unittest
from harness.checks.architecture import _engine as engine


def run(candidate):
    inventory = {row['name']: dict(row, bits=row['count'] * row['depth'] * row['width'])
                 for row in candidate.get('macros', [])}
    requirements = {'storage_rules': [{'kind': 'SRAM', 'read_ports': 1,
                                      'write_ports': 1, 'read_latency_cycles': 1}]}
    return engine._helpers['execution_model'].derive(candidate, {}, requirements,
                                                    engine.expression, inventory)


def base():
    return {'workloads': [{'id': 'task'}],
        'cost_groups': [{'id': 'multiply', 'model': 'compute', 'workload': 'task',
                         'units': 2, 'operations': 6, 'operation_unit': 'MAC'}],
        'macros': [{'name': 'words', 'kind': 'SRAM', 'count': 1, 'depth': 32, 'width': 16}],
        'buffers': [{'id': 'holding', 'kind': 'register', 'count': 1, 'depth': 1, 'width': 32}],
        'schedule_windows': [], 'execution_templates': []}


def gemv():
    candidate = base()
    candidate['execution_templates'] = [{
        'id': 'vector_path', 'workload': 'task', 'basis': 'physical column tile',
        'parameters': {'outputs': 3, 'lanes': 2},
        'repetition_basis': 'three outputs require two blocks; last block uses one lane',
        'phases': [{'id': 'tile', 'cycles': 2, 'repeat': 'ceil(outputs/lanes)',
            'cost_groups': ['multiply'], 'basis': 'read then consume the packed word',
            'reads': [{'id': 'weights', 'storage_reference': 'words', 'instance': 0,
                       'word_address': 'iteration', 'cycle': 0}],
            'retentions': [{'id': 'scalar', 'buffer': 'holding', 'bit_offset': 0, 'width': 8,
                            'start': 0, 'end': 1, 'basis': 'broadcast one retained scalar',
                            'producer_basis': 'input scalar available before this tile'}],
            'uses': [{'for_each': [{'index': 'lane', 'count': 'min(lanes,outputs-iteration*lanes)'}],
                      'cost_group': 'multiply', 'read_id': 'weights', 'cycle': 1,
                      'lane': 'lane', 'operand': 'weight', 'bit_offset': 'lane*8', 'width': 8}],
            'local_uses': [{'for_each': [{'index': 'lane', 'count': 'min(lanes,outputs-iteration*lanes)'}],
                'cost_group': 'multiply', 'retention': 'scalar', 'cycle': 1, 'lane': 'lane',
                'operand': 'input', 'bit_offset': 0, 'width': 8, 'basis': 'scalar broadcast'}],
            'issues': [{'id': 'dot', 'cost_group': 'multiply', 'cycle': 1,
                        'lanes': {'count': 'min(lanes,outputs-iteration*lanes)'},
                        'operands': ['weight', 'input'], 'basis': 'all active lanes supplied',
                        'partial_issue_basis': 'last output block has one valid lane'}]}]}]
    return candidate


def state_update():
    candidate = base()
    candidate['cost_groups'].append({'id': 'update', 'model': 'compute', 'workload': 'task',
                                    'units': 2, 'operations': 4, 'operation_unit': 'MAC'})
    candidate['execution_templates'] = [{
        'id': 'state_path', 'workload': 'task', 'basis': 'old value retained to its last consumer',
        'repetition_basis': 'two packed state words, each updated before advancing',
        'phases': [{'id': 'state_word', 'cycles': 5, 'repeat': 2, 'basis': 'read/decay/update/write',
            'cost_groups': ['multiply', 'update'],
            'reads': [{'id': 'old', 'storage_reference': 'words', 'instance': 0,
                       'word_address': 'iteration', 'cycle': 0}],
            'writes': [{'id': 'new', 'storage_reference': 'words', 'instance': 0,
                        'word_address': 'iteration', 'cycle': 4}],
            'retentions': [
                {'id': 'old_hold', 'buffer': 'holding', 'bit_offset': 0, 'width': 16,
                 'start': 1, 'end': 3, 'read_id': 'old', 'basis': 'old word survives both consumers'},
                {'id': 'gate', 'buffer': 'holding', 'bit_offset': 16, 'width': 8,
                 'start': 0, 'end': 3, 'basis': 'gate available before update',
                 'producer_basis': 'current nonlinear gate precedes this state-word phase'}],
            'uses': [{'for_each': [{'index': 'lane', 'count': 2}], 'cost_group': group,
                      'read_id': 'old', 'retention': 'old_hold', 'cycle': cycle, 'lane': 'lane',
                      'operand': 'old', 'bit_offset': 'lane*8', 'width': 8}
                     for group, cycle in [('multiply', 1), ('update', 3)]],
            'local_uses': [{'for_each': [{'index': 'lane', 'count': 2}], 'cost_group': group,
                       'retention': 'gate', 'cycle': cycle, 'lane': 'lane', 'operand': 'gate',
                       'bit_offset': 0, 'width': 8, 'basis': 'gate broadcast'}
                      for group, cycle in [('multiply', 1), ('update', 3)]],
            'issues': [{'id': group, 'cost_group': group, 'cycle': cycle, 'lanes': {'count': 2},
                        'operands': ['old', 'gate'], 'basis': 'two complete lanes'}
                       for group, cycle in [('multiply', 1), ('update', 3)]],
            'dependencies': [{'before': 'multiply', 'after': 'update', 'min_latency': 2,
                              'basis': 'decay result before update'},
                             {'before': 'update', 'after': 'new', 'min_latency': 1,
                              'basis': 'update result before state overwrite'}]}]}]
    return candidate


class ExecutionTemplateTests(unittest.TestCase):
    def test_gemv_rules_expand_broadcast_and_legal_partial_tail(self):
        result = run(gemv())
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['missing_issue_groups'], [])
        template = result['templates'][0]
        self.assertEqual(template['cycles'], 4)
        self.assertEqual(template['components'][0]['cycles'], 4)
        self.assertEqual(template['peak_buffer_bits'], {'holding': 8})
        self.assertEqual(template['issued_operations'], {'multiply': 3})
        self.assertEqual(template['components'][0]['issued_operations'], {'multiply': 3})
        self.assertEqual(template['components'][0]['window_id'], 'vector_path')
        self.assertEqual(result['windows'][0]['issues'][1]['lanes'], [0])
        self.assertEqual(len(result['windows'][0]['uses']), 6)

    def test_entire_state_update_retains_old_values_until_last_use(self):
        result = run(state_update())
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['missing_issue_groups'], [])
        self.assertEqual(result['templates'][0]['cycles'], 10)
        self.assertEqual(result['templates'][0]['peak_buffer_bits'], {'holding': 24})
        self.assertEqual(result['templates'][0]['physical_writes'], 2)

    def test_matrix_loader_counts_fill_unpack_write_and_drain(self):
        candidate = base(); candidate['cost_groups'] = []
        candidate['execution_templates'] = [{
            'id': 'loader', 'workload': 'task', 'basis': 'declared converter schedule',
            'repetition_basis': 'five elements packed two per word, last word partially filled',
            'parameters': {'elements': 5},
            'phases': [
                {'id': 'fetch_latency', 'cycles': 3, 'basis': 'external response setup'},
                {'id': 'assemble_write', 'cycles': 2, 'repeat': 'ceil(elements/2)',
                 'basis': 'one converter cycle plus one physical write',
                 'writes': [{'id': 'packed', 'storage_reference': 'words', 'instance': 0,
                             'word_address': 'iteration', 'cycle': 1}]},
                {'id': 'drain', 'cycles': 1, 'basis': 'last completion before RUN'}]}]
        result = run(candidate)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['templates'][0]['cycles'], 10)
        self.assertEqual([r['cycles'] for r in result['templates'][0]['components']], [3, 6, 1])
        self.assertEqual(result['templates'][0]['physical_writes'], 3)

    def test_same_cycle_port_conflict_not_hidden_by_repeat_average(self):
        candidate = gemv()
        row = copy.deepcopy(candidate['execution_templates'][0]['phases'][0]['reads'][0])
        row['id'] = 'second'; candidate['execution_templates'][0]['phases'][0]['reads'].append(row)
        result = run(candidate)
        self.assertFalse(result['ok'])
        self.assertTrue(any('exceed input read_ports' in error for error in result['errors']))

    def test_use_before_read_ready_is_rejected_after_expansion(self):
        candidate = gemv(); candidate['execution_templates'][0]['phases'][0]['uses'][0]['cycle'] = 0
        result = run(candidate)
        self.assertFalse(result['ok'])
        self.assertTrue(any('before synchronous read' in error for error in result['errors']))

    def test_overwrite_register_before_last_consumer_fails(self):
        candidate = state_update()
        row = copy.deepcopy(candidate['execution_templates'][0]['phases'][0]['retentions'][0])
        row.update(id='clobber', start=2, end=4, producer_basis='different value overwrites old slot')
        row.pop('read_id')
        candidate['execution_templates'][0]['phases'][0]['retentions'].append(row)
        result = run(candidate)
        self.assertFalse(result['ok'])
        self.assertTrue(any('overlap before last consumer' in error for error in result['errors']))

    def test_declared_dependency_reversal_fails(self):
        candidate = state_update()
        phase = candidate['execution_templates'][0]['phases'][0]
        phase['dependencies'][0].update(before='update', after='multiply')
        result = run(candidate)
        self.assertFalse(result['ok'])
        self.assertTrue(any('dependency latency/order violated' in error for error in result['errors']))

    def test_omitted_tail_operand_is_detected(self):
        candidate = gemv()
        candidate['execution_templates'][0]['phases'][0]['local_uses'][0]['for_each'][0]['count'] = 1
        result = run(candidate)
        self.assertFalse(result['ok'])
        self.assertTrue(any('missing declared operand positions' in error for error in result['errors']))

    def test_lifetime_capacity_and_represented_bits_checked(self):
        candidate = gemv(); candidate['buffers'][0]['width'] = 4
        self.assertFalse(run(candidate)['ok'])
        candidate = gemv(); candidate['execution_templates'][0]['phases'][0]['local_uses'][0]['bit_offset'] = 4
        self.assertFalse(run(candidate)['ok'])

    def test_same_address_simultaneous_read_write_is_not_assumed_old_data(self):
        candidate = state_update(); candidate['execution_templates'][0]['phases'][0]['writes'][0]['cycle'] = 0
        result = run(candidate)
        self.assertFalse(result['ok'])
        # It also violates the explicit data dependency; either check is decisive.
        self.assertTrue(any('dependency' in error or 'same-address' in error for error in result['errors']))

    def test_event_cycle_must_be_in_body_and_repeats_bounded(self):
        candidate = gemv(); candidate['execution_templates'][0]['phases'][0]['reads'][0]['cycle'] = 2
        self.assertFalse(run(candidate)['ok'])
        candidate = gemv(); candidate['execution_templates'][0]['phases'][0]['repeat'] = 65537
        result = run(candidate)
        self.assertFalse(result['ok'])
        self.assertTrue(any('execution-size limit' in error for error in result['errors']))

    def test_fresh_loop_identifiers_and_safe_arithmetic_only(self):
        candidate = gemv(); candidate['execution_templates'][0]['phases'][0]['reads'][0]['word_address'] = '__import__("os")'
        self.assertFalse(run(candidate)['ok'])
        candidate = gemv(); candidate['execution_templates'][0]['phases'][0]['uses'][0]['for_each'][0]['index'] = 'outputs'
        self.assertFalse(run(candidate)['ok'])

    def test_template_local_values_need_lifetime_not_unchecked_ready_claim(self):
        candidate = gemv(); candidate['execution_templates'][0]['phases'][0]['local_uses'][0].pop('retention')
        result = run(candidate)
        self.assertFalse(result['ok'])
        self.assertTrue(any('explicit retention lifetime' in error for error in result['errors']))

    def test_missing_input_capability_stays_unknown(self):
        candidate = gemv()
        inventory = {'words': {'kind': 'SRAM', 'count': 1, 'depth': 32, 'width': 16}}
        requirements = {'storage_rules': [{'kind': 'SRAM', 'write_ports': 1}]}
        result = engine._helpers['execution_model'].derive(candidate, {}, requirements,
                                                          engine.expression, inventory)
        self.assertTrue(result['ok'], result['errors'])
        self.assertTrue(result['unknown'])

    def test_cross_phase_retention_has_global_lifetime_and_can_be_shared(self):
        candidate = gemv()
        template = candidate['execution_templates'][0]
        template['retentions'] = [{'id': 'persistent_scalar', 'buffer': 'holding',
            'bit_offset': 0, 'width': 8, 'start': 0, 'end': 3,
            'basis': 'same scalar kept over both output tiles',
            'producer_basis': 'input captured before the complete representative path'}]
        phase = template['phases'][0]
        phase['retentions'] = []
        phase['local_uses'][0]['retention'] = '/persistent_scalar'
        result = run(candidate)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['templates'][0]['peak_buffer_bits'], {'holding': 8})
        self.assertEqual(result['windows'][0]['uses'][-1]['ready_cycle'], 0)

    def test_template_manual_holding_cannot_skip_lifetime_check(self):
        candidate = gemv()
        candidate['execution_templates'][0]['phases'][0]['uses'][0]['holding'] = {
            'buffer': 'holding', 'bit_offset': 0, 'width': 16}
        result = run(candidate)
        self.assertFalse(result['ok'])
        self.assertTrue(any('explicit retention lifetime' in error for error in result['errors']))

    def test_phase_cannot_hide_issued_work_outside_declared_cost_groups(self):
        candidate = gemv()
        candidate['execution_templates'][0]['phases'][0]['cost_groups'] = []
        result = run(candidate)
        self.assertFalse(result['ok'])
        self.assertTrue(any('omitted from phase cost_groups' in error for error in result['errors']))

    def test_read_capture_cannot_claim_value_before_actual_latency(self):
        candidate = state_update()
        candidate['execution_templates'][0]['phases'][0]['retentions'][0]['start'] = 0
        result = run(candidate)
        self.assertFalse(result['ok'])
        self.assertTrue(any('captures physical read before ready' in error for error in result['errors']))

    def test_cross_phase_dependency_uses_real_global_event_cycles(self):
        candidate = state_update()
        template = candidate['execution_templates'][0]
        template['phases'].insert(0, {'id': 'nonlinear', 'cycles': 3,
            'basis': 'gate producer before the full state path',
            'writes': [{'id': 'gate_ready', 'storage_reference': 'words',
                        'instance': 0, 'word_address': 31, 'cycle': 2}]})
        template['phases'][1]['dependencies'].append({
            'before': '/nonlinear/0/gate_ready', 'after': 'multiply',
            'min_latency': 2, 'basis': 'current nonlinear result ready before decay'})
        result = run(candidate)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['templates'][0]['cycles'], 13)
        template['phases'][1]['dependencies'][-1]['min_latency'] = 3
        result = run(candidate)
        self.assertFalse(result['ok'])
        self.assertTrue(any('dependency latency/order violated' in error for error in result['errors']))

    def test_cross_iteration_dependency_interpolates_before_namespacing(self):
        candidate = state_update()
        phase = candidate['execution_templates'][0]['phases'][0]
        # Only iteration one declares the preceding iteration dependency.  A
        # zero-sized dependent loop omits it for the first body.
        phase['dependencies'].append({
            'for_each': [{'index': 'previous', 'count': 'iteration'}],
            'before': '/state_word/${previous}/new', 'after': 'multiply',
            'min_latency': 2, 'basis': 'prior stored state precedes the next update'})
        result = run(candidate)
        self.assertTrue(result['ok'], result['errors'])
        phase['dependencies'][-1]['before'] = '/state_word/7/new'
        result = run(candidate)
        self.assertFalse(result['ok'])
        self.assertTrue(any('missing expanded event' in error for error in result['errors']))

    def test_cross_phase_dependency_cannot_hide_reversed_causality(self):
        candidate = state_update()
        phase = candidate['execution_templates'][0]['phases'][0]
        phase['dependencies'].append({'before': '/state_word/1/new', 'after': 'multiply',
            'min_latency': 0, 'basis': 'deliberately impossible future producer'})
        result = run(candidate)
        self.assertFalse(result['ok'])
        self.assertTrue(any('dependency latency/order violated' in error for error in result['errors']))

    def test_late_read_capture_is_unknown_without_source_output_holding(self):
        candidate = gemv()
        phase = candidate['execution_templates'][0]['phases'][0]
        phase.update(cycles=4, repeat=1)
        phase['uses'][0].update(cycle=3, retention='late')
        phase['local_uses'][0]['cycle'] = 3
        phase['retentions'][0]['end'] = 3
        phase['issues'][0]['cycle'] = 3
        phase['retentions'].append({'id': 'late', 'buffer': 'holding', 'bit_offset': 8,
            'width': 16, 'start': 3, 'end': 3, 'read_id': 'weights',
            'basis': 'capture a previously read word later'})
        result = run(candidate)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['templates'][0]['status'], 'unverified')
        self.assertTrue(any('late capture needs source-backed output stability' in row for row in result['unknown']))
        self.assertEqual(result['templates'][0]['cycles'], 4)

    def test_register_reuse_during_late_capture_gap_does_not_become_pass(self):
        candidate = gemv()
        phase = candidate['execution_templates'][0]['phases'][0]
        phase.update(cycles=4, repeat=1)
        phase['uses'][0].update(cycle=3, retention='late')
        phase['local_uses'][0]['cycle'] = 3
        phase['retentions'][0]['end'] = 3
        phase['issues'][0]['cycle'] = 3
        phase['retentions'].extend([
            {'id': 'late', 'buffer': 'holding', 'bit_offset': 8, 'width': 16,
             'start': 3, 'end': 3, 'read_id': 'weights', 'basis': 'delayed capture'},
            {'id': 'intervening', 'buffer': 'holding', 'bit_offset': 8, 'width': 16,
             'start': 1, 'end': 2, 'producer_basis': 'different local producer',
             'basis': 'same bits occupied while read output stability is unproved'}])
        result = run(candidate)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['templates'][0]['status'], 'unverified')
        self.assertTrue(result['unknown'])
        self.assertEqual(result['templates'][0]['peak_buffer_bits'], {'holding': 24})

    def test_inputs_unchanged(self):
        candidate = state_update(); before = copy.deepcopy(candidate)
        run(candidate)
        self.assertEqual(candidate, before)


if __name__ == '__main__': unittest.main()
