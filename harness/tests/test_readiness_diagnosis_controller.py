"""Read-only evidence routing, persistent observations and explicit experiment admission."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.adapters import readiness, readiness_diagnosis as diagnosis
from harness.runtime.rtl_worker import read_path
from harness.stages import readiness as entry, rtl


class DiagnosisRemote:
    def __init__(self, *, reused=False, state='prepared', advice='small_experiment'):
        self.calls = []
        self.value = {'observation_id': 'observation-1', 'fingerprint': 'frozen-observation',
                      'index_path': '@run/diagnosis/observation-1/index.json', 'state': state,
                      'reused': reused}
        self.report = {'advice': advice, 'summary': 'Inspect the repeated arithmetic before a bounded comparison.',
                       'findings': [{'observation': 'Existing log identifies an expensive elaboration.',
                                     'evidence': ['@run/control/diagnosis/observation-1/evidence/synthesis_log:120']}], 'experiments': []}
        if state == 'completed':
            self.value.update(report=self.report, report_path='diagnosis/observation-1/report.json')

    def call(self, request):
        self.calls.append(request)
        if request['action'] == 'rtl_diagnosis_prepare':
            return dict(self.value)
        if request['action'] == 'rtl_diagnosis_complete':
            report = request.get('report')
            return self.value | {'state': 'completed' if report else 'incomplete', 'report': report,
                                 'report_path': 'diagnosis/observation-1/report.json' if report else None}
        raise AssertionError(request)


class DiagnosisAdapterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        skill = self.root / 'skills/rtl-implementation-readiness/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('standalone diagnosis and experimentation skill')
        self.context = {'harness_dir': str(self.root), 'project_root': str(self.root),
                        'remote_run_dir': '/server/isolated', 'cache': str(self.root), 'models': {},
                        'settings': {'readiness': {'session_seconds': 900, 'assistant_turns': 80,
                                                  'diagnosis_seconds': 120, 'diagnosis_turns': 12}},
                        'role': 'implementation', 'scope': 'work', 'session_id': 'parent',
                        'session_actor_sha256': 'parent-sha', 'repair_skill': 'old-repair',
                        'review': 'old-review', 'candidate_scope': 'old-candidate'}

    def invoke(self, remote, answer=None):
        observed = {}
        def model(prompt, role, models, root, context_path, cache, actual_remote, label):
            observed.update(prompt=prompt, role=role, child=json.loads(context_path.read_text()), label=label)
            self.assertIs(actual_remote, remote)
            return answer or {'completed': True, 'tool_isolation_ok': True,
                              'final_text': json.dumps(remote.report)}
        with patch.object(diagnosis, 'run_session', side_effect=model) as session:
            result = diagnosis.run(self.context, remote, inputs={'artifacts': [{'name': 'synthesis_log', 'path': '/server/log'}]},
                                   event='observation_budget')
        return result, observed, session.call_count

    def test_existing_log_diagnosis_is_read_only_and_returns_ordinary_report(self):
        remote = DiagnosisRemote()
        result, observed, calls = self.invoke(remote)
        self.assertEqual(calls, 1)
        self.assertEqual(observed['role'], 'implementation')
        child = observed['child']
        self.assertEqual((child['role'], child['scope']), ('readiness_diagnosis', 'work'))
        self.assertEqual(child['readiness_limits'], {'session_seconds': 120, 'assistant_turns': 12})
        for key in ('session_id', 'session_actor_sha256', 'review', 'candidate_scope', 'repair_skill'):
            self.assertNotIn(key, child)
        self.assertIn('only list, read, status and jobs', observed['prompt'])
        self.assertIn('ordinary @run/ snapshot paths', observed['prompt'])
        self.assertIn('without write, handoff or other publishing tool calls', observed['prompt'])
        self.assertNotIn('"path": "/server/log"', observed['prompt'])
        self.assertEqual([call['action'] for call in remote.calls], ['rtl_diagnosis_prepare', 'rtl_diagnosis_complete'])
        complete = remote.calls[-1]
        self.assertEqual(complete['session_id'], observed['label'])
        self.assertEqual(complete['report'], remote.report)
        self.assertEqual(result['advice'], 'small_experiment')
        self.assertTrue(result['completed'])
        self.assertFalse(result['adopted'])

    def test_same_observation_never_restarts_active_failed_or_finished_actor(self):
        for state in ('prepared', 'incomplete', 'completed'):
            with self.subTest(state=state):
                remote = DiagnosisRemote(reused=True, state=state)
                result, observed, calls = self.invoke(remote)
                self.assertEqual(calls, 0)
                self.assertEqual(observed, {})
                self.assertEqual(len(remote.calls), 1)
                self.assertEqual(result['status'], 'rtl_diagnosis_reused')
                self.assertEqual(result['completed'], state == 'completed')

    def test_incomplete_unisolated_budget_or_malformed_report_is_saved_as_failure(self):
        for answer in ({'completed': False, 'tool_isolation_ok': True},
                       {'completed': True, 'tool_isolation_ok': False},
                       {'completed': True, 'tool_isolation_ok': True, 'budget_exhausted': True},
                       {'completed': True, 'tool_isolation_ok': True, 'final_text': 'half a JSON object {'}):
            with self.subTest(answer=answer):
                remote = DiagnosisRemote()
                result, _, calls = self.invoke(remote, answer)
                self.assertEqual(calls, 1)
                self.assertFalse(result['completed'])
                self.assertIsNone(remote.calls[-1]['report'])
                self.assertIn('error', remote.calls[-1])
                self.assertEqual(result['status'], 'rtl_diagnosis_incomplete')

    def test_no_budget_or_invalid_event_has_no_preparation_side_effect(self):
        remote = DiagnosisRemote()
        self.context['settings']['readiness']['diagnosis_turns'] = 0
        with self.assertRaisesRegex(ValueError, 'positive diagnosis'):
            self.invoke(remote)
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            diagnosis.run(self.context, remote, inputs={}, event='special_tool_wait')
        self.assertEqual(remote.calls, [])

    def test_report_preserves_ordinary_fields_and_rejects_bad_recommendations(self):
        report = {'advice': 'continue', 'summary': 'Existing evidence is sufficient.', 'findings': [],
                  'source_scope': {'top': 'actual_top'}}
        self.assertEqual(diagnosis._report('```json\n' + json.dumps(report) + '\n```'), report)
        for bad in (report | {'advice': 'pass'}, report | {'summary': ''}, report | {'findings': {}},
                    report | {'experiments': {}}, []):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                diagnosis._report(json.dumps(bad))

    def test_prior_diagnosis_report_is_visible_to_existing_experiment_session(self):
        self.context['diagnosis_report_path'] = 'diagnosis/observation-1/report.json'
        campaign = {'baseline_path': 'work', 'work_scope': 'readiness/campaign-1/work'}
        self.assertEqual(json.loads(readiness._context_text(self.context, campaign))['diagnosis_report_path'],
                         '@run/diagnosis/observation-1/report.json')
        out = self.root / 'server-run'
        work = out / campaign['work_scope']
        work.mkdir(parents=True)
        report = out / 'diagnosis/observation-1/report.json'
        report.parent.mkdir(parents=True)
        report.write_text('{"advice":"small_experiment"}')
        locator = json.loads(readiness._context_text(self.context, campaign))['diagnosis_report_path']
        self.assertEqual(read_path(out, work, locator), report.resolve())
        self.context['diagnosis_report_path'] = locator
        self.assertEqual(json.loads(readiness._context_text(self.context, campaign))['diagnosis_report_path'], locator)


class DiagnosisEntryTests(unittest.TestCase):
    def run_controller(self, *, allow=False, diagnose_only=False, advice='small_experiment', admitted=False,
                       completed=True, adopted=False, formal=False, resume_phase=None, review_scope=None):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); config = root / 'harness/config'; config.mkdir(parents=True)
            (config / 'environment.toml').write_text('[project]\nroot="../.."\ninput_dir="input"\n'
                '[remote]\nssh_host="fixture"\n[eda]\npython="/fixture/python"\nverilator="/fixture/verilator"\n'
                'yosys="/fixture/yosys"\nopenroad="/fixture/openroad"\n')
            (config / 'rtl.toml').write_text('[readiness]\nenabled=true\nrtl_review=' + str(formal).lower() + '\n' +
                (f'review_scope="{review_scope}"\n' if review_scope is not None else ''))
            (config / 'models.toml').write_text('schema_version=1\n')
            provenance = root / 'provenance.json'; provenance.write_text('{"candidate_sha256":"frozen-candidate"}')
            index = root / 'index.json'; index.write_text('{"artifacts":[{"name":"log","path":"/server/existing.log"}]}')
            args = argparse.Namespace(config=config / 'environment.toml', resume='diagnosis' if resume_phase else None,
                run_id=None if resume_phase else 'diagnosis', task=None, audit_only=False, validate_inputs=False,
                seed_work=None if resume_phase else '/frozen/source', seed_file=[] if resume_phase else ['rtl/dut.sv'],
                seed_provenance=None if resume_phase else provenance, diagnose_only=diagnose_only,
                diagnosis_inputs=index, diagnosis_event='stage_completed', allow_experiments=allow)
            previous = {'phase': resume_phase, 'status': 'interrupted',
                        'diagnosis': {'completed': True, 'observation_id': 'observation-1',
                                      'report_path': 'diagnosis/observation-1/report.json'},
                        'experiment_admission': {'allowed': True, 'reservation_id': 'existing-reservation'},
                        'readiness': {'status': 'rtl_readiness_completed', 'adopted': True}}
            calls = []
            class Remote:
                worker = None
                def __init__(self, *a): pass
                def evidence(self, *a): pass
                def call(self, req):
                    calls.append(req)
                    if req['action'] == 'init':
                        return {'worker': '/fixture/worker', 'run_dir': '/fixture/run', 'capabilities': {},
                                'reference_path': '/fixture/ref', 'input_source_paths': {}}
                    if req['action'] == 'rtl_diagnosis_admission':
                        return {'allowed': admitted, 'reasons': [] if admitted else ['existing job or budget/resources unavailable']}
                    if req['action'] == 'read':
                        return {'text': '1: ' + json.dumps(previous)}
                    return {'ok': True}
            def inspect(context, remote, *, inputs, event):
                self.assertEqual(calls[-1]['action'], 'rtl_readiness_seed')
                self.assertEqual(event, 'stage_completed')
                self.assertEqual(inputs['artifacts'][0]['path'], '/server/existing.log')
                return {'status': 'rtl_diagnosis_completed' if completed else 'rtl_diagnosis_incomplete',
                        'completed': completed, 'observation_id': 'observation-1', 'advice': advice,
                        'report_path': 'diagnosis/observation-1/report.json'}
            def optimize(context, remote):
                self.assertEqual(context['diagnosis_report_path'], 'diagnosis/observation-1/report.json')
                self.assertTrue([call['value'] for call in calls if call['action'] == 'set_status'][-1]['phase'] == 'readiness')
                return {'status': 'rtl_readiness_completed', 'adopted': adopted}
            def review(context, remote, **options):
                scope = review_scope or 'change'
                self.assertEqual(options['review_scope'], scope)
                self.assertIs(options['repair_handler'], rtl.readiness_repair)
                self.assertEqual(options['max_repairs'], 1)
                return {'status': 'rtl_review_finished', 'review_verdict': 'pass',
                        'review_scope': scope, 'rtl_stage_release': scope == 'candidate'}
            with patch.object(rtl, 'validate_skills'), patch.object(rtl, 'load_input_bundle', return_value=(
                    {'spec.md': 'fixture', 'reference.py': 'pass'}, {}, {'source_paths': {}})), \
                    patch.object(rtl, 'Remote', Remote), patch.object(rtl, 'run_session') as generation, \
                    patch.object(rtl, 'run_diagnosis', side_effect=inspect) as diagnostic, \
                    patch.object(rtl, 'run_readiness', side_effect=optimize) as experiment, \
                    patch.object(rtl, 'run_audit', side_effect=review) as audit, \
                    patch.object(rtl.signal, 'signal'), patch('builtins.print'):
                code = entry.run(args)
            generation.assert_not_called()
            final = [call['value'] for call in calls if call['action'] == 'set_status'][-1]
            return code, diagnostic.call_count, experiment.call_count, audit.call_count, calls, final

    def test_diagnose_only_never_starts_experiment_or_eda(self):
        code, diagnostics, experiments, reviews, calls, final = self.run_controller(diagnose_only=True, admitted=True)
        self.assertEqual((code, diagnostics, experiments, reviews), (0, 1, 0, 0))
        self.assertEqual(final['phase'], 'diagnosis')
        self.assertFalse(final['rtl_stage_release'])
        self.assertFalse(any(call['action'] in ('submit', 'cancel', 'cancel_all', 'rtl_diagnosis_admission') for call in calls))

    def test_experiments_require_opt_in_completed_recommendation_and_native_admission(self):
        for changes in ({}, {'allow': True, 'advice': 'continue'}, {'allow': True, 'completed': False},
                        {'allow': True, 'admitted': False}):
            with self.subTest(changes=changes):
                _, diagnostics, experiments, reviews, _, _ = self.run_controller(**changes)
                self.assertEqual((diagnostics, experiments, reviews), (1, 0, 0))
        code, diagnostics, experiments, reviews, calls, final = self.run_controller(allow=True, admitted=True)
        self.assertEqual((code, diagnostics, experiments, reviews), (0, 1, 1, 0))
        self.assertEqual(final['phase'], 'readiness')
        self.assertEqual(sum(call['action'] == 'rtl_diagnosis_admission' for call in calls), 1)

    def test_opt_in_candidate_keeps_existing_independent_review_route(self):
        code, diagnostics, experiments, reviews, calls, final = self.run_controller(
            allow=True, admitted=True, adopted=True, formal=True)
        self.assertEqual((code, diagnostics, experiments, reviews), (0, 1, 1, 1))
        self.assertEqual(final['phase'], 'audit')
        self.assertFalse(final['rtl_stage_release'])
        self.assertEqual(final['review_scope'], 'change')

    def test_admitted_candidate_can_explicitly_request_whole_candidate_review(self):
        code, diagnostics, experiments, reviews, _, final = self.run_controller(
            allow=True, admitted=True, adopted=True, formal=True, review_scope='candidate')
        self.assertEqual((code, diagnostics, experiments, reviews), (0, 1, 1, 1))
        self.assertEqual(final['review_scope'], 'candidate')
        self.assertTrue(final['rtl_stage_release'])

    def test_incomplete_diagnosis_is_not_success_and_mode_flags_cannot_bypass_read_only(self):
        code, _, _, _, _, final = self.run_controller(completed=False)
        self.assertEqual(code, 1)
        self.assertEqual(final['phase'], 'diagnosis')
        with self.assertRaisesRegex(ValueError, 'mutually exclusive'):
            self.run_controller(allow=True, diagnose_only=True)

    def test_admitted_audit_resume_preserves_route_without_diagnosis_admission_or_experiment(self):
        code, diagnostics, experiments, reviews, calls, final = self.run_controller(
            allow=True, admitted=False, formal=True, resume_phase='audit')
        self.assertEqual((code, diagnostics, experiments, reviews), (0, 0, 0, 1))
        self.assertEqual(final['phase'], 'audit')
        self.assertFalse(final['rtl_stage_release'])
        self.assertEqual(final['review_scope'], 'change')
        self.assertEqual(final['experiment_admission']['reservation_id'], 'existing-reservation')
        self.assertEqual([call for call in calls if call['action'] == 'rtl_readiness_seed'],
                         [{'action': 'rtl_readiness_seed', 'validate_only': True}])
        self.assertFalse(any(call['action'] in ('rtl_diagnosis_admission', 'submit', 'cancel', 'cancel_all') for call in calls))

    def test_admitted_readiness_resume_reuses_existing_admission_before_existing_pipeline(self):
        code, diagnostics, experiments, reviews, calls, final = self.run_controller(
            allow=True, admitted=False, formal=True, adopted=True, resume_phase='readiness')
        self.assertEqual((code, diagnostics, experiments, reviews), (0, 0, 1, 1))
        self.assertEqual(final['phase'], 'audit')
        self.assertFalse(any(call['action'] == 'rtl_diagnosis_admission' for call in calls))


if __name__ == '__main__':
    unittest.main()
