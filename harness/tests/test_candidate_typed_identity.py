"""Typed JSON repairs must reach the frozen calculator without host rewriting."""
import copy
import json
import unittest
from unittest.mock import Mock
import test_architecture as base
from harness.runtime import design_session as design


def replacement(document, candidate):
    return {'tool': 'replace_candidates', 'base_design_sha256': design.identity(document),
            'candidates': [copy.deepcopy(candidate)]}


class CandidateTypedIdentityTests(unittest.TestCase):
    def test_numeric_signed_flag_can_be_repaired_to_boolean_and_recalculated(self):
        document, sources = base.fixture()
        document['candidates'][0]['numeric_probes'][0]['format']['signed'] = 1
        before_document = json.dumps(document, sort_keys=True)
        before_sources = copy.deepcopy(sources)
        repaired_candidate = copy.deepcopy(document['candidates'][0])
        repaired_candidate['numeric_probes'][0]['format']['signed'] = True
        request = replacement(document, repaired_candidate)
        before_request = json.dumps(request, sort_keys=True)
        changed = design.replace_candidates(document, request)
        # This is exactly the equality trap that previously discarded the repair.
        self.assertEqual(changed, document)
        self.assertNotEqual(design.identity(changed), design.identity(document))
        self.assertIs(changed['candidates'][0]['numeric_probes'][0]['format']['signed'], True)
        expected = copy.deepcopy(document)
        expected['candidates'][0]['numeric_probes'][0]['format']['signed'] = True
        self.assertEqual(json.dumps(changed, sort_keys=True), json.dumps(expected, sort_keys=True))
        self.assertEqual(json.dumps(document, sort_keys=True), before_document)
        self.assertEqual(json.dumps(request, sort_keys=True), before_request)
        self.assertEqual(sources, before_sources)
        out = base.SkillToolTests().setup_run()
        invalid = design.execute_tool(out, 'typed-invalid', document, sources)
        self.assertFalse(invalid['ok'])
        self.assertTrue(any('boolean signed' in error for error in invalid['errors']))
        valid = design.execute_tool(out, 'typed-fixed', changed, sources)
        self.assertTrue(valid['ok'], valid['errors'])
        self.assertEqual(valid['candidates']['small']['numeric_probes']['coverage_status'], 'checked')

    def test_real_noop_is_still_rejected_even_with_different_key_order(self):
        document, _ = base.fixture()
        reordered = dict(reversed(list(document['candidates'][0].items())))
        with self.assertRaisesRegex(ValueError, 'does not change evaluated data'):
            design.replace_candidates(document, replacement(document, reordered))

    def test_reverse_type_change_is_not_silently_normalized_or_accepted_by_calculator(self):
        document, sources = base.fixture()
        invalid_candidate = copy.deepcopy(document['candidates'][0])
        invalid_candidate['numeric_probes'][0]['format']['signed'] = 1
        changed = design.replace_candidates(document, replacement(document, invalid_candidate))
        self.assertIs(type(changed['candidates'][0]['numeric_probes'][0]['format']['signed']), int)
        out = base.SkillToolTests().setup_run()
        report = design.execute_tool(out, 'typed-regression', changed, sources)
        self.assertFalse(report['ok'])
        self.assertEqual(report['gate']['status'], 'fail')

    def test_generate_recalculates_type_only_repair_instead_of_spending_noop_retries(self):
        document, sources = base.fixture()
        document['candidates'][0]['numeric_probes'][0]['format']['signed'] = 1
        candidate = copy.deepcopy(document['candidates'][0])
        candidate['numeric_probes'][0]['format']['signed'] = True
        request = replacement(document, candidate)
        repaired = copy.deepcopy(document); repaired['candidates'][0] = candidate
        out = base.SkillToolTests().setup_run()
        invoke = Mock(side_effect=[
            {'tool_request': {'tool': 'evaluate_design', 'document': copy.deepcopy(document)}},
            {'tool_request': request},
            {'final': {'evaluated_design_sha256': design.identity(repaired), 'architecture_md': document['architecture_md']}}])
        result = design.generate(out, 'typed-repair', 'PROMPT', document['requirements'], sources, invoke, 2)
        self.assertEqual(invoke.call_count, 3)
        first = json.loads((out / 'typed-repair.step-0.tool.result.json').read_text())
        second = json.loads((out / 'typed-repair.step-1.tool.result.json').read_text())
        self.assertFalse(first['ok'])
        self.assertTrue(second['ok'], second['errors'])
        self.assertNotEqual(first['design_sha256'], second['design_sha256'])
        self.assertEqual(design.identity(result), second['design_sha256'])
        self.assertIs(result['candidates'][0]['numeric_probes'][0]['format']['signed'], True)


if __name__ == '__main__': unittest.main()
