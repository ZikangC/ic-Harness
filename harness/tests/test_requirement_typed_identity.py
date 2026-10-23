"""Typed selector repairs retain their source binding and whole-fact boundaries."""
import copy
import json
import unittest
from unittest.mock import Mock
import test_requirement_session as base
from harness.runtime import requirement_session as session


class RequirementTypedIdentityTests(unittest.TestCase):
    def test_boolean_selector_repairs_reach_the_real_source_binder(self):
        for field, fact_id in (('line', 'upper_bound'), ('number', 'clock')):
            with self.subTest(field=field):
                raw, sources = base.selector_fixture()
                fact = next(row for row in raw['facts'] if row['id'] == fact_id)
                self.assertEqual(fact['source'][field], 1)
                fact['source'][field] = True
                fixed = copy.deepcopy(fact); fixed['source'][field] = 1
                before = json.dumps(raw, sort_keys=True)
                source_before = copy.deepcopy(sources)
                request = {'base_sha256': session.identity(raw), 'facts': [fixed]}
                request_before = json.dumps(request, sort_keys=True)
                repaired = session.repair_requirements(raw, request)
                self.assertEqual(repaired, raw)  # the old false no-op condition
                self.assertNotEqual(session.identity(repaired), session.identity(raw))
                expected = copy.deepcopy(raw)
                next(row for row in expected['facts'] if row['id'] == fact_id)['source'][field] = 1
                self.assertEqual(json.dumps(repaired, sort_keys=True), json.dumps(expected, sort_keys=True))
                self.assertEqual(json.dumps(raw, sort_keys=True), before)
                self.assertEqual(json.dumps(request, sort_keys=True), request_before)
                self.assertEqual(sources, source_before)
                out = base.RequirementSessionTests().snapshot()
                checker = session._checker(out)
                rejected = checker.prepare_requirements(raw, sources)
                self.assertTrue(any('positive integers' in error for error in rejected['errors']), rejected['errors'])
                accepted = checker.prepare_requirements(repaired, sources)
                self.assertEqual(accepted['errors'], [])
                values, errors = checker.validate_requirements(accepted['requirements'], sources)
                self.assertEqual(errors, [])
                self.assertEqual(values['clock'], 7)
                self.assertEqual(values['upper_bound'], 5)

    def test_real_whole_fact_noop_is_still_rejected(self):
        raw, _ = base.selector_fixture()
        reordered = dict(reversed(list(raw['facts'][1].items())))
        request = {'base_sha256': session.identity(raw), 'facts': [reordered]}
        with self.assertRaisesRegex(ValueError, 'does not change the unaccepted extraction'):
            session.repair_requirements(raw, request)

    def test_prepare_freezes_a_repaired_integer_selector_after_one_real_repair(self):
        raw, sources = base.selector_fixture()
        fact = next(row for row in raw['facts'] if row['id'] == 'upper_bound')
        fact['source']['line'] = True
        repaired_fact = copy.deepcopy(fact); repaired_fact['source']['line'] = 1
        request = {'base_sha256': session.identity(raw), 'facts': [repaired_fact]}
        out = base.RequirementSessionTests().snapshot()
        invoke = Mock(side_effect=[{'requirements': copy.deepcopy(raw)}, {'repair': copy.deepcopy(request)}])
        result, errors = session.prepare(out, sources, invoke, 1)
        self.assertEqual(errors, [])
        self.assertIsNotNone(result)
        self.assertEqual(invoke.call_count, 2)
        self.assertTrue((out / 'extraction.json').exists())
        declaration = json.loads((out / 'extract-1.declaration.json').read_text())
        source = next(row for row in declaration['facts'] if row['id'] == 'upper_bound')['source']
        self.assertIs(type(source['line']), int)
        self.assertEqual(source['line'], 1)
        checker = session._checker(out)
        _, final_errors = checker.validate_requirements(result, sources)
        self.assertEqual(final_errors, [])
        self.assertIs(next(row for row in raw['facts'] if row['id'] == 'upper_bound')['source']['line'], True)


if __name__ == '__main__': unittest.main()
