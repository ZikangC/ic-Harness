"""Fixed ABI arithmetic, proposal bindings and counterexample identity."""
import copy
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

import test_audit as protocol
from test_audit_candidate_checks import audit, engine, example, execute, check_rows


BASE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('abi_geometry_test',BASE/'skills/microarchitecture-design/scripts/abi_geometry.py')
geometry=importlib.util.module_from_spec(spec);spec.loader.exec_module(geometry)


def region(alignment=8):
    return {'id':'image','kind':'region','beat_bytes':8,'fields':[
        {'id':name,'payload_bytes':size,'alignment_bytes':alignment}
        for name,size in (('head',5),('body',10),('end',5))]}


def axis(probes=None):
    return {'id':'packed','kind':'axis_pack','shape':[11,3],'axis':0,
            'element_bits':3,'carrier_bits':24,'probes':probes or [
                {'id':'boundary','logical_index':[9,2]}]}


def abi_row(recipe=None,declared=29,metric='extent_bytes',probe=None,quote='region extent = 29'):
    claim={'id':'claim','result':metric,'declared':declared,'relation':'==',
           'references':[{'file':'architecture.md','quote':quote}]}
    if probe is not None:claim['probe']=probe
    return {'id':'layout','geometry':recipe or region(),'claims':[claim],
            'references':[{'file':'spec.md','quote':'items 12'},
                          {'file':'architecture.md','quote':quote}]}


class GeometryArithmeticTests(unittest.TestCase):
    def derive(self,row,names=None):
        result=geometry.derive(row,names or {},engine.expression)
        self.assertTrue(result['ok'],result)
        return result

    def test_cumulative_alignment_and_wire_are_not_total_payload_rounding(self):
        row=region();before=copy.deepcopy(row);r=self.derive(row)
        self.assertEqual(row,before)
        self.assertEqual([x['offset_bytes'] for x in r['details']['fields']],[0,8,24])
        self.assertEqual({k:r['values'][k] for k in ('payload_bytes','extent_bytes','contiguous_wire_bytes','field_beats_lower_bound')},
                         {'payload_bytes':20,'extent_bytes':29,'contiguous_wire_bytes':32,'field_beats_lower_bound':4})
        self.assertNotEqual(r['values']['contiguous_wire_bytes'],((20+7)//8)*8)

    def test_non_power_of_two_alignment_and_sparse_transfer_lower_bound(self):
        row=region(6);r=self.derive(row)
        self.assertEqual([x['offset_bytes'] for x in r['details']['fields']],[0,6,18])
        sparse={'id':'sparse','kind':'region','beat_bytes':8,'fields':[
            {'id':'first','payload_bytes':1,'alignment_bytes':64},
            {'id':'last','payload_bytes':1,'alignment_bytes':64}]}
        r=self.derive(sparse)['values']
        self.assertEqual((r['extent_bytes'],r['contiguous_wire_bytes'],r['field_wire_bytes_lower_bound']),(65,72,16))
        sparse['fields'][1]['alignment_bytes']=1
        self.assertEqual(self.derive(sparse)['values']['field_beats_lower_bound'],1)

    def test_axis_quotient_remainder_inverse_and_padding(self):
        r=self.derive(axis())
        v=r['values']
        self.assertEqual((v['p_boundary__carrier_index'],v['p_boundary__carrier_byte_offset'],v['p_boundary__element_bit_offset']),(5,15,3))
        self.assertEqual((r['probes']['boundary']['packed_index'],r['probes']['boundary']['lane']),([1,2],1))
        probes=[{'id':'p_'+str(i)+'_'+str(j),'logical_index':[i,j]} for i in range(11) for j in range(3)]
        forward=self.derive(axis(probes))
        inverse=[{'id':pid,'packed_index':point['packed_index'],'lane':point['lane']} for pid,point in forward['probes'].items()]
        restored=self.derive(axis(inverse))
        self.assertEqual([point['logical_index'] for point in forward['probes'].values()],
                         [point['logical_index'] for point in restored['probes'].values()])
        invalid=axis([{'id':'padding','packed_index':[1,2],'lane':3}])
        self.assertFalse(geometry.derive(invalid,{},engine.expression)['ok'])

    def test_unused_carrier_bits_and_unknown_order_are_explicit(self):
        row=axis();row['element_bits']=5;row['carrier_bits']=16
        self.assertEqual(self.derive(row)['values']['padding_bits_per_carrier'],1)
        row['order']='F';result=geometry.derive(row,{},engine.expression)
        self.assertFalse(result['ok']);self.assertTrue(result['unknown']);self.assertEqual(result['errors'],[]);self.assertEqual(result['values'],{})

    def test_beat_tail_and_crossing_element(self):
        row={'id':'tokens','kind':'beat_span','payload_bytes':'7*3','beat_bytes':8,
             'probes':[{'id':'cross','byte_offset':6,'size_bytes':3}]}
        values=self.derive(row)['values']
        self.assertEqual((values['payload_bytes'],values['full_beat_extent_bytes'],values['tail_padding_bytes']),(21,24,3))
        self.assertEqual((values['p_cross__aligned_byte_offset'],values['p_cross__byte_lane'],values['p_cross__spanned_beats']),(0,6,2))
        row['probes'][0]['byte_offset']=20
        self.assertFalse(geometry.derive(row,{},engine.expression)['ok'])

    def test_invalid_arithmetic_missing_extent_and_unsafe_expression_do_not_produce_values(self):
        for value in (-1,1.5,'unknown','__import__("os")',True):
            row=region();row['fields'][0]['payload_bytes']=value
            result=geometry.derive(row,{},engine.expression)
            self.assertFalse(result['ok'],value);self.assertTrue(result['errors']);self.assertEqual(result['values'],{})
        row=region();del row['fields'][0]['alignment_bytes']
        self.assertFalse(geometry.derive(row,{},engine.expression)['ok'])

    def test_exact_large_integer_output_collision_and_duplicate_id(self):
        count=2**53+1
        r=self.derive({'id':'big','kind':'beat_span','payload_bytes':count,'beat_bytes':7})
        self.assertEqual(r['values']['full_beat_extent_bytes'],((count+6)//7)*7)
        result=geometry.derive_all([region()],{'image__extent_bytes':999},engine.expression)
        self.assertTrue(result['errors']);self.assertEqual(result['values'],{})
        result=geometry.derive_all([region(),region()],{},engine.expression)
        self.assertTrue(result['errors']);self.assertEqual(result['values']['image__extent_bytes'],29)
        row={'id':'overflow','kind':'axis_pack','shape':[10**20,10**20],
             'axis':0,'element_bits':8,'carrier_bits':8}
        self.assertEqual(geometry.derive(row,{},engine.expression)['values'],{})

    def test_helper_is_standalone_with_standard_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'abi_geometry.py';shutil.copyfile(Path(geometry.__file__),path)
            spec=importlib.util.spec_from_file_location('copied_abi_geometry',path)
            copied=importlib.util.module_from_spec(spec);spec.loader.exec_module(copied)
            result=copied.derive(region(),{},lambda v,n:int(v))
            self.assertTrue(result['ok']);self.assertEqual(result['values']['extent_bytes'],29)


class DesignGeometryTests(unittest.TestCase):
    def test_optional_geometry_publishes_non_overwriting_values_to_fresh_traffic(self):
        document,sources=protocol.fixture();before=copy.deepcopy(document)
        self.assertTrue(engine.evaluate(document,sources)['ok'])
        self.assertEqual(document,before)
        candidate=document['candidates'][0];candidate['abi_geometry']=[region()]
        candidate['traffic'][0].update(payload_bytes='image__extent_bytes',transfers=1)
        before=copy.deepcopy(document);result=engine.evaluate(document,sources)
        self.assertTrue(result['ok'],result['errors']);self.assertEqual(document,before)
        selected=result['candidates']['small']
        self.assertEqual(selected['values']['image__extent_bytes'],29)
        self.assertEqual(selected['ledger']['traffic'][0]['wire_bytes'],32)
        self.assertEqual(selected['ledger']['abi_geometry'][0]['values']['extent_bytes'],29)

    def test_invalid_or_unsupported_geometry_is_not_a_passing_selected_gate(self):
        document,sources=protocol.fixture();candidate=document['candidates'][0]
        candidate['abi_geometry']=[axis()];candidate['abi_geometry'][0]['order']='F'
        result=engine.evaluate(document,sources)
        self.assertTrue(any('abi_geometry/' in reason for reason in result['candidates']['small']['ledger']['unsupported_models']))
        self.assertEqual(result['gate']['status'],'unknown')
        candidate['abi_geometry']=[region()];candidate['abi_geometry'][0]['beat_bytes']=0
        result=engine.evaluate(document,sources)
        self.assertFalse(result['ok']);self.assertTrue(any('ABI geometry' in error for error in result['errors']))


class AuditGeometryTests(unittest.TestCase):
    def args(self):
        model,baseline,proposal,sources=example()
        proposal['architecture_md']='region extent = 29\nlogical formula: i0\nunrelated formula: p0\n'
        model['abi_checks']=[abi_row()]
        return model,baseline,proposal,sources

    def test_missing_geometry_id_names_the_abi_declaration_and_preserves_other_claims(self):
        args=self.args();model=args[0]
        bad=copy.deepcopy(model['abi_checks'][0]);bad['id']='missing_geometry_id'
        del bad['geometry']['id'];model['abi_checks'].append(bad)
        before=copy.deepcopy(args);result=execute(*args)
        self.assertFalse(result['ok']);self.assertEqual(args,before)
        self.assertIn('abi/missing_geometry_id: geometry.id required',result['errors'])
        self.assertEqual([(row['expected'],row['actual'],row['status']) for row in check_rows(result,'abi__')],[(29,29,'pass')])
        failed=next(row for row in result['subreceipts'] if row['id']=='abi/1/claim/0')
        self.assertEqual((failed['status'],failed['checks'],failed['values']),('declaration_failed',[],{}))

    def test_independent_ids_bound_source_parameters_and_fresh_traffic(self):
        args=self.args();model,baseline,proposal,sources=args
        model['abi_checks'][0]['geometry']['beat_bytes']='source__request_peak*8'
        model['abi_checks'][0]['geometry']['id']='auditors_own_id'
        model['abi_checks'][0]['claims'][0].update(result='field_wire_bytes_lower_bound',
            relation='<=',declared={'traffic_id':'reads','metric':'wire_bytes'})
        before=copy.deepcopy(args);r=execute(*args)
        self.assertTrue(r['ok'],r['errors']);self.assertEqual(args,before)
        check=check_rows(r,'abi__')[0]
        self.assertEqual((check['expected'],check['actual'],check['status']),(32,64,'pass'))
        self.assertEqual(check['abi_target'],{'traffic_id':'reads','metric':'wire_bytes'})
        self.assertNotIn('missing_abi_checks',r)
        model['abi_checks'][0]['claims'][0]['declared']['actual']=999
        r=execute(*args);self.assertFalse(r['ok']);self.assertEqual(check_rows(r,'abi__'),[])

    def test_axis_declared_formula_uses_finite_selected_logical_index(self):
        args=self.args();args[0]['abi_checks']=[abi_row(axis(),declared='2*p0',metric='logical_0',probe='boundary',quote='logical formula: i0')]
        r=execute(*args);self.assertTrue(r['ok'],r['errors'])
        check=check_rows(r,'abi__')[0]
        self.assertEqual((check['expected'],check['actual'],check['status']),(9,2,'fail'))
        self.assertEqual(check['geometry_probe'],{'mode':'logical','index':[9,2]})
        self.assertEqual(check['abi_target']['references'],[{'file':'architecture.md','start':2,'end':2}])
        args[0]['abi_checks'][0]['claims'][0]['declared']='i0'
        self.assertEqual(check_rows(execute(*args),'abi__')[0]['status'],'pass')

    def test_selector_diagnostics_preserve_valid_claims_and_do_not_invent_failed_geometry_checks(self):
        args=self.args();model,baseline,proposal,sources=args
        proposal['architecture_md']='tail padding = 3\ncrossing spans 2 beats\n'
        recipe={'id':'tail','kind':'beat_span','payload_bytes':21,'beat_bytes':8,
                'probes':[{'id':'cross','byte_offset':6,'size_bytes':3}]}
        model['abi_checks']=[abi_row(recipe,declared=3,metric='tail_padding_bytes',quote='tail padding = 3')]
        claims=model['abi_checks'][0]['claims']
        cross=abi_row(recipe,declared=2,metric='spanned_beats',probe='cross',quote='crossing spans 2 beats')['claims'][0]
        cross['id']='crossing';claims.append(cross)
        accepted=execute(*args);self.assertTrue(accepted['ok'],accepted['errors'])
        self.assertEqual([(row['expected'],row['actual'],row['status']) for row in check_rows(accepted,'abi__')],
                         [(3,3,'pass'),(2,2,'pass')])
        joined=copy.deepcopy(cross);joined.update(id='joined',result='cross::spanned_beats');joined.pop('probe')
        unknown_probe=copy.deepcopy(cross);unknown_probe.update(id='missing_probe',probe='absent')
        unknown_metric=copy.deepcopy(cross);unknown_metric.update(id='missing_metric',result='span_count')
        claims.extend([joined,unknown_probe,unknown_metric]);before=copy.deepcopy(args)
        rejected=execute(*args)
        self.assertFalse(rejected['ok']);self.assertEqual(args,before)
        self.assertEqual(rejected['checks'],accepted['checks']);self.assertEqual(rejected['values'],accepted['values'])
        invalid=[row for row in rejected['subreceipts'] if row['section']=='abi' and row['status']=='declaration_failed']
        self.assertEqual(len(invalid),3)
        self.assertTrue(all(not row['checks'] and not row['values'] and not row['calculation_ids'] for row in invalid))
        self.assertFalse(any(row['status']=='fail' for row in rejected['checks']))
        joined_error=invalid[0]['errors'][0]
        self.assertIn('unknown generated ABI result',joined_error)
        self.assertIn('recipe results:',joined_error);self.assertIn('tail_padding_bytes',joined_error)
        self.assertIn('select probe and result separately',joined_error)
        for row in invalid:
            self.assertIn("'cross'",row['errors'][0]);self.assertIn('spanned_beats',row['errors'][0])
        self.assertIn('actual selected probe',invalid[1]['errors'][0])
        claims[-3:]=[]
        corrected=execute(*args);self.assertTrue(corrected['ok'],corrected['errors'])
        self.assertEqual(corrected['checks'],accepted['checks']);self.assertEqual(corrected['values'],accepted['values'])

    def test_one_bad_claim_does_not_erase_successful_counterexample_receipt(self):
        args=self.args();claim=args[0]['abi_checks'][0]['claims'][0];claim['declared']=28
        invalid=copy.deepcopy(claim);invalid['id']='bad';invalid['references'][0]['quote']='not in the frozen proposal'
        args[0]['abi_checks'][0]['claims'].append(invalid)
        r=execute(*args)
        self.assertFalse(r['ok']);self.assertEqual(len(check_rows(r,'abi__')),1)
        self.assertEqual(check_rows(r,'abi__')[0]['status'],'fail')
        receipts=[row for row in r['subreceipts'] if row['section']=='abi']
        self.assertEqual([row['status'] for row in receipts],['executed','declaration_failed'])
        self.assertTrue(receipts[0]['values']);self.assertEqual(receipts[1]['values'],{})

    def test_duplicate_claim_cannot_replace_an_executed_failure(self):
        args=self.args();claim=args[0]['abi_checks'][0]['claims'][0];claim['declared']=28
        duplicate=copy.deepcopy(claim);duplicate['declared']=29
        args[0]['abi_checks'][0]['claims'].append(duplicate)
        result=execute(*args)
        self.assertFalse(result['ok']);self.assertIn('duplicate ABI claim id',result['errors'])
        self.assertEqual([row['status'] for row in check_rows(result,'abi__')],['fail'])

    def test_beat_extent_against_actual_declared_allocation_and_proposal_parameters(self):
        args=self.args()
        args[2]['architecture_md']='allocated bytes = 21\n'
        recipe={'id':'tail','kind':'beat_span','payload_bytes':'proposal__items*3-15','beat_bytes':8,
                'probes':[{'id':'cross','byte_offset':6,'size_bytes':3}]}
        args[0]['abi_checks']=[abi_row(recipe,declared=21,metric='full_beat_extent_bytes',quote='allocated bytes = 21')]
        args[0]['abi_checks'][0]['claims'][0]['relation']='<='
        result=execute(*args);self.assertTrue(result['ok'],result['errors'])
        check=check_rows(result,'abi__')[0]
        self.assertEqual((check['expected'],check['actual'],check['status']),(24,21,'fail'))

    def test_unbound_original_only_and_unsupported_are_not_hardware_failures(self):
        args=self.args();args[0]['abi_checks'][0]['claims'][0]['references']=[{'file':'spec.md','quote':'items 12'}]
        r=execute(*args);self.assertFalse(r['ok']);self.assertEqual(check_rows(r,'abi__'),[])
        args=self.args();args[0]['abi_checks'][0]['geometry']=axis();args[0]['abi_checks'][0]['geometry']['order']='F'
        r=execute(*args);self.assertFalse(r['ok']);self.assertEqual(check_rows(r,'abi__'),[])
        receipt=next(row for row in r['subreceipts'] if row['section']=='abi')
        self.assertEqual(receipt['status'],'declaration_failed');self.assertTrue(receipt['unknown']);self.assertFalse(receipt['values'])

    def context(self):
        document,sources=protocol.fixture()
        document['architecture_md']='region extent = 29\nlogical formula: i0\nunrelated formula: p0\n'
        budget=engine.evaluate(document,sources,True)
        return protocol.packet(sources,document,budget)

    def run_check(self,context,request_id,row):
        model=protocol.proposal_request()['model'];model['abi_checks']=[row]
        r=audit.check(model,context['source_analysis'],context['document'],context['budget'],context['sources'],engine,
            source_declaration=context['baseline']['baseline']['analysis'],execution_dependencies=context['execution_dependencies'])
        self.assertTrue(r['ok'],r['errors']);r['request_id']=request_id
        return r

    def review(self,context,failed,corrected):
        context['calculations']=[failed,corrected];context['proposal_checks']=[failed,corrected]
        report=protocol.report_fixture(context);report['verdict']='insufficient_evidence'
        old=next(row for row in failed['subreceipts'] if row['section']=='abi');new=next(row for row in corrected['subreceipts'] if row['section']=='abi')
        check=old['checks'][0]
        report['counterexample_reviews']=[{'request_id':'failed','receipt_id':old['id'],'check_id':check['id'],
            'disposition':'audit_declaration_error','reason':'Correct the independent geometry parameters at the same actual proposal location.',
            'correction_check':{'request_id':'corrected','receipt_id':new['id'],'check_id':new['checks'][0]['id']},
            'references':[{'file':'spec.md','quote':'items 12'},{'file':'architecture.md','quote':'region extent = 29'},
                          {'request_id':'failed','calculation_id':old['id']+'::'+check['id']+'__actual','actual':check['actual']}]}]
        return report

    def test_receipt_context_rejects_stale_source_and_tampering(self):
        context=self.context();r=self.run_check(context,'failed',abi_row(declared=28))
        verify=lambda result,sources:protocol.validator.executed_values(result,sources,context['source_analysis'],context['document'],context['budget'],context['execution_dependencies'])
        self.assertTrue(verify(r,context['sources'])['values'])
        stale=dict(context['sources']);stale['architecture.md']+='changed\n'
        self.assertFalse(verify(r,stale)['values'])
        tampered=copy.deepcopy(r);child=next(row for row in tampered['subreceipts'] if row['section']=='abi');child['checks'][0]['actual']=29
        rejected=verify(tampered,context['sources'])
        self.assertTrue(any('fingerprint' in error for error in rejected['errors']));self.assertFalse(any('abi__' in key for key in rejected['values']))

    def test_actual_failed_abi_comparison_requires_explicit_judgment(self):
        context=self.context();failed=self.run_check(context,'failed',abi_row(declared=28))
        context['calculations']=[failed];context['proposal_checks']=[failed]
        result=protocol.validator.validate(protocol.report_fixture(context),context)
        self.assertFalse(result['ok']);self.assertTrue(any('counterexample requires explicit adjudication' in error for error in result['errors']),result['errors'])

    def test_parameter_correction_at_same_target_can_close_auditor_error(self):
        context=self.context();failed=self.run_check(context,'failed',abi_row(region(4)))
        corrected=self.run_check(context,'corrected',abi_row(region(8)))
        report=self.review(context,failed,corrected)
        result=protocol.validator.validate(report,context)
        self.assertTrue(result['ok'],result['errors'])

    def test_unrelated_proposal_location_metric_or_probe_cannot_close_error(self):
        for alteration in ('target','metric','probe'):
            with self.subTest(alteration=alteration):
                context=self.context()
                original=abi_row(axis(),declared='2*i0',metric='logical_0',probe='boundary',quote='logical formula: i0')
                failed=self.run_check(context,'failed',original)
                correction=copy.deepcopy(original);correction['claims'][0]['declared']='i0'
                if alteration=='target':correction['claims'][0]['references']=[{'file':'architecture.md','start':3,'end':3}]
                elif alteration=='metric':correction['claims'][0].update(result='lane',declared='lane')
                else:correction['geometry']['probes'][0]['logical_index']=[8,2]
                corrected=self.run_check(context,'corrected',correction)
                report=self.review(context,failed,corrected)
                result=protocol.validator.validate(report,context)
                self.assertFalse(result['ok']);self.assertTrue(any('requires a related actual passing check' in error for error in result['errors']),result['errors'])


if __name__=='__main__':unittest.main()
