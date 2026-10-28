"""Structural-health delivery wiring; Python receipts and mock CHECK text, no EDA.

The intentional Python delay supplies bookkeeping samples only. Synthetic Yosys
transcripts exercise parsing/integrity plumbing and establish no DUT/tool claim.
"""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import batches
import comparison
import health
from core import (ReadinessError, create_variant, digest, file_manifest, init_workspace,
                  load_variant, read_json, save_json, sha, variant_build, variant_tree)
from experiments import submit


class HealthDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        project = self.root / "project"
        project.mkdir()
        (project / "top.sv").write_text("module top(input a,output y);assign y=a;endmodule\n")
        self.workspace = self.root / "workspace"
        self.ws = init_workspace(project, self.workspace, ["top.sv"], "top")
        overlay = self.root / "overlay"
        overlay.mkdir()
        (overlay / "top.sv").write_text("module top(input a,output y);assign y=a&a;endmodule\n")
        create_variant(self.workspace, "candidate", overlay)
        config = {"kind": "command", "argv": [sys.executable, "-B", "-c",
                  "import pathlib,sys,time;time.sleep(0.01 if '&' in pathlib.Path(sys.argv[1]).read_text() else 0.25)",
                  "{tree}/top.sv"], "purpose": "measurement", "timeout_seconds": 3,
                  "memory_mib": 256}
        for variant, job in (("baseline", "b0"), ("candidate", "c0")):
            self.assertEqual(submit(self.workspace, variant=variant, config=config,
                                    job_id=job, background=False)["status"], "completed")
        paired = {"kind": "command", "argv": [sys.executable, "-B", "-c", "print('PAIR_OK')",
                  "{baseline_tree}/top.sv", "{tree}/top.sv"], "purpose": "regression",
                  "success_markers": ["PAIR_OK"], "timeout_seconds": 3, "memory_mib": 256}
        self.assertTrue(submit(self.workspace, variant="candidate", config=paired,
                               job_id="check", background=False)["verification_passed"])

    def compare(self, **changes):
        return comparison.compare(self.workspace, "candidate", ["b0"], ["c0"], ["check"],
                                  min_repeats=1, **changes)

    def finalize(self, name="final", **changes):
        return comparison.finalize(self.workspace, "candidate", ["b0"], ["c0"], ["check"],
                                   destination=self.workspace / name, min_repeats=1, **changes)

    def external(self, variant, *, problem=False):
        """Build pinned mock transcript evidence, deliberately without EDA execution."""
        folder = self.workspace / "mock-checks" / variant
        folder.mkdir(parents=True)
        script = folder / "check.ys"
        script.write_text("proc\ncheck -assert\n")
        log = folder / "check.log"
        text = "1. Executing CHECK pass (checking for obvious problems).\nChecking module top...\n"
        if problem:
            text += ("Warning: Wire top.\\missing is used but has no driver.\n"
                     "Found and reported 1 problems.\nERROR: Found 1 problems in 'check -assert'.\n")
        else:
            text += "Found and reported 0 problems.\n"
        log.write_text(text)
        result = folder / "result.json"
        save_json(result, {"status": "failed" if problem else "completed", "exit_code": int(problem)})
        source = variant_tree(self.workspace, variant)
        selected = load_variant(self.workspace, variant)
        descriptor = folder / "evidence.json"
        save_json(descriptor, {"kind": "readiness_structure_evidence_v1",
                  "build": variant_build(self.ws, selected), "source_root": str(source),
                  "source_files": {"top.sv": sha(source / "top.sv")}, "tool_kind": "yosys",
                  "tool": {"path": str(Path(sys.executable).resolve()), "sha256": sha(Path(sys.executable).resolve())},
                  "checker": {"scope": "mock-component-test", "pass": "proc_check", "macros": {},
                              "models": {}, "config": {}},
                  "artifacts": {key: {"path": str(path), "sha256": sha(path)}
                                for key, path in (("script", script), ("log", log), ("result", result))}})
        return descriptor

    def report(self, *, baseline_problem=False, candidate_problem=False):
        baseline = self.external("baseline", problem=baseline_problem)
        candidate = self.external("candidate", problem=candidate_problem)
        path = self.workspace / "health.json"
        save_json(path, health.assess(self.workspace, "candidate", baseline_evidence=[baseline],
                                     candidate_evidence=[candidate]))
        return path

    def assert_unestablished(self, structural):
        self.assertEqual(structural["summary"]["whole_design_readiness"], "not_established")

    def test_default_unknown_preserves_scoped_proposal_without_readiness_claim(self):
        compared = self.compare()
        self.assertEqual(compared["decision"], "candidate_for_integration")
        self.assertIsNone(compared["health_report"])
        self.assertEqual(compared["structural_health"]["summary"]["candidate_state"], "unknown")
        self.assert_unestablished(compared["structural_health"])
        record = self.finalize()
        self.assertEqual(record["structural_health"]["whole_design_readiness"], "not_established")
        checked = comparison.verify_finalization(self.workspace, self.workspace / "final")
        self.assert_unestablished(checked["structural_health"])

    def test_explicit_report_flows_through_compare_finalize_and_read_only_verify(self):
        report = self.report()
        compared = self.compare(health=report)
        self.assertEqual(compared["structural_health"]["summary"]["candidate_state"], "clean_scoped")
        self.assertEqual(compared["health_report"], {"path": str(report), "sha256": sha(report)})
        self.assert_unestablished(compared["structural_health"])
        record = self.finalize(health=report)
        before = file_manifest(self.workspace)
        with patch("experiments.status", side_effect=AssertionError("verification must stay read-only")):
            checked = comparison.verify_finalization(self.workspace, self.workspace / "final")
        self.assertEqual(before, file_manifest(self.workspace))
        self.assertEqual(checked["decision"], "candidate_for_integration")
        self.assertEqual(checked["health_report"], record["health_report"])
        self.assertEqual(checked["structural_health"], read_json(report))

    def test_candidate_new_issue_downgrades_positive_delivery_and_verifies_freshly(self):
        report = self.report(candidate_problem=True)
        compared = self.compare(health=report)
        self.assertEqual(compared["structural_health"]["summary"]["candidate_new_count"], 1)
        self.assertEqual(compared["decision"], "evidence_incomplete")
        self.assertEqual(compared["reason"], "candidate_introduced_structural_issues")
        record = self.finalize(health=report)
        checked = comparison.verify_finalization(self.workspace, self.workspace / "final")
        self.assertEqual(checked["decision"], record["decision"])
        self.assertEqual(read_json(self.workspace / "final/delivery.json")["status"], "proposal_only_keep_original")
        # Health findings do not rewrite successful cost-measurement receipts.
        self.assertEqual(comparison.receipt(self.workspace, "c0")[1]["status"], "completed")

    def test_baseline_existing_issue_keeps_scoped_proposal_and_visible_limits(self):
        report = self.report(baseline_problem=True, candidate_problem=True)
        compared = self.compare(health=report)
        candidate = [entry for entry in compared["structural_health"]["attribution"] if entry["side"] == "candidate"]
        self.assertEqual([entry["attribution"] for entry in candidate], ["baseline_existing"])
        self.assertEqual(compared["decision"], "candidate_for_integration")
        self.assertEqual(compared["structural_health"]["summary"]["candidate_state"], "issues")
        self.assert_unestablished(compared["structural_health"])
        self.finalize(health=report)
        self.assert_unestablished(comparison.verify_finalization(self.workspace, self.workspace / "final")["structural_health"])

    def test_report_and_raw_evidence_mutations_are_rejected_freshly(self):
        report = self.report()
        self.finalize(health=report)
        original = report.read_bytes()
        changed = read_json(report)
        changed["summary"]["whole_design_readiness"] = "ready"
        changed["identity"] = digest({key: value for key, value in changed.items() if key != "identity"})
        save_json(report, changed)
        with self.assertRaises(ReadinessError):
            comparison.verify_finalization(self.workspace, self.workspace / "final")
        report.write_bytes(original)
        raw = self.workspace / "mock-checks/candidate/check.log"
        raw.write_text(raw.read_text() + "edited raw evidence\n")
        with self.assertRaises(ReadinessError):
            comparison.verify_finalization(self.workspace, self.workspace / "final")

    def test_rehashed_health_claim_cannot_hide_candidate_new_before_comparison(self):
        report = self.report(candidate_problem=True)
        forged = read_json(report)
        forged["summary"]["candidate_new_count"] = 0
        forged["candidate"][0]["state"] = "clean_scoped"
        forged["identity"] = digest({key: value for key, value in forged.items() if key != "identity"})
        save_json(report, forged)
        # No earlier comparison pins this file: the rejection must come from
        # re-reading the actual checker evidence, not a previous report SHA.
        with self.assertRaises(ReadinessError):
            self.compare(health=report)

    @staticmethod
    def legacy_compare(original):
        def old_schema(*args, **kwargs):
            value = original(*args, **kwargs)
            value.pop("structural_health", None)
            value.pop("health_report", None)
            value["limits"].remove(comparison.STRUCTURE_LIMIT)
            value["identity"] = digest({key: item for key, item in value.items() if key != "identity"})
            return value
        return old_schema

    def test_historical_finalization_without_health_verifies_and_reports_unknown(self):
        # Fixture generation mirrors the original no-health schema; verification
        # below runs the current unpatched code against those immutable bytes.
        with patch.object(comparison, "compare", self.legacy_compare(comparison.compare)):
            self.finalize()
        stored = read_json(self.workspace / "final/comparison.json")
        self.assertNotIn("structural_health", stored)
        before = file_manifest(self.workspace)
        checked = comparison.verify_finalization(self.workspace, self.workspace / "final")
        self.assertEqual(checked["decision"], "candidate_for_integration")
        self.assertEqual(checked["structural_health"]["summary"]["candidate_state"], "unknown")
        self.assert_unestablished(checked["structural_health"])
        self.assertEqual(before, file_manifest(self.workspace))

    def batch_plan(self, *, health_path=None):
        jobs = []
        for name in ("b0", "c0", "check"):
            request = read_json(self.workspace / "jobs" / name / "request.json")
            jobs.append({"job_id": name, "variant": request["variant"], "config": request["config"], "reuse": True})
        options = {"variant": "candidate", "baseline_jobs": ["b0"], "candidate_jobs": ["c0"],
                   "verification_jobs": ["check"], "min_repeats": 1, "out": "batch-final"}
        if health_path is not None:
            options["health"] = health_path.relative_to(self.workspace).as_posix()
        path = self.workspace / "plan.json"
        save_json(path, {"jobs": jobs, "finalize": options})
        return path

    def test_explicit_report_flows_through_batch_and_fresh_verification(self):
        report = self.report(candidate_problem=True)
        plan = self.batch_plan(health_path=report)
        jobs_before = file_manifest(self.workspace / "jobs")
        result = batches.batch(self.workspace, plan=plan, batch_id="group", tool_seconds=1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finalization"]["decision"], "evidence_incomplete")
        self.assertEqual(result["finalization"]["structural_health"]["summary"]["candidate_new_count"], 1)
        self.assertEqual(jobs_before, file_manifest(self.workspace / "jobs"))
        before = file_manifest(self.workspace)
        self.assertEqual(batches.verify_batch(self.workspace, plan=plan, batch_id="group", tool_seconds=1), result)
        self.assertEqual(before, file_manifest(self.workspace))

    def test_historical_batch_without_health_keeps_its_receipt_and_checks_unknown(self):
        plan = self.batch_plan()
        with patch.object(comparison, "compare", self.legacy_compare(comparison.compare)):
            batches.batch(self.workspace, plan=plan, batch_id="group", tool_seconds=1)
        # The historical batch verifier did not return either of these fields.
        path = self.workspace / "batches/group/result.json"
        result = read_json(path)
        result["finalization"].pop("structural_health", None)
        result["finalization"].pop("health_report", None)
        result["identity"] = digest({key: value for key, value in result.items() if key != "identity"})
        save_json(path, result)
        before = file_manifest(self.workspace)
        checked = batches.verify_batch(self.workspace, plan=plan, batch_id="group", tool_seconds=1)
        self.assertEqual(checked, result)
        fresh = comparison.verify_finalization(self.workspace, self.workspace / "batch-final")
        self.assertEqual(fresh["structural_health"]["summary"]["candidate_state"], "unknown")
        self.assert_unestablished(fresh["structural_health"])
        self.assertEqual(before, file_manifest(self.workspace))

    def test_cli_health_compare_finalize_and_verify_accept_explicit_report(self):
        script = Path(__file__).resolve().parents[1] / "scripts/readiness.py"
        report = self.workspace / "cli-health.json"
        def run(*args):
            process = subprocess.run([sys.executable, "-B", str(script), *map(str, args)],
                                     cwd=self.root, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            return json.loads(process.stdout)
        assessed = run("health", self.workspace, "--variant", "candidate", "--out", report)
        self.assert_unestablished(assessed)
        flags = [self.workspace, "--variant", "candidate", "--baseline-job", "b0", "--candidate-job", "c0",
                 "--verification-job", "check", "--min-repeats", 1, "--health", report]
        compared = run("compare", *flags, "--out", self.workspace / "cli-comparison.json")
        self.assertEqual(compared["structural_health"], assessed)
        record = run("finalize", *flags, "--out", self.workspace / "cli-final")
        checked = run("verify-finalization", self.workspace, "--delivery", self.workspace / "cli-final")
        self.assertEqual(checked["finalization_identity"], record["identity"])
        self.assert_unestablished(checked["structural_health"])


if __name__ == "__main__":
    unittest.main()
