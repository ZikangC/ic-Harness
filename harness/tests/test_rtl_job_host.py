"""Focused host orchestration unit tests; no Docker/model/project execution."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / 'runtime/rtl_job_host.py'
spec = importlib.util.spec_from_file_location('rtl_job_host_unit', str(MODULE))
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class HostJobs(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / 'project'
        self.run = self.project / 'output/run1'
        self.job = self.run / 'jobs/123'
        self.source = self.run / 'audit/review-2'
        self.job.mkdir(parents=True)
        self.source.mkdir(parents=True)
        self.worker = self.run / 'snapshot/harness/runtime/rtl_worker.py'
        self.worker.parent.mkdir(parents=True)
        self.worker.write_text('# fake control worker\n')
        self.helper = self.worker.with_name('rtl_job_host.py')
        self.helper.write_bytes(MODULE.read_bytes())
        self.payload = dict(action='launch', control_container='control', host_workspace_root=str(self.root),
                            container_workspace_root='/workspace', host_project_root=str(self.project),
                            container_project_root='/workspace/project', worker='/workspace/project/output/run1/snapshot/harness/runtime/rtl_worker.py',
                            eda_python='/opt/python', run_id='run1', job_id='123')
        self.isolation = dict(backend=host.BACKEND, control_container_id='a' * 64,
                              image_id='sha256:' + 'b' * 64, uid=os.getuid() or 1000, gid=os.getgid(),
                              host_workspace_root=str(self.root), container_workspace_root='/workspace')
        self.request = dict(job_isolation=self.isolation, argv=['/opt/python', 'test.py'],
                            cwd='/workspace/project/output/run1/audit/review-2',
                            run_dir='/workspace/project/output/run1', scope='audit/review-2', timeout_seconds=20,
                            source_dir='/workspace/project/output/run1/audit/review-2', source_sha256={}, candidate_sha256='c' * 64)
        self.manifest = dict(job_isolation=self.isolation, settings={'tools': {'python': '/opt/python'}}, harness_sha256={
            'runtime/rtl_worker.py': host.sha(self.worker), 'runtime/rtl_job_host.py': host.sha(self.helper)})
        host.save(self.run / 'manifest.json', self.manifest)
        host.save(self.job / 'request.json', self.request)
        self.module_patch = mock.patch.object(host, '__file__', str(self.helper))
        self.module_patch.start()
        self.spec_patch = mock.patch.object(host, 'control_spec', return_value=self.isolation)
        self.spec_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.addCleanup(self.spec_patch.stop)
        self.addCleanup(self.temporary.cleanup)

    def context(self):
        return host.load_context(self.payload)

    def intent(self):
        host.save(self.job / 'host-launch.intent.json', {'nonce': 'n' * 64}, exclusive=True)

    def info(self, context):
        return dict(Id='d' * 64, Image=self.isolation['image_id'],
                    Config={'Labels': host.labels(context), 'User': '{0}:{1}'.format(self.isolation['uid'], self.isolation['gid'])},
                    HostConfig={'ReadonlyRootfs': True, 'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges']},
                    State={'Running': False, 'ExitCode': 0}, Mounts=[
                        {'Source': str(self.root), 'Destination': '/workspace', 'RW': False},
                        {'Source': str(self.source), 'Destination': self.request['source_dir'], 'RW': True},
                        {'Source': str(self.job / 'private-tmp'), 'Destination': '/tmp', 'RW': True},
                        {'Source': str(self.job / 'private-tmp'), 'Destination': '/var/tmp', 'RW': True}])

    def test_exact_manifest_and_frozen_helper(self):
        self.assertEqual(self.context()['host_source'], self.source)
        self.helper.write_text('different')
        with self.assertRaisesRegex(ValueError, 'helper changed'):
            self.context()

    def test_changed_spec_fails_before_target(self):
        self.request['job_isolation'] = dict(self.isolation, image_id='sha256:' + 'e' * 64)
        host.save(self.job / 'request.json', self.request)
        with self.assertRaisesRegex(ValueError, 'manifest mismatch'):
            self.context()

    def test_scope_traversal_and_wrong_cwd_rejected(self):
        for change in ({'scope': 'audit/review-2/../../jobs'}, {'cwd': '/workspace/project/output/run1/jobs'}):
            request = dict(self.request, **change)
            host.save(self.job / 'request.json', request)
            with self.assertRaises(ValueError):
                self.context()

    def test_readiness_roles_mount_only_the_exact_campaign_scope(self):
        for leaf in ('work', 'audit'):
            self.source = self.run / 'readiness/campaign-1' / leaf
            self.source.mkdir(parents=True, exist_ok=True)
            scope = 'readiness/campaign-1/' + leaf
            target = '/workspace/project/output/run1/' + scope
            self.request.update(scope=scope, cwd=target, source_dir=target)
            host.save(self.job / 'request.json', self.request)
            self.assertEqual(self.context()['host_source'], self.source)

    def test_symlink_source_parent_rejected(self):
        self.source.rmdir()
        self.source.symlink_to(self.job, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.context()

    def test_private_scope_only_create_command(self):
        self.intent()
        args = host.create_argv(self.context())
        self.assertIn('--read-only', args)
        self.assertIn('container:' + self.isolation['control_container_id'], args)
        self.assertIn(self.isolation['control_container_id'] + ':ro', args)
        self.assertEqual(args[-4:], ['--entrypoint', '/opt/python', self.isolation['image_id'], 'test.py'])
        self.assertNotIn('--privileged', args)
        self.assertEqual(sum(1 for value in args if value == '--mount'), 3)
        self.assertIn('TMPDIR=/tmp', args)
        self.assertIn('XDG_CACHE_HOME=/tmp/cache', args)

    def test_target_inspection_rejects_any_extra_writable_mount(self):
        self.intent()
        context = self.context()
        info = self.info(context)
        host.assert_mounts(context, info)
        info['Mounts'].append({'Source': str(self.project), 'Destination': '/escape', 'RW': True})
        with self.assertRaisesRegex(ValueError, 'unexpected writable'):
            host.assert_mounts(context, info)

    def test_control_container_cannot_be_cancelled(self):
        self.intent()
        context = self.context()
        wrong = self.info(context)
        wrong['Id'] = self.isolation['control_container_id']
        wrong['Config']['Labels'] = {}
        with mock.patch.object(host, 'inspect_container', return_value=wrong):
            with self.assertRaisesRegex(ValueError, 'ownership labels'):
                host.own_container(context, self.isolation['control_container_id'])

    def test_duplicate_launch_is_exclusive(self):
        self.intent()
        with mock.patch.object(host.subprocess, 'Popen') as popen:
            with self.assertRaises(FileExistsError):
                host.launch(self.payload)
            popen.assert_not_called()

    def test_launch_spawn_failure_produces_terminal_setup_failure(self):
        with mock.patch.object(host.subprocess, 'Popen', side_effect=OSError('spawn failed')):
            with self.assertRaises(OSError):
                host.launch(self.payload)
        result = host.read_json(self.job / 'result.json')
        self.assertTrue(result['sandbox_setup_failed'])
        self.assertTrue(result['isolation']['cleanup_complete'])
        self.assertFalse((self.job / 'container.json').exists())

    def test_preflight_guard_failure_never_executes_target(self):
        self.intent()
        descriptor = dict(payload=self.payload, request_sha256=host.sha(self.job / 'request.json'),
                          manifest_sha256=host.sha(self.run / 'manifest.json'), helper_sha256=host.sha(self.helper))
        host.save(self.job / 'host-launch.json', descriptor)
        with mock.patch.object(host, 'control_check', side_effect=host.JobIntegrityError('job integrity check failed')):
            with mock.patch.object(host, 'docker') as docker:
                host.supervise(self.payload)
                docker.assert_not_called()
        result = host.read_json(self.job / 'result.json')
        self.assertTrue(result['sandbox_setup_failed'])
        self.assertTrue(result['integrity_error'])
        self.assertEqual(result['exit_code'], None)

    def test_early_validation_failure_finishes_queued_job_without_target(self):
        with mock.patch.object(host, 'load_context', side_effect=ValueError('control changed')):
            with mock.patch.object(host.subprocess, 'Popen') as popen:
                with self.assertRaises(ValueError):
                    host.launch(self.payload)
                popen.assert_not_called()
        result = host.read_json(self.job / 'result.json')
        self.assertTrue(result['sandbox_setup_failed'])
        self.assertTrue(result['isolation']['cleanup_complete'])

    def test_control_worker_json_error_preserves_guard_failure(self):
        with mock.patch.object(host, 'docker', return_value='{"error":"protected tree changed"}'):
            with self.assertRaisesRegex(host.JobIntegrityError, 'protected tree changed'):
                host.control_check(self.context(), 'after')

    def test_descriptor_mismatch_is_no_target_setup_failure(self):
        self.intent()
        host.save(self.job / 'host-launch.json', {'payload': {}, 'request_sha256': 'wrong'})
        with mock.patch.object(host, 'docker') as docker:
            host.supervise(self.payload)
            docker.assert_not_called()
        result = host.read_json(self.job / 'result.json')
        self.assertTrue(result['sandbox_setup_failed'])
        self.assertIn('descriptor changed', result['error'])

    def test_cancel_does_not_claim_cleanup_failed_job_is_complete(self):
        host.save(self.job / 'result.json', {'isolation': {'cleanup_complete': False}, 'container_running': True})
        result = host.cancel(self.payload)
        self.assertEqual(result['status'], 'cleanup_failed')
        self.assertTrue(result['requires_operator'])
        self.assertFalse(result['cleanup_complete'])

    def test_only_one_supervisor_can_claim_job(self):
        self.intent()
        host.save(self.job / 'host-supervise.intent.json', {'pid': 1})
        with mock.patch.object(host, 'docker') as docker:
            with self.assertRaises(FileExistsError):
                host.supervise(self.payload)
            docker.assert_not_called()
        self.assertFalse((self.job / 'result.json').exists())

    def run_attached(self, state):
        self.intent()
        host.save(self.job / 'host-launch.json', dict(payload=self.payload,
                  request_sha256=host.sha(self.job / 'request.json'),
                  manifest_sha256=host.sha(self.run / 'manifest.json'), helper_sha256=host.sha(self.helper)))
        context = self.context()
        info = self.info(context)
        info['State'] = state
        process = mock.Mock(returncode=0)
        process.poll.return_value = 0
        process.wait.return_value = 0
        check = [{'ok': True, 'source_at_start_sha256': {}}, {'ok': True, 'source_after_sha256': {}}]
        with mock.patch.object(host, 'control_check', side_effect=check), mock.patch.object(host, 'own_container', return_value=info):
            with mock.patch.object(host, 'docker', side_effect=lambda args: 'd' * 64 if args[0] == 'create' else ''):
                with mock.patch.object(host.os, 'chown'), mock.patch.object(host.subprocess, 'Popen', return_value=process):
                    host.supervise(self.payload)
        return host.read_json(self.job / 'result.json')

    def test_created_but_never_started_is_not_success(self):
        result = self.run_attached({'Running': False, 'ExitCode': 0, 'Status': 'created',
                                    'StartedAt': '0001-01-01T00:00:00Z', 'Error': 'start failure'})
        self.assertTrue(result['sandbox_setup_failed'])
        self.assertIsNone(result['exit_code'])
        self.assertTrue(result['isolation']['cleanup_complete'])

    def test_success_records_actual_exit_and_cid_after_cleanup(self):
        result = self.run_attached({'Running': False, 'ExitCode': 7, 'Status': 'exited',
                                    'StartedAt': '2026-10-21T00:00:00Z', 'Error': ''})
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['container_id'], 'd' * 64)
        self.assertEqual(result['isolation']['container_id'], 'd' * 64)
        self.assertTrue(result['isolation']['cleanup_complete'])
        self.assertNotIn('error', result)

    def test_post_spawn_metadata_failure_never_fabricates_terminal_result(self):
        real_save = host.save
        def failing_metadata(path, value, exclusive=False):
            if Path(path).name == 'host-supervisor.json':
                raise OSError('metadata IO failure after child spawn')
            return real_save(path, value, exclusive=exclusive)
        process = mock.Mock(pid=12345)
        with mock.patch.object(host, 'save', side_effect=failing_metadata):
            with mock.patch.object(host.subprocess, 'Popen', return_value=process):
                with self.assertRaisesRegex(OSError, 'after child spawn'):
                    host.launch(self.payload)
        self.assertTrue((self.job / 'host-launch.intent.json').exists())
        self.assertFalse((self.job / 'result.json').exists())

    def test_guard_checks_have_no_transport_total_deadline(self):
        with mock.patch.object(host, 'docker', return_value='{"ok":true,"source_at_start_sha256":{}}') as docker:
            host.control_check(self.context(), 'before')
        args, kwargs = docker.call_args
        self.assertIsNone(kwargs['timeout'])
        self.assertIn('--run', args[0])
        self.assertEqual(json.loads(args[1]), dict(action='job_check', job_id='123', phase='before'))

    def test_nested_mount_and_escaped_mount_path_rejected(self):
        class FakeMounts:
            def is_file(self): return True
            def read_text(self): return '1 0 0:1 / / rw - x x rw\n2 1 0:2 / /safe/sub\\040mount rw - x x rw\n'
        with mock.patch.object(host, 'Path', return_value=FakeMounts()):
            with self.assertRaisesRegex(ValueError, 'nested host mount'):
                host.reject_nested_mounts(['/safe'])
            host.reject_nested_mounts(['/other'])

    def test_cleanup_requires_matching_full_id_labels_and_stopped(self):
        self.intent()
        context = self.context()
        info = self.info(context)
        info['State']['Running'] = True
        with mock.patch.object(host, 'own_container', return_value=info), mock.patch.object(host, 'docker') as docker:
            self.assertFalse(host.cleanup(context, info['Id'])['cleanup_complete'])
            docker.assert_not_called()
        info['State']['Running'] = False
        with mock.patch.object(host, 'own_container', return_value=info), mock.patch.object(host, 'docker') as docker:
            self.assertTrue(host.cleanup(context, info['Id'])['cleanup_complete'])
            docker.assert_called_once_with(['rm', info['Id']])

    def test_cancel_without_cid_is_only_a_marker(self):
        with mock.patch.object(host, 'docker') as docker:
            host.cancel(self.payload)
            host.cancel(self.payload)
            docker.assert_not_called()
        self.assertEqual(host.read_json(self.job / 'cancel.request.json')['job_id'], '123')


if __name__ == '__main__':
    unittest.main()
