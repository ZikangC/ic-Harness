"""An observer must not turn unavailable process visibility into a lost job."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import experiments
from core import ReadinessError, init_workspace, read_json, save_json


class NamespaceStatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        project = self.root / 'project'
        project.mkdir()
        (project / 'top.sv').write_text('module top; endmodule\n')
        self.workspace = self.root / 'workspace'
        init_workspace(project, self.workspace, ['top.sv'], 'top')
        self.config = dict(kind='command', argv=[sys.executable, '-B', '-c', "print('fixture')"],
                           purpose='measurement', timeout_seconds=5, memory_mib=256)
        self.completed = experiments.submit(self.workspace, variant='baseline', config=self.config,
                                            job_id='owned', background=False)
        self.job = self.workspace / 'jobs/owned'

    def tearDown(self):
        self.temporary.cleanup()

    def pending(self, namespace='pid:[foreign]'):
        (self.job / 'result.json').unlink()
        identity = dict(pid=999999999, start='123', boot='fixture',
                        scheme='linux-proc-start-ticks', pid_namespace=namespace)
        save_json(self.job / 'status.json', dict(status='running', worker=identity, process=identity,
                                               submitted_epoch=0, request_identity=self.completed['request_identity']))

    def test_other_namespace_preserves_receipt_and_concurrency_reservation(self):
        self.pending()
        before = (self.job / 'status.json').read_bytes()
        with patch.object(experiments.sys, 'platform', 'linux'), \
                patch.object(experiments, '_pid_namespace', return_value='pid:[observer]'):
            result = experiments.status(self.workspace, 'owned')
            self.assertEqual(result['process_visibility'], 'different_pid_namespace')
            self.assertIsNone(result['active_process'])
            self.assertEqual(result['status'], 'running')
            self.assertFalse(result['worker_identity_verified'])
            with self.assertRaisesRegex(ReadinessError, 'concurrency budget occupied'):
                experiments.submit(self.workspace, variant='baseline', config=self.config, job_id='duplicate')
        self.assertFalse((self.job / 'result.json').exists())
        self.assertEqual((self.job / 'status.json').read_bytes(), before)

    def test_unreadable_process_in_same_namespace_is_not_persisted_lost(self):
        self.pending(namespace='pid:[observer]')
        original = Path.read_text

        def read(path, *args, **kwargs):
            if str(path) == '/proc/999999999/stat':
                raise PermissionError('unobservable process')
            return original(path, *args, **kwargs)

        with patch.object(experiments.sys, 'platform', 'linux'), \
                patch.object(experiments, '_pid_namespace', return_value='pid:[observer]'), \
                patch.object(Path, 'read_text', read):
            self.assertEqual(experiments.status(self.workspace, 'owned')['process_visibility'],
                             'process_identity_unreadable')
        self.assertFalse((self.job / 'result.json').exists())

    def test_legacy_namespace_identity_is_conservative(self):
        self.pending(namespace=None)
        with patch.object(experiments.sys, 'platform', 'linux'), \
                patch.object(experiments, '_pid_namespace', return_value='pid:[observer]'):
            self.assertEqual(experiments.status(self.workspace, 'owned')['process_visibility'],
                             'pid_namespace_unavailable')
        self.assertFalse((self.job / 'result.json').exists())

    def test_running_worker_without_readable_identity_is_not_reported_lost(self):
        self.pending()
        state = read_json(self.job / 'status.json')
        state.update(worker=None, process=None)
        save_json(self.job / 'status.json', state)
        with patch.object(experiments.sys, 'platform', 'linux'):
            self.assertEqual(experiments.status(self.workspace, 'owned')['process_visibility'],
                             'worker_identity_unavailable')
        self.assertFalse((self.job / 'result.json').exists())

    def test_same_namespace_known_missing_process_can_be_reported_lost(self):
        self.pending(namespace='pid:[observer]')
        original = Path.read_text

        def read(path, *args, **kwargs):
            if str(path) == '/proc/999999999/stat':
                raise FileNotFoundError('process ended')
            return original(path, *args, **kwargs)

        with patch.object(experiments.sys, 'platform', 'linux'), \
                patch.object(experiments, '_pid_namespace', return_value='pid:[observer]'), \
                patch.object(experiments, '_proc_identity', return_value=None), \
                patch.object(Path, 'read_text', read):
            self.assertEqual(experiments.status(self.workspace, 'owned')['status'], 'incomplete')
        self.assertEqual(read_json(self.job / 'result.json')['reason'], 'worker_lost_or_never_started')

    def test_completed_receipt_does_not_require_live_process_visibility(self):
        with patch.object(experiments, '_visibility', side_effect=AssertionError('must not inspect PIDs')):
            self.assertEqual(experiments.status(self.workspace, 'owned')['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
