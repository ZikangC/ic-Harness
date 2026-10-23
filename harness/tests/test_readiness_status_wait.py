"""Bounded same-outer-job observations; no server, model or EDA execution."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from harness.runtime import rtl_bridge


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ReadinessStatusWaitTests(unittest.TestCase):
    request = {'action': 'status', 'job_id': '123456', 'scope': 'readiness/campaign-1/work'}

    def test_terminal_result_returns_immediately_with_unchanged_payload(self):
        for status in ('completed', 'cleanup_failed'):
            with self.subTest(status=status):
                result = {'status': status, 'job_id': '123456', 'exit_code': 0, 'stdout_tail': 'actual output'}
                remote = Mock()
                remote.call.return_value = result
                clock = Clock()
                with patch.object(rtl_bridge.time, 'monotonic', clock.monotonic), \
                        patch.object(rtl_bridge.time, 'sleep', clock.sleep):
                    self.assertIs(rtl_bridge.readiness_status(remote, self.request), result)
                self.assertEqual(clock.sleeps, [])
                remote.call.assert_called_once_with(self.request)

    def test_pending_then_terminal_observes_same_outer_job_and_stops(self):
        remote = Mock()
        terminal = {'status': 'completed', 'job_id': '123456', 'exit_code': 3}
        remote.call.side_effect = [{'status': 'running'}, {'status': 'running'}, terminal]
        clock = Clock()
        with patch.object(rtl_bridge.time, 'monotonic', clock.monotonic), \
                patch.object(rtl_bridge.time, 'sleep', clock.sleep):
            self.assertIs(rtl_bridge.readiness_status(remote, self.request), terminal)
        self.assertEqual(clock.now, 1)
        self.assertEqual(remote.call.call_count, 3)
        self.assertTrue(all(call.args == (self.request,) for call in remote.call.call_args_list))
        self.assertEqual(self.request['job_id'], '123456')

    def test_forever_pending_has_bounded_wait_and_never_schedules_other_actions(self):
        remote = Mock()
        pending = {'status': 'running', 'job_id': '123456'}
        remote.call.return_value = pending
        clock = Clock()
        with patch.object(rtl_bridge.time, 'monotonic', clock.monotonic), \
                patch.object(rtl_bridge.time, 'sleep', clock.sleep):
            self.assertIs(rtl_bridge.readiness_status(remote, self.request), pending)
        self.assertEqual(clock.now, 15)
        self.assertLessEqual(remote.call.call_count, 31)
        self.assertTrue(all(call.args == (self.request,) for call in remote.call.call_args_list))
        self.assertTrue(all(call.args[0]['action'] == 'status' for call in remote.call.call_args_list))

    def test_transport_failure_propagates_without_retry_or_replacement_job(self):
        remote = Mock()
        remote.call.side_effect = [{'status': 'running'}, ConnectionError('outcome unknown')]
        clock = Clock()
        with patch.object(rtl_bridge.time, 'monotonic', clock.monotonic), \
                patch.object(rtl_bridge.time, 'sleep', clock.sleep):
            with self.assertRaises(ConnectionError):
                rtl_bridge.readiness_status(remote, self.request)
        self.assertEqual(remote.call.call_count, 2)
        self.assertTrue(all(call.args[0]['action'] == 'status' for call in remote.call.call_args_list))

    def test_existing_transport_time_consumes_window_without_extra_poll(self):
        remote = Mock()
        clock = Clock()

        def slow(request):
            clock.now += 16
            return {'status': 'running'}

        remote.call.side_effect = slow
        with patch.object(rtl_bridge.time, 'monotonic', clock.monotonic), \
                patch.object(rtl_bridge.time, 'sleep', clock.sleep):
            self.assertEqual(rtl_bridge.readiness_status(remote, self.request), {'status': 'running'})
        remote.call.assert_called_once_with(self.request)
        self.assertEqual(clock.sleeps, [])

    def test_only_readiness_status_uses_wait_adapter(self):
        with tempfile.TemporaryDirectory() as name:
            context = Path(name) / 'context.json'
            for role, action in [('implementation', 'status'), ('rtl_audit', 'status'),
                                 ('architecture', 'status'), ('review', 'status'),
                                 ('rtl_readiness', 'read'), ('readiness_review', 'jobs'),
                                 ('rtl_readiness', 'status'), ('readiness_review', 'status')]:
                scope = 'readiness/campaign-1/work' if role == 'rtl_readiness' else 'work'
                context.write_text(json.dumps(dict(role=role, scope=scope, environment={}, run_id='fixture', worker='worker')))
                req = {'action': action, 'job_id': '123456'}
                with self.subTest(role=role, action=action), \
                        patch.dict(os.environ, HARNESS_RTL_CONTEXT=str(context)), \
                        patch.object(rtl_bridge.sys, 'stdin', io.StringIO(json.dumps(req))), \
                        patch.object(rtl_bridge, 'Remote') as remote, \
                        patch.object(rtl_bridge, 'readiness_status', return_value={'status': 'completed'}) as wait, \
                        contextlib.redirect_stdout(io.StringIO()):
                    remote.return_value.call.return_value = {'status': 'completed'}
                    rtl_bridge.main()
                if role in ('rtl_readiness', 'readiness_review') and action == 'status':
                    wait.assert_called_once_with(remote.return_value, dict(req, scope=scope))
                    remote.return_value.call.assert_not_called()
                else:
                    wait.assert_not_called()
                    remote.return_value.call.assert_called_once_with(dict(req, scope=scope))


if __name__ == '__main__':
    unittest.main()
