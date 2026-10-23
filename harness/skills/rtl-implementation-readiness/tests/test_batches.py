"""Actual local Python experiments exercise grouping; no EDA or DUT claims."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import batches
from core import ReadinessError, create_variant, file_manifest, init_workspace, read_json, save_json
from experiments import _request, submit


class BatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        project = self.root / "project"
        project.mkdir()
        (project / "top.sv").write_text("module top(input a,output y);assign y=a;endmodule\n")
        self.workspace = self.root / "workspace"
        init_workspace(project, self.workspace, ["top.sv"], "top")
        overlay = self.root / "overlay"
        overlay.mkdir()
        (overlay / "top.sv").write_text("module top(input a,output y);assign y=a&a;endmodule\n")
        create_variant(self.workspace, "candidate", overlay)

    def config(self, *, pair=False, code="print('OK')", **changes):
        argv = [sys.executable, "-B", "-c", code, "{tree}/top.sv"]
        if pair:
            argv.append("{baseline_tree}/top.sv")
        return {"kind": "command", "argv": argv, "timeout_seconds": 2, "memory_mib": 256,
                "purpose": "regression" if pair else "measurement", "success_markers": ["OK"], **changes}

    def job(self, name, variant="baseline", *, config=None, **changes):
        return {"job_id": name, "variant": variant, "config": config or self.config(), **changes}

    def plan(self, jobs, *, name="plan.json", **changes):
        path = self.workspace / name
        save_json(path, {"jobs": jobs, **changes})
        return path

    def run_batch(self, path, batch_id="group", tool_seconds=20):
        return batches.batch(self.workspace, plan=path, batch_id=batch_id, tool_seconds=tool_seconds)

    def verify(self, path, batch_id="group", tool_seconds=20):
        return batches.verify_batch(self.workspace, plan=path, batch_id=batch_id, tool_seconds=tool_seconds)

    def options(self, baseline, candidate, verification=(), **changes):
        return {"variant": "candidate", "baseline_jobs": baseline, "candidate_jobs": candidate,
                "verification_jobs": list(verification), "out": "final", **changes}

    def test_actual_sequential_jobs_and_read_only_idempotence(self):
        path = self.plan([self.job("b"), self.job("c", "candidate")])
        result = self.run_batch(path)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([job["status"] for job in result["jobs"]], ["completed", "completed"])
        self.assertIsNone(result["finalization"])
        self.assertGreater(result["tool_elapsed_seconds"], 0)
        before = file_manifest(self.workspace)
        with patch("experiments.status", side_effect=AssertionError("verify must never inspect live processes")):
            self.assertEqual(self.verify(path), result)
            self.assertEqual(self.run_batch(path), result)
        self.assertEqual(before, file_manifest(self.workspace))

    def test_split_groups_finalize_references_prior_ids_without_running_them(self):
        first = self.plan([self.job("b"), self.job("c", "candidate")], name="measure.json")
        self.run_batch(first, batch_id="measure")
        before_jobs = file_manifest(self.workspace / "jobs")
        last = self.plan([self.job("v", "candidate", config=self.config(pair=True))], name="check.json",
                         finalize=self.options(["b"], ["c"], ["v"]))
        result = self.run_batch(last, batch_id="check")
        self.assertIsNotNone(result["finalization"])
        self.assertEqual(result["finalization"]["workspace"], str(self.workspace))
        self.assertEqual(read_json(self.workspace / "final/comparison.json")["min_repeats"], 3)
        self.assertEqual(read_json(self.workspace / "final/comparison.json")["baseline_jobs"], ["b"])
        after = file_manifest(self.workspace / "jobs")
        self.assertTrue(all(after[name] == value for name, value in before_jobs.items()))
        self.assertEqual(self.verify(last, batch_id="check"), result)

    def test_explicit_pilot_reuse_requires_exact_successful_request(self):
        config = self.config()
        submit(self.workspace, variant="baseline", config=config, job_id="pilot", background=False)
        before = file_manifest(self.workspace / "jobs/pilot")
        path = self.plan([self.job("pilot", config=config, reuse=True), self.job("c", "candidate")])
        result = self.run_batch(path, tool_seconds=2)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(before, file_manifest(self.workspace / "jobs/pilot"))
        wrong = self.plan([self.job("pilot", config=self.config(code="print('different')"), reuse=True)], name="wrong.json")
        with self.assertRaises(ReadinessError):
            self.run_batch(wrong, batch_id="wrong")
        implicit = self.plan([self.job("pilot")], name="implicit.json")
        with self.assertRaisesRegex(ReadinessError, "explicit reuse"):
            self.run_batch(implicit, batch_id="implicit")
        self.assertFalse((self.workspace / "batches/implicit").exists())

    def test_budget_admission_precedes_any_claim_or_job(self):
        path = self.plan([self.job("a"), self.job("b")])
        with self.assertRaisesRegex(ReadinessError, "timeouts exceed"):
            self.run_batch(path, tool_seconds=3)
        self.assertFalse((self.workspace / "jobs").exists())
        self.assertFalse((self.workspace / "batches").exists())
        for budget in (0, -1, True, 86401):
            with self.subTest(budget=budget), self.assertRaises(ReadinessError):
                self.run_batch(path, tool_seconds=budget)

    def test_failure_stops_remaining_jobs_and_does_not_resume(self):
        path = self.plan([self.job("bad", config=self.config(code="raise SystemExit(4)")), self.job("skip")])
        result = self.run_batch(path)
        self.assertEqual(result["status"], "failed")
        self.assertEqual([job["status"] for job in result["jobs"]], ["failed", "not_started"])
        self.assertFalse((self.workspace / "jobs/skip").exists())
        self.assertIsNone(result["finalization"])
        self.assertEqual(self.run_batch(path), result)
        self.assertFalse((self.workspace / "jobs/skip").exists())

    def test_failed_last_functional_job_can_finalize_honest_negative(self):
        path = self.plan([self.job("b"), self.job("c", "candidate"),
                          self.job("v", "candidate", config=self.config(pair=True, code="print('NO')"))],
                         finalize=self.options(["b"], ["c"], ["v"]))
        result = self.run_batch(path)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["finalization"]["decision"], "keep_baseline")
        self.assertEqual(result["finalization"]["reason"], "provided_functional_verification_invalid_or_failed")
        self.assertEqual(self.verify(path), result)

    def test_pending_id_and_failed_reuse_never_restart(self):
        failed_config = self.config(code="raise SystemExit(3)")
        submit(self.workspace, variant="baseline", config=failed_config, job_id="failed", background=False)
        failed = self.plan([self.job("failed", config=failed_config, reuse=True)], name="failed.json")
        with self.assertRaises(ReadinessError):
            self.run_batch(failed, batch_id="failed")
        config = self.config()
        request = _request(self.workspace, "baseline", config)
        pending = self.workspace / "jobs/pending"
        pending.mkdir(parents=True)
        save_json(pending / "request.json", request | {"job_id": "pending"})
        save_json(pending / "status.json", {"status": "submitted"})
        for reuse in (False, True):
            path = self.plan([self.job("pending", config=config, reuse=reuse)], name=f"pending-{reuse}.json")
            before = file_manifest(pending)
            with self.assertRaises(ReadinessError):
                self.run_batch(path, batch_id=f"pending-{reuse}")
            self.assertEqual(before, file_manifest(pending))
        self.assertFalse((pending / "result.json").exists())

    def test_interrupted_claim_is_observed_without_continuing(self):
        path = self.plan([self.job("first"), self.job("never")])
        original_submit = batches.submit
        def interrupt(*args, **kwargs):
            original_submit(*args, **kwargs)
            raise KeyboardInterrupt("disconnect")
        with patch.object(batches, "submit", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.run_batch(path)
        before = file_manifest(self.workspace)
        observed = self.run_batch(path)
        self.assertEqual(observed["status"], "incomplete")
        self.assertEqual([job["status"] for job in observed["jobs"]], ["completed", "not_started"])
        self.assertEqual(before, file_manifest(self.workspace))
        self.assertFalse((self.workspace / "jobs/never").exists())

    def test_new_job_claim_race_cannot_silently_reuse_or_finalize(self):
        path = self.plan([self.job("raced")])
        original_submit = batches.submit
        def race(*args, **kwargs):
            other = dict(kwargs)
            other.pop("require_new")
            original_submit(*args, **other)
            return original_submit(*args, **kwargs)
        with patch.object(batches, "submit", side_effect=race):
            result = self.run_batch(path)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("already reserved", result["error"])
        self.assertIsNone(result["finalization"])
        self.assertEqual(self.run_batch(path), result)

    def test_changed_plan_budget_source_tool_or_artifact_is_rejected_freshly(self):
        tool = self.workspace / "tool.py"
        tool.write_text(f"#!{sys.executable}\nprint('OK')\n")
        tool.chmod(0o755)
        config = self.config(argv=[str(tool), "{tree}/top.sv"])
        path = self.plan([self.job("a", config=config)])
        self.run_batch(path)
        with self.assertRaises(ReadinessError):
            self.verify(path, tool_seconds=21)
        original_plan = path.read_bytes()
        path.write_bytes(original_plan + b" \n")
        with self.assertRaises(ReadinessError):
            self.verify(path)
        path.write_bytes(original_plan)
        for target, changed in ((tool, b"#!/invalid\n"),
                                (self.workspace / "variants/baseline/tree/top.sv", b"edited"),
                                (self.workspace / "jobs/a/tool.log", b"edited"),
                                (self.workspace / "jobs/a/work/tree/top.sv", b"edited")):
            with self.subTest(target=target):
                original = target.read_bytes()
                target.write_bytes(changed)
                with self.assertRaises(ReadinessError):
                    self.verify(path)
                target.write_bytes(original)
        self.assertEqual(self.verify(path)["status"], "completed")

    def test_reserved_outside_symlink_and_malformed_plans_are_rejected(self):
        outside = self.root / "outside.json"
        save_json(outside, {"jobs": [self.job("a")]})
        alias = self.workspace / "alias.json"
        alias.symlink_to(outside)
        for path in (outside, alias):
            with self.assertRaises(ReadinessError):
                self.run_batch(path)
        for out in ("", ".", "./", "../outside", "/outside", "batches/final", "jobs/final", "variants/final"):
            path = self.plan([self.job("a")], finalize=self.options(["b"], ["c"], out=out))
            with self.subTest(out=out), self.assertRaises(ReadinessError):
                self.run_batch(path)
        for jobs in ([], [self.job("a"), self.job("a")], [self.job("a", reuse="yes")]):
            with self.subTest(jobs=jobs):
                path = self.plan(jobs)
                with self.assertRaises(ReadinessError):
                    self.run_batch(path)

    def test_cli_batch_and_verify_exact_invocation(self):
        path = self.plan([self.job("a")])
        script = Path(__file__).resolve().parents[1] / "scripts/readiness.py"
        flags = [str(self.workspace), "--plan", str(path), "--batch-id", "cli", "--tool-seconds", "2"]
        first = subprocess.run([sys.executable, "-B", str(script), "batch", *flags], capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = file_manifest(self.workspace)
        checked = subprocess.run([sys.executable, "-B", str(script), "verify-batch", *flags], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(first.stdout), json.loads(checked.stdout))
        self.assertEqual(before, file_manifest(self.workspace))


if __name__ == "__main__":
    unittest.main()
