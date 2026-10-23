"""Budget estimates must account for explicitly linked transfer/operand service."""
import copy
import unittest
from test_architecture import fixture
from harness.checks.architecture import _engine as engine


class ServiceBudgetLinkTests(unittest.TestCase):
    def proposal(self):
        return fixture()

    def evaluate(self, change):
        document, sources = self.proposal()
        change(document['candidates'][0])
        return engine.evaluate(document, sources)

    def ledger(self, report):
        return report['candidates']['small']['ledger']

    def external(self, candidate, payload=49152, cycles=6144, boundary='run'):
        candidate['resource_checks'].append({'id':'external_bus','demand_per_cycle':0,
            'supply_per_cycle':8,'unit':'bytes/cycle','basis':'synthetic accepted bus width/rate'})
        candidate['cost_groups'][2]['cycles']=cycles
        candidate['cost_groups'][2]['resources'].append('external_bus')
        service={'resource_check':'external_bus','boundary':boundary,
                 'basis':'synthetic output acceptance or explicitly prior initialization'}
        if boundary=='run':service['cost_group']='output'
        row={'id':'writes','model':'posted_write','payload_bytes':payload,'transfers':1,
             'alignment_bytes':8,'transaction_bytes':8,'basis':'synthetic complete output',
             'service':service}
        if boundary=='preloaded_before_run':
            row.update(model='continuous_reserved',latency_cycles=8,transactions_per_cycle=1,
                       inflight=8,response_buffer_bytes=64,basis='synthetic prior initialization reads')
        candidate['traffic'].append(row)
        return row

    def compute_service(self,candidate,distinct=8,transfer_bits=None,rate=1):
        group=candidate['cost_groups'][0]
        group.update(units=8,operations_per_unit_cycle=rate)
        relation={'resource_check':'weights','operations_per_batch':8,
                  'operands':[{'role':'input','distinct_values':distinct,'element_bits':8}],
                  'basis':'actual distinct values per eight synthetic operations'}
        if transfer_bits is not None:relation['transfer_bits']=transfer_bits
        group['operand_service']=[relation]
        return relation

    def test_large_output_cannot_be_hidden_in_one_cycle(self):
        report=self.evaluate(lambda c:self.external(c,cycles=1))
        self.assertFalse(report['ok'])
        self.assertTrue(any('linked transfer service lower bound 6144' in e for e in report['errors']))

    def test_actual_output_service_is_recomputable(self):
        report=self.evaluate(self.external)
        self.assertTrue(report['ok'],report['errors'])
        row=self.ledger(report)['traffic'][-1]
        self.assertEqual(row['wire_bytes'],49152)
        self.assertEqual(row['service']['minimum_cycles'],6144)
        self.assertEqual(row['service']['bits_per_cycle'],64)

    def test_each_aligned_payload_and_beat_tail_is_charged(self):
        def change(candidate):
            row=self.external(candidate,payload=17,cycles=6)
            row['transfers']=2
        report=self.evaluate(change)
        self.assertTrue(report['ok'],report['errors'])
        row=self.ledger(report)['traffic'][-1]
        self.assertEqual(row['wire_bytes'],48)
        self.assertEqual(row['service']['minimum_cycles'],6)

    def test_partial_beat_rate_can_be_conservative(self):
        def change(candidate):
            self.external(candidate,payload=48,cycles=12)
            candidate['resource_checks'][-1]['supply_per_cycle']=4
        report=self.evaluate(change)
        self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(self.ledger(report)['traffic'][-1]['service']['minimum_cycles'],12)

    def test_multiple_flows_on_one_charged_group_add(self):
        def change(candidate):
            row=self.external(candidate,payload=16,cycles=3)
            another=copy.deepcopy(row);another['id']='other_output'
            candidate['traffic'].append(another)
        report=self.evaluate(change)
        self.assertFalse(report['ok'])
        self.assertTrue(any('linked transfer service lower bound 4' in e for e in report['errors']))

    def test_same_bus_across_overlapping_groups_adds_necessary_service(self):
        def change(candidate):
            row=self.external(candidate,payload=16,cycles=2)
            candidate['cost_groups'][1]['cycles']=2
            candidate['cost_groups'][1]['resources'].append('external_bus')
            another=copy.deepcopy(row);another['id']='setup_transfer';another['service']['cost_group']='setup'
            candidate['traffic'].append(another)
            candidate['workloads'][0]['components']=[
                {'id':'compute','composition':'serial','references':['compute'],'basis':'synthetic compute'},
                {'id':'transfer','composition':'overlap','references':['setup','output'],'cycles':2,'basis':'claimed overlap'}]
        report=self.evaluate(change)
        self.assertFalse(report['ok'])
        self.assertTrue(any('below shared service external_bus lower bound 4' in e for e in report['errors']))

    def test_different_buses_may_operate_in_parallel(self):
        def change(candidate):
            row=self.external(candidate,payload=16,cycles=2)
            candidate['cost_groups'][1].update(cycles=2,resources=['independent_bus'])
            candidate['cost_groups'][2]['resources']=['external_bus']
            candidate['resource_checks'].append({'id':'independent_bus','demand_per_cycle':0,
                'supply_per_cycle':8,'unit':'bytes/cycle','basis':'separate synthetic service'})
            another=copy.deepcopy(row);another['id']='setup_transfer';another['service'].update(cost_group='setup',resource_check='independent_bus')
            candidate['traffic'].append(another)
            candidate['workloads'][0]['components']=[
                {'id':'compute','composition':'serial','references':['compute'],'basis':'synthetic compute'},
                {'id':'transfer','composition':'parallel','references':['setup','output'],'basis':'separate buses'}]
        report=self.evaluate(change)
        self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(report['candidates']['small']['workloads']['primary']['task_cycles'],82)

    def test_preloaded_cost_remains_visible_outside_run(self):
        report=self.evaluate(lambda c:self.external(c,cycles=20,boundary='preloaded_before_run'))
        self.assertTrue(report['ok'],report['errors'])
        row=self.ledger(report)['traffic'][-1]
        self.assertEqual(row['wire_bytes'],49152)
        self.assertEqual(row['service']['minimum_cycles'],6144)
        self.assertEqual(row['service']['status'],'pre_run_cost_disclosed')
        self.assertEqual(report['candidates']['small']['workloads']['primary']['task_cycles'],120)

    def test_preload_cannot_silently_name_a_run_charging_group(self):
        def change(candidate):
            row=self.external(candidate,boundary='preloaded_before_run')
            row['service']['cost_group']='output'
        report=self.evaluate(change)
        self.assertFalse(report['ok'])
        self.assertTrue(any('omit cost_group' in e for e in report['errors']))

    def test_read_request_rate_and_bit_service_bounds_both_apply(self):
        def change(candidate):
            row=candidate['traffic'][0]
            row['transactions_per_cycle']=0.25
            row['service']={'resource_check':'weights','cost_group':'setup','boundary':'run','basis':'read response service'}
            candidate['resource_checks'][0]['supply_per_cycle']=1024
            candidate['cost_groups'][1]['cycles']=31
        report=self.evaluate(change)
        self.assertFalse(report['ok'])
        self.assertTrue(any('lower bound 32' in e for e in report['errors']))

    def test_missing_optional_relationship_is_unchecked_not_failure(self):
        document,sources=self.proposal()
        report=engine.evaluate(document,sources)
        self.assertTrue(report['ok'],report['errors'])
        gaps=self.ledger(report)['evidence_gaps']
        self.assertTrue(any('traffic_service/reads' in gap for gap in gaps))
        self.assertTrue(any('operand_service/compute' in gap for gap in gaps))

    def test_compute_rate_is_limited_by_actual_distinct_input_service(self):
        report=self.evaluate(self.compute_service)
        self.assertTrue(report['ok'],report['errors'])
        group=self.ledger(report)['cost_groups'][0]
        self.assertEqual(group['compute_minimum_cycles'],10)
        self.assertEqual(group['service_minimum_cycles'],80)
        self.assertEqual(group['minimum_cycles'],80)
        self.assertEqual(group['scheduled_cycles'],80)

    def test_one_broadcast_value_is_not_eight_distinct_reads(self):
        report=self.evaluate(lambda c:self.compute_service(c,distinct=1))
        self.assertTrue(report['ok'],report['errors'])
        group=self.ledger(report)['cost_groups'][0]
        self.assertEqual(group['service_minimum_cycles'],10)
        self.assertEqual(group['minimum_cycles'],10)

    def test_real_transfer_word_padding_is_charged(self):
        report=self.evaluate(lambda c:self.compute_service(c,distinct=1,transfer_bits=16))
        self.assertTrue(report['ok'],report['errors'])
        group=self.ledger(report)['cost_groups'][0]
        self.assertEqual(group['service_minimum_cycles'],20)
        self.assertEqual(group['operand_service_bounds'][0]['payload_bits_per_batch'],8)
        self.assertEqual(group['operand_service_bounds'][0]['charged_bits_per_batch'],16)

    def test_final_partial_compute_batch_is_charged_conservatively(self):
        def change(candidate):
            row=self.compute_service(candidate,distinct=1)
            row['operations_per_batch']=3
        report=self.evaluate(change)
        self.assertTrue(report['ok'],report['errors'])
        bound=self.ledger(report)['cost_groups'][0]['operand_service_bounds'][0]
        self.assertEqual(bound['batches'],27)
        self.assertEqual(bound['demand_bits'],216)
        self.assertEqual(bound['minimum_cycles'],27)

    def test_multiple_roles_sharing_one_service_add_distinct_demand(self):
        def change(candidate):
            row=self.compute_service(candidate)
            row['operands'].append({'role':'broadcast','distinct_values':1,'element_bits':8})
        report=self.evaluate(change)
        self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(self.ledger(report)['cost_groups'][0]['service_minimum_cycles'],90)

    def test_conservative_effective_compute_rate_is_preserved(self):
        report=self.evaluate(lambda c:self.compute_service(c,distinct=1,rate=0.25))
        self.assertTrue(report['ok'],report['errors'])
        group=self.ledger(report)['cost_groups'][0]
        self.assertEqual(group['compute_minimum_cycles'],40)
        self.assertEqual(group['service_minimum_cycles'],10)
        self.assertEqual(group['minimum_cycles'],40)

    def test_service_resource_must_belong_to_charged_group(self):
        def change(candidate):
            self.external(candidate)
            candidate['cost_groups'][2]['resources'].remove('external_bus')
        report=self.evaluate(change)
        self.assertFalse(report['ok'])
        self.assertTrue(any('included in charged group resources' in e for e in report['errors']))

    def test_service_unit_is_not_guessed_from_an_opaque_rate(self):
        def change(candidate):
            self.compute_service(candidate)
            candidate['resource_checks'][0]['unit']='operations/cycle'
        report=self.evaluate(change)
        self.assertFalse(report['ok'])
        self.assertTrue(any('bits/cycle or bytes/cycle' in e for e in report['errors']))

    def test_duplicate_compute_resource_rows_cannot_get_parallel_credit(self):
        def change(candidate):
            row=self.compute_service(candidate)
            candidate['cost_groups'][0]['operand_service'].append(copy.deepcopy(row))
        report=self.evaluate(change)
        self.assertFalse(report['ok'])
        self.assertTrue(any('resource must be unique' in e for e in report['errors']))

    def compute_and_transfer_projection(self,candidate,relation=None):
        self.compute_service(candidate)
        service={'resource_check':'weights','cost_group':'compute','boundary':'run',
                 'basis':'explicit transfer ownership for this synthetic computation'}
        if relation is not None:service['operand_relation']=relation
        candidate['traffic'].append({'id':'operand_transfer','model':'continuous_reserved',
            'payload_bytes':80,'transfers':1,'alignment_bytes':8,'transaction_bytes':8,
            'latency_cycles':1,'transactions_per_cycle':1,'inflight':1,'response_buffer_bytes':8,
            'basis':'synthetic transfer sharing the chosen physical service','service':service})

    def test_same_group_resource_requires_explicit_stream_ownership(self):
        report=self.evaluate(self.compute_and_transfer_projection)
        self.assertFalse(report['ok'])
        self.assertTrue(any('explicit operand_relation' in e for e in report['errors']))

    def test_independent_stream_cannot_get_same_stream_credit(self):
        report=self.evaluate(lambda c:self.compute_and_transfer_projection(c,'independent'))
        self.assertFalse(report['ok'])
        self.assertTrue(any('linked transfer service lower bound 160' in e for e in report['errors']))

    def test_explicit_same_stream_is_not_double_charged(self):
        report=self.evaluate(lambda c:self.compute_and_transfer_projection(c,'same_stream'))
        self.assertTrue(report['ok'],report['errors'])
        bound=self.ledger(report)['cost_groups'][0]['service_bounds'][0]
        self.assertEqual(bound['demand_bits'],640)
        self.assertEqual(bound['minimum_cycles'],80)

    def test_same_stream_and_independent_stream_demands_combine(self):
        def change(candidate):
            self.compute_and_transfer_projection(candidate,'same_stream')
            separate=copy.deepcopy(candidate['traffic'][-1]);separate.update(id='separate_flow',payload_bytes=16)
            separate['service']['operand_relation']='independent'
            candidate['traffic'].append(separate)
        report=self.evaluate(change)
        self.assertFalse(report['ok'])
        self.assertTrue(any('linked transfer service lower bound 96' in e for e in report['errors']))

    def execution_service_proposal(self,payload=160):
        from test_execution_budget import representative
        document,sources=representative()
        candidate=document['candidates'][0]
        candidate['resource_checks'].append({'id':'write_bus','demand_per_cycle':0,
            'supply_per_cycle':1,'unit':'B/cycle','basis':'one accepted byte per cycle'})
        candidate['cost_groups'][0]['resources'].append('write_bus')
        candidate['traffic'].append({'id':'execution_output','model':'posted_write',
            'payload_bytes':payload,'transfers':1,'alignment_bytes':8,'transaction_bytes':8,
            'basis':'output drain inside the explicitly priced execution interval',
            'service':{'resource_check':'write_bus','cost_group':'compute','boundary':'run',
                       'basis':'actual template component includes this service interval'}})
        return document,sources

    def test_execution_uses_its_actual_longer_charge_for_linked_service(self):
        document,sources=self.execution_service_proposal()
        report=engine.evaluate(document,sources)
        self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(self.ledger(report)['cost_groups'][0]['scheduled_cycles'],80)
        component=report['candidates']['small']['workloads']['primary']['components'][1]
        self.assertEqual(component['cycles'],160)
        self.assertEqual(component['service_bounds'][0]['minimum_cycles'],160)

    def test_execution_still_rejects_service_beyond_its_actual_charge(self):
        document,sources=self.execution_service_proposal(payload=161)
        report=engine.evaluate(document,sources)
        self.assertFalse(report['ok'])
        self.assertTrue(any('below shared service write_bus lower bound 168' in error for error in report['errors']))

    def test_execution_preserves_read_request_rate_lower_bound(self):
        document,sources=self.execution_service_proposal(payload=8)
        traffic=document['candidates'][0]['traffic'][-1]
        traffic.update(model='continuous_reserved',latency_cycles=1,
                       transactions_per_cycle=0.005,inflight=1,response_buffer_bytes=8)
        report=engine.evaluate(document,sources)
        self.assertFalse(report['ok'])
        self.assertTrue(any('below shared service write_bus lower bound 200' in error for error in report['errors']))

    def test_execution_preserves_read_response_latency_lower_bound(self):
        document,sources=self.execution_service_proposal(payload=8)
        traffic=document['candidates'][0]['traffic'][-1]
        traffic.update(model='continuous_reserved',latency_cycles=200,
                       transactions_per_cycle=1,inflight=200,response_buffer_bytes=1600)
        report=engine.evaluate(document,sources)
        self.assertFalse(report['ok'])
        self.assertTrue(any('below shared service write_bus lower bound 200' in error for error in report['errors']))

    def test_ordinary_serial_group_cannot_borrow_unrelated_group_spare_cycles(self):
        document,sources=self.execution_service_proposal()
        candidate=document['candidates'][0]
        candidate['workloads'][0]['components'][1]={'id':'compute','composition':'serial',
            'references':['compute'],'basis':'only 80 cycles explicitly charged to this group'}
        candidate['cost_groups'][2]['cycles']=1000
        report=engine.evaluate(document,sources)
        self.assertFalse(report['ok'])
        self.assertTrue(any('charged group cycles 80 below linked transfer service lower bound 160' in error for error in report['errors']))

    def test_inputs_are_not_rewritten_to_meet_the_bound(self):
        document,sources=self.proposal()
        self.compute_service(document['candidates'][0])
        original=copy.deepcopy((document,sources))
        engine.evaluate(document,sources)
        self.assertEqual((document,sources),original)


if __name__=='__main__':
    unittest.main()
