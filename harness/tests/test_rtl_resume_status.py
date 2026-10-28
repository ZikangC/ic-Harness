"""Exact status pagination and pre-session cleanup; no model or EDA execution."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.adapters.readiness import _json_file
from harness.runtime.rtl_worker import read_text_page
from harness.stages import rtl


def completed_status():
    """A realistic nested completed-run shape that exceeds 100 display lines."""
    return {
        'status': 'rtl_review_finished', 'phase': 'audit',
        'implementation_verified': False, 'rtl_stage_release': True,
        'review': 'review-1', 'candidate_sha256': 'a' * 64,
        'seed': {'selected_files': {
            f'rtl/block_{index}.sv': {'sha256': format(index, '064x'), 'size': index + 1}
            for index in range(120)}},
        'readiness': {'status': 'rtl_readiness_completed', 'adopted': True,
                      'campaign_state': {'campaign': 'campaign-1', 'state': 'adopted'}},
    }


class PagedRemote:
    """Only the transport is replaced; pages use the actual worker encoder."""
    def __init__(self, raw):
        self.raw = raw
        self.calls = []
        self.pages = []

    def call(self, request):
        self.calls.append(dict(request))
        action = request['action']
        if action == 'init':
            return {'worker': '/unit/frozen/worker.py', 'run_dir': '/unit/run',
                    'capabilities': {}, 'reference_path': '/unit/reference.py',
                    'input_source_paths': {'spec.md': 'spec.md', 'reference.py': 'reference.py'}}
        if action == 'read':
            if request['path'] != '@run/status.json':
                raise AssertionError('unexpected read')
            page = read_text_page(self.raw, request['path'], 'unit-run-status',
                                  request, resolved_run_path='status.json')
            self.pages.append(page)
            return page
        if action == 'rtl_readiness_seed':
            if request != {'action': 'rtl_readiness_seed', 'validate_only': True}:
                raise AssertionError('resume attempted seed import')
            return {'unit_test_validation_only': True}
        if action == 'set_status':
            return {'ok': True}
        raise AssertionError('no model/job actions are allowed in this regression: ' + action)

    def evidence(self, *args):
        pass


class StatusPaginationTests(unittest.TestCase):
    def test_completed_shape_crosses_hundred_and_multiple_worker_pages(self):
        status = completed_status()
        raw = (json.dumps(status, indent=2) + '\n').encode()
        self.assertGreater(len(raw.splitlines()), 100)
        remote = PagedRemote(raw)
        self.assertEqual(_json_file(remote, 'work', '@run/status.json'), status)
        self.assertGreater(len(remote.pages), 1)
        self.assertEqual(''.join(page['content'] for page in remote.pages).encode(), raw)
        self.assertTrue(remote.pages[-1]['eof'])

    def test_long_utf8_line_keeps_exact_content_across_byte_limited_pages(self):
        status = {'phase': 'audit', 'summary': '完整🙂状态' * 6000,
                  'tail': {'must_survive': True}}
        raw = (json.dumps(status, ensure_ascii=False) + '\n').encode()
        remote = PagedRemote(raw)
        self.assertEqual(_json_file(remote, 'work', '@run/status.json'), status)
        self.assertTrue(any(page['truncated'] for page in remote.pages))
        self.assertEqual(''.join(page['content'] for page in remote.pages).encode(), raw)
        self.assertEqual(remote.pages[-1]['returned_range']['byte_end'], len(raw))


class ControllerResumeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='rtl-resume-regression-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        config = self.root / 'harness/config'
        config.mkdir(parents=True)
        (config / 'environment.toml').write_text(
            '[project]\nroot="../.."\ninput_dir="input"\n'
            '[remote]\nssh_host="unit-test-no-transport"\n'
            '[eda]\npython="/unit/python"\nverilator="/unit/verilator"\n'
            'yosys="/unit/yosys"\nopenroad="/unit/openroad"\n')
        (config / 'rtl.toml').write_text('[readiness]\nenabled=true\nrtl_review=true\nmax_review_repairs=1\n')
        (config / 'models.toml').write_text('schema_version=1\n')
        inputs = self.root / 'input'
        inputs.mkdir()
        (inputs / 'spec.md').write_text('Status transport regression; no engineering acceptance claim.\n')
        (inputs / 'reference.py').write_text('def reference(x): return x\n')
        self.args = argparse.Namespace(config=config / 'environment.toml', resume='status-resume',
                                       run_id=None, task=None, audit_only=False, validate_inputs=False)
        self.lock = Path(tempfile.gettempdir()) / (
            'harness-rtl-' + rtl.digest((str(self.root) + self.args.resume).encode())[:20] + '.lock')
        self.addCleanup(lambda: self.lock.unlink(missing_ok=True))
        self.caches = []
        original_mkdtemp = tempfile.mkdtemp

        def cache_directory(*args, **kwargs):
            kwargs['dir'] = self.root
            path = original_mkdtemp(*args, **kwargs)
            self.caches.append(Path(path))
            return path

        self.mkdtemp = cache_directory

    def invoke(self, remote, *, copy_error=False):
        with patch.object(rtl, 'Remote', return_value=remote), \
                patch.object(rtl, 'validate_skills'), \
                patch.object(rtl.signal, 'signal'), \
                patch.object(rtl.tempfile, 'mkdtemp', side_effect=self.mkdtemp), \
                patch.object(rtl, 'run_session', side_effect=AssertionError('no model invocation')) as session, \
                patch.object(rtl, 'run_readiness', side_effect=AssertionError('no new campaign')) as campaign, \
                patch.object(rtl, 'run_audit', return_value={
                    'status': 'unit_test_audit_not_executed', 'rtl_stage_release': False}) as audit, \
                contextlib.redirect_stdout(io.StringIO()):
            if copy_error:
                with patch.object(rtl.shutil, 'copytree', side_effect=OSError('unit cache copy failure')):
                    with self.assertRaisesRegex(OSError, 'unit cache copy failure'):
                        rtl.run(self.args, readiness_only=True)
            elif remote.raw.startswith(b'{invalid'):
                with self.assertRaises(json.JSONDecodeError):
                    rtl.run(self.args, readiness_only=True)
            else:
                self.assertEqual(rtl.run(self.args, readiness_only=True), 1)
                audit.assert_called_once()
                self.assertEqual(audit.call_args.kwargs['max_repairs'], 1)
                self.assertEqual(audit.call_args.kwargs['repair_handler'], rtl.readiness_repair)
            session.assert_not_called()
            campaign.assert_not_called()
            if copy_error or remote.raw.startswith(b'{invalid'):
                audit.assert_not_called()
                self.assertFalse(any(call['action'] == 'set_status' for call in remote.calls))
        self.assertFalse(self.lock.exists(), 'local controller lock leaked before model execution')
        self.assertTrue(self.caches)
        self.assertTrue(all(not path.exists() for path in self.caches), 'local client cache leaked')

    def test_long_completed_status_restores_audit_phase_without_new_campaign(self):
        remote = PagedRemote((json.dumps(completed_status(), indent=2) + '\n').encode())
        self.invoke(remote)
        self.assertGreater(len(remote.pages), 1)
        self.assertTrue(all(call['value']['phase'] == 'audit'
                            for call in remote.calls if call['action'] == 'set_status'))

    def test_invalid_status_removes_lock_and_cache_before_any_model_or_status_write(self):
        self.invoke(PagedRemote(b'{invalid retained status\n'))

    def test_cache_setup_failure_also_removes_native_lock_and_partial_cache(self):
        self.invoke(PagedRemote(b'{}'), copy_error=True)


if __name__ == '__main__':
    unittest.main()
