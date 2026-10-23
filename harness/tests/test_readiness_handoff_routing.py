"""Handoff transport and fresh-review routing, without a provider or EDA."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.adapters import readiness
from harness.runtime import rtl_bridge
from harness.tests import test_readiness_adapter as adapter_fixture


class HandoffBridgeTests(unittest.TestCase):
    def test_handoff_uses_trusted_actor_and_campaign_only(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'context.json'
            path.write_text(json.dumps(dict(role='rtl_readiness', scope='readiness/campaign-1/work',
                                           session_id='current', session_actor_sha256='actor',
                                           campaign='campaign-1', environment={}, run_id='fixture', worker='worker')))
            request = dict(action='handoff', job_id='123456')
            with patch.dict(os.environ, HARNESS_RTL_CONTEXT=str(path)), \
                    patch.object(rtl_bridge, 'Remote') as remote, \
                    patch.object(rtl_bridge.sys, 'stdin', io.StringIO(json.dumps(request))), \
                    contextlib.redirect_stdout(io.StringIO()):
                remote.return_value.call.return_value = {'accepted': True}
                rtl_bridge.main()
            remote.return_value.call.assert_called_once_with(dict(
                action='rtl_readiness_handoff', session_id='current', actor_sha256='actor',
                campaign='campaign-1', job_id='123456'))

    def test_review_repair_and_extra_identity_fields_cannot_commit_handoff(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'context.json'
            for role, request in [('readiness_review', dict(action='handoff', job_id='123')),
                                  ('implementation', dict(action='handoff', job_id='123')),
                                  ('rtl_readiness', dict(action='handoff', job_id='123', session_id='other'))]:
                path.write_text(json.dumps(dict(role=role, repair_skill='rtl-implementation-readiness',
                                               scope='work', environment={}, run_id='fixture', worker='worker')))
                with self.subTest(role=role, request=request), \
                        patch.dict(os.environ, HARNESS_RTL_CONTEXT=str(path)), \
                        patch.object(rtl_bridge, 'Remote') as remote, \
                        patch.object(rtl_bridge.sys, 'stdin', io.StringIO(json.dumps(request))):
                    with self.assertRaises(ValueError):
                        rtl_bridge.main()
                    remote.return_value.call.assert_not_called()


class HandoffRoutingTests(unittest.TestCase):
    setUp = adapter_fixture.AdapterTests.setUp

    def test_controlled_handoff_starts_independent_review_of_exact_job(self):
        remote = adapter_fixture.Remote(decision='candidate_for_integration')
        original = remote.call

        def call(request):
            if request['action'] == 'rtl_readiness_resolve':
                remote.calls.append(request)
                self.assertEqual(request['finalization_job_id'], '123456')
                return dict(found=True, finalization_job_id='123456', report_path='probe/final/finalization.json',
                            report=dict(decision='candidate_for_integration'))
            return original(request)

        remote.call = call
        invoked = []

        def invoke(prompt, role, *args):
            invoked.append(role)
            if role == 'implementation':
                return dict(completed=False, handoff_complete=True, termination_reason='verified_handoff',
                            handoff_job_id='123456', tool_isolation_ok=True, budget_exhausted=False)
            return dict(completed=True, tool_isolation_ok=True)

        with patch.object(readiness, 'run_session', side_effect=invoke):
            result = readiness.run(self.context, remote)
        self.assertEqual(invoked, ['implementation', 'review'])
        completion = remote.calls[-1]
        self.assertEqual(completion['finalization_job_id'], '123456')
        self.assertIn('review_session_id', completion)
        self.assertFalse(result['implementation_session_status']['completed'])
        self.assertTrue(result['implementation_session_status']['handoff_complete'])

    def test_handoff_claim_does_not_override_exhaustion_or_isolation_failure(self):
        base = dict(completed=False, handoff_complete=True, termination_reason='verified_handoff',
                    tool_isolation_ok=True, budget_exhausted=False)
        self.assertTrue(readiness._finished(base))
        for changes in (dict(budget_exhausted=True), dict(tool_isolation_ok=False),
                        dict(termination_reason='assistant_turns'), dict(handoff_complete=False)):
            self.assertFalse(readiness._finished(base | changes))


if __name__ == '__main__':
    unittest.main()
