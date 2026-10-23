"""Actual independent tool transactions retain evidence without relaxing release."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

from test_audit_candidate_checks import example, execute, engine, source_result
from test_audit import context_fixture, proposal_request, report_fixture

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('partial_receipt_validator', BASE / 'skills/microarchitecture-audit/scripts/validate_review.py')
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def actual_result():
    model, baseline, proposal, sources = example()
    model['operand_checks'][0]['issues'][0]['cycle'] = 'invalid-cycle'
    result = execute(model, baseline, proposal, sources)
    baseline = source_result(baseline, sources)
    sources = dict(sources, **{'design.json': json.dumps(proposal, ensure_ascii=False, sort_keys=True),
                              'architecture.md': proposal['architecture_md']})
    budget = engine.evaluate(proposal, {key: sources[key] for key in ('spec.md', 'reference.py')})
    dependencies = {key: result['binding'][key] for key in ('checker_bundle_sha256', 'calculator_bundle_sha256')}
    return result, sources, baseline, proposal, budget, dependencies


class PartialReceiptTests(unittest.TestCase):
    def select(self, args):
        return validator.executed_values(*args)

    def test_bad_operand_preserves_actual_count_and_flow_calculations(self):
        args = actual_result(); result = args[0]
        self.assertFalse(result['ok']); self.assertFalse(result['coverage_complete'])
        selected = self.select(args)
        self.assertEqual(selected['errors'], [])
        self.assertTrue(any(key.endswith('count__source') for key in selected['calculation_ids']))
        self.assertTrue(any('flow' in key for key in selected['calculation_ids']))
        self.assertEqual(selected['values'], result['values'])
        self.assertTrue(any(row['status'] == 'declaration_failed' for row in result['subreceipts']))

    def test_changed_input_does_not_reuse_valid_local_receipts(self):
        args = list(actual_result()); args[1] = dict(args[1], **{'spec.md': args[1]['spec.md'] + '\nchanged'})
        selected = self.select(args)
        self.assertFalse(selected['values']); self.assertTrue(selected['errors'])

    def test_changed_baseline_budget_or_calculator_invalidates_receipts(self):
        for index in (2, 4, 5):
            args = list(actual_result()); args[index] = copy.deepcopy(args[index]); args[index]['extra'] = 1
            if index == 5:args[index]['calculator_bundle_sha256'] = 'wrong'
            selected = self.select(args)
            self.assertFalse(selected['values']); self.assertTrue(selected['errors'])

    def test_parent_cannot_forge_executed_number(self):
        args = actual_result(); key = next(iter(args[0]['values'])); args[0]['values'][key] += 1
        selected = self.select(args)
        self.assertNotIn(key, selected['values']); self.assertTrue(selected['errors'])

    def test_subreceipt_tampering_rejects_only_that_transaction(self):
        args = actual_result()
        row = next(row for row in args[0]['subreceipts'] if row['values'])
        ids = set(row['calculation_ids']); row['basis'] = 'altered after execution'
        selected = self.select(args)
        self.assertFalse(ids & set(selected['calculation_ids'])); self.assertTrue(selected['values']); self.assertTrue(selected['errors'])

    def test_unexecuted_parent_value_is_not_a_valid_observation(self):
        args = actual_result(); args[0]['values']['invented'] = 17; args[0]['calculation_ids'].append('invented')
        selected = self.select(args)
        self.assertNotIn('invented', selected['values']); self.assertTrue(selected['errors'])

    def test_wrong_parent_declaration_binding_rejects_all_children(self):
        args = actual_result(); args[0]['model_sha256'] = 'different'
        selected = self.select(args)
        self.assertFalse(selected['values']); self.assertTrue(selected['errors'])

    def test_malformed_result_is_diagnostic_not_exception(self):
        for malformed in (None, {'values': []}, {'values': {}, 'calculation_ids': {}}):
            args = list(actual_result()); args[0] = malformed
            self.assertTrue(self.select(args)['errors'])


class RealizationClosureTests(unittest.TestCase):
    def actual(self, operations=80, numeric=True):
        context=context_fixture();request=proposal_request()['model']
        count=request['count_checks'][0]
        count.update(realization='transformed',equivalence_evidence='Independent synthetic effect correspondence; not a formal proof.')
        if not numeric:
            request['operand_checks']=[]
            item=context['document']['work_model']['items'][0]
            item.update(category='movement',count={'kind':'none','reason':'synthetic byte routing, no numerical arithmetic'})
            for candidate in context['document']['candidates']:
                group=candidate['cost_groups'][0]
                for key in ('units','operations_per_unit_cycle','overhead_cycles'):group.pop(key)
                group.update(model='cycles',cycles=80)
                candidate['schedule_windows']=[]
                candidate['coverage']['schedule_windows']={'status':'not_applicable','basis':'pure routing does not declare a numerical compute operand window'}
            context['budget']=engine.evaluate(context['document'],{k:context['sources'][k] for k in ('spec.md','reference.py')},True)
        steps=[{'id':'effects','kind':'elementwise','shape':[operations],'ops_per_element':1}] if numeric else []
        count['physical_realization']={'basis':'independent actual resource recipe','groups':[{
            'cost_group':'compute','steps':steps,'scope':{'kind':'task','axis':'run'},
            'basis':'actual complete synthetic task effect and native cost',
            'references':[{'file':'spec.md','quote':'items 12'},{'file':'design.json','quote':'"id": "small"'}]}]}
        context['sources']['design.json']=json.dumps(context['document'],ensure_ascii=False,indent=2,sort_keys=True)
        path=BASE/'skills/microarchitecture-audit/scripts/candidate_checks.py'
        spec=importlib.util.spec_from_file_location('actual_realization_cost',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        result=module.check(request,context['source_analysis'],context['document'],context['budget'],context['sources'],engine, source_declaration=context['baseline']['baseline']['analysis'], execution_dependencies=context['execution_dependencies'])
        self.assertTrue(result['ok'],result['errors']);result['request_id']='actual-cost'
        context['proposal_checks']=[result];context['calculations']=[result]
        report=report_fixture(context)
        physical_id=next(key for key in result['calculation_ids'] if '__physical__' in key)
        report['realization_reviews']=[{'count_check_id':'direct_work','status':'reviewed',
            'evidence':'Synthetic protocol test of independent correspondence; evidence format does not prove semantic correctness.',
            'references':[{'file':'spec.md','quote':'items 12'},{'file':'design.json','quote':'"id": "small"'},
                          {'request_id':'actual-cost','calculation_id':physical_id,'actual':result['values'][physical_id]}]}]
        return report,context

    def test_reviewed_transformation_with_executed_cost_can_pass(self):
        report,context=self.actual()
        self.assertTrue(validator.validate(report,context)['ok'])
        self.assertEqual(context['proposal_checks'][0]['work_result']['observations'][0]['semantic_equivalence_status'],'unverified')

    def test_zero_arithmetic_routing_is_not_forced_to_match_source_ops(self):
        report,context=self.actual(numeric=False)
        result=validator.validate(report,context)
        self.assertTrue(result['ok'],result['errors'])

    def test_missing_independent_correspondence_or_cost_citation_blocks_accept(self):
        for mode in ('missing','unverified','unrelated'):
            report,context=self.actual()
            if mode=='missing':report.pop('realization_reviews')
            elif mode=='unverified':report['realization_reviews'][0]['status']='unverified'
            else:report['realization_reviews'][0]['references']=report['realization_reviews'][0]['references'][:2]
            self.assertFalse(validator.validate(report,context)['ok'])

    def test_underbilled_physical_recipe_blocks_even_model_reviewed(self):
        report,context=self.actual(operations=96)
        self.assertFalse(validator.validate(report,context)['ok'])

    def test_later_success_cannot_silently_omit_earlier_executed_deficit(self):
        context=context_fixture();request=proposal_request()['model']
        spec=importlib.util.spec_from_file_location('prior_candidate',BASE/'skills/microarchitecture-audit/scripts/candidate_checks.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        macro={'id':'fill','workload':'primary','intervals':[{'start':0,'end':1}],
               'resource':{'kind':'macro_writes','reference':'banks'},
               'demands':[{'traffic_id':'reads','representation':{'input_bits':8,'stored_bits':8}}],
               'basis':'auditor-selected proposed fill window',
               'references':[{'file':'design.json','quote':'"id": "small"'}]}
        request['service_windows'].append(macro)
        prior=module.check(request,context['source_analysis'],context['document'],context['budget'],context['sources'],engine, source_declaration=context['baseline']['baseline']['analysis'], execution_dependencies=context['execution_dependencies'])
        self.assertTrue(prior['ok'],prior['errors']);prior['request_id']='earlier'
        macro['intervals'][0]['end']=120
        later=module.check(request,context['source_analysis'],context['document'],context['budget'],context['sources'],engine, source_declaration=context['baseline']['baseline']['analysis'], execution_dependencies=context['execution_dependencies'])
        self.assertTrue(later['ok'],later['errors']);later['request_id']='later'
        context['calculations']=[prior,later];context['proposal_checks']=[prior,later]
        report=report_fixture(context)
        self.assertTrue(any('counterexample requires explicit adjudication:' in e for e in validator.validate(report,context)['errors']))
        for receipt in prior['subreceipts']:
            for check in receipt['checks']:
                if check['status']=='fail':
                    key=receipt['id']+'::'+check['id']+'__actual'
                    report['checks'][0]['references'].append({'request_id':'earlier','calculation_id':key,'actual':prior['values'][key]})
        report['checks'][0]['evidence']='Earlier one-cycle audit window was a bad mapping; source and actual proposal allow the later complete service interval. Synthetic rebuttal test, not a semantic proof.'
        self.assertFalse(validator.validate(report,context)['ok'])
        report['counterexample_reviews']=[]
        for receipt in prior['subreceipts']:
            for check in receipt['checks']:
                if check['status']!='fail':continue
                corrected_receipt=next(child for child in later['subreceipts']
                                       if any(row['id']==check['id'] and row['status']=='pass' for row in child['checks']))
                failed_key=receipt['id']+'::'+check['id']+'__actual'
                closing_key=corrected_receipt['id']+'::'+check['id']+'__actual'
                report['counterexample_reviews'].append({
                    'request_id':'earlier','receipt_id':receipt['id'],'check_id':check['id'],
                    'disposition':'audit_declaration_error',
                    'reason':'The independent one-cycle fill interval was a bad audit declaration; the corrected complete 120-cycle interval supplies the same macro demand against the frozen proposal.',
                    'correction_check':{'request_id':'later','receipt_id':corrected_receipt['id'],'check_id':check['id']},
                    'references':[{'file':'spec.md','quote':'items 12'},
                                  {'file':'design.json','quote':'"id": "small"'},
                                  {'request_id':'earlier','calculation_id':failed_key,'actual':prior['values'][failed_key]},
                                  {'request_id':'later','calculation_id':closing_key,'actual':later['values'][closing_key]}]})
        result=validator.validate(report,context)
        self.assertTrue(result['ok'],result['errors'])


if __name__ == '__main__':unittest.main()
