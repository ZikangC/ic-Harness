"""Critical finite issues distinguish complete supply from optimistic examples."""
import copy
import unittest
from test_architecture import fixture
from harness.checks.architecture import _engine as engine


class ExecutionIssueTests(unittest.TestCase):
    def test_coarse_budget_does_not_require_execution_or_numeric_proof(self):
        document, sources = fixture()
        candidate = document['candidates'][0]
        candidate['schedule_windows'] = []
        candidate['coverage']['schedule_windows']['status'] = 'not_applicable'
        candidate['numeric_probes'] = []
        result = engine.evaluate(document, sources)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['gate']['status'], 'pass')
        selected = result['candidates']['small']
        self.assertEqual(selected['numeric_probes']['coverage_status'], 'not_checked')
        self.assertTrue(selected['ledger']['evidence_gaps'])

    def proposal(self):
        document, sources = fixture()
        document['work_model']['items'][0]['count']['unit'] = 'MAC'
        candidate = document['candidates'][0]
        candidate['cost_groups'][0]['units'] = 2
        candidate['macros'][0]['width'] = 16
        candidate['buffers'][0]['width'] = 16
        candidate['buffers'].append({'id': 'held_input', 'kind': 'register',
            'count': 1, 'depth': 1, 'width': 8, 'basis': 'one retained input',
            'objects': [{'id': 'captured_input', 'shape': [], 'element_bits': 8,
                         'copies': 1, 'alignment_bits': 8, 'metadata_bits': 0,
                         'live_in': ['run'], 'basis': 'one input broadcast to the lanes'}]})
        candidate['area_components'][0]['covers_buffers'] = ['held_input']
        window = candidate['schedule_windows'][0]
        window.update(cycles=3,
            reads=[{'id': 'packed', 'storage_reference': 'banks', 'instance': 0,
                    'word_address': 0, 'cycle': 0}],
            writes=[],
            uses=[{'cost_group': 'compute', 'read_id': 'packed', 'cycle': 1,
                   'lane': lane, 'operand': 'coefficient', 'bit_offset': lane * 8,
                   'width': 8} for lane in (0, 1)],
            local_uses=[{'cost_group': 'compute', 'kind': 'register',
                'buffer': 'held_input', 'cycle': 1, 'lane': lane, 'operand': 'input',
                'bit_offset': 0, 'width': 8, 'ready_cycle': 0,
                'basis': 'retained scalar broadcast',
                'producer_basis': 'input was captured before this finite issue'}
                for lane in (0, 1)],
            issues=[{'cost_group': 'compute', 'cycle': 1, 'lanes': [0, 1],
                     'operands': ['coefficient', 'input'], 'basis': 'both MAC inputs on both lanes'}])
        return document, sources

    def check(self, mutate=None):
        document, sources = self.proposal()
        if mutate:
            mutate(document['candidates'][0])
        return engine.evaluate(document, sources)

    def window(self, report):
        return report['candidates']['small']['ledger']['schedule_windows'][0]

    def test_packed_coefficients_and_one_register_broadcast_prove_finite_issue(self):
        report = self.check()
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['gate']['status'], 'pass')
        window = self.window(report)
        self.assertEqual(window['physical_reads'], 1)
        self.assertEqual(window['issues'][0]['declared_positions'], 4)
        self.assertEqual(window['issue_completeness'], 'pass')
        self.assertEqual({row['supply_kind'] for row in window['uses']}, {'physical_read', 'register'})

    def test_missing_operand_on_one_lane_is_a_contradiction(self):
        report = self.check(lambda c: c['schedule_windows'][0]['local_uses'].pop())
        self.assertFalse(report['ok'])
        self.assertTrue(any('missing declared operand positions' in error for error in report['errors']))

    def test_partial_lane_witness_stays_unverified_without_blocking_budget(self):
        def mutate(candidate):
            window = candidate['schedule_windows'][0]
            window['issues'][0].update(lanes=[0], partial_issue_basis='one tail lane')
            window['uses'] = window['uses'][:1]
            window['local_uses'] = window['local_uses'][:1]
        report = self.check(mutate)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['gate']['status'], 'pass')
        self.assertEqual(self.window(report)['issues'][0]['status'], 'unverified')
        self.assertTrue(report['candidates']['small']['ledger']['evidence_gaps'])

    def test_one_role_MAC_does_not_prove_two_input_operation(self):
        report = self.check(lambda c: c['schedule_windows'][0]['issues'][0].update(operands=['coefficient']))
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['gate']['status'], 'pass')
        self.assertTrue(any('two independently reviewed input roles' in reason
                            for reason in self.window(report)['issues'][0]['unverified']))

    def test_missing_issues_remain_diagnostic_without_blocking_budget(self):
        report = self.check(lambda c: c['schedule_windows'][0].pop('issues'))
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(self.window(report)['physical_reads'], 1)
        self.assertEqual(report['gate']['status'], 'pass')
        self.assertTrue(any('no complete full-lane finite issue' in reason
                            for reason in report['candidates']['small']['ledger']['execution_derivation']['unknown']))

    def test_complete_issue_in_another_window_closes_group_coverage(self):
        def mutate(candidate):
            complete = copy.deepcopy(candidate['schedule_windows'][0])
            complete['id'] = 'complete_issue'
            candidate['schedule_windows'][0]['issues'][0]['lanes'] = [0]
            candidate['schedule_windows'].append(complete)
        report = self.check(mutate)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['gate']['status'], 'pass')
        self.assertEqual(report['candidates']['small']['ledger']['execution_derivation']['complete_issue_groups'], ['compute'])

    def test_register_operand_cannot_arrive_after_use(self):
        report = self.check(lambda c: c['schedule_windows'][0]['local_uses'][0].update(ready_cycle=2))
        self.assertFalse(report['ok'])
        self.assertTrue(any('before declared producer is ready' in error for error in report['errors']))

    def test_register_slice_cannot_exceed_actual_capacity(self):
        report = self.check(lambda c: c['schedule_windows'][0]['local_uses'][0].update(bit_offset=4))
        self.assertFalse(report['ok'])
        self.assertTrue(any('exceeds register buffer capacity' in error for error in report['errors']))

    def test_register_must_be_priced(self):
        report = self.check(lambda c: c['area_components'][0].pop('covers_buffers'))
        self.assertFalse(report['ok'])
        self.assertTrue(any('register buffer has no linked logic cost' in error for error in report['errors']))

    def test_local_use_requires_declared_producer_provenance(self):
        report = self.check(lambda c: c['schedule_windows'][0]['local_uses'][0].pop('producer_basis'))
        self.assertFalse(report['ok'])
        self.assertTrue(any('producer_basis' in error for error in report['errors']))

    def test_feedback_requires_iteration_basis_but_is_a_legal_source(self):
        def feedback(candidate):
            for use in candidate['schedule_windows'][0]['local_uses']:
                use.update(kind='feedback', feedback_basis='previous reduction iteration, retained before overwrite')
        report = self.check(feedback)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['gate']['status'], 'pass')
        def absent(candidate):
            feedback(candidate)
            candidate['schedule_windows'][0]['local_uses'][0].pop('feedback_basis')
        report = self.check(absent)
        self.assertFalse(report['ok'])

    def test_duplicate_physical_and_register_supply_cannot_fill_one_position_twice(self):
        report = self.check(lambda c: c['schedule_windows'][0]['local_uses'][0].update(operand='coefficient'))
        self.assertFalse(report['ok'])
        self.assertTrue(any('duplicate physical/local operand position' in error for error in report['errors']))

    def test_register_only_issue_needs_no_invented_macro_read(self):
        def mutate(candidate):
            window = candidate['schedule_windows'][0]
            window['reads'] = []
            window['uses'] = []
            for lane in (0, 1):
                use = copy.deepcopy(window['local_uses'][lane])
                use['operand'] = 'coefficient'
                window['local_uses'].append(use)
        report = self.check(mutate)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['gate']['status'], 'pass')
        self.assertEqual(self.window(report)['physical_reads'], 0)

    def test_missing_input_readiness_prevents_complete_issue(self):
        document, sources = self.proposal()
        document['requirements']['storage_rules'][0].pop('read_latency_cycles')
        report = engine.evaluate(document, sources)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['gate']['status'], 'pass')
        self.assertEqual(self.window(report)['issues'][0]['status'], 'unverified')

    def test_independent_audit_consumes_declared_design_local_sources(self):
        from test_audit_candidate_checks import example, execute, check_rows
        model, baseline, proposal, sources = example()
        design, _ = self.proposal()
        proposal['work_model']['items'][0]['count']['unit'] = 'MAC'
        baseline['computations'][0]['steps'] = [{'id': 'effects', 'kind': 'dot', 'width': 80}]
        proposal['candidates'][0] = design['candidates'][0]
        model['service_windows'][0]['intervals'][0]['end'] = 80
        model['operand_checks'][0]['issues'] = [{'cycle': 1, 'lanes': [0, 1],
                                              'operands': ['coefficient', 'input']}]
        report = execute(model, baseline, proposal, sources)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(check_rows(report, 'operands__')[0]['status'], 'pass', report['checks'])

    def test_inputs_unchanged(self):
        document, sources = self.proposal()
        original = copy.deepcopy((document, sources))
        engine.evaluate(document, sources)
        self.assertEqual((document, sources), original)


if __name__ == '__main__':
    unittest.main()
