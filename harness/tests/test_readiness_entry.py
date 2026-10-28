"""A real readiness entry imports a fresh seed and never invokes RTL generation."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.stages import readiness, rtl


class EntryTests(unittest.TestCase):
    def test_fresh_requires_explicit_seed_and_resume_forbids_reimport(self):
        base = {'resume': None, 'seed_work': None, 'seed_file': [], 'seed_provenance': None}
        with patch.object(readiness, 'run_rtl_controller') as controller:
            for change in ({}, {'seed_work': '/server/seed'}, {'seed_work': '/server/seed', 'seed_file': ['rtl/dut.sv']}):
                with self.assertRaisesRegex(ValueError, 'fresh readiness requires'):
                    readiness.run(argparse.Namespace(**(base | change)))
            with self.assertRaisesRegex(ValueError, 'omit all seed options'):
                readiness.run(argparse.Namespace(**(base | {'resume': 'retained', 'seed_work': '/server/seed'})))
            controller.assert_not_called()
            readiness.run(argparse.Namespace(**(base | {'resume': 'retained'})))
            self.assertTrue(controller.call_args.kwargs['readiness_only'])

    def test_explicit_entry_runs_with_disabled_ordinary_hook_and_no_generation_or_audit(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / 'harness/config'
            config.mkdir(parents=True)
            (config / 'environment.toml').write_text(
                '[project]\nroot="../.."\ninput_dir="input"\n'
                '[remote]\nssh_host="fixture"\n'
                '[eda]\npython="/fixture/python"\nverilator="/fixture/verilator"\nyosys="/fixture/yosys"\nopenroad="/fixture/openroad"\n')
            (config / 'rtl.toml').write_text('[readiness]\nenabled=false\n')
            (config / 'models.toml').write_text('schema_version=1\n')
            provenance = root / 'provenance.json'
            provenance.write_text('{"candidate_sha256":"frozen-candidate"}')
            args = argparse.Namespace(config=config / 'environment.toml', resume=None, run_id='fixture-seed',
                                      task=None, audit_only=False, validate_inputs=False,
                                      seed_work='/isolated/source/work', seed_file=['rtl/dut.sv'],
                                      seed_provenance=provenance)
            calls = []
            class Remote:
                worker = None
                def __init__(self, *a):
                    pass
                def evidence(self, *a):
                    pass
                def call(self, req):
                    calls.append(req)
                    if req['action'] == 'init':
                        return {'worker': '/fixture/frozen/worker.py', 'run_dir': '/fixture/run',
                                'capabilities': {}, 'reference_path': '/fixture/reference',
                                'input_source_paths': {}}
                    return {'ok': True}
            def hook(context, remote):
                self.assertTrue(context['settings']['readiness']['enabled'])
                initialized = next(q for q in calls if q['action'] == 'init')
                self.assertTrue(initialized['settings']['readiness']['enabled'])
                self.assertEqual(calls[-1]['action'], 'rtl_readiness_seed')
                self.assertEqual(calls[-1]['include_paths'], ['rtl/dut.sv'])
                self.assertEqual(calls[-1]['provenance']['candidate_sha256'], 'frozen-candidate')
                return {'status': 'rtl_readiness_completed', 'adopted': False}
            with patch.object(rtl, 'validate_skills'), \
                    patch.object(rtl, 'load_input_bundle', return_value=(
                        {'spec.md': 'fixture', 'reference.py': 'pass'}, {}, {'source_paths': {}})), \
                    patch.object(rtl, 'Remote', Remote), \
                    patch.object(rtl, 'run_session') as generation, \
                    patch.object(rtl, 'run_readiness', side_effect=hook), \
                    patch.object(rtl.signal, 'signal'), patch('builtins.print'):
                result = readiness.run(args)
            self.assertEqual(result, 0)
            generation.assert_not_called()
            self.assertFalse(any(q['action'].startswith('rtl_review') for q in calls))
            final = [q['value'] for q in calls if q['action'] == 'set_status'][-1]
            self.assertFalse(final['rtl_stage_release'])

    def run_formal_entry(self, *, adopted=True, audit_pass=True, resume_phase=None, extra_settings='', interrupt_audit=False):
        """No implementation skill is installed in this readiness-only fixture."""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / 'harness/config'
            config.mkdir(parents=True)
            (config / 'environment.toml').write_text(
                '[project]\nroot="../.."\ninput_dir="input"\n'
                '[remote]\nssh_host="fixture"\n'
                '[eda]\npython="/fixture/python"\nverilator="/fixture/verilator"\nyosys="/fixture/yosys"\nopenroad="/fixture/openroad"\n')
            (config / 'rtl.toml').write_text('[readiness]\nenabled=true\nrtl_review=true\n' + extra_settings)
            (config / 'models.toml').write_text('schema_version=1\n')
            provenance = root / 'provenance.json'
            provenance.write_text('{"candidate_sha256":"frozen-candidate"}')
            args = argparse.Namespace(config=config / 'environment.toml',
                                      resume='formal-seed' if resume_phase else None,
                                      run_id=None if resume_phase else 'formal-seed',
                                      task=None, audit_only=False, validate_inputs=False,
                                      seed_work=None if resume_phase else '/isolated/source/work',
                                      seed_file=[] if resume_phase else ['rtl/dut.sv'],
                                      seed_provenance=None if resume_phase else provenance)
            calls = []
            class Remote:
                worker = None
                def __init__(self, *a):
                    pass
                def evidence(self, *a):
                    pass
                def call(self, req):
                    calls.append(req)
                    if req['action'] == 'init':
                        return {'worker': '/fixture/frozen/worker.py', 'run_dir': '/fixture/run',
                                'capabilities': {}, 'reference_path': '/fixture/reference',
                                'input_source_paths': {}}
                    if req['action'] == 'read':
                        return {'text': '1: ' + json.dumps({'phase': resume_phase})}
                    return {'ok': True}
            def hook(context, remote):
                self.assertEqual(calls[-1]['action'], 'rtl_readiness_seed')
                return {'status': 'rtl_readiness_completed', 'adopted': adopted}
            def audit(context, remote, **options):
                self.assertFalse((Path(context['harness_dir']) / 'skills/rtl-implementation/SKILL.md').exists())
                self.assertIs(options['repair_handler'], rtl.readiness_repair)
                self.assertEqual(options['max_repairs'], 1)
                scope = context['settings']['readiness'].get('review_scope', 'change')
                self.assertEqual(options['review_scope'], scope)
                self.assertEqual(calls[-1]['value']['phase'], 'audit')
                if interrupt_audit:
                    raise KeyboardInterrupt()
                return {'status': 'rtl_review_finished', 'review_verdict': 'pass' if audit_pass else 'blocked',
                        'review_scope': scope, 'rtl_stage_release': audit_pass and scope == 'candidate'}
            with patch.object(rtl, 'validate_skills'), \
                    patch.object(rtl, 'load_input_bundle', return_value=(
                        {'spec.md': 'fixture', 'reference.py': 'pass'}, {}, {'source_paths': {}})), \
                    patch.object(rtl, 'Remote', Remote), \
                    patch.object(rtl, 'run_session') as generation, \
                    patch.object(rtl, 'run_readiness', side_effect=hook) as investigation, \
                    patch.object(rtl, 'run_audit', side_effect=audit) as independent, \
                    patch.object(rtl.signal, 'signal'), patch('builtins.print'):
                if interrupt_audit:
                    with self.assertRaises(KeyboardInterrupt):
                        readiness.run(args)
                    result = None
                else:
                    result = readiness.run(args)
            generation.assert_not_called()
            final = [req['value'] for req in calls if req['action'] == 'set_status'][-1]
            return result, investigation.call_count, independent.call_count, calls, final

    def test_opt_in_adopted_candidate_uses_existing_audit_with_readiness_repair(self):
        result, investigations, reviews, calls, final = self.run_formal_entry()
        self.assertEqual((result, investigations, reviews), (0, 1, 1))
        self.assertFalse(final['rtl_stage_release'])
        self.assertEqual(final['review_scope'], 'change')
        self.assertEqual(final['phase'], 'audit')

    def test_explicit_candidate_scope_retains_whole_candidate_release_route(self):
        result, investigations, reviews, calls, final = self.run_formal_entry(
            extra_settings='review_scope="candidate"\n')
        self.assertEqual((result, investigations, reviews), (0, 1, 1))
        self.assertEqual(final['review_scope'], 'candidate')
        self.assertTrue(final['rtl_stage_release'])

    def test_opt_in_negative_investigation_does_not_invoke_formal_audit(self):
        result, investigations, reviews, calls, final = self.run_formal_entry(adopted=False)
        self.assertEqual((result, investigations, reviews), (0, 1, 0))
        self.assertFalse(final['rtl_stage_release'])
        self.assertEqual(final['phase'], 'readiness')

    def test_opt_in_failed_formal_review_returns_failure(self):
        result, investigations, reviews, calls, final = self.run_formal_entry(audit_pass=False)
        self.assertEqual((result, investigations, reviews), (1, 1, 1))
        self.assertEqual(final['review_verdict'], 'blocked')
        self.assertFalse(final['rtl_stage_release'])

    def test_formal_audit_resume_validates_seed_without_repeating_investigation(self):
        result, investigations, reviews, calls, final = self.run_formal_entry(resume_phase='audit')
        self.assertEqual((result, investigations, reviews), (0, 0, 1))
        self.assertEqual([req for req in calls if req['action'] == 'rtl_readiness_seed'],
                         [{'action': 'rtl_readiness_seed', 'validate_only': True}])
        self.assertFalse(final['rtl_stage_release'])
        self.assertEqual(final['review_scope'], 'change')

    def test_formal_readiness_interrupt_preserves_persistent_jobs_and_audit_phase(self):
        result, investigations, reviews, calls, final = self.run_formal_entry(interrupt_audit=True)
        self.assertIsNone(result)
        self.assertEqual((investigations, reviews), (1, 1))
        self.assertEqual(final['status'], 'interrupted')
        self.assertEqual(final['phase'], 'audit')
        self.assertFalse(final['rtl_stage_release'])
        self.assertFalse(any(req['action'] in ('cancel', 'cancel_all', 'submit') for req in calls))

    def test_resume_validates_seed_before_hook_without_generation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / 'harness/config'
            config.mkdir(parents=True)
            (config / 'environment.toml').write_text(
                '[project]\nroot="../.."\ninput_dir="input"\n'
                '[remote]\nssh_host="fixture"\n'
                '[eda]\npython="/fixture/python"\nverilator="/fixture/verilator"\nyosys="/fixture/yosys"\nopenroad="/fixture/openroad"\n')
            (config / 'rtl.toml').write_text('[readiness]\nenabled=true\n')
            (config / 'models.toml').write_text('schema_version=1\n')
            args = argparse.Namespace(config=config / 'environment.toml', resume='retained-seed', run_id=None,
                                      task=None, audit_only=False, validate_inputs=False,
                                      seed_work=None, seed_file=[], seed_provenance=None)
            calls = []
            class Remote:
                worker = None
                def __init__(self, *a):
                    pass
                def evidence(self, *a):
                    pass
                def call(self, req):
                    calls.append(req)
                    if req['action'] == 'init':
                        return {'worker': '/fixture/frozen/worker.py', 'run_dir': '/fixture/run',
                                'capabilities': {}, 'reference_path': '/fixture/reference',
                                'input_source_paths': {}}
                    if req['action'] == 'read':
                        return {'text': '1: {"phase":"readiness"}'}
                    return {'ok': True}
            def hook(context, remote):
                self.assertEqual(calls[-1], {'action': 'rtl_readiness_seed', 'validate_only': True})
                return {'status': 'rtl_readiness_reused', 'adopted': False}
            with patch.object(rtl, 'validate_skills'), \
                    patch.object(rtl, 'load_input_bundle', return_value=(
                        {'spec.md': 'fixture', 'reference.py': 'pass'}, {}, {'source_paths': {}})), \
                    patch.object(rtl, 'Remote', Remote), \
                    patch.object(rtl, 'run_session') as generation, \
                    patch.object(rtl, 'run_readiness', side_effect=hook), \
                    patch.object(rtl.signal, 'signal'), patch('builtins.print'):
                result = readiness.run(args)
            self.assertEqual(result, 0)
            generation.assert_not_called()
            self.assertEqual(sum(q['action'] == 'rtl_readiness_seed' for q in calls), 1)
            self.assertFalse(any(q['action'].startswith('rtl_review') for q in calls))


if __name__ == '__main__':
    unittest.main()
