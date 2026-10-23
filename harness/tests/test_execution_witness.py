"""Physical supply behavior, including valid packing/broadcast and unknown models."""
import copy
import unittest
from test_architecture import fixture
from harness.checks.architecture import _engine as engine

class ExecutionWitnessTests(unittest.TestCase):
    def evaluate(self,change=None):
        d,s=fixture()
        if change:change(d['candidates'][0])
        return engine.evaluate(d,s)

    def test_read_latency_enforced(self):
        result=self.evaluate(lambda c:c['schedule_windows'][0]['uses'][0].update(cycle=0))
        self.assertFalse(result['ok']);self.assertTrue(any('before synchronous read' in e for e in result['errors']))

    def test_average_service_does_not_hide_same_cycle_conflict(self):
        result=self.evaluate(lambda c:c['schedule_windows'][0]['reads'][1].update(cycle=0))
        self.assertFalse(result['ok']);self.assertTrue(any('physical requests exceed input' in e for e in result['errors']))

    def test_two_instances_can_read_same_cycle(self):
        def change(c):
            c['schedule_windows'][0]['reads'][1].update(cycle=0,instance=1)
            c['schedule_windows'][0]['uses'][1].update(cycle=1,operand='other_weight')
        result=self.evaluate(change);self.assertTrue(result['ok'],result['errors'])

    def test_single_word_can_broadcast(self):
        def change(c):
            c['cost_groups'][0]['units']=2
            use=copy.deepcopy(c['schedule_windows'][0]['uses'][0]);use['lane']=1
            c['schedule_windows'][0]['uses'].append(use)
        result=self.evaluate(change);self.assertTrue(result['ok'],result['errors'])
        ledger=result['candidates']['small']['ledger']['schedule_windows'][0]
        self.assertEqual(ledger['physical_reads'],7);self.assertEqual(len(ledger['uses']),8)

    def test_two_packed_operands_need_one_word(self):
        def change(c):
            c['macros'][0]['width']=16;c['buffers'][0]['width']=16
            use=c['schedule_windows'][0]['uses'][0];use['operand']='a'
            second=copy.deepcopy(use);second.update(operand='b',bit_offset=8)
            c['schedule_windows'][0]['uses'].append(second)
            c['schedule_windows'][0]['issues'][0]['operands']=['a','b']
        result=self.evaluate(change);self.assertTrue(result['ok'],result['errors'])

    def test_out_of_word_and_instance_fail(self):
        for apply in (lambda c:c['schedule_windows'][0]['uses'][0].update(bit_offset=4),
                      lambda c:c['schedule_windows'][0]['reads'][0].update(instance=2),
                      lambda c:c['schedule_windows'][0]['reads'][0].update(word_address=16)):
            self.assertFalse(self.evaluate(apply)['ok'])

    def test_late_use_needs_explicit_holding(self):
        def change(c):c['schedule_windows'][0]['uses'][0].update(cycle=2)
        self.assertFalse(self.evaluate(change)['ok'])
        def held(c):
            change(c);c['macros'][0]['width']=128
            c['schedule_windows'][0]['uses'][0].update(holding={'buffer':'hold','bit_offset':0,'width':8},operand='held_weight')
            c['buffers'].append({'id':'hold','kind':'register','count':1,'depth':1,'width':8,'basis':'one read payload retained',
              'objects':[{'id':'held_word','shape':[],'element_bits':8,'copies':1,'alignment_bits':8,'metadata_bits':0,'live_in':['run'],'basis':'one held word'}]})
            c['area_components'][0]['covers_buffers']=['hold']
            c['schedule_windows'][0]['issues'][0].update(cycle=2,operands=['held_weight'])
        result=self.evaluate(held);self.assertTrue(result['ok'],result['errors'])

    def test_missing_input_capability_is_unknown(self):
        d,s=fixture();d['requirements']['storage_rules'][0].pop('read_latency_cycles')
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors']);self.assertEqual(r['gate']['status'],'pass')

    def test_unrepresented_service_is_unknown_without_forcing_macros(self):
        def change(c):c['schedule_windows']=[{'id':'register_window','model':'unsupported','reason':'register-mux dataflow outside finite macro service model','basis':'explicit unsupported evidence'}]
        result=self.evaluate(change);self.assertTrue(result['ok'],result['errors']);self.assertEqual(result['gate']['status'],'pass')

    def test_input_and_proposal_unchanged(self):
        d,s=fixture();before=copy.deepcopy((d,s));engine.evaluate(d,s);self.assertEqual((d,s),before)

if __name__=='__main__':unittest.main()
