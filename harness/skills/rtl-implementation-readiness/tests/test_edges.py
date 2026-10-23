"""Independent input/receipt/export boundary tests; no EDA is executed.

Mocks isolate measured-value comparison where noted. The failed verification
test executes a real Python command and keeps its original raw failed log.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import comparison
from core import (ReadinessError, create_variant, digest, file_manifest,
                  init_workspace, load_variant, read_json, save_json, variant_tree)
from experiments import submit


class EngineeringEdges(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "top.sv").write_text("module top(input a, output y); assign y=a; endmodule\n")
        self.workspace = self.root / "workspace"
        self.ws = init_workspace(self.project, self.workspace, ["top.sv"], "top")
        overlay = self.root / "overlay"
        overlay.mkdir()
        (overlay / "top.sv").write_text("module top(input a, output y); assign y=a&a; endmodule\n")
        self.candidate = create_variant(self.workspace, "candidate", overlay)

    def tearDown(self):
        self.temp.cleanup()

    def export_report(self):
        # The export tests isolate destination/source handling, independently
        # of measurement execution or recommendation policy.
        report = {"variant": "candidate", "baseline_jobs": ["base"], "candidate_jobs": ["cand"],
                  "verification_jobs": ["verify"], "metric": "elapsed_seconds", "min_ratio": 1.2,
                  "min_repeats": 1, "decision": "candidate_for_integration",
                  "workspace_id": self.ws["workspace_id"], "baseline_identity": self.ws["baseline_identity"],
                  "variant_identity": self.candidate["identity"]}
        report["identity"] = digest(report)
        return report

    def test_export_rejects_sealed_source_or_job_destinations(self):
        report = self.export_report()
        before = file_manifest(self.workspace / "variants")
        destinations = [variant_tree(self.workspace, "baseline") / "delivery",
                        variant_tree(self.workspace, "candidate") / "delivery",
                        self.workspace / "jobs" / "new-receipt"]
        with patch.object(comparison, "compare", return_value=report):
            for destination in destinations:
                with self.subTest(destination=destination):
                    with self.assertRaises(ReadinessError):
                        comparison.export(self.workspace, report, destination)
        self.assertEqual(before, file_manifest(self.workspace / "variants"))

    def test_export_cannot_silently_omit_changed_files_from_metadata(self):
        path = self.workspace / "variants/candidate/variant.json"
        metadata = read_json(path)
        metadata["changes"] = []
        save_json(path, metadata)
        report = self.export_report()
        try:
            with patch.object(comparison, "compare", return_value=report):
                comparison.export(self.workspace, report, self.workspace / "delivery")
        except ReadinessError:
            return  # Rejecting inconsistent derived metadata is also valid.
        self.assertGreater((self.workspace / "delivery/candidate.patch").stat().st_size, 0)
        self.assertIn("a&a", (self.workspace / "delivery/candidate.patch").read_text())

    def test_delivery_uses_revalidated_source_identity(self):
        fresh = self.export_report()
        changed = dict(fresh)
        changed["baseline_identity"] = "unrelated-baseline"
        changed["workspace_id"] = "unrelated-workspace"
        changed["identity"] = digest({k: v for k, v in changed.items() if k != "identity"})
        try:
            with patch.object(comparison, "compare", return_value=fresh):
                delivery = comparison.export(self.workspace, changed, self.workspace / "delivery")
        except ReadinessError:
            return
        self.assertEqual(delivery["baseline_identity"], self.ws["baseline_identity"])
        self.assertEqual(delivery["workspace_id"], self.ws["workspace_id"])

    @unittest.skipUnless(shutil.which("git"), "git required to check exported patch format")
    def test_exported_patch_applies_when_source_has_no_final_newline(self):
        # Use a separate workspace; never modify the existing sealed trees.
        (self.project / "top.sv").write_text((self.project / "top.sv").read_text().rstrip("\n"))
        self.workspace = self.root / "no-newline-workspace"
        self.ws = init_workspace(self.project, self.workspace, ["top.sv"], "top")
        overlay = self.root / "no-newline-overlay"
        overlay.mkdir()
        (overlay / "top.sv").write_text("module top(input a, output y); assign y=a&a; endmodule")
        self.candidate = create_variant(self.workspace, "candidate", overlay)
        report = self.export_report()
        with patch.object(comparison, "compare", return_value=report):
            comparison.export(self.workspace, report, self.workspace / "delivery")
        checkout = self.root / "apply-check"
        shutil.copytree(variant_tree(self.workspace, "baseline"), checkout)
        applied = subprocess.run(["git", "apply", str(self.workspace / "delivery/candidate.patch")],
                                 cwd=checkout, capture_output=True, text=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual((checkout / "top.sv").read_bytes(),
                         (variant_tree(self.workspace, "candidate") / "top.sv").read_bytes())

    def test_modified_success_flags_do_not_override_missing_command_marker(self):
        failed = submit(self.workspace, variant="candidate", job_id="failed-check", background=False,
                        config={"kind": "command", "argv": [sys.executable, "-c", "print('FAILED_CHECK')",
                                "{tree}/top.sv", "{baseline_tree}/top.sv"],
                                "purpose": "regression", "success_markers": ["PAIR_OK"],
                                "timeout_seconds": 5, "memory_mib": 256})
        self.assertFalse(failed["verification_passed"])
        result_path = self.workspace / "jobs/failed-check/result.json"
        result = read_json(result_path)
        result.update(status="completed", exit_code=0, verification_passed=True)
        save_json(result_path, result)
        original_receipt = comparison.receipt

        def measured_receipt(workspace, job):
            if job == "failed-check":
                return original_receipt(workspace, job)
            identity = self.ws["baseline_identity"] if job == "base" else self.candidate["identity"]
            return ({"measurement_key": "same-fixture-measurement", "variant_identity": identity,
                     "config": {"kind": "command", "purpose": "measurement"}},
                    {"elapsed_seconds": 10 if job == "base" else 1})

        with patch.object(comparison, "receipt", side_effect=measured_receipt):
            report = comparison.compare(self.workspace, "candidate", ["base"], ["cand"],
                                        ["failed-check"], min_repeats=1)
        self.assertNotEqual(report["decision"], "candidate_for_integration")
        self.assertEqual(report["valid_verification"], [])

    def test_parent_relative_include_inside_project_is_in_closure(self):
        other = self.root / "include-project"
        (other / "rtl/sub").mkdir(parents=True)
        (other / "rtl/defs.svh").write_text("`define WIDTH 1\n")
        (other / "rtl/sub/top.sv").write_text('`include "../defs.svh"\nmodule top; endmodule\n')
        ws = init_workspace(other, self.root / "include-workspace", ["rtl/sub/top.sv"], "top")
        self.assertIn("rtl/defs.svh", ws["files"])

    def test_include_text_inside_string_is_not_a_dependency(self):
        other = self.root / "string-project"
        other.mkdir()
        (other / "top.sv").write_text('module top; initial $display("`include \\"ghost.svh\\""); endmodule\n')
        ws = init_workspace(other, self.root / "string-workspace", ["top.sv"], "top")
        self.assertEqual(set(ws["files"]), {"top.sv"})


if __name__ == "__main__":
    unittest.main()
