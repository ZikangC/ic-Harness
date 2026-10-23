"""Caller-selected readiness repair returns to the same independent RTL auditor."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.adapters import rtl_audit as audit
from harness.adapters import readiness_repair
from harness.tests import test_rtl_audit_flow as fixtures


class ReadinessRepairTests(unittest.TestCase):
    setUp = fixtures.FlowTests.setUp

    def install_readiness(self):
        skill = self.harness / 'skills/rtl-implementation-readiness/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('Standalone readiness investigation and experiment guidance.\n')
        return skill

    def test_handler_loads_readiness_without_implementation_skill(self):
        skill = self.install_readiness()
        for path in (self.harness / 'skills/rtl-implementation').rglob('*'):
            if path.is_file():
                path.unlink()
        self.remote.call({'action': 'rtl_review_begin'})
        self.remote.decide('revise')
        review = self.remote.current
        result = readiness_repair.prepare(self.context, self.remote, review)
        self.assertEqual(result.role, 'implementation')
        self.assertEqual(result.context_updates, {'repair_skill': 'rtl-implementation-readiness'})
        self.assertTrue(result.prompt.startswith(skill.read_text()))
        self.assertEqual(result.prompt.count(self.task), 1)
        for name in ('architecture.md', 'SOURCE.md', 'decision.json'):
            self.assertEqual(result.prompt.count(self.remote.architecture_texts[name]), 1)
        self.assertIn('@run/' + review['decision']['report_path'], result.prompt)
        self.assertIn('/server/run/' + review['candidate_scope'], result.prompt)
        self.assertIn('/server/run/work/readiness-repair/review-1', result.prompt)

    def test_revise_returns_to_readiness_then_fresh_existing_rtl_audit(self):
        self.install_readiness()
        observed = []
        def session(prompt, role, models, root, context_path, *args):
            child = json.loads(context_path.read_text())
            observed.append((role, child))
            if role == 'review':
                self.remote.decide('revise' if self.remote.number == 1 else 'pass')
                self.assertNotIn('repair_skill', child)
            else:
                self.assertEqual(child['role'], 'implementation')
                self.assertEqual(child['scope'], 'work')
                self.assertEqual(child['repair_skill'], 'rtl-implementation-readiness')
                self.assertIn('Standalone readiness investigation', prompt)
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(audit, 'run_session', side_effect=session):
            result = audit.run(self.context, self.remote,
                               repair_handler=readiness_repair.readiness_repair, max_repairs=1)
        self.assertTrue(result['rtl_stage_release'])
        self.assertEqual([role for role, _ in observed], ['review', 'implementation', 'review'])
        self.assertEqual([child['scope'] for _, child in observed],
                         ['audit/review-1', 'work', 'audit/review-2'])
        receipt = json.loads(self.remote.files['@run/control/rtl-audit/review-1.repair.json'])
        self.assertEqual(receipt['repair_actor'], 'rtl-implementation-readiness')

    def test_selected_missing_skill_is_explicit_and_never_falls_back_to_implementation(self):
        self.remote.call({'action': 'rtl_review_begin'})
        self.remote.decide('revise')
        with self.assertRaisesRegex(FileNotFoundError, 'selected readiness repair skill'):
            readiness_repair.prepare(self.context, self.remote, self.remote.current)

    def test_non_revision_decision_cannot_be_used_as_source_repair(self):
        self.install_readiness()
        for verdict in ('pass', 'blocked', 'insufficient_evidence'):
            with self.subTest(verdict=verdict):
                remote = fixtures.Remote()
                remote.call({'action': 'rtl_review_begin'})
                remote.decide(verdict)
                with self.assertRaisesRegex(ValueError, 'requires an independent revise'):
                    readiness_repair.prepare(self.context, remote, remote.current)


if __name__ == '__main__':
    unittest.main()
