"""Executor tests use tiny local Python commands, never an EDA installation."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import experiments
from core import ReadinessError, create_variant, init_workspace, load_variant, read_json, save_json


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        project = self.root / "project"
        project.mkdir()
        (project / "top.sv").write_text("module top(input a, output y); assign y=a; endmodule\n")
        self.workspace = self.root / "workspace"
        init_workspace(project, self.workspace, ["top.sv"], "top")
        overlay = self.root / "overlay"
        overlay.mkdir()
        (overlay / "top.sv").write_text("module top(input a, output y); assign y=~a; endmodule\n")
        create_variant(self.workspace, "candidate", overlay)

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, code="print('PASS')", *, pair=False, **updates):
        argv = [sys.executable, "-B", "-c", code, "{tree}"]
        if pair:
            argv.append("{baseline_tree}")
        return {"kind": "command", "argv": argv, "timeout_seconds": 5,
                "memory_mib": 256, "success_markers": ["PASS"],
                "purpose": "regression", **updates}

    def test_real_command_and_idempotent_identity(self):
        config = self.command(pair=True)
        first = experiments.submit(self.workspace, variant="candidate", config=config, job_id="same", background=False)
        self.assertEqual(first["status"], "completed")
        self.assertTrue(first["verification_passed"])
        self.assertTrue(first["source_integrity_passed"])
        self.assertEqual(first["variant_identity"], load_variant(self.workspace, "candidate")["identity"])
        self.assertEqual(first["baseline_identity"], load_variant(self.workspace, "baseline")["identity"])
        again = experiments.submit(self.workspace, variant="candidate", config=config, job_id="same", background=False)
        self.assertEqual(first["started_utc"], again["started_utc"])
        self.assertEqual(first["request_identity"], again["request_identity"])
        self.assertIn("tool.log", first["artifact_hashes"])
        with self.assertRaises(ReadinessError):
            experiments.submit(self.workspace, variant="candidate", config=self.command("print('other')", pair=True), job_id="same", background=False)

    def test_measurement_key_excludes_variant_identity(self):
        config = self.command()
        a = experiments.submit(self.workspace, variant="baseline", config=config, job_id="baseline-run", background=False)
        b = experiments.submit(self.workspace, variant="candidate", config=config, job_id="candidate-run", background=False)
        self.assertEqual(a["measurement_key"], b["measurement_key"])
        self.assertNotEqual(a["request_identity"], b["request_identity"])

    def test_no_markers_or_pair_never_becomes_functional_proof(self):
        for name, config in [("no-markers", self.command(pair=True, success_markers=[])),
                             ("no-pair", self.command()),
                             ("wrong-purpose", self.command(pair=True, purpose="benchmark"))]:
            result = experiments.submit(self.workspace, variant="candidate", config=config, job_id=name, background=False)
            self.assertEqual(result["status"], "completed")
            self.assertFalse(result["verification_passed"])

    def test_missing_marker_and_nonzero_are_failures(self):
        for name, config in [("missing", self.command("print('NO')", pair=True)),
                             ("exit", self.command("print('PASS'); raise SystemExit(3)", pair=True))]:
            result = experiments.submit(self.workspace, variant="candidate", config=config, job_id=name, background=False)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["verification_passed"])

    def test_timeout_only_owned_group_and_no_retry(self):
        result = experiments.submit(self.workspace, variant="candidate",
                                    config=self.command("import time; time.sleep(5)", timeout_seconds=1),
                                    job_id="timeout", background=False)
        self.assertEqual(result["status"], "timeout")
        self.assertFalse(result["verification_passed"])
        self.assertLess(result["worker_elapsed_seconds"], 4)
        self.assertFalse(experiments._alive(result["process"]))

    def test_modified_copy_invalidates_result_and_preserves_variant(self):
        before = load_variant(self.workspace, "candidate")
        code = "import sys; from pathlib import Path; (Path(sys.argv[1])/'top.sv').write_text('bad'); print('PASS')"
        result = experiments.submit(self.workspace, variant="candidate", config=self.command(code, pair=True),
                                    job_id="mutation", background=False)
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["verification_passed"])
        self.assertFalse(result["source_integrity_passed"])
        self.assertEqual(before, load_variant(self.workspace, "candidate"))

    def test_background_status_and_concurrency(self):
        config = self.command("import time; time.sleep(1.5); print('PASS')", pair=True)
        result = experiments.submit(self.workspace, variant="candidate", config=config, job_id="background")
        self.assertIn(result["status"], {"submitted", "running"})
        again = experiments.submit(self.workspace, variant="candidate", config=config, job_id="background")
        self.assertEqual(result["request_identity"], again["request_identity"])
        with self.assertRaises(ReadinessError):
            experiments.submit(self.workspace, variant="candidate", config=self.command(), job_id="overlap")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = experiments.status(self.workspace, "background")
            if result["status"] in experiments.TERMINAL:
                break
            time.sleep(0.1)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["verification_passed"])
        self.assertIn("worker", read_json(self.workspace / "jobs/background/launcher.json"))

    def test_process_identity_reuse_does_not_count_alive(self):
        actual = experiments._proc_identity(__import__("os").getpid())
        self.assertTrue(experiments._alive(actual))
        self.assertFalse(experiments._alive({**actual, "start": "wrong-start"}))

    def test_worker_claim_cannot_be_replayed(self):
        result = experiments.submit(self.workspace, variant="candidate", config=self.command(), job_id="claimed", background=False)
        job = self.workspace / "jobs/claimed"
        (job / "result.json").unlink()
        with self.assertRaises(ReadinessError):
            experiments.worker(job)
        self.assertEqual(experiments.status(self.workspace, "claimed")["status"], "incomplete")
        self.assertTrue((job / "result.json").is_file())

    def test_background_descendants_are_cleaned_and_not_accepted(self):
        code = ("import subprocess,sys; p=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(30)']); print(p.pid, flush=True); print('PASS')")
        result = experiments.submit(self.workspace, variant="candidate", config=self.command(code, pair=True),
                                    job_id="descendants", background=False)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["reason"], "background_descendants_after_tool_exit")
        pid = int((self.workspace / "jobs/descendants/tool.log").read_text().splitlines()[0])
        self.assertIsNone(experiments._proc_identity(pid))
        self.assertFalse(result["verification_passed"])

    def test_changed_request_is_rejected_by_status(self):
        experiments.submit(self.workspace, variant="candidate", config=self.command(), job_id="request", background=False)
        path = self.workspace / "jobs/request/request.json"
        request = read_json(path)
        request["config"]["argv"] = ["changed"]
        save_json(path, request)
        with self.assertRaises(ReadinessError):
            experiments.status(self.workspace, "request")

    def test_source_changes_before_submit_refused(self):
        (self.workspace / "variants/candidate/tree/top.sv").write_text("tampered")
        with self.assertRaises(ReadinessError):
            experiments.submit(self.workspace, variant="candidate", config=self.command(), job_id="tampered")

    def test_include_options_use_space_free_bound_aliases_outside_source_tree(self):
        tree = (self.root / "source tree").resolve()
        request = {"top": "top", "sources": ["rtl/top.sv"],
                   "include_dirs": ["rtl/headers", "include with spaces", "."],
                   "config": {"defines": [], "parameters": {}}}
        # The actual executor always uses the fixed work/tree name. An enclosing
        # job path may contain spaces; neither aliases nor frontend tokens do.
        job = (self.root / "job with spaces").resolve()
        tree = job / "work/tree"
        (tree / "rtl/headers").mkdir(parents=True)
        (tree / "include with spaces").mkdir()
        request["request_identity"] = "test-request"
        aliases = experiments._prepare_include_aliases(request, job)
        command = experiments._read_design(request, tree)[0]
        self.assertIn('-I../include_paths/tree/0', command)
        self.assertIn('-I../include_paths/tree/1', command)
        self.assertIn('-I../include_paths/tree/2', command)
        self.assertNotIn('-I"', command)
        self.assertEqual((job / aliases[1]["alias"]).resolve(), (tree / "include with spaces").resolve())
        self.assertTrue(all(not (job / record["alias"]).is_relative_to(tree) for record in aliases))
        experiments._verify_include_aliases(job, aliases)
        (job / aliases[1]["alias"]).unlink()
        (job / aliases[1]["alias"]).symlink_to(tree)
        with self.assertRaises(ReadinessError):
            experiments._verify_include_aliases(job, aliases)

    def test_equiv_coverage_rejects_vacuous_or_missing_output(self):
        job = self.root / "coverage"
        job.mkdir()
        interface = {"ports": {"a": {"direction": "input", "bits": [2]},
                               "y": {"direction": "output", "bits": [3]}}}
        save_json(job / "gold-design.json", {"modules": {"top": interface}})
        save_json(job / "gate-design.json", {"modules": {"top": interface}})
        save_json(job / "equiv-design.json", {"modules": {"equiv": {**interface, "cells": {}}}})
        self.assertFalse(experiments._equiv_coverage(job, "top")["passed"])
        save_json(job / "equiv-design.json", {"modules": {"equiv": {**interface, "cells": {
            "proof": {"type": "$equiv", "connections": {"Y": [3]}}}}}})
        self.assertTrue(experiments._equiv_coverage(job, "top")["passed"])
        interface["ports"]["y"]["bits"].append(4)
        save_json(job / "gate-design.json", {"modules": {"top": interface}})
        self.assertFalse(experiments._equiv_coverage(job, "top")["passed"])


if __name__ == "__main__":
    unittest.main()
