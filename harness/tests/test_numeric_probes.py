"""Boundary behavior, including legal alternatives; no end-to-end accuracy claim."""
import copy
import unittest
from test_architecture import fixture
from harness.checks.architecture import _engine as engine, _audit_engine as audit
from harness.runtime.audit_session import parse_turn


def probe(**changes):
    row = {'id': 'boundary', 'operation': 'identity', 'input': '1/128',
           'format': {'bits': 8, 'fractional_bits': 4, 'signed': True},
           'rounding': 'nearest_even', 'overflow': 'saturate',
           'assertions': [], 'basis': 'virtual boundary, not a source reachability proof'}
    row.update(changes)
    return row


class NumericProbeTests(unittest.TestCase):
    def run_probe(self, row):
        result = engine.derive_numeric_probes([row], {})
        self.assertTrue(result['ok'], result['errors'])
        return result['probes'][0]

    def test_small_positive_and_epsilon_can_round_to_zero(self):
        for row in (probe(input='1/1000000'), probe(operation='sigmoid', input=-18)):
            row['assertions'] = [{'metric': 'quantized', 'relation': '>', 'expected': 0}]
            result = self.run_probe(row)
            self.assertGreater(result['results']['raw'], 0)
            self.assertEqual(result['results']['quantized'], 0)
            self.assertFalse(result['pass'])

    def test_more_fractional_bits_or_upward_rounding_are_legal_alternatives(self):
        for row in (probe(input='1/1000000', format={'bits': 32, 'fractional_bits': 24, 'signed': False}),
                    probe(input='1/1000000', rounding='ceil')):
            row['assertions'] = [{'metric': 'quantized', 'relation': '>', 'expected': 0}]
            self.assertTrue(self.run_probe(row)['pass'])

    def test_nearest_even_and_signed_ties(self):
        for value, expected in (('1/32', 0), ('3/32', 2), ('-1/32', 0), ('-3/32', -2)):
            self.assertEqual(self.run_probe(probe(input=value))['results']['integer'], expected)

    def test_rounding_modes(self):
        for mode, value in (('toward_zero', -1), ('floor', -2), ('ceil', -1)):
            self.assertEqual(self.run_probe(probe(input='-3/32', rounding=mode))['results']['integer'], value)

    def test_explicit_saturation_or_rejection(self):
        result = self.run_probe(probe(input=20))
        self.assertTrue(result['overflowed']); self.assertEqual(result['results']['integer'], 127)
        result = engine.derive_numeric_probes([probe(input=20, overflow='reject')], {})
        self.assertFalse(result['ok'])

    def test_domain_errors_and_unsupported_policy_fail_declaration(self):
        for row in (probe(operation='reciprocal', input=0), probe(operation='sqrt', input=-1),
                    probe(overflow='wrap'), probe(operation='custom'), probe(rounding='nearest')):
            self.assertFalse(engine.derive_numeric_probes([row], {})['ok'])

    def test_fixed_nonlinear_point_values(self):
        for operation, value, expected in (('sigmoid', 0, .5), ('exp', 0, 1),
                                           ('reciprocal', 2, .5), ('sqrt', 4, 2), ('rsqrt', 4, .5)):
            self.assertEqual(self.run_probe(probe(operation=operation, input=value))['results']['quantized'], expected)

    def test_wide_fraction_quantizes_the_computed_float_not_its_decimal_display(self):
        from decimal import Decimal, localcontext
        with localcontext() as context:
            context.prec = 180
            expected = int((Decimal.from_float(.1) * (Decimal(2) ** 100)).to_integral_value())
        result = self.run_probe(probe(input=.1, format={'bits': 128, 'fractional_bits': 100, 'signed': False}))
        self.assertEqual(result['results']['integer'], expected)

    def test_audit_assertion_does_not_overwrite_an_existing_comparison(self):
        row = probe(assertions=[{'metric': 'quantized', 'relation': '==', 'expected': 0}])
        request = {'tool': 'recompute', 'facts': [], 'assumptions': [],
                   'calculations': [{'id': 'boundary__assertion_0', 'value': 7, 'expected': 7,
                                     'unit': 'count', 'basis': 'existing independent comparison'}],
                   'numeric_probes': [row]}
        result = audit.recompute(request, {}, engine)
        self.assertFalse(result['ok']); self.assertEqual(result['comparisons']['boundary__assertion_0']['actual'], 7)

    def test_names_and_inputs_not_mutated(self):
        row = probe(input='small'); names = {'small': .25}; before = copy.deepcopy((row, names))
        self.assertTrue(engine.derive_numeric_probes([row], names)['ok']); self.assertEqual((row, names), before)
        self.assertFalse(engine.derive_numeric_probes([row, row], names)['ok'])

    def test_design_assertion_blocks_gate_but_informative_probe_stays_unchecked(self):
        d, sources = fixture(); row = probe()
        d['candidates'][0]['numeric_probes'] = [row]
        informative = engine.evaluate(d, sources)
        self.assertTrue(informative['ok'])
        self.assertEqual(informative['candidates'][d['selected']]['numeric_probes']['coverage_status'], 'computed_only')
        self.assertIsNone(informative['candidates'][d['selected']]['numeric_probes']['probes'][0]['pass'])
        row['assertions'] = [{'metric': 'quantized', 'relation': '>', 'expected': 0}]
        result = engine.evaluate(d, sources)
        self.assertFalse(result['ok']); self.assertEqual(result['gate']['status'], 'fail')
        self.assertTrue(any('declared assertion failed' in error for error in result['errors']))

    def test_independent_audit_numeric_only_request_is_executed_not_rejected(self):
        row = probe(); row['assertions'] = [{'metric': 'quantized', 'relation': '>', 'expected': 0}]
        request = {'tool': 'recompute', 'facts': [], 'assumptions': [], 'calculations': [], 'numeric_probes': [row]}
        import json
        self.assertEqual(parse_turn('<<<AUDIT_TOOL_REQUEST>>>\n' + json.dumps(request)), {'tool_request': request})
        result = audit.recompute(request, {}, engine)
        self.assertTrue(result['ok'], result['errors'])
        self.assertFalse(result['comparisons']['boundary__assertion_0']['matches_expected'])
        self.assertEqual(result['values']['boundary__quantized'], 0)
        self.assertIn('boundary__raw', result['calculation_ids'])

    def test_denominator_probe_consumes_actual_previous_rounded_value(self):
        constant = probe(id='tiny', input='1/1000000',
                         assertions=[{'metric': 'quantized', 'relation': '>=', 'expected': 0}])
        guarded = probe(id='guarded', operation='reciprocal', input='max(tiny__quantized,1/16)',
                        format={'bits': 16, 'fractional_bits': 8, 'signed': True},
                        assertions=[{'metric': 'quantized', 'relation': '==', 'expected': 16}])
        result = engine.derive_numeric_probes([constant, guarded], {})
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['coverage_status'], 'checked')
        self.assertEqual(result['values']['tiny__quantized'], 0)
        self.assertEqual(result['values']['guarded__raw'], 16)
        unguarded = copy.deepcopy(guarded); unguarded['input'] = 'tiny__quantized'
        result = engine.derive_numeric_probes([constant, unguarded], {})
        self.assertFalse(result['ok'])
        self.assertEqual(result['coverage_status'], 'invalid')
        self.assertTrue(any('zero' in error for error in result['errors']))

    def test_probe_forward_or_cyclic_references_are_not_accepted(self):
        first = probe(id='first', input='second__quantized')
        second = probe(id='second', input='first__quantized')
        result = engine.derive_numeric_probes([first, second], {})
        self.assertFalse(result['ok'])
        self.assertEqual(result['values'], {})
        self.assertTrue(all('undefined identifier' in error for error in result['errors']))

    def test_no_forged_result_or_assertion_metric(self):
        row = probe(); row['result'] = 1
        self.assertFalse(engine.derive_numeric_probes([row], {})['ok'])
        row = probe(assertions=[{'metric': 'invented', 'relation': '==', 'expected': 0}])
        self.assertFalse(engine.derive_numeric_probes([row], {})['ok'])


if __name__ == '__main__': unittest.main()
