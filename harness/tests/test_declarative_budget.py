"""Known-answer budgets and protocol distinctions, not architecture-quality proof."""
import copy
import json
import unittest
from unittest.mock import Mock
from test_architecture import fixture
from test_audit import engine, context_fixture, report_fixture, snapshot, validator
from harness.runtime import audit_session
from harness.runtime.design_session import progress_signature
from harness.prompts import architecture as prompts
from harness.checks.architecture import delivery_decision


class DeclarativeBudgetTests(unittest.TestCase):
    def test_citation_feedback_exposes_raw_source_without_rewriting_facts(self):
        sources={'spec.md':'capacity `123` bits'}
        req={'facts':[{'id':'capacity','value':123,'unit':'bits','citations':[{'file':'spec.md','start':1,'quote':'capacity 123 bits'}]}],'constraints':[]}
        before=copy.deepcopy(req);values,errors=engine.validate_requirements(req,sources)
        self.assertEqual(req,before);self.assertTrue(any('capacity `123` bits' in e for e in errors))

    def test_wrong_named_row_container_reports_the_contract_directly(self):
        d,s=fixture();d['candidates'][0]['assumptions']={'lanes':4}
        r=engine.evaluate(d,s);self.assertFalse(r['ok'])
        self.assertTrue(any('assumptions must be an array' in e for e in r['errors']))
        self.assertFalse(any('string indices' in e for e in r['errors']))

    def test_fact_id_spelling_never_controls_clock_units(self):
        d,s=fixture();f=next(x for x in d['requirements']['facts'] if x['id']=='clock')
        f.update(id='frequency_named_hz',value=80,unit='MHz',citations=[{'file':'spec.md','start':2,'quote':'frequency 80 MHz'}])
        s['spec.md']+='\nfrequency 80 MHz'
        for c in d['candidates']:c['workloads'][0]['clock_fact']='frequency_named_hz'
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors'])
        self.assertEqual(r['candidates']['small']['workloads']['primary']['clock_hz'],80000000)
        d['candidates'][0]['workloads'][0]['clock_hz']=80000000
        self.assertFalse(engine.evaluate(d,s)['ok']) # old input not converted

    def test_authoritative_results_and_source_document_unchanged(self):
        d,s=fixture();before=copy.deepcopy(d);r=engine.evaluate(d,s,True)
        self.assertTrue(r['ok'],r['errors']);self.assertEqual(d,before)
        c=r['candidates']['small']
        self.assertEqual(c['macro_inventory_bits'],256)
        self.assertEqual(c['ledger']['traffic'][0]['total_bytes'],64)
        self.assertEqual(c['ledger']['buffers'][0]['allocated_bits'],256)
        self.assertEqual(c['ledger']['area_total'],{'lower_mm2':2.25,'upper_mm2':2.25})
        self.assertEqual(c['workloads']['primary']['task_cycles'],120)
        self.assertEqual(c['workloads']['primary']['cycles_per_item'],10)
        self.assertEqual(c['workloads']['primary']['items_per_second'],8000000)
        self.assertEqual([x['actual'] for x in c['checks']],[2.25,256,8000000])

    def test_legacy_derived_fields_rejected_not_converted(self):
        for change in [lambda c:c.update(area_totals={'lower':2.25,'upper':2.25}),
                       lambda c:c['traffic'][0].update(total_bytes=64),
                       lambda c:c['buffers'][0].update(allocated_bits=256),
                       lambda c:c['workloads'][0].update(task_cycles=120),
                       lambda c:c['metrics'].update(capacity=256),
                       lambda c:c['calculations'].append({'id':'macro_bits','value':256,'unit':'bits','basis':'old total'})]:
            d,s=fixture();change(d['candidates'][0]);before=copy.deepcopy(d);r=engine.evaluate(d,s)
            self.assertFalse(r['ok']);self.assertEqual(d,before)

    def test_independent_payload_tails_have_separate_transactions(self):
        d,s=fixture();row=d['candidates'][0]['traffic'][0]
        row.update(payload_bytes=9,transfers=3,alignment_bytes=1,transaction_bytes=8)
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors']);t=r['candidates']['small']['ledger']['traffic'][0]
        self.assertEqual(t['total_bytes'],27);self.assertEqual(t['transactions'],6);self.assertEqual(t['wire_bytes'],48)

    def test_compute_reference_tracks_changed_organization(self):
        d,s=fixture();d['candidates'][0]['cost_groups'][0]['overhead_cycles']=20
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors'])
        self.assertEqual(r['candidates']['small']['workloads']['primary']['task_cycles'],140)
        d['candidates'][0]['cost_groups'][0]['overhead_cycles']=-1
        self.assertFalse(engine.evaluate(d,s)['ok'])
        d,s=fixture();d['candidates'][0]['workloads'][0]['components'][1]['references']=['absent']
        self.assertFalse(engine.evaluate(d,s)['ok'])

    def test_unsupported_model_is_unknown_and_cannot_release(self):
        d,s=fixture();t=d['candidates'][0]['traffic'][0]
        t.update(model='unsupported',reason='streaming consumer needs occupancy witness')
        r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors']);self.assertEqual(r['gate']['status'],'unknown')
        self.assertEqual(r['gate']['unsupported_models'],['traffic/reads'])
        self.assertFalse(delivery_decision(d,r,{'verdict':'accept','checks':[],'findings':[],'unverified':[]},{'ok':True})['deliverable'])
        d['selected']='large';self.assertEqual(engine.evaluate(d,s)['gate']['status'],'fail') # hard area overrun, not unsupported

    def test_sustained_rate_credit_deficit_is_concrete_error(self):
        d,s=fixture();row=d['candidates'][0]['traffic'][0];row.update(latency_cycles=12,inflight=8,transactions_per_cycle=1)
        self.assertFalse(engine.evaluate(d,s)['ok'])
        row['transactions_per_cycle']='2/3';r=engine.evaluate(d,s);self.assertTrue(r['ok'],r['errors'])
        self.assertEqual(r['candidates']['small']['ledger']['traffic'][0]['minimum_inflight'],8)
        # Calculator pass is not a guarantee that the existing task schedule accounts for throttling.

    def test_wrong_performance_dimension_does_not_become_numeric_pass(self):
        d,s=fixture();d['requirements']['constraints'][2]['unit']='bytes'
        self.assertFalse(engine.evaluate(d,s)['ok'])

    def test_progress_includes_ledger_changes_but_not_editorial(self):
        d,s=fixture();other=copy.deepcopy(d);other['candidates'][0]['traffic'][0]['inflight']=9
        self.assertNotEqual(progress_signature(d),progress_signature(other))
        other=copy.deepcopy(d);other['candidates'][0]['traffic'][0]['basis']='clearer explanation';other['architecture_md']='new prose'
        self.assertEqual(progress_signature(d),progress_signature(other))


class ReviewRepairScopeTests(unittest.TestCase):
    def test_current_findings_never_become_prior_revision_ids(self):
        ctx=context_fixture();prior={'findings':[{'id':'old-port','severity':'blocking','disposition':'open'}]}
        repairs=[{'kind':'report_validation_repair','rejected_report':{'findings':[{'id':'current-ABI'}]},'validation':{'errors':['reference absent']}}]
        p=prompts.audit('skill',ctx['sources'],history=repairs,previous_audit=prior)
        self.assertIn('EXACT PRIOR BLOCKING FINDING IDS FOR revision_checks: ["old-port"]',p)
        p=prompts.audit('skill',ctx['sources'],history=repairs)
        self.assertIn('EXACT PRIOR BLOCKING FINDING IDS FOR revision_checks: []',p)
        self.assertIn('CURRENT STEP: return ONLY <<<AUDIT_TOOL_REQUEST>>>',p)
        self.assertNotIn('CURRENT STEP: return ONLY <<<AUDIT_REPAIR>>>',p)
        self.assertNotIn('PRIOR AUDIT (',p)

    def test_report_repair_reuses_packet_and_executed_calculations(self):
        d,s=fixture();budget=engine.evaluate(d,s,True);out=snapshot();prompts_seen=[]
        from test_audit import arithmetic_request,report_content_fixture,wire_provider
        def reply(name,role,prompt,parser):
            prompts_seen.append(prompt)
            if name.endswith('step-0'):return {'tool_request':arithmetic_request()}
            ctx=json.loads((out/'repair.packet.json').read_text());receipt=json.loads((out/'repair.step-0.tool.result.json').read_text());receipt['request_id']='repair.step-0';ctx['calculations']=[receipt]
            report=report_content_fixture(ctx);report['verdict']='insufficient_evidence'
            if name.endswith('step-1'):
                report['checks'][0]['evidence']=[]
                return {'final':report}
            return {'report_repair':{'checks':[report['checks'][0]]}}
        report,check=audit_session.run(out,'repair',s,d,budget,wire_provider(callback=reply),1)
        self.assertTrue(check['ok']);self.assertEqual(len(prompts_seen),3)
        self.assertEqual(report['revision_checks'],[])
        self.assertIn('CURRENT RESPONSE FEEDBACK',prompts_seen[-1])
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),1)

    def test_blocking_report_requires_specific_proposal_reference(self):
        ctx=context_fixture();r=report_fixture(ctx);r['verdict']='revise'
        r['findings']=[{'id':'port','category':'design','severity':'blocking','disposition':'open','location':'schedule','issue':'declared demand exceeds service','evidence':'concrete window','required_change':'provide sufficient service or a slower recosted schedule','references':[{'file':'spec.md','quote':'area 3'}]}]
        self.assertFalse(validator.validate(r,ctx)['ok'])
        r['findings'][0]['references'].append({'file':'design.json','quote':'"id": "small"'})
        self.assertTrue(validator.validate(r,ctx)['ok']) # reference existence still does not establish semantic correctness

if __name__=='__main__':unittest.main()
