"""Readiness publication and native structure queue share work admission; no EDA."""
import json
import threading
import unittest
from unittest.mock import patch

from harness.runtime import rtl_readiness as readiness
from harness.runtime import rtl_worker as worker
from harness.tests import test_rtl_readiness as fixtures


class ReadinessStructureAdmissionTests(unittest.TestCase):
    setUp = fixtures.ReadinessTests.setUp
    tearDown = fixtures.ReadinessTests.tearDown
    call = fixtures.ReadinessTests.call
    begin = fixtures.ReadinessTests.begin
    candidate = fixtures.ReadinessTests.candidate
    session = fixtures.ReadinessTests.session
    complete = fixtures.ReadinessTests.complete

    def native(self):
        return self.call(action='structure_check', scope='work',
                         configuration={'top': 'block', 'sources': ['rtl/block.sv']}, timeout_seconds=5)

    def test_publication_holds_work_admission_until_whole_candidate_is_published(self):
        campaign, verified, sessions = self.candidate()
        entered, release, native_done = threading.Event(), threading.Event(), threading.Event()
        outcomes = {}
        original = readiness._publish_updates
        def paused(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('publication barrier expired')
            return original(*args)
        def publish():
            try:outcomes['publication'] = self.complete(campaign, **sessions)
            except BaseException as error:outcomes['publication_error'] = error
        def check():
            try:outcomes['native'] = self.native()
            except BaseException as error:outcomes['native_error'] = error
            finally:native_done.set()
        with patch.object(readiness, 'verify_core', return_value=verified), \
                patch.object(readiness, '_publish_updates', side_effect=paused):
            first = threading.Thread(target=publish, daemon=True);first.start()
            self.assertTrue(entered.wait(2))
            second = threading.Thread(target=check, daemon=True);second.start()
            try:self.assertFalse(native_done.wait(.05))
            finally:release.set()
            first.join(4);second.join(4)
        self.assertFalse(first.is_alive());self.assertFalse(second.is_alive())
        self.assertNotIn('publication_error', outcomes);self.assertNotIn('native_error', outcomes)
        self.assertTrue(outcomes['publication']['adopted'])
        self.assertEqual(outcomes['native']['status'], 'queued')
        config = json.loads((self.out / outcomes['native']['config_path']).read_text())
        self.assertEqual(config['binding']['source_inventory_sha256'], worker.digest(worker.source_inventory(self.out / 'work')))
        self.assertIn('wire x = 1', (self.rtl / 'block.sv').read_text())
        # Queue metadata only; no host job was launched by this test.
        worker.save(self.out / 'jobs' / outcomes['native']['job_id'] / 'result.json',
                    {'cleanup_complete': True, 'container_running': False,
                     'isolation': {'cleanup_complete': True, 'container_running': False}})

    def test_queued_native_wins_admission_and_prevents_readiness_publication(self):
        campaign, verified, sessions = self.candidate()
        entered, release, publication_done = threading.Event(), threading.Event(), threading.Event()
        outcomes = {};original = worker._dispatch
        def paused(root, run_id, request):
            if request['action'] == 'structure_check':
                entered.set()
                if not release.wait(3):raise AssertionError('native admission barrier expired')
            return original(root, run_id, request)
        def check():
            try:outcomes['native'] = self.native()
            except BaseException as error:outcomes['native_error'] = error
        def publish():
            try:outcomes['publication'] = self.complete(campaign, **sessions)
            except BaseException as error:outcomes['publication_error'] = error
            finally:publication_done.set()
        with patch.object(worker, '_dispatch', side_effect=paused), \
                patch.object(readiness, 'verify_core', return_value=verified) as verify:
            first = threading.Thread(target=check, daemon=True);first.start()
            self.assertTrue(entered.wait(2))
            second = threading.Thread(target=publish, daemon=True);second.start()
            try:self.assertFalse(publication_done.wait(.05))
            finally:release.set()
            first.join(4);second.join(4)
        self.assertFalse(first.is_alive());self.assertFalse(second.is_alive())
        self.assertNotIn('native_error', outcomes)
        self.assertIsInstance(outcomes.get('publication_error'), ValueError)
        self.assertIn('terminal cleanup', str(outcomes['publication_error']))
        verify.assert_not_called()
        self.assertEqual(readiness.load(self.out)['state'], 'active')
        self.assertIn('wire x = 0', (self.rtl / 'block.sv').read_text())
        worker.save(self.out / 'jobs' / outcomes['native']['job_id'] / 'result.json',
                    {'cleanup_complete': True, 'container_running': False,
                     'isolation': {'cleanup_complete': True, 'container_running': False}})

    def test_native_terminal_cleanup_must_be_complete_before_publication(self):
        campaign, verified, sessions = self.candidate()
        queued = self.native();job = self.out / 'jobs' / queued['job_id']
        worker.save(job / 'result.json', {'cleanup_complete': False, 'container_running': False,
                                        'isolation': {'cleanup_complete': True, 'container_running': False}})
        with patch.object(readiness, 'verify_core', return_value=verified) as verify:
            with self.assertRaisesRegex(ValueError, 'terminal cleanup'):
                self.complete(campaign, **sessions)
            verify.assert_not_called()
        self.assertEqual(readiness.load(self.out)['state'], 'active')
        worker.save(job / 'result.json', {'cleanup_complete': True, 'container_running': False,
                                        'isolation': {'cleanup_complete': True, 'container_running': False}})
        with patch.object(readiness, 'verify_core', return_value=verified):
            self.assertTrue(self.complete(campaign, **sessions)['adopted'])


if __name__ == '__main__':
    unittest.main()
