"""Explicit support files stay original inputs, with complete frozen bindings."""
import copy
import hashlib
import json
import unittest
from harness.runtime import input_bundle
import test_audit as review_cases
import test_audit_source_model as source_cases
import test_audit_candidate_checks as candidate_cases

engine = source_cases.engine
source = source_cases.source
validator = review_cases.validator
quantities = engine._quantities


def source_fixture():
    model, texts = source_cases.witness()
    texts['support/pkg/shapes.py'] = 'ROWS = 6\nINNER = 7\nTOKENS = 5\nEPS = 0.000001\n'
    texts['support/pkg/unused.py'] = 'UNUSED = 19\n'
    for row, line in zip(model['facts'][:3], (1, 2, 3)):
        row['source'] = {'file': 'support/pkg/shapes.py', 'line': line, 'number': 1}
    model['facts'].append({'id': 'epsilon', 'source': {'file': 'support/pkg/shapes.py', 'line': 4, 'number': 1}, 'unit': 'dimensionless'})
    model['computations'][0]['references'].append({'file': 'support/pkg/shapes.py', 'start': 1, 'end': 3})
    return model, texts


def candidate_fixture():
    model, baseline, proposal, texts = candidate_cases.example()
    texts['support/shape.py'] = 'WORK = 80\nRATE = 1 requests/cycle\n'
    texts['support/unused.py'] = 'UNUSED = 23\n'
    helper = {'file': 'support/shape.py', 'quote': 'WORK = 80'}
    proposal['requirements']['facts'].append({'id': 'helper_work', 'source': {'file': 'support/shape.py', 'line': 1, 'number': 1}, 'unit': 'count'})
    proposal['work_model']['items'][0]['count']['operations'] = 'helper_work'
    proposal['work_model']['items'][0]['references'] = [helper]
    baseline['facts'][0]['source'] = {'file': 'support/shape.py', 'line': 2, 'number': 1}
    baseline['facts'].append({'id': 'independent_work', 'source': {'file': 'support/shape.py', 'line': 1, 'number': 1}, 'unit': 'count'})
    baseline['computations'][0]['steps'][0]['shape'] = ['independent_work']
    baseline['computations'][0]['references'].append(helper)
    model['count_checks'][0]['references'] = [helper]
    texts['design.json'] = json.dumps(proposal, ensure_ascii=False, sort_keys=True)
    texts['architecture.md'] = proposal['architecture_md']
    return model, baseline, proposal, texts


def candidate_result(model, baseline, proposal, texts):
    originals = validator.original_sources(texts)
    budget = engine.evaluate(proposal, originals)
    raw = source.analyze(baseline, originals, engine)
    return candidate_cases.audit.check(model, raw, proposal, budget, texts, engine,
                                      source_declaration=baseline), budget, raw


class MultifileSourceTests(unittest.TestCase):
    def test_namespace_predicates_match_runtime_for_legal_and_malicious_keys(self):
        legal = ['spec.md', 'reference.py', 'support/a.py', 'support/pkg/a.py',
                 'support/参考 输入.md', 'support/budget.json', 'support/~literal.py']
        illegal = [None, 1, True, '', 'support', 'support/', '/spec.md', 'pkg/a.py',
                   'architecture.md', 'design.json', 'proposal.json', 'budget.json',
                   'support//a.py', 'support/a//b.py', 'support/./a.py', 'support/../a.py',
                   'support/a/../../b.py', 'support/a.py/', 'support/\\a.py',
                   'support/a\\b.py', 'support/C:/a.py', 'support/a\n.py',
                   'support/a\r.py', 'support/a\t.py', 'support/a\0.py', 'support/a\x7f.py']
        for name in legal + illegal:
            with self.subTest(name=name):
                expected = name in legal
                self.assertEqual(input_bundle.is_original_source(name), expected)
                self.assertEqual(quantities.is_original_source(name), expected)
                self.assertEqual(validator.is_original_source(name), expected)

    def test_complete_original_closure_preserves_text_and_program_role(self):
        originals = {'spec.md': 'contract\r\n', 'reference.py': 'pass\n',
                     'support/pkg/a.py': 'SHAPE = 3\r\n', 'support/budget.json': '{"given":5}\n'}
        mixed = dict(originals, **{'architecture.md': 'generated design', 'design.json': '{}',
                                   'proposal.json': '{}', 'budget.json': '{}'})
        before = copy.deepcopy(mixed)
        for module in (input_bundle, quantities, validator):
            self.assertEqual(module.original_sources(mixed), originals)
            self.assertEqual(module.original_sources(originals, strict=True), originals)
            with self.assertRaises(ValueError): module.original_sources(mixed, strict=True)
            for main in ('spec.md', 'reference.py'):
                with self.assertRaises(ValueError): module.original_sources({key: value for key, value in originals.items() if key != main})
            for name in ('support/../bad.py', 'support//bad.py', 'support/a\\b.py'):
                with self.assertRaises(ValueError): module.original_sources(dict(originals, **{name: 'N=3'}))
        self.assertEqual(mixed, before)

    def test_source_model_derives_helper_shapes_constants_and_full_closure_hashes(self):
        model, texts = source_fixture(); before = copy.deepcopy((model, texts))
        result = source.analyze(model, texts, engine)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['computations']['projection']['totals']['p'], 6 * 7 * 5)
        self.assertEqual(result['fact_values']['epsilon'], .000001)
        self.assertEqual(set(result['binding']['source_sha256s']), set(texts))
        self.assertEqual(result['binding']['source_sha256s']['support/pkg/unused.py'], hashlib.sha256(texts['support/pkg/unused.py'].encode()).hexdigest())
        self.assertEqual((model, texts), before)
        verified = source.source_view(result, model, texts, engine)
        self.assertTrue(verified['source_receipts_verified'], verified['source_integrity_errors'])
        self.assertTrue(verified['source_coverage_complete'])

    def test_even_uncited_helper_change_or_removal_breaks_transaction_replay(self):
        model, texts = source_fixture(); result = source.analyze(model, texts, engine)
        for key in ('support/pkg/shapes.py', 'support/pkg/unused.py'):
            changed = dict(texts); changed[key] += '# changed\n'
            verified = source.source_view(result, model, changed, engine)
            self.assertFalse(verified['source_receipts_verified'])
            self.assertTrue(verified['source_integrity_errors'])
            removed = dict(texts); removed.pop(key)
            self.assertFalse(source.source_view(result, model, removed, engine)['source_receipts_verified'])

    def test_source_only_analysis_rejects_program_keys_and_unsafe_support_names(self):
        model, texts = source_fixture()
        for key in ('architecture.md', 'proposal.json', 'budget.json', 'support/../escape.py', 'support/C:/bad.py'):
            result = source.analyze(model, dict(texts, **{key: 'N=3'}), engine)
            self.assertFalse(result['ok'], key)
            self.assertFalse(result['values'])
        missing = dict(texts); missing.pop('reference.py')
        self.assertFalse(source.analyze(model, missing, engine)['ok'])

    def test_design_work_and_budget_can_reference_helper_without_promoting_program(self):
        model, baseline, proposal, texts = candidate_fixture()
        result, budget, raw = candidate_result(model, baseline, proposal, texts)
        self.assertTrue(budget['ok'], budget['errors'])
        self.assertTrue(raw['ok'], raw['errors'])
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(budget['candidates']['small']['ledger']['cost_groups'][0]['operations'], 80)
        self.assertTrue(any(row.get('resolved', {}).get('file') == 'support/shape.py' for row in result['citation_bindings']))
        self.assertTrue(any(row['id'].startswith('count__') and row['status'] == 'pass' for row in result['checks']))
        altered = copy.deepcopy(proposal)
        altered['work_model']['items'][0]['references'] = [{'file': 'design.json', 'quote': 'helper_work'}]
        invalid = engine.evaluate(altered, validator.original_sources(texts))
        self.assertFalse(invalid['ok'])
        self.assertTrue(any('original-input reference required' in error for error in invalid['errors']))
        self.assertFalse(engine.evaluate(proposal, texts)['ok'])

    def test_support_quantity_view_and_independent_recompute_use_actual_helper_values(self):
        _, texts = source_fixture()
        self.assertIn('FILE support/pkg/shapes.py', engine.quantity_view(texts))
        request = {'tool': 'recompute', 'facts': [
            {'id': name, 'source': {'file': 'support/pkg/shapes.py', 'line': line, 'number': 1}, 'unit': 'count'}
            for name, line in (('rows', 1), ('inner', 2))], 'assumptions': [],
            'calculations': [{'id': 'dot_work', 'formula': 'rows*inner', 'unit': 'MAC',
                              'basis': 'independent helper shape product', 'expected': 42}]}
        result = validator.recompute(request, texts, engine)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['values']['dot_work'], 42)
        self.assertTrue(result['comparisons']['dot_work']['matches_expected'])
        self.assertTrue(all(row['source_sha256'] == hashlib.sha256(texts['support/pkg/shapes.py'].encode()).hexdigest() for row in result['citation_bindings']))

    def test_support_evidence_ids_retain_source_role_and_reject_stale_versions(self):
        _, texts = source_fixture()
        ref = {'file': 'support/pkg/shapes.py', 'start': 1, 'end': 2}
        resolved = validator.resolve_citation(ref, texts, (), input_only=True)
        self.assertEqual(resolved['quote'], 'ROWS = 6\nINNER = 7')
        self.assertEqual(validator.resolve_citation({'evidence_id': resolved['evidence_id']}, texts, [resolved], input_only=True), resolved)
        changed = dict(texts); changed['support/pkg/shapes.py'] += '# changed\n'
        with self.assertRaises(ValueError): validator.resolve_citation({'evidence_id': resolved['evidence_id']}, changed, [resolved], input_only=True)
        for key in ('architecture.md', 'design.json', 'proposal.json', 'budget.json'):
            mixed = dict(texts, **{key: 'N = 3'})
            for ref in ({'file': key, 'start': 1, 'end': 1}, {'file': key, 'quote': 'N = 3'}):
                with self.assertRaises(ValueError): validator.resolve_citation(ref, mixed, (), input_only=True)

    def test_baseline_support_references_are_optional_but_both_mains_remain_required(self):
        _, texts = source_fixture()
        baseline = review_cases.baseline_fixture(texts)
        self.assertTrue(validator.validate_baseline(baseline, texts)['ok'])
        baseline['obligations'].append({'id': 'helper_shape', 'category': 'computation',
            'requirement': 'helper shape declarations', 'references': [{'file': 'support/pkg/shapes.py', 'start': 1, 'end': 3}]})
        result = validator.validate_baseline(baseline, texts)
        self.assertTrue(result['ok'], result['errors'])
        self.assertIn('support/pkg/shapes.py', result['input_evidence_files'])
        baseline['obligations'][0]['references'] = [{'file': 'support/pkg/shapes.py', 'start': 1, 'end': 3}]
        rejected = validator.validate_baseline(baseline, texts)
        self.assertFalse(rejected['ok'])
        self.assertIn('baseline must inspect both spec.md and reference.py', rejected['errors'])

    def test_verified_baseline_includes_successful_helper_transactions(self):
        model, texts = source_fixture(); result = source.analyze(model, texts, engine)
        baseline = review_cases.baseline_fixture(texts); baseline['analysis'] = model
        dependencies = {key + '_bundle_sha256': result['binding'][key + '_bundle_sha256'] for key in ('checker', 'calculator')}
        checked = validator.validate_baseline(baseline, texts, source_analysis=result, execution_dependencies=dependencies)
        self.assertTrue(checked['ok'], checked['errors'])
        self.assertIn('support/pkg/shapes.py', checked['input_evidence_files'])
        selected = validator.executed_source_values(result, texts, model, dependencies)
        self.assertEqual(selected['verified_data']['fact_values']['epsilon'], .000001)
        tampered = dict(texts); tampered['support/pkg/unused.py'] += '# tampered\n'
        self.assertFalse(validator.validate_baseline(baseline, tampered, source_analysis=result, execution_dependencies=dependencies)['ok'])

    def test_helper_can_supply_abi_original_role_and_proposal_binding_covers_it(self):
        model, baseline, proposal, texts = candidate_fixture()
        model['abi_checks'] = [{'id': 'helper_abi', 'geometry': {'id': 'geometry', 'kind': 'beat_span', 'payload_bytes': 8, 'beat_bytes': 8,
             'probes': [{'id': 'byte', 'byte_offset': 0, 'size_bytes': 8}]},
             'claims': [{'id': 'extent', 'result': 'full_beat_extent_bytes', 'declared': 8, 'relation': '==',
                         'references': [{'file': 'design.json', 'quote': '"id": "small"'}]}],
             'references': [{'file': 'support/shape.py', 'quote': 'WORK = 80'}, {'file': 'design.json', 'quote': '"id": "small"'}]}]
        result, budget, raw = candidate_result(model, baseline, proposal, texts)
        self.assertTrue(result['ok'], result['errors'])
        self.assertTrue(any(row['id'].startswith('abi__') and row['status'] == 'pass' for row in result['checks']))
        self.assertEqual(set(result['binding']['source_sha256s']), set(texts))
        dependencies = {key + '_bundle_sha256': result['binding'][key + '_bundle_sha256'] for key in ('checker', 'calculator')}
        checked = validator.executed_values(result, texts, raw, proposal, budget, dependencies)
        self.assertFalse(checked['errors'], checked['errors'])
        tampered = dict(texts); tampered['support/unused.py'] += '# changed\n'
        self.assertTrue(validator.executed_values(result, tampered, raw, proposal, budget, dependencies)['errors'])

    def test_final_review_roles_accept_helper_but_not_program_as_original_evidence(self):
        proposal, texts = review_cases.fixture()
        texts['support/contract.md'] = 'Area limit 3 mm2; source helper declaration.\n'
        context = review_cases.packet(texts, proposal, engine.evaluate(proposal, texts, True))
        arithmetic = validator.recompute(review_cases.arithmetic_request(), context['sources'], engine)
        arithmetic['request_id'] = 'audit.step-0'
        context['calculations'] = [arithmetic]
        context['proposal_checks'] = [review_cases.executed_proposal_check(context)]
        report = review_cases.report_fixture(context)
        for collection in ('requirements', 'checks', 'unverified'):
            for row in report[collection]:
                row['references'][0] = {'file': 'support/contract.md', 'quote': texts['support/contract.md'].strip()}
        accepted = validator.validate(report, context)
        self.assertTrue(accepted['ok'], accepted['errors'])
        for collection in ('requirements', 'checks', 'unverified'):
            for row in report[collection]:
                row['references'][0] = {'file': 'budget.json', 'quote': context['sources']['budget.json']}
        rejected = validator.validate(report, context)
        self.assertFalse(rejected['ok'])
        self.assertTrue(any('original-input' in error for error in rejected['errors']), rejected['errors'])

    def test_invalid_bundle_retains_safe_diagnostics_without_becoming_a_valid_baseline(self):
        _, texts = source_fixture()
        baseline = review_cases.baseline_fixture(texts)
        broken = dict(texts); broken.pop('spec.md')
        before = copy.deepcopy((baseline, broken))
        result = validator.validate_baseline(baseline, broken)
        self.assertFalse(result['ok'])
        self.assertTrue(any('both spec.md and reference.py' in error for error in result['errors']))
        diagnostic = result['citation_diagnostics'][0]
        self.assertEqual(diagnostic['file'], 'spec.md')
        self.assertEqual(diagnostic['kind'], 'invalid_reference')
        self.assertFalse(diagnostic.get('matches'))
        self.assertFalse(diagnostic.get('suggestions'))
        self.assertEqual((baseline, broken), before)
        unsafe = dict(texts, **{'support/../unsafe.py': 'ROWS = 6'})
        invalid = validator.validate_baseline(baseline, unsafe)
        self.assertFalse(invalid['ok'])
        self.assertTrue(any('invalid original-source name' in error for error in invalid['errors']))
        # Available canonical evidence does not erase the rejected closure key.
        self.assertEqual(invalid['input_evidence_files'], ['reference.py', 'spec.md'])

    def test_malformed_support_paths_are_rejected_by_quantity_extraction(self):
        texts = {'spec.md': 'A = 1', 'reference.py': 'pass\n', 'support/../unsafe.py': 'N = 3'}
        with self.assertRaises(ValueError): engine.source_quantities(texts)
        row = {'id': 'unsafe', 'source': {'file': 'support/../unsafe.py', 'line': 1, 'number': 1}, 'unit': 'count'}
        self.assertTrue(engine.prepare_requirements({'facts': [row]}, texts)['errors'])


if __name__ == '__main__': unittest.main()
