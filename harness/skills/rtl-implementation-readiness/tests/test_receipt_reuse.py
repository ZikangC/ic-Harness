"""Synthetic terminal measurements test reuse provenance; fresh checks use Python."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import batches
import comparison
from core import (ReadinessError, create_variant, file_manifest, init_workspace,
                  load_variant, load_workspace, read_json, save_json, sha, variant_tree)
from experiments import _request, submit
from receipt_reuse import clone_workspace


class ReceiptReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        project = self.root / 'project'; (project / 'rtl').mkdir(parents=True)
        (project / 'rtl/top.sv').write_text('module top(input a,output y);assign y=a;endmodule\n')
        self.source, self.target = self.root / 'source', self.root / 'clone'
        init_workspace(project, self.source, ['rtl/top.sv'], 'top', allowed_paths=['rtl/top.sv'])
        overlay = self.root / 'overlay'; (overlay / 'rtl').mkdir(parents=True)
        (overlay / 'rtl/top.sv').write_text('module top(input a,output y);assign y=a&a;endmodule\n')
        create_variant(self.source, 'candidate', overlay)
        self.baseline, self.candidate = [], []
        for variant, names, prefix, elapsed in (('baseline', self.baseline, 'b', 10),
                                                ('candidate', self.candidate, 'c', 1)):
            for number in range(3):
                job_id = prefix + str(number); names.append(job_id)
                self.measurement(job_id, variant, elapsed + number * .01)
        self.jobs = self.baseline + self.candidate

    def measurement(self, job_id, variant, elapsed, config=None):
        config = config or {'kind': 'command', 'argv': [sys.executable, '-c', 'print("measurement fixture")'],
                            'purpose': 'measurement', 'timeout_seconds': 20, 'memory_mib': 256}
        request = _request(self.source, variant, config)
        job = self.source / 'jobs' / job_id; job.mkdir(parents=True)
        save_json(job / 'request.json', request | {'job_id': job_id})
        shutil.copytree(variant_tree(self.source, variant), job / 'work/tree')
        (job / 'tool.log').write_text('Synthetic completed measurement fixture; no EDA claim.\n')
        save_json(job / 'execution.json', {'fixture': 'synthetic measurement'})
        result = {'status': 'completed', 'exit_code': 0, 'source_integrity_passed': True,
                  'elapsed_seconds': elapsed, 'verification_passed': False,
                  **{key: request.get(key) for key in ('request_identity', 'workspace_id',
                      'variant_identity', 'baseline_identity', 'measurement_key')},
                  'artifact_hashes': {name: sha(job / name) for name in ('tool.log', 'execution.json')}}
        save_json(job / 'result.json', result)

    def clone(self):
        return clone_workspace(self.source, self.target, self.jobs)

    def check_config(self):
        return {'kind': 'command', 'argv': [sys.executable, '-B', '-c', 'print("PAIR_OK")',
                    '{tree}/rtl/top.sv', '{baseline_tree}/rtl/top.sv'], 'purpose': 'regression',
                'success_markers': ['PAIR_OK'], 'timeout_seconds': 3, 'memory_mib': 256}

    def test_clone_retains_source_bytes_identities_times_and_copies_no_raw_jobs(self):
        before = file_manifest(self.source); record = self.clone()
        self.assertEqual(record['workspace_id'], load_workspace(self.source)['workspace_id'])
        self.assertEqual(load_workspace(self.source), load_workspace(self.target))
        self.assertEqual(load_variant(self.source, 'candidate'), load_variant(self.target, 'candidate'))
        for job_id in self.jobs:
            self.assertEqual([p.name for p in (self.target / 'jobs' / job_id).iterdir()], ['reference.json'])
            original = comparison.receipt(self.source, job_id)
            self.assertEqual(comparison.receipt(self.target, job_id), original)
            self.assertEqual(original[0]['workspace_root'], str(self.source))
        self.assertEqual(before, file_manifest(self.source))

    def test_fresh_local_check_batch_finalizes_and_revalidates_imported_measurements(self):
        self.clone(); before = file_manifest(self.source)
        missing = comparison.compare(self.target, 'candidate', self.baseline, self.candidate)
        self.assertEqual(missing['decision'], 'evidence_incomplete')
        plan = self.target / 'check-plan.json'
        save_json(plan, {'jobs': [{'job_id': 'check', 'variant': 'candidate', 'config': self.check_config()}],
                         'finalize': {'variant': 'candidate', 'baseline_jobs': self.baseline,
                                      'candidate_jobs': self.candidate, 'verification_jobs': ['check'], 'out': 'final'}})
        result = batches.batch(self.target, plan=plan, batch_id='check-only', tool_seconds=3)
        self.assertEqual(result['finalization']['decision'], 'candidate_for_integration')
        self.assertEqual(batches.verify_batch(self.target, plan=plan, batch_id='check-only', tool_seconds=3), result)
        verified = comparison.verify_finalization(self.target, self.target / 'final')
        self.assertEqual(verified['changes'], ['rtl/top.sv'])
        self.assertEqual(before, file_manifest(self.source))
        self.assertEqual(read_json(self.target / 'jobs/check/request.json')['workspace_root'], str(self.target))

    def test_request_result_raw_artifact_and_source_copy_are_rechecked_each_time(self):
        self.clone()
        for name in ('request.json', 'result.json', 'tool.log', 'execution.json', 'work/tree/rtl/top.sv'):
            path = self.source / 'jobs/b0' / name; raw = path.read_bytes()
            path.write_bytes(raw + b'changed')
            with self.subTest(name=name), self.assertRaises((ReadinessError, ValueError)):
                comparison.receipt(self.target, 'b0')
            path.write_bytes(raw)
        self.assertEqual(comparison.receipt(self.target, 'b0'), comparison.receipt(self.source, 'b0'))

    def test_pending_failed_reference_chains_and_aliases_are_rejected(self):
        self.clone()
        reference = self.target / 'jobs/b0/reference.json'
        original = read_json(reference)
        third = self.root / 'third'
        clone_workspace(self.target, third, ['b0'])
        # Persisted references are direct; a manually introduced chain remains
        # invalid even though clone itself can flatten a verified direct one.
        chained = dict(original, source_workspace=str(third))
        from core import digest
        chained['identity'] = digest({k: v for k, v in chained.items() if k != 'identity'})
        save_json(reference, chained)
        with self.assertRaisesRegex(ReadinessError, 'chains'):
            comparison.receipt(self.target, 'b0')
        save_json(reference, original)
        shutil.copytree(self.target / 'jobs/b0', self.target / 'jobs/alias')
        with self.assertRaisesRegex(ReadinessError, 'original job ID'):
            comparison.receipt(self.target, 'alias')
        path = self.source / 'jobs/b0/result.json'; raw = path.read_bytes(); path.unlink()
        with self.assertRaises(ReadinessError):
            clone_workspace(self.source, self.root / 'pending', ['b0'])
        path.write_bytes(raw)
        result = read_json(path); save_json(path, result | {'status': 'failed', 'exit_code': 1})
        with self.assertRaises(ReadinessError):
            clone_workspace(self.source, self.root / 'failed', ['b0'])

    def test_target_sealed_source_tampering_and_local_conflict_are_rejected(self):
        self.clone()
        path = variant_tree(self.target, 'baseline') / 'rtl/top.sv'; raw = path.read_bytes()
        path.write_bytes(raw + b'changed')
        with self.assertRaises(ReadinessError): comparison.receipt(self.target, 'b0')
        path.write_bytes(raw)
        save_json(self.target / 'jobs/b0/request.json', {})
        with self.assertRaisesRegex(ReadinessError, 'conflicts with a local job'):
            comparison.receipt(self.target, 'b0')

    def test_cli_clones_only_selected_receipts_without_mutating_source(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/readiness.py'
        argv = [sys.executable, '-B', str(script), 'clone-workspace', str(self.source),
                '--workspace', str(self.target), '--job', 'b0', '--job', 'c0', '--variant', 'candidate']
        before = file_manifest(self.source)
        result = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['referenced_jobs'], ['b0', 'c0'])
        self.assertEqual(before, file_manifest(self.source))

    def test_builtin_yosys_measurement_without_command_purpose_can_be_referenced(self):
        # The executable is only pinned in this synthetic receipt; no Yosys runs.
        self.measurement('yosys-base', 'baseline', 10, config={'kind': 'yosys', 'tool': sys.executable,
                         'stage': 'proc', 'timeout_seconds': 20, 'memory_mib': 256})
        original = comparison.receipt(self.source, 'yosys-base')
        self.assertNotIn('purpose', original[0]['config'])
        clone_workspace(self.source, self.target, ['yosys-base'])
        self.assertEqual(comparison.receipt(self.target, 'yosys-base'), original)

    def proved_clone(self):
        self.clone()
        result = submit(self.target, variant='candidate', config=self.check_config(), job_id='functional', background=False)
        self.assertTrue(result['verification_passed'])
        final = self.root / 'final-clone'
        clone_workspace(self.target, final, self.jobs + ['functional'])
        return final

    def test_mixed_origin_references_and_paired_proof_finalize_without_rerunning_any_job(self):
        final = self.proved_clone()
        before_source, before_proof = file_manifest(self.source), file_manifest(self.target)
        for job_id in self.jobs:
            reference = read_json(final / 'jobs' / job_id / 'reference.json')
            self.assertEqual(reference['source_workspace'], str(self.source))
        self.assertEqual(read_json(final / 'jobs/functional/reference.json')['source_workspace'], str(self.target))
        record = comparison.finalize(final, 'candidate', self.baseline, self.candidate, ['functional'],
                                     destination=final / 'final')
        self.assertEqual(record['decision'], 'candidate_for_integration')
        self.assertEqual(comparison.verify_finalization(final, final / 'final')['decision'], 'candidate_for_integration')
        self.assertEqual(before_source, file_manifest(self.source))
        self.assertEqual(before_proof, file_manifest(self.target))

    def test_reused_proof_request_result_raw_and_both_source_copies_remain_pinned(self):
        final = self.proved_clone()
        for name in ('request.json', 'result.json', 'tool.log', 'work/tree/rtl/top.sv', 'work/baseline_tree/rtl/top.sv'):
            path = self.target / 'jobs/functional' / name; raw = path.read_bytes()
            path.write_bytes(raw + b'changed')
            with self.subTest(name=name), self.assertRaises((ReadinessError, ValueError)):
                comparison.receipt(final, 'functional')
            path.write_bytes(raw)
        self.assertEqual(comparison.receipt(final, 'functional'), comparison.receipt(self.target, 'functional'))

    def test_referenced_proof_requires_matching_valid_target_workspace_identity(self):
        final = self.proved_clone()
        project = self.root / 'different-project'; (project / 'rtl').mkdir(parents=True)
        (project / 'rtl/top.sv').write_text('module top(input a,output y);assign y=~a;endmodule\n')
        other = self.root / 'different-workspace'
        init_workspace(project, other, ['rtl/top.sv'], 'top', allowed_paths=['rtl/top.sv'])
        shutil.copytree(final / 'jobs/functional', other / 'jobs/functional')
        with self.assertRaisesRegex(ReadinessError, 'target sealed sources'):
            comparison.receipt(other, 'functional')

    def test_referenced_functional_marker_checks_use_origin_raw_log(self):
        self.clone()
        result = submit(self.target, variant='candidate', config=self.check_config(), job_id='functional', background=False)
        self.assertTrue(result['verification_passed'])
        job = self.target / 'jobs/functional'
        (job / 'tool.log').write_text('No successful marker in this synthetic intact log.\n')
        result = read_json(job / 'result.json')
        result['artifact_hashes']['tool.log'] = sha(job / 'tool.log')
        save_json(job / 'result.json', result)
        final = self.root / 'final-clone'
        clone_workspace(self.target, final, self.jobs + ['functional'])
        report = comparison.compare(final, 'candidate', self.baseline, self.candidate, ['functional'])
        self.assertEqual(report['decision'], 'keep_baseline')
        self.assertIn('successful marker', report['invalid_verification'][0]['reason'])

    def test_builtin_equivalence_coverage_is_checked_at_the_actual_proof_origin(self):
        self.clone()
        config = {'kind': 'equiv', 'tool': sys.executable, 'timeout_seconds': 20, 'memory_mib': 256}
        request = _request(self.target, 'candidate', config)
        job = self.target / 'jobs/equiv-proof'; job.mkdir(parents=True)
        save_json(job / 'request.json', request | {'job_id': 'equiv-proof'})
        shutil.copytree(variant_tree(self.target, 'candidate'), job / 'work/tree')
        shutil.copytree(variant_tree(self.target, 'baseline'), job / 'work/baseline_tree')
        (job / 'tool.log').write_text('Equivalence successfully proven!\n')
        for name in ('gold-design.json', 'gate-design.json'):
            save_json(job / name, {'modules': {'top': {'attributes': {}}}})
        save_json(job / 'result.json', {'status': 'completed', 'exit_code': 0,
            'source_integrity_passed': True, 'verification_passed': True, 'elapsed_seconds': 1,
            **{key: request.get(key) for key in ('request_identity', 'workspace_id', 'variant_identity',
                                                'baseline_identity', 'measurement_key')},
            'artifact_hashes': {name: sha(job / name) for name in ('tool.log', 'gold-design.json', 'gate-design.json')}})
        final = self.root / 'final-clone'
        clone_workspace(self.target, final, self.jobs + ['equiv-proof'])
        with patch('experiments._equiv_coverage', return_value={'passed': False}) as coverage:
            report = comparison.compare(final, 'candidate', self.baseline, self.candidate, ['equiv-proof'])
        self.assertEqual(report['decision'], 'keep_baseline')
        self.assertEqual(coverage.call_args.args[0], job)
        with patch('experiments._equiv_coverage', return_value={'passed': True}):
            report = comparison.compare(final, 'candidate', self.baseline, self.candidate, ['equiv-proof'])
        self.assertEqual(report['decision'], 'candidate_for_integration')


if __name__ == '__main__':
    unittest.main()
