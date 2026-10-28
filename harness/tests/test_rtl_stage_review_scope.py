"""Stage scope selection preserves routing and finite budgets; no transport/model jobs."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.stages import rtl


class StageReviewScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='stage-review-scope-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'harness/config'
        self.config.mkdir(parents=True)
        (self.config / 'environment.toml').write_text(
            '[project]\nroot="../.."\ninput_dir="input"\n[remote]\nssh_host="fixture"\n'
            '[eda]\npython="python"\nverilator="verilator"\nyosys="yosys"\nopenroad="openroad"\n')
        (self.config / 'models.toml').write_text('schema_version=1\n')
        skill = self.root / 'harness/skills/rtl-implementation/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('fixture instructions\n')
        self.provenance = self.root / 'provenance.json'
        self.provenance.write_text('{}')

    def invoke(self, *, readiness_only=True, configured_scope=None, returned_scope=None,
               verdict='pass', release=False, previous=None, adopted=True, enabled=False):
        self.config.joinpath('rtl.toml').write_text(
            '[rtl_audit]\nmax_repairs=2\n[readiness]\nenabled=' + str(enabled).lower() + '\nrtl_review=true\n'
            'max_review_repairs=5\n' +
            (f'review_scope="{configured_scope}"\n' if configured_scope is not None else ''))
        calls = []
        class Remote:
            worker = None
            def __init__(self, *args):
                pass
            def evidence(self, *args):
                pass
            def call(self, request):
                calls.append(request)
                if request['action'] == 'init':
                    return {'worker': '/unit/worker', 'run_dir': '/unit/run', 'capabilities': {},
                            'reference_path': '/unit/reference', 'input_source_paths': {}}
                if request['action'] == 'read':
                    return {'content': json.dumps(previous)}
                return {'ok': True}
        options = []
        def audit(context, remote, **kwargs):
            options.append(kwargs)
            return {'status': 'rtl_review_finished', 'review_verdict': verdict,
                    'review_scope': returned_scope or kwargs['review_scope'],
                    'rtl_stage_release': release}
        args = argparse.Namespace(config=self.config / 'environment.toml',
            resume='existing' if previous is not None else None,
            run_id=None if previous is not None else 'new', task=None, audit_only=False,
            validate_inputs=False, seed_provenance=self.provenance,
            seed_work='/unit/seed', seed_file=['rtl/top.sv'])
        with patch.object(rtl, 'validate_skills') as validate, \
             patch.object(rtl, 'load_input_bundle', return_value=(
                 {'spec.md': 'fixture', 'reference.py': 'pass'}, {}, {'source_paths': {}})), \
             patch.object(rtl, 'Remote', Remote), patch.object(rtl.signal, 'signal'), \
             patch.object(rtl, 'run_session', return_value={
                 'exit_code': 0, 'tool_isolation_ok': True, 'completed': True, 'final_text': ''}) as generate, \
             patch.object(rtl, 'run_readiness', return_value={
                 'status': 'rtl_readiness_completed', 'adopted': adopted}) as campaign, \
             patch.object(rtl, 'run_audit', side_effect=audit), patch('builtins.print'):
            code = rtl.run(args, readiness_only=readiness_only)
        state = [call['value'] for call in calls if call['action'] == 'set_status'][-1]
        initialized = next(call['settings'] for call in calls if call['action'] == 'init')
        return code, state, options, initialized, validate, campaign.call_count, generate.call_count

    def test_explicit_readiness_defaults_to_change_and_finishes_without_release(self):
        code, state, options, initialized, validate, campaigns, generated = self.invoke()
        self.assertEqual(code, 0)
        self.assertFalse(state['rtl_stage_release'])
        self.assertEqual(options[0]['review_scope'], 'change')
        self.assertIs(options[0]['repair_handler'], rtl.readiness_repair)
        self.assertEqual(options[0]['max_repairs'], 5)
        self.assertEqual(state['audit_repair_actor'], rtl.readiness_repair.name)
        self.assertTrue(initialized['readiness']['enabled'])
        self.assertEqual(validate.call_args.kwargs['required'], 'rtl-implementation-readiness')
        self.assertEqual((campaigns, generated), (1, 0))

    def test_readiness_can_explicitly_request_candidate_acceptance(self):
        code, state, options, *_ = self.invoke(configured_scope='candidate', release=True)
        self.assertEqual(code, 0)
        self.assertEqual(options[0]['review_scope'], 'candidate')
        self.assertTrue(state['rtl_stage_release'])

    def test_change_revision_is_not_successful_completion(self):
        code, state, *_ = self.invoke(verdict='revise')
        self.assertEqual(code, 1)
        self.assertFalse(state['rtl_stage_release'])

    def test_candidate_request_does_not_treat_returned_change_pass_as_success(self):
        code, state, *_ = self.invoke(configured_scope='candidate', returned_scope='change')
        self.assertEqual(code, 1)
        self.assertFalse(state['rtl_stage_release'])

    def test_ordinary_rtl_keeps_candidate_scope_and_implementation_budget(self):
        code, _, options, initialized, validate, _, generated = self.invoke(
            readiness_only=False, configured_scope='change', adopted=False, release=True)
        self.assertEqual(code, 0)
        self.assertEqual(options[0]['review_scope'], 'candidate')
        self.assertIs(options[0]['repair_handler'], rtl.implementation_repair)
        self.assertEqual(options[0]['max_repairs'], 2)
        self.assertFalse(initialized['readiness']['enabled'])
        self.assertEqual(validate.call_args.kwargs['required'], 'rtl-implementation')
        self.assertEqual(generated, 1)

    def test_ordinary_readiness_candidate_keeps_candidate_scope_and_origin_budget(self):
        _, state, options, *_ = self.invoke(readiness_only=False, adopted=True, release=True, enabled=True)
        self.assertEqual(options[0]['review_scope'], 'candidate')
        self.assertIs(options[0]['repair_handler'], rtl.readiness_repair)
        self.assertEqual(options[0]['max_repairs'], 5)
        self.assertEqual(state['audit_repair_actor'], rtl.readiness_repair.name)

    def test_readiness_resume_keeps_actor_budget_and_skips_new_campaign(self):
        code, _, options, _, _, campaigns, generated = self.invoke(previous={
            'phase': 'audit', 'audit_repair_actor': rtl.readiness_repair.name})
        self.assertEqual(code, 0)
        self.assertEqual(options[0]['review_scope'], 'change')
        self.assertIs(options[0]['repair_handler'], rtl.readiness_repair)
        self.assertEqual(options[0]['max_repairs'], 5)
        self.assertEqual((campaigns, generated), (0, 0))

    def test_unknown_scope_is_rejected_before_initialization(self):
        with self.assertRaisesRegex(ValueError, 'readiness.review_scope'):
            self.invoke(configured_scope='region')


if __name__ == '__main__':
    unittest.main()
