"""Known-answer cases for unit/model errors, local repair and release scope."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock
import test_architecture as base_tests
import test_audit as audit_tests
from harness.runtime.design_session import replace_candidates, generate, identity, parse_turn, DesignToolLimit
from harness.runtime import audit_session
from harness.checks.architecture import delivery_decision

engine = audit_tests.engine


def density_case(spelling='mm^2', binary=True):
    d, sources = base_tests.fixture()
    quote = ('binary' if binary else 'decimal')+' density 2 Mbit/'+spelling
    sources['spec.md'] += '\n'+quote
    d['requirements']['facts'].append({'id':'density','value':2,'unit':'Mbit/'+spelling,
        'citations':[{'file':'spec.md','start':2,'quote':quote}]})
    d['requirements']['storage_area_model']={'status':'supplied','density_fact':'density'}
    for c in d['candidates']:
        c['area_components']=[row for row in c['area_components'] if row['kind']=='logic']
    return d,sources


class BudgetReliabilityTests(unittest.TestCase):
    def test_bad_candidate_does_not_hide_input_model_behind_key_error(self):
        d,s=density_case();d['candidates'][0].pop('calculations')
        r=engine.evaluate(d,s)
        self.assertFalse(r['ok']);self.assertTrue(any('calculations must be an array' in e for e in r['errors']))
        self.assertFalse(any('KeyError' in e for e in r['errors']))
        self.assertEqual(r['storage_area_model']['bits_per_mm2'],2*2**20)

    def test_failed_metric_keeps_original_diagnostics_and_computed_cost(self):
        d,s=density_case();d['candidates'][0]['area_components'][0]['unit_area_upper']='undefined_area'
        r=engine.evaluate(d,s)
        self.assertFalse(r['ok']);self.assertIn('small',r['candidates'])
        self.assertTrue(any('undefined identifier undefined_area' in e for e in r['errors']))
        self.assertFalse(any('KeyError' in e for e in r['errors']))
        self.assertEqual(r['gate']['status'],'fail')

    def test_intrinsic_binary_byte_density(self):
        d,s=density_case();fact=d['requirements']['facts'][-1]
        quote='density 2 MiB/mm²';s['spec.md']=s['spec.md'].splitlines()[0]+'\n'+quote
        fact.update(unit='MiB/mm²',prefix_base='binary',citations=[{'file':'spec.md','start':2,'quote':quote}])
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors'])
        self.assertEqual(r['storage_area_model']['prefix_base'],'binary')
        self.assertEqual(r['storage_area_model']['bits_per_mm2'],16*2**20)

    def test_equivalent_area_spellings_and_prefix_bases(self):
        for spelling in ('mm2','mm²','mm^2'):
            for binary in (True,False):
                d,s=density_case(spelling,binary)
                d['requirements']['constraints'][0]['unit']=spelling
                r=engine.evaluate(d,s,True)
                self.assertTrue(r['ok'],r['errors'])
                self.assertEqual(r['storage_area_model']['bits_per_mm2'],2*(2**20 if binary else 10**6))

    def test_bit_byte_density_error_cannot_override_generated_cost(self):
        d,s=density_case()
        for c in d['candidates']:
            c['metrics']['area']='logic+256/(density*8*2**20)'
        r=engine.evaluate(d,s)
        self.assertFalse(r['ok']);self.assertTrue(any('program-derived metric' in e for e in r['errors']))
        self.assertAlmostEqual(r['candidates']['small']['ledger']['area_total']['upper_mm2'],2+256/(2*2**20))

    def test_input_unit_conversion_for_limits_and_capacity(self):
        d,s=density_case();req=d['requirements']
        req['facts'][0]['unit']='mm²';req['facts'][1]['unit']='bits'
        req['constraints'][0]['unit']='um^2';req['constraints'][1]['unit']='bytes'
        r=engine.evaluate(d,s)
        self.assertTrue(r['ok'],r['errors'])
        checks=r['candidates']['small']['checks'];self.assertEqual(checks[0]['limit'],3e6);self.assertEqual(checks[1]['limit'],125)

    def test_density_citation_and_prefix_cannot_be_replaced(self):
        d,s=density_case();d['requirements']['facts'][-1]['prefix_base']='decimal'
        self.assertFalse(engine.evaluate(d,s)['ok'])
        d,s=density_case();d['requirements']['facts'][-1]['unit']='Mbyte/mm2'
        self.assertFalse(engine.evaluate(d,s)['ok'])
        d,s=density_case();s['spec.md']+=' other scalar 4'
        d['requirements']['facts'][-1].update(value=4,citations=[{'file':'spec.md','start':2,'quote':s['spec.md'].splitlines()[1]}])
        self.assertFalse(engine.evaluate(d,s)['ok'])

    def test_frequency_squared_is_not_a_conversion(self):
        sources={'spec.md':'frequency 31 MHz rate 12500 items/s'}
        facts=[{'id':'f','source':{'file':'spec.md','line':1,'number':1}},
               {'id':'hz','convert':{'fact':'f','to_unit':'Hz'}},
               {'id':'r','source':{'file':'spec.md','line':1,'number':2}},
               {'id':'cpt','formula':'hz/r','unit':'cycles/item','citations':[{'file':'spec.md','start':1,'quote':sources['spec.md']}]}]
        req={'runtime_parameters':[],'facts':facts,'constraints':[]};values,errors=engine.validate_requirements(req,sources)
        self.assertFalse(errors);self.assertEqual(values['cpt'],2480)
        facts[-1]['formula']='hz*hz/r'
        self.assertTrue(any('dimension mismatch' in e for e in engine.validate_requirements(req,sources)[1]))

    def test_limited_inflight_is_valid_when_rate_and_schedule_match(self):
        d,s=density_case();c=d['candidates'][0];row=c['traffic'][0]
        row.update(latency_cycles='12',inflight='8',transactions_per_cycle='2/3')
        self.assertTrue(engine.evaluate(d,s)['ok'])
        row['transactions_per_cycle']='1'
        self.assertFalse(engine.evaluate(d,s)['ok'])

    def test_script_bundle_runs_without_harness(self):
        d,s=density_case();folder=Path(tempfile.mkdtemp(prefix='budget-bundle-test-'))
        shutil.copytree(audit_tests.BASE/'skills/microarchitecture-design/scripts',folder/'scripts',ignore=shutil.ignore_patterns('__pycache__'))
        r=subprocess.run([sys.executable,'-B',str(folder/'scripts/evaluate.py')],cwd=folder,input=json.dumps({'document':d,'sources':s}),capture_output=True,text=True,check=True)
        self.assertTrue(json.loads(r.stdout)['ok'])


class LocalRepairTests(unittest.TestCase):
    def test_complete_replacement_keeps_other_candidates(self):
        d,s=base_tests.fixture();replacement=copy.deepcopy(d['candidates'][0])
        replacement['assumptions'][0]['value']=1.5
        request={'tool':'replace_candidates','base_design_sha256':identity(d),'candidates':[replacement]}
        changed=replace_candidates(d,request)
        self.assertEqual(changed['candidates'][0],replacement)
        self.assertEqual(changed['candidates'][1],d['candidates'][1])
        self.assertEqual(d['candidates'][0]['assumptions'][0]['value'],2)
        self.assertTrue(engine.evaluate(changed,s)['ok'])

    def test_single_json_fence_is_framing_and_ambiguous_requests_rejected(self):
        d,s=base_tests.fixture();raw=json.dumps({'tool':'evaluate_design','document':d})
        prefix='<<<BUDGET_REQUEST>>>\n'
        for payload in (raw,raw+'\n```','```json\n'+raw+'\n```'):
            self.assertEqual(parse_turn(prefix+payload)['tool_request']['document'],d)
        for suffix in ('\n'+raw,'\n<<<BUDGET_REQUEST>>>\n'+raw,'\n</BUDGET_REQUEST>','\nmore narration'):
            with self.assertRaises(ValueError):parse_turn(prefix+raw+suffix)

    def test_complete_replacement_fixes_conflicting_expressions_without_editing_input(self):
        d,s=base_tests.fixture();d['candidates'][0]['calculations']=[{'id':'custom','formula':'2*16*8','unit':'bits','basis':'fixture custom work'}];bad=copy.deepcopy(d)
        bad['candidates'][0]['calculations'][0]['value']=256
        self.assertFalse(engine.evaluate(bad,s)['ok'])
        request={'tool':'replace_candidates','base_design_sha256':identity(bad),'candidates':[d['candidates'][0]]}
        repaired=replace_candidates(bad,request)
        self.assertTrue(engine.evaluate(repaired,s)['ok']);self.assertEqual(identity(repaired),identity(d))
        self.assertIn('value',bad['candidates'][0]['calculations'][0])

    def test_matching_base_and_preserved_data_required(self):
        d,s=base_tests.fixture();old=copy.deepcopy(d);replacement=copy.deepcopy(d['candidates'][0])
        replacement['traffic'][0]['inflight']='9'
        request={'tool':'replace_candidates','base_design_sha256':identity(d),'candidates':[replacement]}
        changed=replace_candidates(d,request)
        self.assertEqual(d,old);self.assertEqual(changed['candidates'][0]['traffic'][0]['inflight'],'9')
        self.assertEqual(changed['candidates'][1],d['candidates'][1])
        request['base_design_sha256']='stale'
        with self.assertRaises(ValueError):replace_candidates(d,request)
        request['base_design_sha256']=identity(d);request['requirements']=[]
        with self.assertRaises(ValueError):replace_candidates(d,request)

    def test_local_fix_recomputed_before_final_with_resume(self):
        d,s=base_tests.fixture();bad=copy.deepcopy(d);bad['candidates'][0]['traffic'][0]['inflight']='7'
        out=base_tests.SkillToolTests().setup_run()
        req={'tool':'replace_candidates','base_design_sha256':identity(bad),'candidates':[d['candidates'][0]]}
        invoke=Mock(side_effect=[{'tool_request':{'tool':'evaluate_design','document':bad}}, {'tool_request':req},RuntimeError('interrupt')])
        with self.assertRaises(RuntimeError):generate(out,'repair','',d['requirements'],s,invoke,2)
        invoke=Mock(return_value={'final':{'evaluated_design_sha256':identity(d),'architecture_md':d['architecture_md']}})
        final=generate(out,'repair','',d['requirements'],s,invoke,2)
        self.assertEqual(identity(final),identity(d));self.assertEqual(invoke.call_count,1)
        self.assertTrue(json.loads((out/'repair.step-1.tool.result.json').read_text())['ok'])

    def test_invalid_data_cannot_spend_turns_generating_narrative(self):
        d,s=base_tests.fixture();d['candidates'][0]['cost_groups'][0]['units']='MAC'
        out=base_tests.SkillToolTests().setup_run();invoke=Mock(return_value={'tool_request':{'tool':'evaluate_design','document':d}})
        with self.assertRaisesRegex(DesignToolLimit,'invalid data'):generate(out,'invalid','',d['requirements'],s,invoke,1)
        self.assertEqual(invoke.call_count,1);self.assertFalse((out/'invalid.json').exists())


class DeliveryScaleTests(unittest.TestCase):
    def test_packet_and_data_hashes_have_distinct_domains(self):
        ctx=audit_tests.context_fixture()
        self.assertTrue(ctx['identity_check']['budget_matches_design_data'])
        self.assertNotEqual(ctx['packet_sha256'],ctx['identity_check']['design_data_sha256'])
        other=copy.deepcopy(ctx['document']);other['architecture_md']+=' changed prose'
        new=audit_session.packet({'spec.md':ctx['sources']['spec.md'],'reference.py':ctx['sources']['reference.py']},other,ctx['budget'])
        self.assertTrue(new['identity_check']['budget_matches_design_data'])
        self.assertNotEqual(new['packet_sha256'],ctx['packet_sha256'])
        budget=copy.deepcopy(ctx['budget']);budget['design_sha256']='wrong'
        self.assertFalse(audit_session.packet({},other,budget)['identity_check']['budget_matches_design_data'])

    def test_missing_decision_blocks_accept_but_execution_obligation_does_not(self):
        ctx=audit_tests.context_fixture();review=audit_tests.report_fixture(ctx)
        self.assertTrue(audit_tests.validator.validate(review,ctx)['ok'])
        review['unverified'][0].update(kind='design_decision',topic='abi',obligation='choose tensor layout')
        self.assertFalse(audit_tests.validator.validate(review,ctx)['ok'])
        review['verdict']='revise';review['findings'].append({'id':'missing_layout','category':'design','severity':'blocking','disposition':'open','location':'ABI','issue':'layout undecided','evidence':'proposal leaves a necessary choice unresolved','required_change':'specify the complete tensor address mapping','references':review['checks'][3]['references']});self.assertTrue(audit_tests.validator.validate(review,ctx)['ok'])
        self.assertFalse(delivery_decision(ctx['document'],ctx['budget'],review,{'ok':True})['deliverable'])

    def test_optional_suggestion_does_not_force_another_design(self):
        ctx=audit_tests.context_fixture();review=audit_tests.report_fixture(ctx)
        f={'id':'optional','category':'design','severity':'suggestion','disposition':'open','location':'candidate',
           'issue':'optional resource sharing improvement','evidence':'valid current budget','required_change':'optional exploration',
           'references':copy.deepcopy(review['checks'][0]['references'])}
        review['findings']=[f];checked=audit_tests.validator.validate(review,ctx)
        self.assertTrue(checked['ok']);self.assertTrue(delivery_decision(ctx['document'],ctx['budget'],review,checked)['deliverable'])
        f.update(severity='condition',condition_kind='design_decision')
        self.assertFalse(audit_tests.validator.validate(review,ctx)['ok'])


if __name__=='__main__':unittest.main()
