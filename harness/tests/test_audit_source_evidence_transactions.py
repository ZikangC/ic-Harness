"""Actual source transaction views, not invented successful JSON fixtures."""
import copy
import importlib.util
from pathlib import Path
import unittest
from test_audit_source_model import witness, source, engine

path=Path(__file__).resolve().parents[1]/'skills/microarchitecture-audit/scripts/validate_review.py'
spec=importlib.util.spec_from_file_location('source_evidence_transactions',path)
validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)


class SourceEvidenceTransactions(unittest.TestCase):
    def actual(self,broken=False):
        model,sources=witness()
        if broken:model['states'][0]['element_bits']=32
        result=source.analyze(model,sources,engine)
        dependencies={key:result['binding'][key] for key in ('checker_bundle_sha256','calculator_bundle_sha256')}
        return result,sources,model,dependencies

    def test_complete_actual_source_view_matches_receipts(self):
        args=self.actual();selected=validator.executed_source_values(*args)
        self.assertTrue(selected['coverage_complete'],selected['errors'])
        self.assertEqual(selected['verified_data']['flows'],args[0]['flows'])
        self.assertEqual(selected['values'],args[0]['values'])

    def test_bad_state_does_not_erase_verified_flow_but_never_completes(self):
        args=self.actual(True);selected=validator.executed_source_values(*args)
        self.assertFalse(args[0]['ok']);self.assertFalse(selected['coverage_complete'])
        self.assertEqual(selected['errors'],[])
        self.assertEqual(selected['verified_data']['flows']['out']['totals']['p']['bytes'],80)
        self.assertTrue(any(row['status']=='declaration_failed' for row in selected['applicability']))

    def test_different_original_source_or_declaration_rejects_reuse(self):
        for index in (1,2):
            args=list(self.actual());args[index]=copy.deepcopy(args[index])
            if index==1:args[index]['spec.md']+='\nchanged'
            else:args[index]['limits']='different interpretation'
            selected=validator.executed_source_values(*args)
            self.assertFalse(selected['values']);self.assertTrue(selected['errors'])

    def test_changed_calculator_closure_rejects_values(self):
        args=self.actual();args[3]['calculator_bundle_sha256']='other'
        selected=validator.executed_source_values(*args)
        self.assertFalse(selected['values']);self.assertTrue(selected['errors'])

    def test_tampered_parent_flow_cannot_sneak_into_verified_view(self):
        args=self.actual(True);args[0]['flows']['out']['totals']['p']['bytes']=1
        selected=validator.executed_source_values(*args)
        self.assertNotIn('out',selected['verified_data']['flows']);self.assertTrue(selected['errors'])
        self.assertTrue(selected['verified_data']['computations'])

    def test_unbound_added_parent_profile_or_number_blocks_complete_coverage(self):
        args=self.actual();args[0]['workloads']['invented']=1
        selected=validator.executed_source_values(*args)
        self.assertNotIn('invented',selected['verified_data']['workloads'])
        self.assertFalse(selected['coverage_complete']);self.assertTrue(selected['errors'])

    def test_tampered_child_rejects_only_that_child_and_dependents(self):
        args=self.actual(True)
        child=next(row for row in args[0]['subreceipts'] if row['section']=='fact' and row['status']=='executed')
        child['output']['data']['value']+=1
        selected=validator.executed_source_values(*args)
        self.assertTrue(selected['errors']);self.assertFalse(selected['coverage_complete'])
        self.assertNotIn(child['output']['key'],selected['verified_data']['fact_values'])

    def test_malformed_parent_applicability_or_dependency_types_are_diagnosed(self):
        for kind in ('parent','applicability','dependencies'):
            args=list(self.actual(True))
            if kind=='parent':args[0]['fact_values']=[]
            elif kind=='dependencies':args[3]=['not-an-object']
            else:
                child=args[0]['subreceipts'][0];child['applicability']=[]
                child['receipt_sha256']=validator.fingerprint({key:value for key,value in child.items() if key!='receipt_sha256'})
            selected=validator.executed_source_values(*args)
            self.assertFalse(selected['coverage_complete']);self.assertTrue(selected['errors'])

    def test_deleted_failed_declaration_cannot_forge_complete_coverage(self):
        args=self.actual(True)
        args[0]['subreceipts']=[row for row in args[0]['subreceipts'] if row['status']=='executed']
        args[0].update(ok=True,coverage_complete=True,errors=[],missing_declarations=[])
        selected=validator.executed_source_values(*args)
        self.assertFalse(selected['coverage_complete'])
        self.assertTrue(selected['missing_output_declarations']);self.assertTrue(selected['errors'])


if __name__=='__main__':unittest.main()
