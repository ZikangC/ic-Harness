"""Output encoding preserves actual integer-code semantics at risky boundaries."""
import copy
import unittest
from harness.checks.architecture import _engine as engine, _audit_engine as audit


def encoded(**changes):
    row = {'id': 'output_boundary', 'operation': 'encode_row',
           'input': ['1/8', '1/4', '3/8'], 'scale': 8,
           'format': {'bits': 16, 'fractional_bits': 0, 'signed': True},
           'rounding': 'nearest_even', 'overflow': 'saturate',
           'assertions': [{'metric': 'argmax_match', 'relation': '==', 'expected': 1},
                          {'metric': 'cosine', 'relation': '>=', 'expected': '.999999'}],
           'basis': 'synthetic retained-fraction output conversion; no real input reachability claim'}
    row.update(changes)
    return row


class NumericRowEncodingTests(unittest.TestCase):
    def run_row(self, row):
        result = engine.derive_numeric_probes([row], {})
        self.assertTrue(result['ok'], result['errors'])
        return result['probes'][0]

    def test_empty_collection_does_not_report_numerical_pass(self):
        result = engine.derive_numeric_probes([], {})
        self.assertTrue(result['ok'])
        self.assertEqual(result['coverage_status'], 'not_checked')
        self.assertEqual(result['assertion_count'], 0)
        self.assertEqual(result['checked_probe_count'], 0)

    def test_empty_assertions_compute_but_do_not_check(self):
        result = engine.derive_numeric_probes([encoded(assertions=[])], {})
        self.assertTrue(result['ok'])
        self.assertEqual(result['coverage_status'], 'computed_only')
        self.assertIsNone(result['probes'][0]['pass'])
        self.assertEqual(result['informative_probe_count'], 1)

    def test_mixed_collection_does_not_hide_an_informative_probe(self):
        informative = encoded(id='unasserted', assertions=[])
        result = engine.derive_numeric_probes([encoded(), informative], {})
        self.assertEqual(result['coverage_status'], 'computed_only')
        self.assertEqual(result['checked_probe_count'], 1)
        self.assertEqual(result['informative_probe_count'], 1)

    def test_fraction_retaining_positive_scale_checks_actual_codes(self):
        result = engine.derive_numeric_probes([encoded()], {})
        row = result['probes'][0]
        self.assertEqual(result['coverage_status'], 'checked')
        self.assertTrue(row['pass'])
        self.assertEqual(row['encoding']['codes'], [1, 2, 3])
        self.assertEqual(row['encoding']['decoded_values'], [.125, .25, .375])
        self.assertAlmostEqual(row['results']['cosine'], 1)
        self.assertEqual(row['results']['encoded_argmax'], 2)
        self.assertEqual(result['values']['output_boundary__encoded_argmax'], 2)
        self.assertTrue(all(type(value) in (int, float) for value in result['values'].values()))

    def test_coarse_row_causes_new_ties_and_changes_smallest_index_argmax(self):
        result = engine.derive_numeric_probes([encoded(scale=1)], {})
        row = result['probes'][0]
        self.assertTrue(result['ok'])
        self.assertEqual(result['coverage_status'], 'failed')
        self.assertFalse(row['pass'])
        self.assertEqual(row['encoding']['codes'], [0, 0, 0])
        self.assertEqual(row['results']['reference_argmax'], 2)
        self.assertEqual(row['results']['encoded_argmax'], 0)
        self.assertEqual(row['results']['argmax_match'], 0)
        self.assertEqual(row['results']['introduced_tie_pairs'], 3)
        self.assertEqual(row['results']['encoded_max_ties'], 3)
        self.assertEqual(row['results']['zero_count'], 3)
        self.assertIsNone(row['results']['cosine'])
        self.assertNotIn('output_boundary__cosine', result['values'])
        self.assertFalse(row['assertions'][1]['pass'])

    def test_original_ties_use_actual_codes_smallest_index_without_new_tie_claim(self):
        row = self.run_row(encoded(input=[2, 3, 3], scale=2))
        self.assertTrue(row['pass'])
        self.assertEqual(row['results']['encoded_argmax'], 1)
        self.assertEqual(row['results']['encoded_max_ties'], 2)
        self.assertEqual(row['results']['introduced_tie_pairs'], 0)

    def test_explicit_negative_half_step_policies_are_distinct(self):
        expected = {'nearest_even': [-2, 0, 0, 2],
                    'nearest_ties_positive': [-1, 0, 1, 2],
                    'half_away': [-2, -1, 1, 2]}
        for policy, codes in expected.items():
            row = self.run_row(encoded(input=['-3/4', '-1/4', '1/4', '3/4'], scale=2,
                                       rounding=policy, assertions=[]))
            self.assertEqual(row['encoding']['codes'], codes)

    def test_saturation_is_explicit_and_can_fail_an_assertion(self):
        row = self.run_row(encoded(input=[-10, 1, 2, 10], scale=1,
                                  format={'bits': 3, 'fractional_bits': 0, 'signed': True},
                                  assertions=[{'metric': 'saturation_count', 'relation': '==', 'expected': 0}]))
        self.assertEqual(row['encoding']['codes'], [-4, 1, 2, 3])
        self.assertEqual(row['results']['saturation_count'], 2)
        self.assertTrue(row['overflowed'])
        self.assertFalse(row['pass'])

    def test_zero_reference_and_zero_codes_do_not_invent_cosine(self):
        row = self.run_row(encoded(input=[0, 0], assertions=[{'metric': 'cosine_defined', 'relation': '==', 'expected': 1}]))
        self.assertIsNone(row['results']['cosine'])
        self.assertEqual(row['results']['argmax_match'], 1)
        self.assertFalse(row['pass'])

    def test_reject_policy_and_malformed_row_declarations(self):
        invalid = [encoded(scale=0), encoded(scale=-1), encoded(input=[]), encoded(input=4),
                   encoded(scale=[1, 2]), encoded(format={'bits': 8, 'fractional_bits': 1, 'signed': True}),
                   encoded(format={'bits': 8, 'fractional_bits': 0, 'signed': False}),
                   encoded(input=[1000], format={'bits': 8, 'fractional_bits': 0, 'signed': True}, overflow='reject'),
                   encoded(rounding='half_up'), encoded(overflow='wrap'), encoded(algorithm='invented')]
        for row in invalid:
            result = engine.derive_numeric_probes([row], {})
            self.assertFalse(result['ok'], row)
            self.assertEqual(result['coverage_status'], 'invalid')

    def test_row_input_names_and_scale_are_not_mutated_or_rebound(self):
        names = {'factor': 16, 'x': .25}
        row = encoded(input=['x', 'x*2'], scale='factor')
        before = copy.deepcopy((row, names))
        result = engine.derive_numeric_probes([row], names)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['probes'][0]['encoding']['codes'], [4, 8])
        self.assertEqual((row, names), before)
        names['output_boundary__encoded_argmax'] = 12
        self.assertFalse(engine.derive_numeric_probes([row], names)['ok'])

    def test_independent_audit_consumes_actual_row_codes_and_failed_undefined_cosine(self):
        request = {'tool': 'recompute', 'facts': [], 'assumptions': [], 'calculations': [],
                   'numeric_probes': [encoded(scale=1)]}
        result = audit.recompute(request, {}, engine)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['values']['output_boundary__encoded_argmax'], 0)
        self.assertIn('output_boundary__encoded_argmax', result['calculation_ids'])
        self.assertFalse(result['comparisons']['output_boundary__assertion_0']['matches_expected'])
        cosine = result['comparisons']['output_boundary__assertion_1']
        self.assertIsNone(cosine['actual'])
        self.assertFalse(cosine['matches_expected'])
        self.assertEqual(result['numeric_probes']['coverage_status'], 'failed')

    def test_failed_assertion_and_invalid_declaration_are_distinct(self):
        failed = engine.derive_numeric_probes([encoded(scale=1)], {})
        invalid = engine.derive_numeric_probes([encoded(scale=0)], {})
        self.assertTrue(failed['ok'])
        self.assertEqual(failed['coverage_status'], 'failed')
        self.assertFalse(invalid['ok'])
        self.assertEqual(invalid['coverage_status'], 'invalid')


if __name__ == '__main__': unittest.main()
