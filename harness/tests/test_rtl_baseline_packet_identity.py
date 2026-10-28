"""Complete imported baseline identity; pure metadata, no EDA/model execution."""
import copy
import json
import unittest
from pathlib import Path

from harness.runtime import rtl_worker as worker
from harness.tests import test_rtl_remote as fixture


class BaselinePacketIdentityTests(unittest.TestCase):
    setUp = fixture.WorkerTests.setUp
    tearDown = fixture.WorkerTests.tearDown

    def baseline(self):
        root = self.root / 'result/architecture'
        extras = {'CHANGES.md': 'Complete source-bound imported change narrative.\n',
                  'budget_binding.json': '{"design_file_sha256":"synthetic-binding"}\n',
                  'budget.execution.json': '{"exit_code":0,"fixture_only":true}\n',
                  'support/memory-interface.md': 'Retained declared source interface.\n'}
        for name, text in extras.items():
            path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
        return root, extras

    def initialize(self, run='imported-complete'):
        return worker.dispatch(self.root, run, copy.deepcopy(self.req))

    def test_complete_extra_packet_is_frozen_and_identical_resume_succeeds(self):
        baseline, extras = self.baseline(); init = self.initialize(); out = Path(init['run_dir'])
        declared = json.loads((out / 'manifest.json').read_text())['architecture_sha256']
        self.assertEqual(declared, worker.inventory(baseline))
        self.assertEqual(declared, worker.inventory(out / 'snapshot/architecture'))
        for name, text in extras.items():
            self.assertEqual((out / 'snapshot/architecture' / name).read_text(), text)
        self.assertEqual(worker.dispatch(self.root, out.name, {'action': 'list'}), {'files': []})
        resumed = worker.dispatch(self.root, out.name, dict(self.req, resume=True))
        self.assertEqual(resumed['run_dir'], str(out))

    def test_appended_deleted_or_changed_original_extra_rejects_resume(self):
        for operation in ('append', 'delete', 'change'):
            with self.subTest(operation=operation):
                baseline, _ = self.baseline(); run = 'original-extra-' + operation
                init = self.initialize(run); out = Path(init['run_dir']); old = (out / 'manifest.json').read_bytes()
                path = baseline / 'CHANGES.md'
                if operation == 'append':
                    (baseline / 'additional-proof.json').write_text('{"new":"evidence"}\n')
                elif operation == 'delete': path.unlink()
                else: path.write_text('Changed imported source narrative.\n')
                with self.assertRaisesRegex(ValueError, 'resume identity differs'):
                    worker.dispatch(self.root, run, dict(self.req, resume=True))
                self.assertEqual((out / 'manifest.json').read_bytes(), old)
                (baseline / 'additional-proof.json').unlink(missing_ok=True)

    def test_appended_deleted_or_changed_snapshot_extra_rejects_normal_actions(self):
        for operation in ('append', 'delete', 'change'):
            with self.subTest(operation=operation):
                self.baseline(); run = 'snapshot-extra-' + operation
                init = self.initialize(run); out = Path(init['run_dir']); old = (out / 'manifest.json').read_bytes()
                path = out / 'snapshot/architecture/CHANGES.md'
                if operation == 'append':
                    (out / 'snapshot/architecture/unrecorded-proof.json').write_text('{}\n')
                elif operation == 'delete': path.unlink()
                else: path.write_text('Changed frozen narrative.\n')
                for action in ('list', 'resume'):
                    with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'frozen architecture changed'):
                        worker.dispatch(self.root, run, dict(self.req, resume=True) if action == 'resume' else {'action': 'list'})
                self.assertEqual((out / 'manifest.json').read_bytes(), old)

    def test_missing_required_core_file_remains_rejected_before_freeze(self):
        baseline = self.root / 'result/architecture'
        required = ('architecture.md', 'design.json', 'budgets.json', 'review.json', 'decision.json',
                    'input/spec.md', 'input/reference.py', 'SOURCE.md')
        for number, name in enumerate(required):
            with self.subTest(name=name):
                path = baseline / name; original = path.read_bytes(); path.unlink()
                try:
                    with self.assertRaisesRegex(ValueError, 'missing architecture handoff'):
                        self.initialize('missing-core-' + str(number))
                finally: path.write_bytes(original)
                self.assertFalse((self.root / 'output' / ('missing-core-' + str(number))).exists())


if __name__ == '__main__':
    unittest.main()
