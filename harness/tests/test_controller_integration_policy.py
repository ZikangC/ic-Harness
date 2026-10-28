"""Offline controller routing/budget wiring; model and transport are substituted."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.stages import rtl
from harness.adapters import rtl_audit as audit
from harness.tests import test_rtl_audit_flow as audit_fixtures


class ControllerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='controller-policy-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / 'harness/config'
        self.config.mkdir(parents=True)
        (self.config / 'environment.toml').write_text(
            '[project]\nroot="../.."\ninput_dir="input"\n[remote]\nssh_host="fixture"\n'
            '[eda]\npython="python"\nverilator="verilator"\nyosys="yosys"\nopenroad="openroad"\n')
        (self.config / 'models.toml').write_text('schema_version=1\n')
        skill = self.root / 'harness/skills/rtl-implementation/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('fixture engineering instructions\n')
        self.settings()

    def settings(self, *, maximum='2', after_repair=False):
        (self.config / 'rtl.toml').write_text(
            '[rtl_audit]\nmax_repairs=' + maximum + '\n[readiness]\nenabled=true\n'
            'max_review_repairs=5\nafter_repair=' + str(after_repair).lower() + '\n')

    def invoke(self, *, previous=None, adopted=False, audit_hook=None):
        calls = []
        class Remote:
            worker = None
            def __init__(self, *args):
                pass
            def evidence(self, *args):
                pass
            def call(self, request):
                calls.append(request.copy())
                if request['action'] == 'init':
                    return {'worker': '/unit/frozen/worker.py', 'run_dir': '/unit/run',
                            'capabilities': {}, 'reference_path': '/unit/reference', 'input_source_paths': {}}
                if request['action'] == 'read':
                    return {'content': json.dumps(previous)}
                return {'ok': True}
        args = argparse.Namespace(config=self.config / 'environment.toml',
                                  resume='existing-policy' if previous is not None else None,
                                  run_id=None if previous is not None else 'new-policy', task=None,
                                  audit_only=False, validate_inputs=False)
        audit_options = []
        def audit(context, remote, **options):
            audit_options.append(options)
            if audit_hook:
                audit_hook(options)
            return {'status': 'rtl_review_finished', 'review_verdict': 'pass', 'rtl_stage_release': True}
        adoptions = iter(adopted) if isinstance(adopted, tuple) else None
        def investigate(*args, **kwargs):
            return {'status': 'rtl_readiness_completed',
                    'adopted': next(adoptions) if adoptions is not None else adopted}
        with patch.object(rtl, 'validate_skills'), \
                patch.object(rtl, 'load_input_bundle', return_value=(
                    {'spec.md': 'fixture', 'reference.py': 'pass'}, {}, {'source_paths': {}})), \
                patch.object(rtl, 'Remote', Remote), patch.object(rtl.signal, 'signal'), \
                patch.object(rtl, 'run_session', return_value={
                    'exit_code': 0, 'tool_isolation_ok': True, 'completed': True, 'final_text': ''}) as generate, \
                patch.object(rtl, 'run_readiness', side_effect=investigate) as readiness, \
                patch.object(rtl, 'run_audit', side_effect=audit), patch('builtins.print'):
            self.assertEqual(rtl.run(args), 0)
        state = [q['value'] for q in calls if q['action'] == 'set_status'][-1]
        return audit_options, state, readiness.call_count, generate.call_count

    def test_ordinary_candidate_uses_finite_implementation_policy(self):
        options, state, readiness, generated = self.invoke()
        self.assertIs(options[0]['repair_handler'], rtl.implementation_repair)
        self.assertEqual(options[0]['max_repairs'], 2)
        self.assertEqual(state['audit_repair_actor'], rtl.implementation_repair.name)
        self.assertEqual((readiness, generated), (1, 1))

    def test_readiness_modified_candidate_returns_to_readiness(self):
        options, state, _, _ = self.invoke(adopted=True)
        self.assertIs(options[0]['repair_handler'], rtl.readiness_repair)
        self.assertEqual(options[0]['max_repairs'], 5)
        self.assertEqual(state['audit_repair_actor'], rtl.readiness_repair.name)

    def test_audit_resume_uses_persisted_actor_without_new_generation_or_readiness(self):
        # This is the persisted window after a repair callback adopted a new
        # readiness candidate but before its next immutable review was frozen.
        previous = {'phase': 'audit', 'audit_repair_actor': rtl.readiness_repair.name,
                    'readiness': {'adopted': False}, 'readiness_after_repair': {'adopted': True}}
        options, state, readiness, generated = self.invoke(previous=previous)
        self.assertIs(options[0]['repair_handler'], rtl.readiness_repair)
        self.assertEqual(options[0]['max_repairs'], 5)
        self.assertEqual(state['audit_repair_actor'], rtl.readiness_repair.name)
        self.assertEqual((readiness, generated), (0, 0))

    def test_legacy_resume_uses_structured_controller_adoption(self):
        options, _, readiness, generated = self.invoke(previous={
            'phase': 'audit', 'readiness': {'adopted': True},
            'summary': 'This untrusted prose deliberately names rtl-implementation.'})
        self.assertIs(options[0]['repair_handler'], rtl.readiness_repair)
        self.assertEqual((readiness, generated), (0, 0))

    def test_after_repair_adoption_hands_new_policy_to_audit_and_persists_actor(self):
        self.settings(after_repair=True)
        returned = []
        def hook(options):
            returned.append(options['before_freeze']())
        options, state, readiness, _ = self.invoke(adopted=(False, True), audit_hook=hook)
        self.assertEqual(readiness, 2)
        self.assertIs(options[0]['repair_handler'], rtl.implementation_repair)
        self.assertEqual(options[0]['max_repairs'], 2)
        self.assertEqual(returned, [{'repair_handler': rtl.readiness_repair, 'max_repairs': 5}])
        self.assertEqual(state['audit_repair_actor'], rtl.readiness_repair.name)

    def test_invalid_round_budget_fails_before_remote_initialization(self):
        for value in ('-1', 'true', '"3"'):
            with self.subTest(value=value):
                self.settings(maximum=value)
                args = argparse.Namespace(config=self.config / 'environment.toml')
                with patch.object(rtl, 'validate_skills'), \
                        patch.object(rtl, 'load_input_bundle', return_value=(
                            {'spec.md': 'fixture', 'reference.py': 'pass'}, {}, {'source_paths': {}})), \
                        patch.object(rtl, 'Remote') as remote, patch.object(rtl.signal, 'signal'):
                    with self.assertRaisesRegex(ValueError, 'rtl_audit.max_repairs'):
                        rtl.run(args)
                    remote.assert_not_called()

    def test_readiness_skill_can_validate_without_other_skills(self):
        harness = self.root / 'readiness-only'
        skill = harness / 'skills/rtl-implementation-readiness'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('fixture readiness method\n')
        fingerprints = {'rtl-implementation-readiness/' + name: value
                        for name, value in rtl.inventory(skill).items()}
        (harness / 'skills/manifest.json').write_text(json.dumps({'sha256': fingerprints}))
        rtl.validate_skills(harness, required='rtl-implementation-readiness')
        with self.assertRaisesRegex(ValueError, 'required skill missing: rtl-implementation'):
            rtl.validate_skills(harness)


class CompletedRepairTransitionTests(unittest.TestCase):
    setUp = audit_fixtures.FlowTests.setUp
    prepare_repair = audit_fixtures.FlowTests.prepare_repair

    def test_resume_after_callback_uses_original_completed_receipt_then_new_selected_policy(self):
        self.prepare_repair(self.remote)
        old_review = self.remote.current['review']
        complete = {'completed': True, 'tool_isolation_ok': True}
        audit._save_receipt(self.remote, self.remote.current, 'repair', complete,
                            'original-implementation-session', rtl.implementation_repair.name)
        self.remote.current['repair'] = {'status': 'completed'}
        self.assertTrue(audit._reserve_repair(
            self.remote, self.remote.current, rtl.implementation_repair.name, 1))
        before = dict(self.remote.files)
        roles = []
        def session(prompt, role, *args):
            roles.append(role)
            self.assertEqual(role, 'review', 'the completed old repair must never be rerun')
            self.remote.decide('pass')
            return complete
        with patch.object(audit, 'run_session', side_effect=session):
            result = audit.run(self.context, self.remote,
                               repair_handler=rtl.readiness_repair, max_repairs=0)
        self.assertTrue(result['rtl_stage_release'])
        self.assertEqual(roles, ['review'])
        self.assertFalse(any(q['action'] in ('rtl_repair_begin', 'rtl_repair_end')
                             for q in self.remote.calls))
        self.assertEqual([q.get('seed_review') for q in self.remote.calls
                          if q['action'] == 'rtl_review_begin'], [None, old_review])
        for path, content in before.items():
            self.assertEqual(self.remote.files[path], content)
        readiness_ledger = 'repair-budget-' + rtl.digest(rtl.readiness_repair.name.encode()) + '.json'
        self.assertFalse(any(path.endswith(readiness_ledger) for path in self.remote.files))

    def test_active_original_repair_cannot_inherit_next_actor_before_completion(self):
        self.prepare_repair(self.remote)
        self.remote.current['repair'] = {'status': 'active'}
        audit._save_receipt(self.remote, self.remote.current, 'repair',
                            {'completed': False, 'tool_isolation_ok': False},
                            'original-implementation-session', rtl.implementation_repair.name)
        with patch.object(audit, 'run_session', side_effect=AssertionError('wrong executor ran')):
            result = audit.run(self.context, self.remote,
                               repair_handler=rtl.readiness_repair, max_repairs=1)
        self.assertEqual(result['status'], 'rtl_repair_actor_mismatch')
        self.assertFalse(result['rtl_stage_release'])
        self.assertEqual(self.remote.number, 1)

    def test_completed_repair_with_changed_candidate_or_report_cannot_advance(self):
        for field in ('candidate_sha256', 'decision_report_sha256'):
            with self.subTest(field=field):
                remote = audit_fixtures.Remote()
                self.prepare_repair(remote)
                remote.current['repair'] = {'status': 'completed'}
                audit._save_receipt(remote, remote.current, 'repair',
                                    {'completed': True, 'tool_isolation_ok': True},
                                    'original-implementation-session', rtl.implementation_repair.name)
                path = '@run/control/rtl-audit/review-1.repair.json'
                receipt = json.loads(remote.files[path]); receipt[field] = 'another-candidate-or-report'
                remote.files[path] = json.dumps(receipt)
                with patch.object(audit, 'run_session', side_effect=AssertionError('stale receipt advanced')):
                    result = audit.run(self.context, remote,
                                       repair_handler=rtl.readiness_repair, max_repairs=1)
                self.assertEqual(result['status'], 'rtl_repair_actor_mismatch')
                self.assertFalse(result['rtl_stage_release'])
                self.assertEqual(remote.number, 1)


if __name__ == '__main__':
    unittest.main()
