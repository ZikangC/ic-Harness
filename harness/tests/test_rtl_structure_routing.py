"""Structure tool routing contracts; no SSH, containers, models or EDA execute."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from harness.adapters.rtl_remote import Remote

SPEC = importlib.util.spec_from_file_location(
    'structure_routing_bridge', Path(__file__).parents[1] / 'runtime/rtl_bridge.py')
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class StructureBridgeRoutingTests(unittest.TestCase):
    def invoke(self, context, request, remote):
        with tempfile.TemporaryDirectory() as directory:
            context_file = Path(directory) / 'context.json'
            context_file.write_text(json.dumps(dict(environment={}, run_id='fixture',
                                                   worker='/fixture/frozen-worker.py', **context)))
            with patch.dict(os.environ, {'HARNESS_RTL_CONTEXT': str(context_file)}), \
                    patch.object(bridge, 'Remote', return_value=remote), \
                    patch('sys.stdin', io.StringIO(json.dumps(request))), \
                    patch('sys.stdout', io.StringIO()) as output:
                bridge.main()
                return json.loads(output.getvalue())

    def test_audit_uses_trusted_context_scope_and_review_not_model_arguments(self):
        remote = Mock()
        remote.call.return_value = {'job_id': '17', 'status': 'submitted'}
        configuration = {'top': 'top', 'sources': ['rtl/top.sv'], 'defines': {'MODE': None}}
        request = {'action': 'structure_check', 'scope': 'work', 'review': 'review-999',
                   'configuration': configuration, 'timeout_seconds': 123}
        result = self.invoke({'role': 'rtl_audit', 'scope': 'audit/review-3', 'review': 'review-3'}, request, remote)
        remote.call.assert_called_once_with(dict(request, scope='audit/review-3', review='review-3'))
        self.assertEqual(result, {'job_id': '17', 'status': 'submitted'})

    def test_implementation_keeps_work_scope_even_with_model_audit_scope(self):
        remote = Mock()
        remote.call.return_value = {'job_id': '18', 'status': 'submitted'}
        request = {'action': 'structure_check', 'scope': 'audit/review-77',
                   'configuration': {'top': 'top', 'sources': ['top.sv']}}
        self.invoke({'role': 'implementation', 'scope': 'work'}, request, remote)
        remote.call.assert_called_once_with(dict(request, scope='work'))

    def test_architecture_and_unknown_roles_cannot_reach_native_structure_action(self):
        for role in ('architecture', 'review', 'controller', 'unknown'):
            remote = Mock()
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, 'implementation or RTL audit role'):
                self.invoke({'role': role, 'scope': 'architecture/revision-1/audit'},
                            {'action': 'structure_check', 'configuration': {'top': 'top', 'sources': ['top.sv']}}, remote)
            remote.call.assert_not_called()


class StructureRemoteRoutingTests(unittest.TestCase):
    def setUp(self):
        environment = {
            '_project_name': 'fixture',
            'remote': {'container_workspace_root': '/work', 'host_workspace_root': '/host/work',
                       'ssh_host': 'fixture-host', 'container': 'fixture-eda', 'connect_timeout_seconds': 9},
            'eda': {'python': '/opt/eda/bin/python3'},
        }
        self.remote = Remote(environment, 'fixture-run',
                             '/work/fixture/output/fixture-run/snapshot/harness/runtime/rtl_worker.py')
        self.request = {'action': 'structure_check', 'scope': 'audit/review-3', 'review': 'review-3',
                        'configuration': {'top': 'top', 'sources': ['top.sv'], 'parameters': {'W': "8'd8"}},
                        'timeout_seconds': 123}
        self.queued = {'job_id': '17', 'status': 'queued',
                       'logs': '/work/fixture/output/fixture-run/jobs/17',
                       'configuration_sha256': 'a' * 64,
                       'config_path': 'jobs/17/structure-config.json'}

    def reply(self, value, returncode=0):
        return SimpleNamespace(stdout=json.dumps(value), stderr='', returncode=returncode)

    def test_native_check_queues_once_then_launches_exact_frozen_host_helper(self):
        launched = {'ok': True, 'job_id': '17', 'status': 'submitted', 'logs': '/host/work/fixture/output/fixture-run/jobs/17'}
        status = {'job_id': '17', 'status': 'completed', 'exit_code': 1}
        replies = [self.reply(self.queued), self.reply(launched), self.reply(status)]
        with patch('harness.adapters.rtl_remote.subprocess.run', side_effect=replies) as run:
            submitted = self.remote.call(self.request)
            observed = self.remote.call({'action': 'status', 'job_id': '17', 'brief': True})
        self.assertEqual(run.call_count, 3)
        queued_call, launch_call, status_call = run.call_args_list
        self.assertEqual(json.loads(queued_call.kwargs['input']), self.request)
        self.assertIn('docker exec -i -w /work/fixture fixture-eda', queued_call.args[0][-1])
        self.assertIn('/work/fixture/output/fixture-run/snapshot/harness/runtime/rtl_worker.py', queued_call.args[0][-1])
        payload = json.loads(launch_call.kwargs['input'])
        self.assertEqual((payload['action'], payload['job_id'], payload['run_id']), ('launch', '17', 'fixture-run'))
        self.assertEqual(payload['worker'], self.remote.worker)
        self.assertEqual(payload['host_project_root'], '/host/work/fixture')
        self.assertIn('/host/work/fixture/output/fixture-run/snapshot/harness/runtime/rtl_job_host.py', launch_call.args[0][-1])
        self.assertEqual(json.loads(status_call.kwargs['input']), {'action': 'status', 'job_id': '17', 'brief': True})
        self.assertEqual(submitted['logs'], self.queued['logs'])
        self.assertEqual(submitted['configuration_sha256'], self.queued['configuration_sha256'])
        self.assertEqual(submitted['config_path'], self.queued['config_path'])
        self.assertEqual(submitted['status'], 'submitted')
        self.assertEqual(observed, status)
        for invocation in run.call_args_list:
            self.assertNotIn('timeout', invocation.kwargs)
            self.assertIn('ConnectTimeout=9', invocation.args[0])
            self.assertIn('ServerAliveInterval=10', invocation.args[0])

    def test_worker_rejection_never_launches_or_requeues(self):
        with patch('harness.adapters.rtl_remote.subprocess.run',
                   return_value=self.reply({'error': 'invalid structure compile configuration'}, 1)) as run:
            with self.assertRaisesRegex(RuntimeError, 'invalid structure compile configuration'):
                self.remote.call(self.request)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['action'], 'structure_check')

    def test_lost_host_reply_does_not_retry_uncertain_launch_or_requeue_check(self):
        replies = [self.reply(self.queued), SimpleNamespace(stdout='truncated', stderr='private transport', returncode=255)]
        with patch('harness.adapters.rtl_remote.subprocess.run', side_effect=replies) as run:
            with self.assertRaisesRegex(ConnectionError, 'outcome unknown'):
                self.remote.call(self.request)
        self.assertEqual(run.call_count, 2)
        self.assertEqual([json.loads(call.kwargs['input'])['action'] for call in run.call_args_list],
                         ['structure_check', 'launch'])

    def test_different_launch_job_identity_is_rejected_without_retry(self):
        with patch('harness.adapters.rtl_remote.subprocess.run', side_effect=[self.reply(self.queued),
                   self.reply({'ok': True, 'job_id': '999', 'status': 'submitted'})]) as run:
            with self.assertRaisesRegex(ConnectionError, 'different job identity'):
                self.remote.call(self.request)
        self.assertEqual(run.call_count, 2)

    def test_cancel_remains_host_owned_and_queries_status_without_admission_or_requeue(self):
        with patch.object(self.remote, 'host_call', return_value={'ok': True}) as host, \
                patch.object(self.remote, '_worker_call', return_value={'job_id': '17', 'status': 'completed'}) as worker:
            result = self.remote.call({'action': 'cancel', 'job_id': '17'})
        host.assert_called_once_with('cancel', '17')
        worker.assert_called_once_with({'action': 'status', 'job_id': '17'})
        self.assertEqual(result['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
