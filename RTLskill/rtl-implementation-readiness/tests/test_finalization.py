"""Atomic final output tests; local Python fixtures test receipts, never EDA benefit."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import comparison
from core import (ReadinessError, create_variant, digest, file_manifest, init_workspace,
                  read_json, save_json)
from experiments import submit


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        project = self.root / "project"
        project.mkdir()
        (project / "top.sv").write_text("module top(input a,output y);assign y=a;endmodule\n")
        self.workspace = self.root / "workspace"
        self.ws = init_workspace(project, self.workspace, ["top.sv"], "top")
        overlay = self.root / "overlay"
        overlay.mkdir()
        (overlay / "top.sv").write_text("module top(input a,output y);assign y=a&a;endmodule\n")
        create_variant(self.workspace, "candidate", overlay)
        self.baseline_jobs, self.candidate_jobs = [], []
        # Real command receipts; the fixture's intentional delay only supplies
        # deterministic bookkeeping samples, not synthesis or functional claims.
        config = {"kind": "command", "argv": [sys.executable, "-B", "-c",
                  "import pathlib,sys,time;time.sleep(0.01 if '&' in pathlib.Path(sys.argv[1]).read_text() else 0.25)",
                  "{tree}/top.sv"], "purpose": "measurement", "timeout_seconds": 3, "memory_mib": 256}
        for number in range(3):
            for variant, jobs, prefix in (("baseline", self.baseline_jobs, "b"),
                                          ("candidate", self.candidate_jobs, "c")):
                job = prefix + str(number)
                result = submit(self.workspace, variant=variant, config=config, job_id=job, background=False)
                self.assertEqual(result["status"], "completed")
                jobs.append(job)
        check = {"kind": "command", "argv": [sys.executable, "-B", "-c", "print('PAIR_OK')",
                 "{baseline_tree}/top.sv", "{tree}/top.sv"], "purpose": "regression",
                 "success_markers": ["PAIR_OK"], "timeout_seconds": 3, "memory_mib": 256}
        self.assertTrue(submit(self.workspace, variant="candidate", config=check,
                               job_id="check", background=False)["verification_passed"])
        self.destination = self.workspace / "report/final"

    def finalize(self, **changes):
        arguments = dict(destination=self.destination)
        arguments.update(changes)
        return comparison.finalize(self.workspace, "candidate", self.baseline_jobs,
                                   self.candidate_jobs, ["check"], **arguments)

    def test_positive_bundle_and_fresh_verifier_match_compare_without_writes(self):
        record = self.finalize()
        self.assertEqual(record["kind"], "readiness_finalization_v1")
        self.assertEqual(record["decision"], "candidate_for_integration")
        self.assertEqual(set(p.name for p in self.destination.iterdir()), comparison.FINALIZATION_FILES)
        self.assertEqual(record["identity"], digest({k: v for k, v in record.items() if k != "identity"}))
        self.assertEqual(record["comparison"], str(self.destination.resolve() / "comparison.json"))
        before = file_manifest(self.workspace)
        with patch("experiments.status", side_effect=AssertionError("live status must not be queried")):
            verified = comparison.verify_finalization(self.workspace, self.destination)
        self.assertEqual(before, file_manifest(self.workspace))
        self.assertEqual(verified["finalization_identity"], record["identity"])
        self.assertEqual(verified["reason"], record["reason"])
        self.assertEqual(verified["comparison_identity"], record["comparison_identity"])
        self.assertEqual(verified["changes"], ["top.sv"])
        self.assertEqual(verified["finalization_path"], str(self.destination.resolve() / "finalization.json"))

    def test_existing_exact_output_is_idempotent_and_other_invocation_is_conflict(self):
        first = self.finalize()
        before = file_manifest(self.destination)
        self.assertEqual(self.finalize(), first)
        self.assertEqual(before, file_manifest(self.destination))
        with self.assertRaisesRegex(ReadinessError, "different invocation"):
            self.finalize(min_ratio=1.3)
        self.assertEqual(before, file_manifest(self.destination))

    def test_missing_or_failed_functional_evidence_preserves_negative_decisions(self):
        missing = comparison.finalize(self.workspace, "candidate", self.baseline_jobs, self.candidate_jobs,
                                      destination=self.workspace / "missing")
        self.assertEqual(missing["decision"], "evidence_incomplete")
        failed = comparison.finalize(self.workspace, "candidate", self.baseline_jobs, self.candidate_jobs,
                                     ["missing-check"], destination=self.workspace / "failed")
        self.assertEqual(failed["decision"], "keep_baseline")
        for record in (missing, failed):
            checked = comparison.verify_finalization(self.workspace, record["delivery"])
            self.assertEqual(checked["decision"], record["decision"])
            self.assertEqual(read_json(Path(record["delivery"]) / "delivery.json")["status"], "proposal_only_keep_original")
        self.assertFalse((self.workspace / "jobs/missing-check").exists())

    def test_corrupt_record_patch_or_underlying_receipts_are_rejected_freshly(self):
        self.finalize()
        path = self.destination / "finalization.json"
        record = read_json(path)
        corrupted = dict(record, decision="keep_baseline")
        corrupted["identity"] = digest({k: v for k, v in corrupted.items() if k != "identity"})
        save_json(path, corrupted)
        with self.assertRaises(ReadinessError):
            comparison.verify_finalization(self.workspace, self.destination)
        save_json(path, record)
        patch_path = self.destination / "candidate.patch"
        original = patch_path.read_text()
        patch_path.write_text("forged patch\n")
        with self.assertRaises(ReadinessError):
            self.finalize()
        patch_path.write_text(original)
        (self.workspace / "jobs/b0/tool.log").write_text("edited raw evidence")
        with self.assertRaises(ReadinessError):
            comparison.verify_finalization(self.workspace, self.destination)

    def test_preparation_interruption_or_missing_file_does_not_publish_final_directory(self):
        real_export = comparison.export
        def interrupted(*args):
            real_export(*args)
            raise KeyboardInterrupt("interrupted preparation")
        with patch.object(comparison, "export", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.finalize()
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.glob(".finalize-*")), [])

        real_save = comparison.save_json
        def omit_record(path, value):
            if Path(path).name != "finalization.json":
                real_save(path, value)
        with patch.object(comparison, "save_json", side_effect=omit_record):
            with self.assertRaises(ReadinessError):
                self.finalize()
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.glob(".finalize-*")), [])

    def test_missing_terminal_receipt_is_rejected_without_status_repair(self):
        self.finalize()
        (self.workspace / "jobs/b0/result.json").unlink()
        before = file_manifest(self.workspace)
        with patch("experiments.status", side_effect=AssertionError("must not observe/repair a live job")):
            with self.assertRaises(ReadinessError):
                comparison.verify_finalization(self.workspace, self.destination)
        self.assertEqual(before, file_manifest(self.workspace))

    def test_existing_partial_or_extra_output_is_never_repaired_or_replaced(self):
        self.destination.mkdir(parents=True)
        (self.destination / "comparison.json").write_text("{}\n")
        before = file_manifest(self.destination)
        with self.assertRaises(ReadinessError):
            self.finalize()
        self.assertEqual(before, file_manifest(self.destination))
        (self.destination / "comparison.json").unlink()
        self.destination.rmdir()
        self.finalize()
        (self.destination / "extra.txt").write_text("unexpected")
        with self.assertRaises(ReadinessError):
            comparison.verify_finalization(self.workspace, self.destination)

    def test_outside_reserved_and_symlink_outputs_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.workspace / "alias"
        link.symlink_to(outside, target_is_directory=True)
        targets = (self.workspace, self.workspace / "jobs/final", self.workspace / "variants/final", self.workspace / "batches/final",
                   self.workspace / "workspace.json", outside / "final", link / "final")
        for destination in targets:
            with self.subTest(destination=destination):
                with self.assertRaises(ReadinessError):
                    self.finalize(destination=destination)
        self.assertEqual(list(outside.iterdir()), [])

    def test_cli_defaults_repeat_arguments_and_read_only_finalization_verification(self):
        script = Path(__file__).resolve().parents[1] / "scripts/readiness.py"
        argv = [sys.executable, "-B", str(script), "finalize", str(self.workspace),
                "--variant", "candidate", "--out=" + str(self.destination)]
        for flag, jobs in (("--baseline-job", self.baseline_jobs), ("--candidate-job", self.candidate_jobs),
                           ("--verification-job", ["check"])):
            for job in jobs:
                argv.extend([flag, job])
        first = subprocess.run(argv, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        record = json.loads(first.stdout)
        self.assertEqual(record["decision"], "candidate_for_integration")
        self.assertEqual(read_json(self.destination / "comparison.json")["min_repeats"], 3)
        before = file_manifest(self.workspace)
        checked = subprocess.run([sys.executable, "-B", str(script), "verify-finalization", str(self.workspace),
                                  "--delivery", str(self.destination)], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout)["finalization_identity"], record["identity"])
        self.assertEqual(before, file_manifest(self.workspace))


if __name__ == "__main__":
    unittest.main()
