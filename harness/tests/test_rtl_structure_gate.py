"""Native admission/evidence contracts using explicit synthetic host receipts.

These unit tests invoke no EDA, containers or models. Real tool/host integration
is qualified separately; fabricated unit receipts are not project evidence.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from harness.runtime import rtl_worker as worker
from harness.runtime import rtl_structure as executor
from harness.runtime import rtl_structure_gate as gate


class StructureGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.out = self.root / 'output' / 'fixture'
        self.work = self.out / 'work'
        self.work.mkdir(parents=True)
        (self.work / 'design.sv').write_text('module design(input wire a, output wire y); assign y=a; endmodule\n')
        (self.work / 'build.json').write_text('{"top":"design","sources":["design.sv"]}\n')
        worker.save(self.work / 'RTL_STRUCTURE.json', {'version': 1, 'configurations': [{'id': 'delivery', 'top': 'design', 'sources': ['design.sv']}], 'blackboxes': []})
        for scope in ('snapshot/input', 'snapshot/architecture', 'snapshot/harness/runtime'):
            (self.out / scope).mkdir(parents=True)
        (self.out / 'snapshot/input/spec.md').write_text('The design forwards its input. Compile the external top.\n')
        (self.out / 'snapshot/architecture/architecture.md').write_text('One external top, design; no alternate configurations.\n')
        runtime = Path(worker.__file__).parent
        for name in ('rtl_structure.py', 'rtl_worker.py', 'rtl_job_host.py'):
            shutil.copyfile(runtime / name, self.out / 'snapshot/harness/runtime' / name)
        self.isolation = {'backend': 'docker_readonly_job_v1', 'image_id': 'sha256:' + 'd' * 64,
                          'uid': os.getuid(), 'gid': os.getgid()}
        self.manifest = {'input_sha256': worker.inventory(self.out / 'snapshot/input'),
                         'architecture_sha256': worker.inventory(self.out / 'snapshot/architecture'),
                         'harness_sha256': worker.inventory(self.out / 'snapshot/harness'),
                         'settings': {'tools': {'python': sys.executable, 'yosys': '/tool/yosys',
                                                'verilator': '/tool/verilator'}},
                         'job_isolation': self.isolation}
        worker.save(self.out / 'manifest.json', self.manifest)
        worker.save(self.out / 'architecture-current.json', {'path': 'snapshot/architecture', 'review_status': 'accepted_estimate_only'})
        self.review = self.call(action='rtl_review_begin')
        self.metadata = json.loads((self.out / self.review['inventory_path']).read_text())
        self.audit = self.out / self.review['audit_scope']

    def call(self, **request):
        return worker.dispatch(self.root, 'fixture', request)

    def references(self):
        return [{'path': '@input/spec.md', 'sha256': worker.sha(self.out / 'snapshot/input/spec.md')},
                {'path': '@candidate/RTL_STRUCTURE.json', 'sha256': worker.sha(self.out / self.review['candidate_scope'] / 'RTL_STRUCTURE.json')},
                {'path': '@run/' + self.review['architecture_scope'] + '/architecture.md',
                 'sha256': worker.sha(self.out / self.review['architecture_scope'] / 'architecture.md')}]

    def enqueue(self, **changes):
        configuration = {'top': 'design', 'sources': ['design.sv']}
        configuration.update(changes)
        return self.call(action='structure_check', scope=self.review['audit_scope'],
                         review=self.review['review'], configuration=configuration, timeout_seconds=10)

    def receipts(self, job_id, exit_code=0):
        """Synthetic protocol fixture, never started or claimed as a real job."""
        job = self.out / 'jobs' / job_id
        request = json.loads((job / 'request.json').read_text())
        cid = hashlib.sha256(('unit-fixture-' + job_id).encode()).hexdigest()
        result = {'exit_code': exit_code, 'timed_out': False,
                  'candidate_sha256': request.get('candidate_sha256'),
                  'source_sha256': request['source_sha256'],
                  'source_at_start_sha256': request['source_sha256'],
                  'source_after_sha256': request['source_sha256'],
                  'cleanup_complete': True, 'container_running': False, 'container_id': cid,
                  'job_isolation': self.isolation,
                  'isolation': dict(self.isolation, cleanup_complete=True, container_running=False, container_id=cid)}
        worker.save(job / 'result.json', result)
        worker.save(job / 'host-launch.json', {'request_sha256': worker.sha(job / 'request.json'),
                                             'manifest_sha256': worker.sha(self.out / 'manifest.json'),
                                             'helper_sha256': self.manifest['harness_sha256']['runtime/rtl_job_host.py']})
        worker.save(job / 'host-launch.intent.json', {'job_id': job_id, 'nonce': 'unit-fixture-only'})
        worker.save(job / 'container.json', {'container_id': cid, 'image_id': self.isolation['image_id']})
        worker.save(job / 'container-finished.json', {'container_id': cid, 'image_id': self.isolation['image_id'],
                                                    'state': {'Running': False, 'Status': 'exited', 'ExitCode': exit_code}})
        return result

    def complete(self, queued, warning=None, status=None, *, blackbox=(), whitebox=(), lint_warning=None):
        job_id = queued['job_id']; job = self.out / 'jobs' / job_id
        config = json.loads((job / 'structure-config.json').read_text())
        boxes = {'blackbox': blackbox, 'whitebox': whitebox}
        transcript = ''.join(begin + '\n' + ''.join(name + '\n' for name in boxes[kind]) + end + '\n' for kind, (begin, end) in executor.MODULE_MARKERS.items())
        transcript += ((warning + '\n') if warning else '')
        transcript += "ERROR: Found 1 problems in 'check -assert'.\n" if warning else executor.COMPLETE_MARKER + '\n'
        selected = status or ('findings' if warning or lint_warning else 'passed')
        actual = {'argv': [config['tool_path'], '-s', '/private-unit-fixture/check.ys'],
                  'exit_code': 1 if warning else 0, 'timed_out': False,
                  'capture_complete': True, 'duration_seconds': .01}
        for stream, value in [('stdout', transcript), ('stderr', '')]:
            raw = value.encode(); actual[stream] = value
            actual[stream + '_base64'] = base64.b64encode(raw).decode()
            actual[stream + '_sha256'] = hashlib.sha256(raw).hexdigest()
        inventory = self.metadata['work_inventory']
        script = executor.make_script(config, "/private-unit-fixture")
        lint_execution = {'argv': executor.make_lint_argv(config, '/private-unit-fixture'),
                          'exit_code': 0, 'timed_out': False, 'capture_complete': True, 'duration_seconds': .01}
        for stream, value in [('stdout', ''), ('stderr', (lint_warning + '\n') if lint_warning else '')]:
            raw = value.encode(); lint_execution[stream] = value
            lint_execution[stream + '_base64'] = base64.b64encode(raw).decode()
            lint_execution[stream + '_sha256'] = hashlib.sha256(raw).hexdigest()
        lint_compile = {key: config[key] for key in gate.CONFIGURATION_FIELDS}
        checked = {'schema': 2, 'version': 2, 'kind': 'rtl_structure_check', 'ok': selected == 'passed',
                   'status': selected, 'source_stable': True, 'config_sha256': gate.digest(config),
                   'binding': config['binding'], 'script': {'content': script, 'sha256': hashlib.sha256(script.encode()).hexdigest()},
                   'tool': {'path': config['tool_path'], 'binary_sha256': 'e' * 64, 'binary_after_sha256': 'e' * 64, 'binary_stable': True,
                            'version_exit_code': 0, 'version_stdout': 'Synthetic Yosys unit fixture only\n', 'version_stderr': ''},
                   'compile': {name: config[name] for name in gate.CONFIGURATION_FIELDS}, 'execution': actual,
                   'diagnostics': executor.parse_diagnostics(transcript, '', gate.digest(config)),
                   'fast_lint': {'schema': 1, 'kind': 'verilator_full_config_lint',
                                 'status': 'findings' if lint_warning else 'passed', 'completed': True,
                                 'profile': executor.synthesis_profile(config),
                                 'config_sha256': gate.digest(config), 'compile_config_sha256': gate.digest(lint_compile),
                                 'compile': lint_compile, 'execution': lint_execution,
                                 'tool': {'path': config['lint_tool_path'], 'binary_sha256': 'f' * 64,
                                          'binary_after_sha256': 'f' * 64, 'binary_stable': True,
                                          'version_exit_code': 0, 'version_stdout': 'Synthetic Verilator unit fixture only\n',
                                          'version_stderr': ''},
                                 'diagnostics': executor.parse_lint_diagnostics('', lint_execution['stderr'], gate.digest(config))}}
        checked['compile'].update(executor.parse_module_inventory(transcript))
        checked['compile'].update(include_aliases=executor.include_aliases(config, '/private-unit-fixture'), include_aliases_stable=True)
        checked['compile'].update(source_inventory_sha256=gate.digest(inventory),
                                  source_after_inventory_sha256=gate.digest(inventory))
        worker.save(job / 'stdout.log', checked)
        self.receipts(job_id, 0 if selected == 'passed' else 1)
        return checked

    def report(self, checks):
        return {'verdict': 'pass', 'review_scope': self.review['review_scope'], 'candidate_sha256': self.review['candidate_sha256'],
                'reason': 'Synthetic admission contract only; no hardware acceptance claim.',
                'evidence': [value['job_id'] for value in checks],
                'structure_checks': {'version': 1, 'required_configurations': [
                    {key: value[key] for key in ('job_id', 'configuration_sha256')} for value in checks],
                    'coverage': {'claim': 'full_design', 'reason': 'All configurations from the frozen synthetic build manifest.',
                                 'source_refs': self.references()}, 'waivers': []}}

    def validate(self, report):
        return gate.validate_release(self.out, self.metadata, report)

    def test_include_alias_or_script_argument_rewrite_cannot_be_self_reported(self):
        queued = self.enqueue()
        checked = self.complete(queued)
        report = self.report([queued]); job = self.out / 'jobs' / queued['job_id']
        for mutate in (lambda x: x['compile'].update(include_aliases_stable=False),
                       lambda x: x['compile'].update(include_aliases=[{'include_dir':'unrecorded','path':'/tmp/x','target':'/outside'}]),
                       lambda x: x['script'].update(content=x['script']['content'].replace('-DSYNTHESIS=1', '-D"SYNTHESIS=1"'))):
            changed = copy.deepcopy(checked); mutate(changed)
            worker.save(job / 'stdout.log', changed)
            with self.assertRaises(ValueError): self.validate(report)
        worker.save(job / 'stdout.log', checked)
        self.validate(report)

    def test_native_queue_pins_frozen_candidate_and_rejects_tool_override(self):
        before = worker.closure_inventory(self.out / self.review['candidate_scope'])
        queued = self.enqueue()
        request = json.loads((self.out / 'jobs' / queued['job_id'] / 'request.json').read_text())
        config = json.loads((self.out / queued['config_path']).read_text())
        self.assertEqual(config['source_root'], str((self.out / self.review['candidate_scope']).resolve()))
        self.assertEqual(config['binding']['source_inventory_sha256'], gate.digest(before))
        self.assertEqual(config['binding']['candidate_sha256'], self.review['candidate_sha256'])
        self.assertEqual(config['tool_path'], '/tool/yosys')
        self.assertEqual(config['lint_tool_path'], '/tool/verilator')
        self.assertEqual(config['version'], 2)
        self.assertEqual(request['structure_check']['version'], 2)
        self.assertEqual(request['argv'][0], sys.executable)
        self.assertEqual(worker.closure_inventory(self.out / self.review['candidate_scope']), before)
        with self.assertRaisesRegex(ValueError, 'invalid structure compile configuration'):
            self.enqueue(tool_path='/untrusted/tool')
        with self.assertRaisesRegex(ValueError, 'invalid structure compile configuration'):
            self.enqueue(lint_tool_path='/untrusted/lint')

    def test_conflicting_yosys_intrinsic_is_rejected_before_native_queue(self):
        jobs = self.out / 'jobs'
        before = worker.closure_inventory(self.out / self.review['candidate_scope'])
        prior_jobs = sorted(str(path.relative_to(jobs)) for path in jobs.rglob('*')) if jobs.exists() else []
        for value in ('0', None):
            with self.subTest(value=value), self.assertRaisesRegex(executor.UnsupportedConfiguration, 'YOSYS must be 1'):
                self.enqueue(defines={'YOSYS': value})
            self.assertEqual(sorted(str(path.relative_to(jobs)) for path in jobs.rglob('*')) if jobs.exists() else [], prior_jobs)
            self.assertEqual(worker.closure_inventory(self.out / self.review['candidate_scope']), before)

    def test_job_check_rejects_replaced_config_and_source_binding(self):
        queued = self.enqueue(); job = self.out / 'jobs' / queued['job_id']
        self.assertTrue(self.call(action='job_check', job_id=queued['job_id'], phase='before')['ok'])
        path = job / 'structure-config.json'; old = path.read_bytes()
        config = json.loads(old); config['top'] = 'another'; worker.save(path, config)
        with self.assertRaisesRegex(ValueError, 'configuration or runner changed'):
            self.call(action='job_check', job_id=queued['job_id'], phase='before')
        path.write_bytes(old)
        candidate = self.out / self.review['candidate_scope'] / 'design.sv'
        candidate.write_text('module changed; endmodule\n')
        with self.assertRaisesRegex(ValueError, 'protected RTL tree changed'):
            self.call(action='job_check', job_id=queued['job_id'], phase='after')

    def ordinary_work_job(self):
        return self.call(action='submit', scope='work', argv=['python', '-c', 'print("metadata-only fixture")'],
                         timeout_seconds=10)

    def test_ordinary_work_missing_entry_is_classified_only_after_protected_checks(self):
        queued = self.ordinary_work_job()
        job = self.out / 'jobs' / queued['job_id']
        request = json.loads((job / 'request.json').read_text())
        for stage in ('writable_scope_links', 'source_inventory'):
            with self.subTest(stage=stage), patch.object(worker, stage, side_effect=FileNotFoundError(2, 'vanished', str(self.work / 'build.o'))):
                response = self.call(action='job_check', job_id=queued['job_id'], phase='after')
            self.assertFalse(response['ok'])
            self.assertNotIn('source_after_sha256', response)
            failure = response['job_check_failure']
            self.assertEqual(failure['stage'], stage)
            self.assertEqual(failure['job_id'], queued['job_id'])
            self.assertEqual(failure['phase'], 'after')
            self.assertEqual(failure['request_sha256'], worker.sha(job / 'request.json'))
            self.assertEqual(failure['guard_set_sha256'], worker.digest(request['integrity_guards']))
            self.assertTrue(failure['protected_guards_checked'])
            self.assertTrue(failure['source_containment_checked'])
            self.assertEqual(failure['version'], 2)
            self.assertFalse(failure['native_job'])

    def test_dangling_external_symlink_does_not_impersonate_disappearing_build_file(self):
        queued = self.ordinary_work_job()
        (self.work / 'dangling-outside').symlink_to(self.root / 'outside-nonexistent')
        with self.assertRaisesRegex(ValueError, 'unproved safe containment'):
            self.call(action='job_check', job_id=queued['job_id'], phase='after')

    def test_unsafe_symlink_elsewhere_blocks_missing_build_entry_classification(self):
        queued = self.ordinary_work_job()
        outside = self.work / 'unrelated-link'
        outside.symlink_to(self.root / 'outside-nonexistent')
        with patch.object(worker, 'writable_scope_links', side_effect=FileNotFoundError(2, 'vanished', str(self.work / 'build.o'))):
            with self.assertRaisesRegex(ValueError, 'unproved safe containment'):
                self.call(action='job_check', job_id=queued['job_id'], phase='after')

    def test_internal_dangling_symlink_remains_fail_closed_not_build_file_exception(self):
        queued = self.ordinary_work_job()
        (self.work / 'dangling-inside').symlink_to(self.work / 'missing-inside')
        with self.assertRaisesRegex(ValueError, 'unproved safe containment'):
            self.call(action='job_check', job_id=queued['job_id'], phase='after')

    def test_existing_internal_symlink_does_not_disable_actual_missing_build_entry_classification(self):
        queued = self.ordinary_work_job()
        (self.work / 'existing-inside').symlink_to(self.work / 'design.sv')
        with patch.object(worker, 'source_inventory', side_effect=FileNotFoundError(2, 'vanished', str(self.work / 'build.o'))):
            result = self.call(action='job_check', job_id=queued['job_id'], phase='after')
        self.assertFalse(result['ok'])
        self.assertTrue(result['job_check_failure']['source_containment_checked'])
        self.assertNotIn('source_after_sha256', result)

    def test_chained_external_symlink_or_incomplete_containment_scan_stays_fail_closed(self):
        queued = self.ordinary_work_job()
        (self.work / 'inside-alias').symlink_to(self.work / 'outside-alias')
        (self.work / 'outside-alias').symlink_to(self.root / 'missing-outside')
        missing = FileNotFoundError(2, 'vanished', str(self.work / 'build.o'))
        with patch.object(worker, 'source_inventory', side_effect=missing):
            with self.assertRaisesRegex(ValueError, 'unproved safe containment'):
                self.call(action='job_check', job_id=queued['job_id'], phase='after')
        (self.work / 'inside-alias').unlink(); (self.work / 'outside-alias').unlink()
        with patch.object(worker, 'source_inventory', side_effect=missing), patch.object(worker.os, 'walk', side_effect=PermissionError('scan unavailable')):
            with self.assertRaisesRegex(ValueError, 'unproved safe containment'):
                self.call(action='job_check', job_id=queued['job_id'], phase='after')

    def test_missing_build_entry_with_unprovable_path_or_symlink_loop_stays_fail_closed(self):
        queued = self.ordinary_work_job()
        for path in (None, 'relative.o', str(self.root / 'outside-missing.o')):
            error = FileNotFoundError(2, 'vanished', path)
            with self.subTest(path=path), patch.object(worker, 'source_inventory', side_effect=error):
                with self.assertRaisesRegex(ValueError, 'unproved safe containment'):
                    self.call(action='job_check', job_id=queued['job_id'], phase='after')
        first, second = self.work / 'cycle-a', self.work / 'cycle-b'
        first.symlink_to(second); second.symlink_to(first)
        with patch.object(worker, 'source_inventory', side_effect=FileNotFoundError(2, 'vanished', str(self.work / 'build.o'))):
            with self.assertRaisesRegex(ValueError, 'unproved safe containment'):
                self.call(action='job_check', job_id=queued['job_id'], phase='after')

    def test_source_failure_class_never_catches_guard_errors_or_other_io_errors(self):
        queued = self.ordinary_work_job()
        for error in (FileNotFoundError('protected read incomplete'), ValueError('protected RTL tree changed')):
            with self.subTest(error=type(error).__name__), patch.object(worker, 'check_guards', side_effect=error):
                with self.assertRaises(type(error)):
                    self.call(action='job_check', job_id=queued['job_id'], phase='after')
        for stage in ('writable_scope_links', 'source_inventory'):
            for error in (PermissionError('read denied'), OSError('I/O error'), ValueError('unsafe source boundary')):
                with self.subTest(stage=stage, error=type(error).__name__), patch.object(worker, stage, side_effect=error):
                    with self.assertRaises(type(error)):
                        self.call(action='job_check', job_id=queued['job_id'], phase='after')

    def test_frozen_audit_or_native_missing_entry_does_not_use_work_exception(self):
        ordinary = self.call(action='submit', scope=self.review['audit_scope'], review=self.review['review'],
                             argv=['python', '-c', 'print("metadata-only fixture")'], timeout_seconds=10)
        with patch.object(worker, 'writable_scope_links', side_effect=FileNotFoundError('missing')):
            with self.assertRaises(FileNotFoundError):
                self.call(action='job_check', job_id=ordinary['job_id'], phase='after')
        self.receipts(ordinary['job_id'])
        native = self.enqueue()
        with patch.object(worker, 'writable_scope_links', side_effect=FileNotFoundError('missing')):
            with self.assertRaises(FileNotFoundError):
                self.call(action='job_check', job_id=native['job_id'], phase='after')

    def test_actual_external_hardlink_is_still_rejected_not_source_incomplete(self):
        queued = self.ordinary_work_job()
        outside = self.root / 'outside-build.o'
        outside.write_bytes(b'fixture')
        os.link(outside, self.work / 'linked-build.o')
        with self.assertRaisesRegex(ValueError, 'external hardlinks'):
            self.call(action='job_check', job_id=queued['job_id'], phase='after')

    def begin_repair_with_ordinary_error(self, *, integrity=False):
        report = {'verdict': 'revise', 'review_scope': self.review['review_scope'], 'candidate_sha256': self.review['candidate_sha256'],
                  'reason': 'Synthetic diagnostic candidate remains open; no project repair claim.', 'evidence': []}
        self.call(action='write', scope=self.review['audit_scope'], path='review.json', base_sha256=None,
                  content=json.dumps(report))
        self.call(action='rtl_review_decision', review=self.review['review'], report_path='review.json',
                  expected_verdict='revise')
        self.call(action='rtl_repair_begin', review=self.review['review'])
        queued = self.ordinary_work_job()
        result = self.receipts(queued['job_id'])
        result['error'] = 'Synthetic failed check; actual result must not be reused.'
        if integrity: result['integrity_error'] = 'original protected mismatch or ambiguous legacy error'
        else:
            result['source_check_error'] = {'kind': 'ordinary_work_inventory_incomplete'}
            result.pop('source_after_sha256')
        worker.save(self.out / 'jobs' / queued['job_id'] / 'result.json', result)
        return queued

    def test_known_source_observation_error_does_not_poison_repair_but_never_passes(self):
        queued = self.begin_repair_with_ordinary_error()
        completed = self.call(action='rtl_repair_end', review=self.review['review'])
        self.assertEqual(completed['repair']['status'], 'completed')
        self.assertEqual(completed['decision']['verdict'], 'revise')
        next_review = self.call(action='rtl_review_begin', seed_review=self.review['review'])
        self.assertIsNone(next_review['decision'])
        report = {'verdict': 'pass', 'review_scope': next_review['review_scope'], 'candidate_sha256': next_review['candidate_sha256'],
                  'reason': 'A repair milestone cannot authorize independent pass.', 'evidence': []}
        self.call(action='write', scope=next_review['audit_scope'], path='new-review.json', base_sha256=None,
                  content=json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'successful independent evidence'):
            self.call(action='rtl_review_decision', review=next_review['review'], report_path='new-review.json',
                      expected_verdict='final')
        self.assertIn('error', json.loads((self.out / 'jobs' / queued['job_id'] / 'result.json').read_text()))

    def test_historical_protected_or_ambiguous_integrity_error_remains_sticky(self):
        self.begin_repair_with_ordinary_error(integrity=True)
        worker.integrity_guards(self.out)  # Current bytes being intact cannot erase the historical failure.
        with self.assertRaisesRegex(ValueError, 'changed protected evidence'):
            self.call(action='rtl_repair_end', review=self.review['review'])

    def test_exit_zero_with_source_check_error_is_not_reusable_independent_evidence(self):
        queued = self.call(action='submit', scope=self.review['audit_scope'], review=self.review['review'],
                           argv=['python', '-c', 'print("metadata-only fixture")'], timeout_seconds=10)
        result = self.receipts(queued['job_id'])
        result['error'] = 'incomplete source observation'
        result['source_check_error'] = {'kind': 'ordinary_work_inventory_incomplete'}
        result.pop('source_after_sha256')
        worker.save(self.out / 'jobs' / queued['job_id'] / 'result.json', result)
        with self.assertRaisesRegex(ValueError, 'did not complete with clean isolation'):
            gate._completed_job(self.out, self.metadata, queued['job_id'], success=True)

    def test_clean_native_result_supplies_structure_scope_only_and_pass_guard(self):
        queued = self.enqueue(); self.complete(queued)
        report = self.report([queued]); proof = self.validate(report)
        self.assertEqual(proof['scope'], 'pre-mapping_structure_only')
        self.assertEqual(proof['waived_findings'], 0)
        worker.save(self.audit / 'review.json', report)
        decision = self.call(action='rtl_review_decision', review=self.review['review'], report_path='review.json', expected_verdict='final')
        self.assertEqual(decision['decision']['verdict'], 'pass')
        self.assertEqual(decision['decision']['structure_checks']['checks'][0]['job_id'], queued['job_id'])
        with self.assertRaisesRegex(ValueError, 'decided audit scope'):
            self.enqueue()

    def test_plain_successful_job_and_self_reported_pass_are_insufficient(self):
        queued = self.call(action='submit', scope=self.review['audit_scope'], argv=['python', '-c', 'pass'])
        job_id = queued['job_id']; self.receipts(job_id)
        worker.save(self.audit / 'review.json', {'verdict': 'pass', 'review_scope': self.review['review_scope'], 'candidate_sha256': self.review['candidate_sha256'],
                    'reason': 'Untrusted self-report', 'evidence': [job_id], 'structure_checks': {'version': 1}})
        with self.assertRaisesRegex(ValueError, 'full-design structure coverage'):
            self.call(action='rtl_review_decision', review=self.review['review'], report_path='review.json', expected_verdict='final')
        report = self.report([{'job_id': job_id, 'configuration_sha256': '0' * 64}])
        with self.assertRaisesRegex(ValueError, 'ordinary submit job'):
            self.validate(report)
        self.assertFalse(json.loads((self.out / self.review['inventory_path']).read_text())['decision'])

    def test_all_native_configurations_required_and_latest_attempt_cannot_be_hidden(self):
        declaration = json.loads((self.work / 'RTL_STRUCTURE.json').read_text())
        declaration['configurations'].append({'id': 'wide', 'top': 'design', 'sources': ['design.sv'], 'parameters': {'WIDTH': '8'}})
        worker.save(self.work / 'RTL_STRUCTURE.json', declaration)
        self.review = self.call(action='rtl_review_begin'); self.metadata = json.loads((self.out / self.review['inventory_path']).read_text()); self.audit = self.out / self.review['audit_scope']
        first = self.enqueue(); self.complete(first)
        second = self.enqueue(parameters={'WIDTH': '8'}); self.complete(second)
        with self.assertRaisesRegex(ValueError, 'omits a native checked configuration'):
            self.validate(self.report([first]))
        self.assertEqual(len(self.validate(self.report([first, second]))['checks']), 2)
        retry = self.enqueue(); self.complete(retry, warning='Warning: Wire design.\\missing is used but has no driver.')
        with self.assertRaisesRegex(ValueError, 'latest native check'):
            self.validate(self.report([first, second]))
        with self.assertRaisesRegex(ValueError, 'unresolved used-undriven'):
            self.validate(self.report([retry, second]))

    def test_partial_used_bits_fail_until_exact_independent_waiver(self):
        queued = self.enqueue(); checked = self.complete(queued, warning='Warning: Wire design.\\wide [7] is used but has no driver.')
        report = self.report([queued])
        with self.assertRaisesRegex(ValueError, 'unresolved used-undriven'):
            self.validate(report)
        probe = self.call(action='submit', scope=self.review['audit_scope'], argv=['python', '-c', 'pass'])
        self.receipts(probe['job_id']); report['evidence'].append(probe['job_id'])
        waiver = {'job_id': queued['job_id'], 'finding_id': checked['diagnostics']['findings'][0]['finding_id'],
                  'reason': 'Synthetic disposition contract, independently assessed in a separate unit probe.',
                  'source_refs': self.references(), 'evidence': [probe['job_id']]}
        report['structure_checks']['waivers'] = [waiver]
        self.assertEqual(self.validate(report)['waived_findings'], 1)
        for field, replacement, pattern in [('finding_id', '0' * 64, 'exact finding'),
                                              ('reason', '', 'engineering reason'),
                                              ('evidence', [queued['job_id']], 'own waiver'),
                                              ('source_refs', [], 'source references')]:
            invalid = copy.deepcopy(report); invalid['structure_checks']['waivers'][0][field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, pattern):
                self.validate(invalid)

    def test_unknown_warning_or_unusable_tool_state_cannot_be_waived(self):
        for kind in ('unknown', 'timed_out', 'unsupported', 'tool_failure', 'invalid'):
            with self.subTest(kind=kind):
                queued = self.enqueue(parameters={'VARIANT': str(len(list((self.out / 'jobs').glob('*'))))})
                self.complete(queued, warning='Warning: Unclassified tool diagnostic.' if kind == 'unknown' else None,
                              status=None if kind == 'unknown' else kind)
                report = self.report([queued])
                with self.assertRaisesRegex(ValueError, 'unclassified|usable result'):
                    gate.native_check(self.out, self.metadata, queued['job_id'], expected_closure=self.metadata['work_inventory'])

    def test_coverage_requires_actual_bound_sources_not_summary(self):
        queued = self.enqueue(); self.complete(queued); report = self.report([queued])
        for change in ('missing_input', 'stale_sha', 'unfrozen_reference', 'missing_reason'):
            invalid = copy.deepcopy(report); coverage = invalid['structure_checks']['coverage']
            if change == 'missing_input': coverage['source_refs'] = coverage['source_refs'][1:]
            elif change == 'stale_sha': coverage['source_refs'][0]['sha256'] = '0' * 64
            elif change == 'unfrozen_reference': coverage['source_refs'][0]['path'] = '@run/work/design.sv'
            else: coverage['reason'] = ''
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(invalid)

    def test_native_raw_transcript_and_host_receipts_cannot_be_faked_or_truncated(self):
        queued = self.enqueue(); self.complete(queued); report = self.report([queued]); job = self.out / 'jobs' / queued['job_id']
        old = (job / 'stdout.log').read_bytes()
        checked = json.loads(old); checked['execution']['stdout'] += 'Warning: hidden defect\n'
        worker.save(job / 'stdout.log', checked)
        with self.assertRaisesRegex(ValueError, 'transcript identity'):
            self.validate(report)
        (job / 'stdout.log').write_bytes(old)
        (job / 'host-launch.json').unlink()
        with self.assertRaisesRegex(ValueError, 'actual host-launched'):
            self.validate(report)

    def test_known_memory_lowering_requires_exact_independent_disposition(self):
        queued = self.enqueue()
        checked = self.complete(queued, warning='Warning: Replacing memory \\scratch with list of registers. See design.sv:4')
        self.assertEqual(checked['diagnostics']['unclassified_warning_count'], 0)
        report = self.report([queued])
        with self.assertRaisesRegex(ValueError, 'unresolved used-undriven/structural'):
            self.validate(report)
        support = self.call(action='submit', scope=self.review['audit_scope'], argv=['python', '-c', 'pass'])
        self.receipts(support['job_id']); report['evidence'].append(support['job_id'])
        report['structure_checks']['waivers'] = [{
            'job_id': queued['job_id'], 'finding_id': checked['diagnostics']['findings'][0]['finding_id'],
            'reason': 'Synthetic independently assessed register-lowering disposition, not a project waiver.',
            'source_refs': self.references(), 'evidence': [support['job_id']]}]
        self.assertEqual(self.validate(report)['waived_findings'], 1)

    def test_large_native_report_status_exposes_bounded_early_findings(self):
        queued = self.enqueue(); checked = self.complete(queued, warning='Warning: Wire design.\\missing is used but has no driver.')
        checked['execution']['stdout'] += 'large complete tool transcript\n' * 100000
        original = checked['diagnostics']['findings'][0]
        checked['diagnostics']['findings'] = [dict(original, message='early finding ' + str(i) + ': ' + 'x' * 50000) for i in range(20)]
        worker.save(self.out / 'jobs' / queued['job_id'] / 'stdout.log', checked)
        request = json.loads((self.out / 'jobs' / queued['job_id'] / 'request.json').read_text())
        summary = gate.job_summary(self.out / 'jobs' / queued['job_id'], request)
        self.assertEqual(summary['status'], 'findings')
        self.assertEqual(summary['counts']['findings'], 20)
        self.assertEqual(len(summary['findings']), 8); self.assertTrue(summary['findings_truncated'])
        self.assertTrue(summary['findings'][0]['message'].startswith('early finding 0:'))
        self.assertLess(len(json.dumps(summary).encode()), 10000)
        status = self.call(action='status', job_id=queued['job_id'], brief=True)
        self.assertEqual(status['status'], 'completed')
        self.assertEqual(status['exit_code'], 1)
        self.assertEqual(status['structure_check']['status'], 'findings')
        self.assertLess(len(json.dumps(status).encode()), 11000)

    def test_decided_structure_logs_keep_original_byte_identity(self):
        queued = self.enqueue(); self.complete(queued); report = self.report([queued])
        worker.save(self.audit / 'review.json', report)
        self.call(action='rtl_review_decision', review=self.review['review'], report_path='review.json', expected_verdict='final')
        path = self.out / 'jobs' / queued['job_id'] / 'stdout.log'
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'decided RTL structure evidence changed'):
            self.call(action='rtl_review_status')

    def test_wrong_top_or_unexecuted_required_configuration_cannot_claim_full_design(self):
        wrong = self.enqueue(top='leaf'); self.complete(wrong)
        with self.assertRaisesRegex(ValueError, 'required declared top/configuration'):
            self.validate(self.report([wrong]))

    def test_frozen_compile_declaration_missing_or_stale_is_not_optional(self):
        # Start a separate candidate from a work tree that genuinely lacks the
        # declaration; never remove it from a frozen candidate to test admission.
        (self.work / 'RTL_STRUCTURE.json').unlink()
        self.review = self.call(action='rtl_review_begin'); self.metadata = json.loads((self.out / self.review['inventory_path']).read_text()); self.audit = self.out / self.review['audit_scope']
        queued = self.enqueue(); self.complete(queued)
        report = {'structure_checks': {'version': 1, 'coverage': {'claim': 'full_design', 'reason': 'Missing required declaration.'}, 'required_configurations': []}}
        with self.assertRaisesRegex(ValueError, 'RTL_STRUCTURE.json compile declaration'):
            self.validate(report)

    def test_source_backed_declared_boxes_are_exact_and_top_cannot_be_a_box(self):
        for kind in ('blackbox', 'whitebox'):
            with self.subTest(kind=kind):
                declaration = json.loads((self.work / 'RTL_STRUCTURE.json').read_text())
                declaration['blackboxes'] = [{
                    'module': 'memory_wrapper', 'kind': kind,
                    'reason': 'Synthetic source-authorized memory interface, not a functional/physical sign-off.',
                    'source_refs': [
                        {'path': '@input/spec.md', 'sha256': worker.sha(self.out / 'snapshot/input/spec.md')},
                        {'path': '@candidate/design.sv', 'sha256': worker.sha(self.work / 'design.sv')},
                        {'path': '@architecture/architecture.md', 'sha256': worker.sha(self.out / 'snapshot/architecture/architecture.md')}]}]
                worker.save(self.work / 'RTL_STRUCTURE.json', declaration)
                self.review = self.call(action='rtl_review_begin'); self.metadata = json.loads((self.out / self.review['inventory_path']).read_text()); self.audit = self.out / self.review['audit_scope']
                queued = self.enqueue(); self.complete(queued, **{kind: ['memory_wrapper']})
                proof = self.validate(self.report([queued]))
                self.assertEqual(proof['compile_contract']['allowed_modules'][kind + '_modules'], ['memory_wrapper'])
        declaration['blackboxes'][0]['module'] = 'design'
        worker.save(self.work / 'RTL_STRUCTURE.json', declaration)
        self.review = self.call(action='rtl_review_begin'); self.metadata = json.loads((self.out / self.review['inventory_path']).read_text()); self.audit = self.out / self.review['audit_scope']
        queued = self.enqueue(); self.complete(queued, whitebox=['design'])
        with self.assertRaisesRegex(ValueError, 'top cannot itself'):
            self.validate(self.report([queued]))

    def test_undeclared_actual_box_or_incomplete_inventory_is_rejected(self):
        queued = self.enqueue(); self.complete(queued, blackbox=['undeclared_memory'])
        with self.assertRaisesRegex(ValueError, 'actual black/white-box modules differ'):
            self.validate(self.report([queued]))
        job = self.out / 'jobs' / queued['job_id']; checked = json.loads((job / 'stdout.log').read_text())
        checked['compile']['module_inventory_complete'] = False; worker.save(job / 'stdout.log', checked)
        with self.assertRaisesRegex(ValueError, 'module inventory differs'):
            self.validate(self.report([queued]))

    def test_abnormal_tool_exit_cannot_become_a_waivable_finding(self):
        queued = self.enqueue(); self.complete(queued, warning='Warning: Wire design.\\missing is used but has no driver.')
        path = self.out / 'jobs' / queued['job_id'] / 'stdout.log'
        original = json.loads(path.read_text())
        for code in (-11, 2, 139):
            with self.subTest(code=code):
                invalid = copy.deepcopy(original); invalid['execution']['exit_code'] = code; worker.save(path, invalid)
                with self.assertRaisesRegex(ValueError, 'execution status differs'):
                    self.validate(self.report([queued]))

    def test_cleanup_failure_blocks_structure_acceptance(self):
        queued = self.enqueue(); self.complete(queued); job = self.out / 'jobs' / queued['job_id']
        result = json.loads((job / 'result.json').read_text()); result['isolation']['cleanup_complete'] = False
        worker.save(job / 'result.json', result)
        with self.assertRaisesRegex(ValueError, 'clean isolation'):
            self.validate(self.report([queued]))

    def test_missing_configured_lint_tool_is_explicitly_unclosed_without_queue_or_path_fallback(self):
        self.manifest['settings']['tools'].pop('verilator')
        worker.save(self.out / 'manifest.json', self.manifest)
        with self.assertRaisesRegex(ValueError, 'unclosed.*Verilator'):
            self.enqueue()
        self.assertFalse(list((self.out / 'jobs').glob('*')))

    def test_fast_lint_missing_partial_or_forged_identity_cannot_be_replaced_by_clean_yosys(self):
        queued = self.enqueue(); original = self.complete(queued)
        path = self.out / 'jobs' / queued['job_id'] / 'stdout.log'
        changes = [
            ('missing', lambda x: x.pop('fast_lint')),
            ('not_completed', lambda x: x['fast_lint'].update(completed=False)),
            ('timeout', lambda x: x['fast_lint']['execution'].update(timed_out=True)),
            ('abnormal_exit', lambda x: x['fast_lint']['execution'].update(exit_code=139)),
            ('partial_pipe', lambda x: x['fast_lint']['execution'].update(capture_complete=False)),
            ('wrong_tool', lambda x: x['fast_lint']['tool'].update(path='/model-selected/lint')),
            ('changed_tool', lambda x: x['fast_lint']['tool'].update(binary_stable=False)),
            ('wrong_config', lambda x: x['fast_lint'].update(config_sha256='0' * 64)),
            ('wrong_compile_identity', lambda x: x['fast_lint'].update(compile_config_sha256='0' * 64)),
            ('missing_macro_profile', lambda x: x['fast_lint'].pop('profile')),
            ('wrong_macro_profile', lambda x: x['fast_lint']['profile']['effective_defines'].update(SYNTHESIS='0')),
            ('wrong_top', lambda x: x['fast_lint']['compile'].update(top='leaf')),
            ('missing_sources', lambda x: x['fast_lint']['compile'].update(sources=[])),
            ('raw_log_spoof', lambda x: x['fast_lint']['execution'].update(stderr='%Error: hidden\n')),
            ('diagnostic_spoof', lambda x: x['fast_lint']['diagnostics'].update(errors=['invented'])),
            ('argument_spoof', lambda x: x['fast_lint']['execution']['argv'].append('-Wno-UNDRIVEN')),
        ]
        for label, mutate in changes:
            with self.subTest(label=label):
                checked = copy.deepcopy(original); mutate(checked); worker.save(path, checked)
                with self.assertRaises(ValueError):
                    self.validate(self.report([queued]))

    def test_complete_lint_cannot_replace_missing_or_incomplete_yosys(self):
        queued = self.enqueue(); original = self.complete(queued)
        path = self.out / 'jobs' / queued['job_id'] / 'stdout.log'
        for label, mutate in [
            ('missing', lambda x: x.update(execution=None)),
            ('partial_pipe', lambda x: x['execution'].update(capture_complete=False)),
            ('wrong_script_path', lambda x: x['execution']['argv'].__setitem__(2, '/different/check.ys')),
            ('stale_source', lambda x: x.update(source_stable=False)),
        ]:
            with self.subTest(label=label):
                checked = copy.deepcopy(original); mutate(checked); worker.save(path, checked)
                with self.assertRaises(ValueError):
                    self.validate(self.report([queued]))

    def test_execution_plan_is_native_top_level_intent_bound_to_the_protected_config(self):
        for plan in (None, 'complete', 'diagnostic_first'):
            request = {'action': 'structure_check', 'scope': self.review['audit_scope'],
                       'review': self.review['review'], 'timeout_seconds': 10,
                       'configuration': {'top': 'design', 'sources': ['design.sv']}}
            if plan is not None: request['execution_plan'] = plan
            queued = self.call(**request)
            job = self.out / 'jobs' / queued['job_id']
            config = json.loads((job / 'structure-config.json').read_text())
            bound = json.loads((job / 'request.json').read_text())['structure_check']
            self.assertEqual(config.get('execution_plan'), plan)
            self.assertEqual(bound['config_sha256'], executor.digest(config))
            self.assertEqual(bound['config_file_sha256'], worker.sha(job / 'structure-config.json'))
            if plan is None: self.assertNotIn('execution_plan', config)
            self.complete(queued)
        with self.assertRaisesRegex(ValueError, 'execution_plan'):
            self.call(**dict(request, execution_plan='lint_only'))
        with self.assertRaisesRegex(ValueError, 'compile configuration'):
            self.enqueue(execution_plan='diagnostic_first')

    def test_diagnostic_only_lint_evidence_cannot_pass_even_with_an_exact_waiver(self):
        queued = self.call(action='structure_check', scope=self.review['audit_scope'],
                           review=self.review['review'], execution_plan='diagnostic_first',
                           configuration={'top': 'design', 'sources': ['design.sv']}, timeout_seconds=10)
        checked = self.complete(queued, lint_warning='%Warning-UNDRIVEN: design.sv:1: synthetic unused interface bit')
        checked.update(execution=None, ok=False, coverage={'kind': 'diagnostic_only', 'fast_lint': 'complete',
                       'bitwise': 'unclosed', 'yosys': 'not_run', 'structural_release': False})
        worker.save(self.out / 'jobs' / queued['job_id'] / 'stdout.log', checked)
        support = self.call(action='submit', scope=self.review['audit_scope'], argv=['python', '-c', 'pass'])
        self.receipts(support['job_id'])
        report = self.report([queued]); report['evidence'].append(support['job_id'])
        report['structure_checks']['waivers'] = [{
            'job_id': queued['job_id'], 'finding_id': checked['fast_lint']['diagnostics']['findings'][0]['finding_id'],
            'reason': 'Synthetic exact disposition cannot supply missing structural execution.',
            'source_refs': self.references(), 'evidence': [support['job_id']]}]
        with self.assertRaisesRegex(ValueError, 'Yosys invocation|Yosys execution'):
            self.validate(report)

    def test_diagnostic_only_summary_keeps_lint_findings_and_unknown_yosys_totals(self):
        queued = self.call(action='structure_check', scope=self.review['audit_scope'],
                           review=self.review['review'], execution_plan='diagnostic_first',
                           configuration={'top': 'design', 'sources': ['design.sv']}, timeout_seconds=10)
        warning = '\n'.join(f'%Warning-UNDRIVEN: design.sv:1: synthetic signal{i}' for i in range(4))
        checked = self.complete(queued, lint_warning=warning)
        checked.update(execution=None, ok=False, coverage={'kind': 'diagnostic_only', 'fast_lint': 'complete',
                       'bitwise': 'unclosed', 'yosys': 'not_run', 'structural_release': False})
        job = self.out / 'jobs' / queued['job_id']
        worker.save(job / 'stdout.log', checked)
        summary = gate.job_summary(job, json.loads((job / 'request.json').read_text()))
        self.assertEqual(summary['status'], 'findings')
        self.assertEqual(summary['yosys'], {'status': 'not_run', 'coverage': 'unclosed'})
        self.assertTrue(all(v is None for v in summary['counts'].values()))
        self.assertIsNone(summary['unclassified_warning_count'])
        self.assertTrue(summary['fast_lint']['completed'])
        self.assertEqual(summary['fast_lint']['counts']['findings'], 4)
        self.assertEqual(len(summary['fast_lint']['findings']), 4)
        self.assertEqual(summary['fast_lint']['status'], 'findings')

    def test_lint_target_with_clean_full_yosys_requires_exact_independent_disposition(self):
        queued = self.enqueue()
        checked = self.complete(queued, lint_warning='%Warning-UNDRIVEN: design.sv:1:1: Synthetic unused interface bit')
        self.assertEqual(checked['execution']['exit_code'], 0)
        self.assertIn(executor.COMPLETE_MARKER, checked['execution']['stdout'])
        report = self.report([queued])
        with self.assertRaisesRegex(ValueError, 'unresolved used-undriven/structural'):
            self.validate(report)
        support = self.call(action='submit', scope=self.review['audit_scope'], argv=['python', '-c', 'pass'])
        self.receipts(support['job_id']); report['evidence'].append(support['job_id'])
        finding = checked['fast_lint']['diagnostics']['findings'][0]
        report['structure_checks']['waivers'] = [{
            'job_id': queued['job_id'], 'finding_id': finding['finding_id'],
            'reason': 'Synthetic source-backed interface disposition independently checked; no general undriven waiver.',
            'source_refs': self.references(), 'evidence': [support['job_id']]}]
        proof = self.validate(report)
        self.assertEqual(proof['waived_findings'], 1)
        self.assertEqual(proof['checks'][0]['fast_lint']['status'], 'findings')
        self.assertEqual(proof['checks'][0]['status'], 'findings')

    def test_live_lint_diagnostics_are_bounded_and_never_complete_native_evidence(self):
        queued = self.enqueue(); job = self.out / 'jobs' / queued['job_id']
        (job / 'stdout.log').write_bytes(b'')
        (job / 'stderr.log').write_text(
            'HARNESS_RTL_STRUCTURE_FAST_LINT_BEGIN_V2\n'
            '%Warning-UNDRIVEN: design.sv:1:1: Early synthetic diagnostic\n'
            '  1 | wire missing;\n'
            'HARNESS_RTL_STRUCTURE_FAST_LINT_END_V2\n'
            'HARNESS_RTL_STRUCTURE_YOSYS_BEGIN_V2\n' + 'Later tool output\n' * 100000)
        request = json.loads((job / 'request.json').read_text())
        summary = gate.job_summary(job, request)
        self.assertFalse(summary['available']); self.assertEqual(summary['status'], 'pending_result')
        live = summary['live_diagnostics']
        self.assertTrue(live['provisional']); self.assertFalse(live['acceptance_evidence'])
        self.assertEqual(live['phase'], 'yosys'); self.assertEqual(live['counts']['findings'], 1)
        self.assertTrue(live['counts_complete']); self.assertEqual(live['observed_counts'], live['counts'])
        self.assertTrue(live['prefix_truncated']); self.assertLess(len(json.dumps(summary)), 10000)
        with self.assertRaisesRegex(ValueError, 'missing or unfinished'):
            self.validate(self.report([queued]))
        self.receipts(queued['job_id'], 137)
        self.assertEqual(gate.job_summary(job, request)['status'], 'invalid_result')

    def test_large_lint_preview_does_not_turn_unseen_findings_into_zero(self):
        queued = self.enqueue(); job = self.out / 'jobs' / queued['job_id']
        (job / 'stdout.log').write_bytes(b'')
        (job / 'stderr.log').write_text(
            'HARNESS_RTL_STRUCTURE_FAST_LINT_BEGIN_V2\n' +
            '%Warning-WIDTHEXPAND: design.sv:1:1: Synthetic width context\n' * 12000 +
            '%Warning-UNDRIVEN: design.sv:2:1: Later synthetic driver diagnostic\n' +
            'HARNESS_RTL_STRUCTURE_FAST_LINT_END_V2\n' +
            'HARNESS_RTL_STRUCTURE_YOSYS_BEGIN_V2\n')
        request = json.loads((job / 'request.json').read_text())
        summary = gate.job_summary(job, request); live = summary['live_diagnostics']
        self.assertFalse(summary['available']); self.assertTrue(live['prefix_truncated'])
        self.assertFalse(live['fast_lint_end_observed']); self.assertFalse(live['counts_complete'])
        self.assertEqual(live['counts'], {'warnings': None, 'errors': None, 'findings': None})
        self.assertEqual(live['observed_counts']['findings'], 0)
        self.assertEqual(live['phase'], 'unknown'); self.assertLess(len(json.dumps(summary)), 10000)
        complete = executor.parse_lint_diagnostics('', (job / 'stderr.log').read_text(),
                                                  request['structure_check']['config_sha256'])
        self.assertEqual(len(complete['findings']), 1)
        with self.assertRaisesRegex(ValueError, 'missing or unfinished'):
            self.validate(self.report([queued]))

    def test_live_lint_without_end_marker_keeps_positive_findings_but_unknown_totals(self):
        queued = self.enqueue(); job = self.out / 'jobs' / queued['job_id']
        (job / 'stderr.log').write_text(
            'HARNESS_RTL_STRUCTURE_FAST_LINT_BEGIN_V2\n'
            '%Warning-UNDRIVEN: design.sv:1:1: An observed synthetic diagnostic\n')
        request = json.loads((job / 'request.json').read_text())
        live = gate.job_summary(job, request)['live_diagnostics']
        self.assertFalse(live['prefix_truncated']); self.assertFalse(live['counts_complete'])
        self.assertIsNone(live['counts']['findings']); self.assertEqual(live['observed_counts']['findings'], 1)
        self.assertEqual(len(live['findings']), 1); self.assertFalse(live['acceptance_evidence'])

    def test_completed_clean_lint_can_report_zero_without_becoming_native_pass(self):
        queued = self.enqueue(); job = self.out / 'jobs' / queued['job_id']
        (job / 'stderr.log').write_text(
            'HARNESS_RTL_STRUCTURE_FAST_LINT_BEGIN_V2\n'
            'HARNESS_RTL_STRUCTURE_FAST_LINT_END_V2\n'
            'HARNESS_RTL_STRUCTURE_YOSYS_BEGIN_V2\n')
        request = json.loads((job / 'request.json').read_text())
        summary = gate.job_summary(job, request); live = summary['live_diagnostics']
        self.assertTrue(live['counts_complete']); self.assertEqual(live['counts']['findings'], 0)
        self.assertFalse(summary['available']); self.assertFalse(live['acceptance_evidence'])

    def test_end_marker_before_begin_does_not_complete_the_current_lint(self):
        queued = self.enqueue(); job = self.out / 'jobs' / queued['job_id']
        (job / 'stderr.log').write_text(
            'HARNESS_RTL_STRUCTURE_FAST_LINT_END_V2\n'
            'HARNESS_RTL_STRUCTURE_FAST_LINT_BEGIN_V2\n'
            '%Warning-UNDRIVEN: design.sv:1:1: Current stage remains incomplete\n')
        request = json.loads((job / 'request.json').read_text())
        live = gate.job_summary(job, request)['live_diagnostics']
        self.assertFalse(live['counts_complete']); self.assertIsNone(live['counts']['findings'])
        self.assertEqual(live['observed_counts']['findings'], 1)

    def test_marker_substring_in_a_diagnostic_does_not_complete_lint(self):
        queued = self.enqueue(); job = self.out / 'jobs' / queued['job_id']
        (job / 'stderr.log').write_text(
            'HARNESS_RTL_STRUCTURE_FAST_LINT_BEGIN_V2\n'
            '%Warning-WIDTHEXPAND: design.sv:1:1: Message mentions HARNESS_RTL_STRUCTURE_FAST_LINT_END_V2\n' +
            'Ordinary context\n' * 40000 +
            '%Warning-UNDRIVEN: design.sv:2:1: A later diagnostic\n'
            'HARNESS_RTL_STRUCTURE_FAST_LINT_END_V2\n')
        request = json.loads((job / 'request.json').read_text())
        live = gate.job_summary(job, request)['live_diagnostics']
        self.assertTrue(live['prefix_truncated']); self.assertFalse(live['counts_complete'])
        self.assertIsNone(live['counts']['findings']); self.assertEqual(live['observed_counts']['findings'], 0)

    def test_only_standalone_begin_and_ordered_crlf_stage_markers_are_used(self):
        queued = self.enqueue(); job = self.out / 'jobs' / queued['job_id']
        request = json.loads((job / 'request.json').read_text())
        (job / 'stderr.log').write_text('Message mentions HARNESS_RTL_STRUCTURE_FAST_LINT_BEGIN_V2\n')
        self.assertNotIn('live_diagnostics', gate.job_summary(job, request))
        (job / 'stderr.log').write_bytes(
            b'HARNESS_RTL_STRUCTURE_FAST_LINT_BEGIN_V2\r\n'
            b'%Warning-UNDRIVEN: design.sv:1:1: Observed diagnostic\r\n'
            b'HARNESS_RTL_STRUCTURE_FAST_LINT_END_V2\r\n'
            b'HARNESS_RTL_STRUCTURE_YOSYS_BEGIN_V2\r\n')
        live = gate.job_summary(job, request)['live_diagnostics']
        self.assertTrue(live['counts_complete']); self.assertEqual(live['counts']['findings'], 1)
        self.assertEqual(live['phase'], 'yosys')

    def test_prefix_eof_cannot_terminate_a_partial_stage_marker_line(self):
        queued = self.enqueue(); job = self.out / 'jobs' / queued['job_id']
        request = json.loads((job / 'request.json').read_text())
        begin = 'HARNESS_RTL_STRUCTURE_FAST_LINT_BEGIN_V2\n'
        end = 'HARNESS_RTL_STRUCTURE_FAST_LINT_END_V2'
        maximum = 512 * 1024
        log = (begin + 'x' * (maximum - len(begin) - len(end) - 1) + '\n' + end +
               '_NOT_A_STAGE\n%Warning-UNDRIVEN: design.sv:2:1: Beyond the preview\n' + end + '\n')
        self.assertEqual(log[:maximum][-len(end):], end)
        (job / 'stderr.log').write_text(log)
        live = gate.job_summary(job, request)['live_diagnostics']
        self.assertTrue(live['prefix_truncated']); self.assertFalse(live['counts_complete'])
        self.assertIsNone(live['counts']['findings'])
        # The trusted writer terminates marker lines with LF/CRLF. An
        # in-flight marker without that terminator is still incomplete.
        (job / 'stderr.log').write_text(begin + end)
        live = gate.job_summary(job, request)['live_diagnostics']
        self.assertFalse(live['counts_complete']); self.assertIsNone(live['counts']['findings'])

    def work_check(self):
        return self.call(action='structure_check', scope='work',
                         configuration={'top': 'design', 'sources': ['design.sv']}, timeout_seconds=10)

    def test_native_scope_blocks_edits_submits_and_duplicate_check_until_terminal_cleanup(self):
        queued = self.work_check(); job = self.out / 'jobs' / queued['job_id']
        edit = {'scope': 'work', 'path': 'design.sv', 'base_sha256': worker.sha(self.work / 'design.sv'),
                'content': 'module design; endmodule\n'}
        for action in ('write', 'replace', 'submit'):
            request = {'action': action, **edit}
            if action == 'replace':request.update(old='assign y=a;', content='assign y=~a;')
            if action == 'submit':request = {'action': action, 'scope': 'work', 'argv': ['python', '-c', 'pass']}
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'wait.*terminal cleanup'):
                self.call(**request)
        with self.assertRaisesRegex(ValueError, 'requires.*idle'):
            self.work_check()
        result = self.receipts(queued['job_id'], 137)
        result['isolation']['cleanup_complete'] = False; worker.save(job / 'result.json', result)
        with self.assertRaisesRegex(ValueError, 'terminal cleanup'):
            self.call(action='write', scope='work', path='note.txt', base_sha256=None, content='after cleanup only')
        result['isolation']['cleanup_complete'] = True; worker.save(job / 'result.json', result)
        result['container_running'] = True; worker.save(job / 'result.json', result)
        with self.assertRaisesRegex(ValueError, 'terminal cleanup'):
            self.call(action='submit', scope='work', argv=['python', '-c', 'pass'])
        result['container_running'] = False; worker.save(job / 'result.json', result)
        written = self.call(action='write', scope='work', path='note.txt', base_sha256=None, content='Now terminal')
        self.assertEqual(written['path'], 'note.txt')
        self.assertEqual(self.call(action='submit', scope='work', argv=['python', '-c', 'pass'])['status'], 'queued')

    def test_native_admission_waits_for_existing_writer_and_does_not_block_reads_or_status(self):
        writing = self.call(action='submit', scope='work', argv=['python', '-c', 'pass'])
        with self.assertRaisesRegex(ValueError, 'requires.*idle'):
            self.work_check()
        self.receipts(writing['job_id'])
        checked = self.work_check()
        self.assertIn('assign y=a;', self.call(action='read', scope='work', path='design.sv')['content'])
        self.assertEqual(self.call(action='status', job_id=checked['job_id'], brief=True)['status'], 'running')
        # Cancel is routed to the configured host launcher, never rejected by
        # the source-closure admission lock. Remote routing is tested separately.
        with self.assertRaisesRegex(ValueError, 'cancel jobs through the configured host'):
            self.call(action='cancel', job_id=checked['job_id'])

    def test_native_scope_does_not_stop_unrelated_scope_and_compares_canonical_parent_child_ranges(self):
        checked = self.enqueue()
        self.assertEqual(self.call(action='write', scope='work', path='unrelated.txt', base_sha256=None,
                                   content='A different writable source closure')['scope'], 'work')
        with self.assertRaisesRegex(ValueError, 'terminal cleanup'):
            self.call(action='write', scope=self.review['audit_scope'], path='unsafe.txt', base_sha256=None, content='x')
        self.assertEqual(worker.scope_pending_jobs(self.out, 'work', native_only=True), [])
        nested = self.out / 'jobs' / '99'; nested.mkdir()
        worker.save(nested / 'request.json', {'scope': 'work/child', 'source_dir': str(self.work / 'child'),
                                            'structure_check': {'version': 2}})
        self.assertIn('99', worker.scope_pending_jobs(self.out, 'work', native_only=True))
        self.assertNotIn('99', worker.scope_pending_jobs(self.out, 'work-other', native_only=True))

    def test_same_scope_concurrent_queue_and_edit_do_not_both_pass_admission(self):
        original = worker._dispatch
        entered, release, writer_done = threading.Event(), threading.Event(), threading.Event()
        outcomes = {}
        def paused(root, run_id, request):
            if request['action'] == 'structure_check':
                entered.set()
                if not release.wait(3):raise AssertionError('unit admission barrier expired')
            return original(root, run_id, request)
        def native():
            try:outcomes['native'] = self.work_check()
            except Exception as error:outcomes['native_error'] = error
        def writer():
            try:outcomes['writer'] = self.call(action='write', scope='work', path='racing.txt', base_sha256=None, content='x')
            except Exception as error:outcomes['writer_error'] = error
            finally:writer_done.set()
        with patch.object(worker, '_dispatch', side_effect=paused):
            first = threading.Thread(target=native, daemon=True); first.start()
            self.assertTrue(entered.wait(2))
            second = threading.Thread(target=writer, daemon=True); second.start()
            try:self.assertFalse(writer_done.wait(.05))
            finally:release.set()
            first.join(4); second.join(4)
        self.assertFalse(first.is_alive()); self.assertFalse(second.is_alive())
        self.assertNotIn('native_error', outcomes)
        self.assertEqual(outcomes['native']['status'], 'queued')
        self.assertIsInstance(outcomes.get('writer_error'), ValueError)
        self.assertIn('source closure', str(outcomes['writer_error']))
        self.assertFalse((self.work / 'racing.txt').exists())


if __name__ == '__main__':
    unittest.main()
