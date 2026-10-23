"""Count identities come from executed source/work/native-cost objects."""
import copy
import unittest

import test_audit as protocol
from test_audit_candidate_checks import audit, engine, example, execute, check_rows


class CountMetadataTests(unittest.TestCase):
    def test_source_count_metadata_is_derived_from_resolved_objects(self):
        model, declaration, proposal, sources = example()
        model['count_checks'][0].update(unit='invented_rows', source_workload='invented_profile',
                                        kind='native_cost_cycles', cost_group='invented_group')
        result = execute(model, declaration, proposal, sources)
        self.assertTrue(result['ok'], result['errors'])
        check = check_rows(result, 'count__')[0]
        self.assertEqual(check['kind'], 'source_count_correspondence')
        self.assertEqual(check['unit'], 'ops')
        self.assertEqual(check['source_paths'], ['path'])
        self.assertEqual(check['source_workload'], 'task')
        self.assertEqual(check['candidate_workload'], 'primary')
        self.assertEqual(check['candidate_work_ids'], ['arithmetic'])
        self.assertEqual(check['source_by_unit'], {'ops': 80})
        self.assertEqual(check['candidate_by_unit'], {'ops': 80})
        self.assertNotIn('cost_group', check)

    def test_native_cost_metadata_binds_actual_group_unit_resources_and_work(self):
        model, declaration, proposal, sources = example()
        proposal['work_model']['items'][0]['count'].update(operations=12, unit='MAC')
        declaration['computations'][0]['steps'] = [{'id': 'effects', 'kind': 'dot', 'width': 12}]
        group = proposal['candidates'][0]['cost_groups'][0]
        group.update(realization='transformed', count={'kind': 'custom', 'operations': 4,
                     'unit': 'MAC', 'basis': 'deliberate native cost undercount'},
                     equivalence='Independent semantic review remains required.')
        model['service_windows'] = []
        model['count_checks'][0].update(realization='transformed',
            equivalence_evidence='Synthetic source/native path, not assumed equivalent.',
            physical_realization={'basis': 'Explicit native primitive witness', 'groups': [{
                'cost_group': 'compute', 'steps': [{'id': 'matrix', 'kind': 'gemv',
                    'input_elements': 4, 'output_elements': 3}],
                'scope': {'kind': 'task', 'axis': 'run'}, 'basis': 'Actual chosen scalar MAC work',
                'references': [{'file': 'design.json', 'quote': '"selected": "small"'}],
                'work_ids': ['invented_work'], 'resources': ['invented_resource'],
                'operation_unit': 'invented_unit'}]})
        result = execute(model, declaration, proposal, sources)
        self.assertTrue(result['ok'], result['errors'])
        native = {row['kind']: row for row in check_rows(result, 'count__') if '__physical__' in row['id']}
        self.assertEqual(set(native), {'native_cost_operations', 'native_cost_cycles'})
        for check in native.values():
            self.assertEqual(check['cost_group'], 'compute')
            self.assertEqual(check['cost_work_ids'], ['arithmetic'])
            self.assertEqual(check['resource_ids'], ['weights'])
            self.assertEqual(check['schedule_references'], ['compute'])
            self.assertEqual(check['source_paths'], ['path'])
            self.assertEqual(check['operation_unit'], 'MAC')
            self.assertEqual(check['status'], 'fail')
        self.assertEqual(native['native_cost_operations']['unit'], 'MAC')
        self.assertEqual(native['native_cost_cycles']['unit'], 'cycles')
        self.assertEqual((native['native_cost_operations']['expected'], native['native_cost_operations']['actual']), (12, 4))

    def test_same_display_id_cannot_rebut_failure_by_switching_source_path(self):
        document, sources = protocol.fixture()
        baseline = protocol.baseline_fixture(sources)
        baseline['analysis']['computations'][0]['steps'][0]['shape'] = [96]
        unrelated = copy.deepcopy(baseline['analysis']['computations'][0])
        unrelated['id'] = 'unrelated_work'
        unrelated['steps'][0]['shape'] = [80]
        baseline['analysis']['computations'].append(unrelated)
        raw = audit._source.analyze(baseline['analysis'], sources, protocol.engine)
        self.assertTrue(raw['ok'], raw['errors'])
        record = {'baseline': baseline, 'baseline_sha256': protocol.audit_session.report_identity(baseline),
                  'analysis_result': raw,
                  'audit_script_bundle_sha256': raw['binding']['checker_bundle_sha256'],
                  'calculator_bundle_sha256': raw['binding']['calculator_bundle_sha256']}
        record['source_execution'] = protocol.validator.executed_source_values(raw, sources, baseline['analysis'],
            {'checker_bundle_sha256': record['audit_script_bundle_sha256'],
             'calculator_bundle_sha256': record['calculator_bundle_sha256']})
        budget = protocol.engine.evaluate(document, sources, True)
        context = protocol.audit_session.packet(sources, document, budget, None, record)
        model = protocol.proposal_request()['model']
        def run(request_id):
            result = audit.check(model, raw, document, budget, context['sources'], protocol.engine,
                source_declaration=baseline['analysis'], execution_dependencies=context['execution_dependencies'])
            self.assertTrue(result['ok'], result['errors'])
            result['request_id'] = request_id
            return result
        failed = run('failed')
        model['count_checks'][0]['source_paths'] = ['unrelated_work']
        switched = run('switched')
        old = check_rows(failed, 'count__')[0]
        new = check_rows(switched, 'count__')[0]
        self.assertEqual(old['id'], new['id'])
        self.assertEqual((old['status'], new['status']), ('fail', 'pass'))
        self.assertNotEqual(old['source_paths'], new['source_paths'])
        context['calculations'] = [failed, switched]
        context['proposal_checks'] = [failed, switched]
        report = protocol.report_fixture(context)
        report['verdict'] = 'insufficient_evidence'
        receipt = next(row for row in failed['subreceipts'] if any(check['id'] == old['id'] for check in row['checks']))
        correction = next(row for row in switched['subreceipts'] if any(check['id'] == new['id'] for check in row['checks']))
        report['counterexample_reviews'] = [{'request_id': 'failed', 'receipt_id': receipt['id'],
            'check_id': old['id'], 'disposition': 'audit_declaration_error',
            'reason': 'Deliberately invalid attempted rebuttal using unrelated source work.',
            'correction_check': {'request_id': 'switched', 'receipt_id': correction['id'], 'check_id': new['id']},
            'references': [{'file': 'spec.md', 'quote': 'items 12'},
                           {'file': 'design.json', 'quote': '"id": "small"'},
                           {'request_id': 'failed', 'calculation_id': receipt['id']+'::'+old['id']+'__actual',
                            'actual': old['actual']}]}]
        checked = protocol.validator.validate(report, context)
        self.assertFalse(checked['ok'])
        self.assertTrue(any('requires a related actual passing check' in error for error in checked['errors']), checked['errors'])


if __name__ == '__main__':
    unittest.main()
