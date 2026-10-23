"""Read-only observation, actual session provenance and one-shot admission.

No model, EDA or external jobs are launched by these tests.
"""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from harness.runtime import rtl_diagnosis as diagnosis
from harness.runtime import rtl_worker as worker
from harness.tests import test_rtl_remote as fixtures


class DiagnosisNativeTests(unittest.TestCase):
    tearDown = fixtures.WorkerTests.tearDown

    def setUp(self):
        fixtures.WorkerTests.setUp(self)
        rtl = self.out / 'work/rtl'; rtl.mkdir()
        (rtl / 'top.sv').write_text('module top; endmodule\n')
        self.external = self.root / 'historical'; self.external.mkdir()
        (self.external / 'source.sv').write_text('module top; endmodule\n')
        (self.external / 'synth.log').write_text('source parsed\nprocess conversion remains active\n')
        (self.external / 'status.json').write_text(json.dumps({'state': 'running', 'phase': 'coarse',
            'elapsed_seconds': 100, 'cpu_seconds': 50, 'rss_bytes': 123, 'updated_utc': 'yesterday'}))
        self.inputs = {'version': 1, 'source': {'candidate_sha256': 'frozen-candidate'},
            'files': [{'name': 'source.sv', 'path': str(self.external / 'source.sv'), 'kind': 'source',
                       'sha256': worker.sha(self.external / 'source.sv')},
                      {'name': 'synth.log', 'path': str(self.external / 'synth.log'), 'kind': 'log'},
                      {'name': 'status.json', 'path': str(self.external / 'status.json'), 'kind': 'status'}],
            'processes': [], 'metadata': {'top': 'top', 'sources': ['source.sv']},
            'observation': {'configured_budget_seconds': 10}}
        manifest = json.loads((self.out / 'manifest.json').read_text())
        manifest['settings']['readiness'] = {'enabled': True, 'experiment_min_available_memory_mib': 10}
        worker.save(self.out / 'manifest.json', manifest)

    def call(self, **request):
        return worker.dispatch(self.root, self.runid, request)

    def prepare(self, **updates):
        return self.call(action='rtl_diagnosis_prepare', event='observation_budget',
                         inputs=self.inputs, **updates)

    def session(self, *, completed=True, isolated=True, exhausted=False, role='readiness_diagnosis', report=None, observation=None):
        label = 'diagnosis-actor-' + str(len(list((self.out / 'control/sessions').glob('*.json'))))
        self.call(action='put_evidence', path='control/' + label + '.prompt.txt', content='Synthetic diagnosis invocation')
        observation = observation or self.prepare()
        binding = {'diagnosis_observation_id': observation['observation_id'],
                   'diagnosis_fingerprint': observation['fingerprint']} if role == 'readiness_diagnosis' else {}
        self.call(action='rtl_session_begin', session_id=label, role=role, scope='work', model_role='implementation', **binding)
        report = report or {'advice': 'small_experiment',
                'summary': 'Investigate the observed expensive transform with a bounded comparison.', 'findings': []}
        for suffix, value in [
            ('result.json', {'role': 'implementation', 'completed': completed, 'exit_code': 0 if completed else 143,
                'stop_reason': 'stop' if completed else 'toolUse', 'tool_isolation_ok': isolated,
                'budget_exhausted': exhausted, 'budget_reason': 'assistant_turns' if exhausted else None,
                'final_text': json.dumps(report)}),
            ('isolation.json', {'checks': [{'active_tools': ['rtl_remote']}], 'blocked': [] if isolated else ['write']}),
            ('events.jsonl', '')]:
            self.call(action='put_evidence', path='control/' + label + '.' + suffix,
                      content=value if isinstance(value, str) else json.dumps(value))
        self.call(action='rtl_session_end', session_id=label)
        return label

    def finish(self, observation=None, *, report=None, session=None, **updates):
        observation = observation or self.prepare()
        report = report or {'advice': 'small_experiment',
                'summary': 'Investigate the observed expensive transform with a bounded comparison.', 'findings': []}
        return self.call(action='rtl_diagnosis_complete', observation_id=observation['observation_id'],
            session_id=session or self.session(report=report), report=report, **updates)

    def admit(self, observation):
        return self.call(action='rtl_diagnosis_admission', observation_id=observation['observation_id'])

    def test_packet_captures_real_bytes_and_metadata_without_mutating_external_inputs(self):
        before = worker.inventory(self.external)
        observation = self.prepare()
        self.assertFalse(observation['reused']); self.assertEqual(observation['state'], 'active')
        index = json.loads((self.out / observation['index_path']).read_text())
        self.assertEqual(index['source'], self.inputs['source'])
        self.assertEqual(index['metadata'], self.inputs['metadata'])
        self.assertIn('available_memory_bytes', index['resources'])
        self.assertEqual(len(index['files']), 3)
        for row in index['files']:
            copied = self.out / row['path']
            self.assertEqual(copied.read_bytes(), Path(row['original_path']).read_bytes())
            self.assertEqual(row['sha256'], worker.sha(copied))
            self.assertEqual(row['observed_bytes'], copied.stat().st_size)
        self.assertEqual(before, worker.inventory(self.external))
        self.assertEqual(list((self.out / 'jobs').glob('*')), [])

    def test_duplicate_active_observation_is_reused_without_creating_second_attempt(self):
        first, second = self.prepare(), self.prepare()
        self.assertTrue(second['reused']); self.assertEqual(first['observation_id'], second['observation_id'])
        self.assertEqual(len(list((self.out / 'control/diagnosis').glob('observation-*'))), 1)

    def test_concurrent_prepare_reserves_only_one_attempt(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.prepare(), range(2)))
        self.assertEqual(len({result['observation_id'] for result in results}), 1)
        self.assertEqual(sorted(result['reused'] for result in results), [False, True])

    def test_status_counter_resource_and_capture_clock_changes_do_not_retrigger(self):
        self.inputs['metadata']['actual_processes'] = [{'state': 'R', 'cpu_seconds': 1, 'rss_bytes': 100}]
        self.inputs['metadata']['resources'] = {'load_average': [0, 0, 0], 'MemAvailable_bytes': 10, 'SwapFree_bytes': 20}
        self.inputs['metadata']['log_inventory'] = [{'path': 'log', 'mtime_utc': 'before', 'bytes': 50}]
        first = self.prepare()
        status = self.external / 'status.json'
        value = json.loads(status.read_text())
        value.update(elapsed_seconds=9999, cpu_seconds=8888, rss_bytes=987654, updated_utc='today')
        status.write_text(json.dumps(value))
        self.inputs['observation']['observed_at'] = 'later-time-only'
        self.inputs['metadata']['actual_processes'] = [{'state': 'S', 'cpu_seconds': 10, 'rss_bytes': 200}]
        self.inputs['metadata']['resources'] = {'load_average': [2, 2, 2], 'MemAvailable_bytes': 20, 'SwapFree_bytes': 30}
        self.inputs['metadata']['log_inventory'][0]['mtime_utc'] = 'after'
        with patch.object(diagnosis, 'resources', return_value={'available_memory_bytes': 1}):
            second = self.prepare()
        self.assertTrue(second['reused']); self.assertEqual(first['fingerprint'], second['fingerprint'])

    def test_top_parameters_and_constraints_changes_are_new_applicability_evidence(self):
        observations = [self.prepare()['observation_id']]
        for key, value in (('top', 'different_top'), ('parameters', {'WIDTH': 42}),
                           ('constraints', {'clock_period_ns': 5}), ('ordered_sources', ['second.sv', 'first.sv'])):
            self.inputs['metadata'][key] = value
            observations.append(self.prepare()['observation_id'])
        self.assertEqual(len(set(observations)), len(observations))

    def test_new_log_bytes_semantic_state_and_explicit_budget_token_trigger_observation(self):
        observations = [self.prepare()['observation_id']]
        with (self.external / 'synth.log').open('a') as stream: stream.write('new useful log evidence\n')
        observations.append(self.prepare()['observation_id'])
        status = self.external / 'status.json'
        value = json.loads(status.read_text()); value['state'] = 'failed'; status.write_text(json.dumps(value))
        observations.append(self.prepare()['observation_id'])
        self.inputs['observation']['configured_budget_seconds'] = 20
        observations.append(self.prepare()['observation_id'])
        self.assertEqual(len(set(observations)), 4)

    def test_process_cpu_counters_are_not_identity_but_state_and_start_identity_are(self):
        self.inputs['processes'] = [{'pid': 123, 'start_ticks': 99, 'boot_id': 'boot', 'kind': 'synthesis'}]
        process = {'declared': self.inputs['processes'][0], 'available': True, 'identity_match': True,
                   'pid': 123, 'start_ticks': 99, 'boot_id': 'boot', 'state': 'R', 'cpu_seconds': 1., 'rss_bytes': 100}
        with patch.object(diagnosis, '_process', return_value=process): first = self.prepare()
        with patch.object(diagnosis, '_process', return_value=process | {'cpu_seconds': 10., 'rss_bytes': 200}): second = self.prepare()
        self.assertTrue(second['reused'])
        with patch.object(diagnosis, '_process', return_value=process | {'state': 'S'}): asleep = self.prepare()
        self.assertTrue(asleep['reused'])
        with patch.object(diagnosis, '_process', return_value=process | {'state': 'Z'}): third = self.prepare()
        self.assertNotEqual(first['fingerprint'], third['fingerprint'])

    def test_frozen_hash_mismatch_and_unsafe_aliases_are_rejected_without_published_packet(self):
        self.inputs['files'][0]['sha256'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'SHA-256'): self.prepare()
        self.assertEqual(list((self.out / 'control/diagnosis').glob('observation-*')), [])
        self.inputs['files'][0]['sha256'] = worker.sha(self.external / 'source.sv')
        for name in ('../escape', '/absolute', 'a/b'):
            with self.subTest(name=name):
                self.inputs['files'][0]['name'] = name
                with self.assertRaisesRegex(ValueError, 'safe single'): self.prepare()
        self.inputs['files'][0]['name'] = 'source.sv'
        alias = self.external / 'alias.sv'; alias.symlink_to(self.external / 'source.sv')
        self.inputs['files'][0]['path'] = str(alias)
        with self.assertRaisesRegex(ValueError, 'aliases'): self.prepare()

    def test_duplicate_file_names_and_unknown_events_are_rejected(self):
        self.inputs['files'][1]['name'] = self.inputs['files'][0]['name']
        with self.assertRaisesRegex(ValueError, 'unique'): self.prepare()
        with self.assertRaisesRegex(ValueError, 'event'):
            self.call(action='rtl_diagnosis_prepare', event='specific_synthesis_pass', inputs=self.inputs)

    def test_naturally_completed_actual_actor_saves_report_and_immutable_repeat(self):
        observation = self.prepare(); result = self.finish(observation)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(json.loads((self.out / result['report_path']).read_text()), result['report'])
        repeated = self.call(action='rtl_diagnosis_complete', observation_id=observation['observation_id'],
                             session_id='not-an-actor', report={'advice': 'continue', 'summary': 'Replacement'})
        self.assertTrue(repeated['reused']); self.assertEqual(repeated['report'], result['report'])
        self.assertTrue(self.prepare()['reused'])

    def test_incomplete_exhausted_or_unisolated_actor_never_publishes_half_report(self):
        for updates in ({'completed': False}, {'exhausted': True}, {'isolated': False}):
            with self.subTest(updates=updates):
                self.inputs['observation']['test_case'] = str(updates)
                observation = self.prepare()
                result = self.finish(observation, session=self.session(**updates))
                self.assertEqual(result['state'], 'incomplete'); self.assertNotIn('report', result)
                self.assertFalse((self.out / Path(result['index_path']).parent / 'report.json').exists())
                self.assertTrue(self.prepare()['reused'])

    def test_missing_wrong_actor_and_terminal_evidence_changes_are_incomplete(self):
        first = self.prepare(); result = self.finish(first, session='missing')
        self.assertEqual(result['state'], 'incomplete')
        self.inputs['observation']['test_case'] = 'changed_actor'
        observation = self.prepare(); session = self.session()
        (self.out / 'control' / (session + '.events.jsonl')).write_text('changed after native end\n')
        result = self.finish(observation, session=session)
        self.assertEqual(result['state'], 'incomplete')
        self.assertIn('incomplete or not isolated', result['diagnosis_error'])

    def test_report_cannot_be_borrowed_from_different_terminal_output(self):
        observation = self.prepare(); session = self.session()
        result = self.finish(observation, session=session,
            report={'advice': 'continue', 'summary': 'Invented replacement output.', 'findings': []})
        self.assertEqual(result['state'], 'incomplete')
        self.assertIn('terminal actor output', result['diagnosis_error'])

    def test_actor_cannot_be_replayed_for_a_new_observation_of_changed_evidence(self):
        first = self.prepare(); session = self.session(observation=first)
        self.assertEqual(self.finish(first, session=session)['state'], 'completed')
        with (self.external / 'synth.log').open('a') as stream:
            stream.write('new evidence after the first invocation\n')
        second = self.prepare()
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])
        result = self.finish(second, session=session)
        self.assertEqual(result['state'], 'incomplete')
        self.assertIn('different observation', result['diagnosis_error'])
        self.assertNotIn('report', result)

    def test_report_lists_and_saved_report_integrity_are_checked(self):
        for report in ({'advice': 'continue', 'summary': 'x'},
                       {'advice': 'continue', 'summary': 'x', 'findings': [], 'experiments': {}}):
            self.inputs['observation']['case'] = str(report)
            self.assertEqual(self.finish(report=report)['state'], 'incomplete')
        self.inputs['observation']['case'] = 'report_tamper'
        completed = self.finish()
        (self.out / completed['report_path']).write_text('{"advice":"continue","summary":"changed"}')
        with self.assertRaisesRegex(ValueError, 'completed report changed'): self.prepare()
        with self.assertRaisesRegex(ValueError, 'completed report changed'): self.admit(completed)

    def test_invalid_json_report_or_controller_error_remains_incomplete(self):
        for number, report in enumerate(({'advice': 'pass', 'summary': 'x'}, {'advice': 'continue', 'summary': ' '},
                                        {'advice': 'continue', 'summary': 'x', 'cost': float('nan')})):
            with self.subTest(report=report):
                self.inputs['observation']['case'] = number
                self.assertEqual(self.finish(report=report)['state'], 'incomplete')
        self.inputs['observation']['case'] = 'explicit-error'
        result = self.finish(error='final model report was truncated')
        self.assertEqual(result['state'], 'incomplete'); self.assertIn('truncated', result['diagnosis_error'])

    def test_source_or_evidence_change_cannot_be_accepted(self):
        observation = self.prepare()
        (self.out / 'work/rtl/top.sv').write_text('module changed; endmodule\n')
        self.assertEqual(self.finish(observation)['state'], 'incomplete')
        observation = self.prepare()
        index = json.loads((self.out / observation['index_path']).read_text())
        (self.out / index['files'][0]['path']).write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'captured evidence changed'): self.finish(observation)

    def test_admission_reserves_once_with_fresh_memory_and_does_not_submit_any_job(self):
        observation = self.finish()
        with patch.object(diagnosis, 'resources', return_value={'available_memory_bytes': 11 * 1024 * 1024}):
            admitted = self.admit(observation); duplicate = self.admit(observation)
        self.assertTrue(admitted['allowed']); self.assertTrue(admitted['reserved'])
        self.assertFalse(duplicate['allowed']); self.assertIn('experiment_dispatch_already_reserved', duplicate['reasons'])
        self.assertEqual(list((self.out / 'jobs').glob('*')), [])

    def test_memory_threshold_must_be_explicit_and_available(self):
        observation = self.finish()
        for memory in (None, 1):
            with self.subTest(memory=memory), patch.object(diagnosis, 'resources', return_value={'available_memory_bytes': memory}):
                self.assertFalse(self.admit(observation)['allowed'])
        manifest = json.loads((self.out / 'manifest.json').read_text())
        del manifest['settings']['readiness']['experiment_min_available_memory_mib']
        worker.save(self.out / 'manifest.json', manifest)
        with patch.object(diagnosis, 'resources', return_value={'available_memory_bytes': 10**12}):
            result = self.admit(observation)
        self.assertIn('experiment_memory_admission_threshold_unconfigured', result['reasons'])

    def test_no_report_continue_and_exhausted_budget_cannot_dispatch(self):
        observation = self.prepare()
        with patch.object(diagnosis, 'resources', return_value={'available_memory_bytes': 10**12}):
            self.assertFalse(self.admit(observation)['allowed'])
            self.finish(observation, report={'advice': 'continue', 'summary': 'Existing flow remains appropriate.', 'findings': []})
            self.assertFalse(self.admit(observation)['allowed'])
        self.inputs['observation']['case'] = 'disabled'
        observation = self.finish()
        manifest = json.loads((self.out / 'manifest.json').read_text())
        manifest['settings']['readiness']['enabled'] = False
        worker.save(self.out / 'manifest.json', manifest)
        self.assertFalse(self.admit(observation)['allowed'])

    def test_pending_native_job_blocks_dispatch(self):
        observation = self.finish()
        path = self.out / 'jobs/123'; path.mkdir(parents=True)
        worker.save(path / 'request.json', {'scope': 'work'})
        with patch.object(diagnosis, 'resources', return_value={'available_memory_bytes': 10**12}):
            result = self.admit(observation)
        self.assertFalse(result['allowed']); self.assertIn('current_run_has_pending_jobs', result['reasons'])

    def test_original_synthesis_may_continue_but_active_experiment_or_unknown_identity_blocks(self):
        declared = {'pid': 123, 'start_ticks': 99, 'boot_id': 'boot', 'kind': 'synthesis'}
        self.inputs['processes'] = [declared]
        process = {'declared': declared, 'available': True, 'identity_match': True,
            'pid': 123, 'start_ticks': 99, 'boot_id': 'boot', 'state': 'R'}
        with patch.object(diagnosis, '_process', return_value=process): observation = self.finish()
        with patch.object(diagnosis, 'resources', return_value={'available_memory_bytes': 10**12}), \
             patch.object(diagnosis, '_process', return_value=process | {'identity_match': False}):
            self.assertFalse(self.admit(observation)['allowed'])
        with patch.object(diagnosis, 'resources', return_value={'available_memory_bytes': 10**12}), \
             patch.object(diagnosis, '_process', return_value=process):
            self.assertTrue(self.admit(observation)['allowed'])
        self.inputs['processes'][0]['kind'] = 'experiment'
        with patch.object(diagnosis, '_process', return_value=process): observation = self.finish()
        with patch.object(diagnosis, 'resources', return_value={'available_memory_bytes': 10**12}), \
             patch.object(diagnosis, '_process', return_value=process):
            result = self.admit(observation)
        self.assertIn('external_experiment_active', result['reasons'])

    def test_metadata_experiment_declaration_is_not_ignored(self):
        self.inputs['metadata']['jobs'] = [{'kind': 'experiment', 'status': 'running'}]
        observation = self.finish()
        with patch.object(diagnosis, 'resources', return_value={'available_memory_bytes': 10**12}): result = self.admit(observation)
        self.assertFalse(result['allowed']); self.assertIn('external_experiment_active_or_unknown', result['reasons'])

    @unittest.skipUnless(Path('/proc/self/stat').is_file(), 'objective Linux /proc snapshot requires Linux')
    def test_actual_current_process_identity_cpu_command_and_memory_are_observable(self):
        raw = Path('/proc/self/stat').read_text()
        fields = raw[raw.rfind(')') + 2:].split()
        declared = {'pid': os.getpid(), 'start_ticks': int(fields[19]),
                    'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
        process = diagnosis._process(declared)
        self.assertTrue(process['available']); self.assertTrue(process['identity_match'])
        self.assertGreaterEqual(process['cpu_seconds'], 0)
        self.assertGreater(process['rss_bytes'], 0); self.assertTrue(process['command'])
        self.assertGreater(diagnosis.resources()['available_memory_bytes'], 0)


if __name__ == '__main__':
    unittest.main()
