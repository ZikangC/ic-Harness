"""Explicit unit conversion and raw engineering-formula scale consistency."""
import copy
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'library/microarchitecture-design/scripts/budget_units.py'
if not SCRIPT.is_file():
    SCRIPT = ROOT / 'harness/skills/microarchitecture-design/scripts/budget_units.py'
spec = importlib.util.spec_from_file_location('quantity_conversion_units', SCRIPT)
units = importlib.util.module_from_spec(spec)
spec.loader.exec_module(units)


def literal(identifier, value, unit, quote=None, **updates):
    row = {'id': identifier, 'value': value, 'unit': unit,
           'citations': [{'file': 'spec.md', 'quote': str(value) + ' ' + unit if quote is None else quote}]}
    row.update(updates)
    return row


def conversion(identifier, source, target, **updates):
    row = {'id': identifier, 'convert': {'fact': source, 'to_unit': target}}
    row.update(updates)
    return row


class QuantityConversionTests(unittest.TestCase):
    def test_frequency_conversion_is_program_owned_and_inputs_unchanged(self):
        facts = [literal('frequency', 37, 'MHz')]
        values = {'frequency': 37}
        row = conversion('frequency_hz', 'frequency', 'Hz')
        before = copy.deepcopy((facts, values, row))
        result = units.convert_fact(row, facts, values)
        self.assertEqual(result['value'], 37000000)
        self.assertEqual(result['unit'], 'Hz')
        self.assertEqual(result['source_fact'], 'frequency')
        self.assertEqual((facts, values, row), before)

    def test_percentage_keeps_source_number_then_converts(self):
        facts = [literal('threshold_percent', 97.5, '%')]
        result = units.convert_fact(conversion('threshold', 'threshold_percent', '1'), facts,
                                    {'threshold_percent': 97.5})
        self.assertAlmostEqual(result['value'], 0.975)
        self.assertEqual(units.unit('%'), ({}, 0.01))
        reverse = units.convert_fact(conversion('percent_again', 'ratio', '%'),
                                     [literal('ratio', 0.975, '1')], {'ratio': 0.975})
        self.assertAlmostEqual(reverse['value'], 97.5)

    def test_bit_byte_and_area_conversion(self):
        cases = [('payload', 24, 'bits', 'bytes', 3),
                 ('capacity', 3, 'kB', 'bits', 24000),
                 ('area', 2, 'mm²', 'um^2', 2000000),
                 ('delay', 4, 'ns', 's', 4e-9)]
        for identifier, value, source, target, expected in cases:
            with self.subTest(source=source, target=target):
                result = units.convert_fact(conversion('converted', identifier, target),
                                            [literal(identifier, value, source)], {identifier: value})
                self.assertAlmostEqual(result['value'], expected)

    def test_ordinary_capacity_prefix_defaults_to_decimal(self):
        source = literal('capacity', 3, 'MB')
        result = units.convert_fact(conversion('capacity_bits', 'capacity', 'bits'),
                                    [source], {'capacity': 3})
        self.assertEqual(result['value'], 24000000)
        self.assertEqual(result['source_prefix_base'], 'decimal')
        self.assertIsNone(result['prefix_context'])

    def test_intrinsic_binary_does_not_redefine_plain_mb(self):
        source = literal('capacity', 1, 'MiB')
        result = units.convert_fact(conversion('capacity_mb', 'capacity', 'MB'),
                                    [source], {'capacity': 1})
        self.assertAlmostEqual(result['value'], 1.048576)
        self.assertEqual(result['source_prefix_base'], 'binary')
        self.assertEqual(result['prefix_base'], 'decimal')

    def test_decimal_source_to_intrinsic_binary_target_is_valid(self):
        source = literal('capacity', 2, 'MB', 'decimal capacity 2 MB')
        result = units.convert_fact(conversion('capacity_mib', 'capacity', 'MiB'),
                                    [source], {'capacity': 2})
        self.assertAlmostEqual(result['value'], 2000000 / 1048576)
        self.assertEqual(result['prefix_base'], 'binary')
        self.assertEqual(result['prefix_context'], 'decimal')

    def test_explicit_binary_density_context_is_inherited(self):
        source = literal('density', 3, 'Mbit/mm²', 'binary density 3 Mbit/mm²')
        result = units.convert_fact(conversion('density_bits', 'density', 'bits/mm^2'),
                                    [source], {'density': 3})
        self.assertEqual(result['value'], 3 * 2**20)
        self.assertEqual(result['prefix_base'], 'binary')
        self.assertEqual(result['prefix_context'], 'binary')
        self.assertEqual(units.fact_dimension_errors([source]), [])

    def test_chained_conversion_retains_source_context(self):
        source = literal('density', 3, 'Mbit/mm²', 'binary density 3 Mbit/mm²')
        first = conversion('density_bits', 'density', 'bits/mm²')
        first_result = units.convert_fact(first, [source], {'density': 3})
        resolved = {**first, **{k: first_result[k] for k in ('value', 'unit', 'prefix_base')}}
        result = units.convert_fact(conversion('density_mbit', 'density_bits', 'Mbit/mm²'),
                                    [source, resolved], {'density': 3, 'density_bits': first_result['value']})
        self.assertEqual(result['value'], 3)
        self.assertEqual(result['prefix_context'], 'binary')

    def test_canonical_decimal_to_intrinsic_binary_then_back_tracks_ancestry(self):
        source = literal('capacity', 1, 'MB', 'decimal capacity 1 MB')
        first = conversion('capacity_mib', 'capacity', 'MiB')
        value = units.convert_fact(first, [source], {'capacity': 1})
        canonical = {'id': 'capacity_mib', 'value': value['value'], 'unit': value['unit'],
                     'prefix_base': value['prefix_base'], 'converted_from': first['convert'],
                     'citations': source['citations']}
        self.assertEqual(units.fact_dimension_errors([source, canonical]), [])
        second = units.convert_fact(conversion('capacity_mb_again', 'capacity_mib', 'MB'),
                                     [source, canonical], {'capacity': 1, 'capacity_mib': value['value']})
        self.assertEqual(second['value'], 1)
        self.assertEqual(second['prefix_base'], 'decimal')
        self.assertEqual(second['prefix_context'], 'decimal')

    def test_canonical_binary_density_chain_preserves_explicit_context(self):
        source = literal('density', 3, 'Mbit/mm²', 'binary density 3 Mbit/mm²')
        first = conversion('density_bits', 'density', 'bits/mm²')
        value = units.convert_fact(first, [source], {'density': 3})
        canonical = {'id': 'density_bits', 'value': value['value'], 'unit': value['unit'],
                     'prefix_base': value['prefix_base'], 'converted_from': first['convert'],
                     'citations': source['citations']}
        self.assertEqual(units.fact_dimension_errors([source, canonical]), [])
        second = units.convert_fact(conversion('density_again', 'density_bits', 'Mbit/mm²'),
                                     [source, canonical], {'density': 3, 'density_bits': value['value']})
        self.assertEqual(second['value'], 3)
        self.assertEqual(second['prefix_context'], 'binary')

    def test_materialized_conversion_cannot_masquerade_as_raw_density(self):
        source = literal('density', 2, 'Mbit/mm²', 'binary density 2 Mbit/mm² (2097152 bit/mm²)')
        first = conversion('density_bits', 'density', 'bit/mm²')
        value = units.convert_fact(first, [source], {'density': 2})
        canonical = {'id': 'density_bits', 'value': value['value'], 'unit': value['unit'],
                     'prefix_base': value['prefix_base'], 'converted_from': first['convert'],
                     'citations': source['citations']}
        requirements = {'facts': [source, canonical],
                        'storage_area_model': {'status': 'supplied', 'density_fact': 'density_bits'}}
        with self.assertRaisesRegex(ValueError, 'retained cited literal'):
            units.macro_density(requirements, {'density': 2, 'density_bits': value['value']})

    def test_conversion_quote_cannot_invent_binary_source_context(self):
        source = literal('capacity', 2, 'MB')
        row = conversion('capacity_bits', 'capacity', 'bits',
                         citations=[{'file': 'spec.md', 'quote': 'unrelated binary datum'}])
        result = units.convert_fact(row, [source], {'capacity': 2})
        self.assertEqual(result['value'], 16000000)
        self.assertIsNone(result['prefix_context'])

    def test_macro_density_and_conversion_use_the_same_source_basis(self):
        for spelling in ('Mbit/mm²', 'kiB/mm^2'):
            with self.subTest(spelling=spelling):
                source = literal('density', 3, spelling, 'binary density 3 ' + spelling)
                requirements = {'facts': [source], 'storage_area_model': {'status': 'supplied', 'density_fact': 'density'}}
                density = units.macro_density(requirements, {'density': 3})
                converted = units.convert_fact(conversion('density_bits', 'density', 'bits/mm²'), [source], {'density': 3})
                self.assertEqual(density['bits_per_mm2'], converted['value'])
                self.assertEqual(density['prefix_base'], converted['source_prefix_base'])

    def test_intrinsic_binary_lower_k_has_consistent_basis(self):
        source = literal('capacity', 2, 'kiB')
        result = units.convert_fact(conversion('capacity_bytes', 'capacity', 'B'), [source], {'capacity': 2})
        self.assertEqual(result['value'], 2048)
        self.assertEqual(result['source_prefix_base'], 'binary')

    def test_ambiguous_or_fabricated_basis_rejected(self):
        for source in (literal('capacity', 2, 'MB', 'binary and decimal capacity 2 MB'),
                       literal('capacity', 2, 'MiB', 'decimal capacity 2 MiB'),
                       literal('capacity', 2, 'MB', prefix_base='binary')):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    units.convert_fact(conversion('result', 'capacity', 'bits'), [source], {'capacity': 2})

    def test_new_row_cannot_supply_converted_value_unit_or_basis(self):
        source = literal('frequency', 2, 'MHz')
        for extra in ({'value': 2000000}, {'formula': 'frequency*1000000'},
                      {'unit': 'Hz'}, {'prefix_base': 'binary'}):
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):
                    units.convert_fact(conversion('result', 'frequency', 'Hz', **extra), [source], {'frequency': 2})

    def test_cross_dimension_opaque_and_semantic_renaming_rejected(self):
        for source, target in [('bytes', 'tokens'), ('MHz', 'cycles'), ('count', 'cycles'),
                               ('cycles', 'count'), ('count', 'tokens'), ('MAC', 'ops')]:
            with self.subTest(source=source, target=target):
                with self.assertRaises(ValueError):
                    units.convert_fact(conversion('result', 'quantity', target),
                                        [literal('quantity', 2, source)], {'quantity': 2})
        result = units.convert_fact(conversion('result', 'quantity', 'items'),
                                    [literal('quantity', 2, 'tokens')], {'quantity': 2})
        self.assertEqual(result['value'], 2)

    def test_invalid_missing_self_or_nonfinite_source_rejected(self):
        cases = [(conversion('result', 'missing', 'Hz'), [literal('frequency', 2, 'MHz')], {'frequency': 2}),
                 (conversion('frequency', 'frequency', 'Hz'), [literal('frequency', 2, 'MHz')], {'frequency': 2}),
                 (conversion('result', 'frequency', 'Hz'), [literal('frequency', 2, 'MHz')], {}),
                 (conversion('result', 'frequency', 'Hz'), [literal('frequency', 2, 'MHz')], {'frequency': float('inf')})]
        for row, facts, values in cases:
            with self.assertRaises(ValueError):
                units.convert_fact(row, facts, values)

    def test_same_dimension_correct_scale_formula_source_can_be_converted(self):
        source = literal('clock', 7, 'Hz')
        derived = {'id': 'fast_clock', 'formula': 'clock*2', 'unit': 'Hz', 'citations': source['citations']}
        self.assertEqual(units.fact_dimension_errors([source, derived]), [])
        result = units.convert_fact(conversion('fast_khz', 'fast_clock', 'kHz'),
                                    [source, derived], {'clock': 7, 'fast_clock': 14})
        self.assertEqual(result['value'], 0.014)

    def test_conversion_api_can_copy_no_citations_but_does_not_create_them(self):
        row = conversion('result', 'frequency', 'Hz')
        units.convert_fact(row, [literal('frequency', 2, 'MHz')], {'frequency': 2})
        self.assertNotIn('citations', row)
        with self.assertRaises(ValueError):
            units.convert_fact(conversion('result', 'frequency', 'Hz', citations=[]),
                                [literal('frequency', 2, 'MHz')], {'frequency': 2})


class FormulaScaleTests(unittest.TestCase):
    def test_prefix_conversion_through_formula_is_rejected(self):
        source = literal('clock', 37, 'MHz')
        for formula in ('clock', 'clock*1000000', 'clock*1000'):
            with self.subTest(formula=formula):
                errors = units.fact_dimension_errors([source, {'id': 'hz', 'formula': formula, 'unit': 'Hz'}])
                self.assertTrue(any('unit scale mismatch' in e for e in errors), errors)

    def test_mixed_scale_add_sub_min_max_and_mod_rejected(self):
        facts = [literal('a', 1, 'ms'), literal('b', 1, 'us')]
        for formula in ('a+b', 'a-b', 'min(a,b)', 'max(a,b)', 'a%b'):
            with self.subTest(formula=formula):
                errors = units.fact_dimension_errors(facts + [{'id': 'result', 'formula': formula, 'unit': 'ms'}])
                self.assertTrue(any('incompatible unit scales' in e for e in errors), errors)

    def test_after_explicit_conversion_same_scale_arithmetic_is_valid(self):
        facts = [literal('a', 1, 'ms'), literal('b', 1, 'us'), conversion('b_ms', 'b', 'ms')]
        facts.append({'id': 'result', 'formula': 'a+b_ms', 'unit': 'ms'})
        self.assertEqual(units.fact_dimension_errors(facts), [])

    def test_hz_tokens_per_second_and_token_quantity_give_cycles(self):
        facts = [literal('hz', 37000000, 'Hz'), literal('rate', 18500, 'tokens/s'),
                 literal('quantity', 17, 'tokens'),
                 {'id': 'cpt', 'formula': 'hz/rate', 'unit': 'cycles/token'},
                 {'id': 'task_cycles', 'formula': 'quantity*cpt', 'unit': 'cycles'}]
        self.assertEqual(units.fact_dimension_errors(facts), [])
        facts[2]['unit'] = 'count'
        self.assertTrue(any('dimension mismatch' in e for e in units.fact_dimension_errors(facts)))

    def test_multiplication_division_and_unary_integer_power_scales(self):
        facts = [literal('frequency', 37, 'Hz'),
                 {'id': 'period', 'formula': 'frequency**-1', 'unit': 's'}]
        self.assertEqual(units.fact_dimension_errors(facts), [])
        facts[0]['unit'] = 'MHz'
        self.assertTrue(any('unit scale mismatch' in e for e in units.fact_dimension_errors(facts)))
        facts = [literal('a', 1, 'ms'), literal('b', 1, 's'),
                 {'id': 'ratio', 'formula': 'a/b', 'unit': '1'}]
        self.assertTrue(any('unit scale mismatch' in e for e in units.fact_dimension_errors(facts)))

    def test_dimensional_errors_remain_independent_of_scale_errors(self):
        facts = [literal('frequency', 37, 'Hz'),
                 {'id': 'wrong', 'formula': 'frequency*frequency', 'unit': 'Hz'}]
        self.assertTrue(any('dimension mismatch' in e for e in units.fact_dimension_errors(facts)))

    def test_percent_requires_explicit_conversion_before_ratio_formula(self):
        facts = [literal('threshold', 97, '%'), literal('ratio', 0.3, '1')]
        self.assertTrue(any('incompatible unit scales' in e for e in units.fact_dimension_errors(
            facts + [{'id': 'sum', 'formula': 'threshold+ratio', 'unit': '1'}])))
        facts.append(conversion('threshold_ratio', 'threshold', '1'))
        self.assertEqual(units.fact_dimension_errors(
            facts + [{'id': 'sum', 'formula': 'threshold_ratio+ratio', 'unit': '1'}]), [])


if __name__ == '__main__':
    unittest.main()
