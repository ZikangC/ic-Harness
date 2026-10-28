"""Synthetic transcripts test evidence semantics; no EDA command is executed."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import comparison
import health
from core import (ReadinessError, create_variant, digest, file_manifest, init_workspace,
                  load_variant, load_workspace, read_json, save_json, sha, variant_build, variant_tree)
from experiments import _request
from receipt_reuse import clone_workspace


def transcript(messages=(), *, count=None, fatal=False, complete=True):
    count = len(messages) if count is None else count
    return ("2. Executing CHECK pass (checking for obvious problems).\nChecking module top...\n"
            + "".join(message + "\n" for message in messages)
            + (f"Found and reported {count} problems.\n" if complete else "")
            + (f"ERROR: Found {count} problems in 'check -assert'.\n" if fatal else ""))


NO_DRIVER = "Warning: Wire top.\\dangling is used but has no driver."
OTHER_DRIVER = "Warning: Wire top.\\other is used but has no driver."


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        project = self.root / "project"
        (project / "rtl").mkdir(parents=True)
        (project / "rtl/defs.svh").write_text("`define CONSTANT 1\n")
        (project / "rtl/top.sv").write_text('`include "defs.svh"\nmodule top(input a,output y);assign y=a;endmodule\n')
        self.workspace = self.root / "workspace"
        init_workspace(project, self.workspace, ["rtl/top.sv"], "top", include_dirs=["rtl"], allowed_paths=["rtl/top.sv"])
        overlay = self.root / "overlay"
        (overlay / "rtl").mkdir(parents=True)
        (overlay / "rtl/top.sv").write_text('`include "defs.svh"\nmodule top(input a,output y);assign y=a&a;endmodule\n')
        create_variant(self.workspace, "changed", overlay)

    def evidence(self, name, variant="baseline", log=None, code=0, status="completed", *, checker=None, script=None):
        """Real files and byte pins, with explicitly synthetic log contents."""
        directory = self.root / name
        directory.mkdir()
        (directory / "run.ys").write_text(script if script is not None else "proc\nopt_clean\ncheck -assert\n")
        (directory / "tool.log").write_text(log if log is not None else transcript())
        result = {"exit_code": code, "started_utc": "2026-10-25T01:00:00+00:00",
                  "ended_utc": "2026-10-25T01:00:01+00:00"}
        if status is not None:
            result["status"] = status
        save_json(directory / "result.json", result)
        tree = variant_tree(self.workspace, variant)
        build = variant_build(load_workspace(self.workspace), load_variant(self.workspace, variant))
        descriptor = {"kind": "readiness_structure_evidence_v1", "source_root": str(tree),
                      "source_files": {rel: meta["sha256"] for rel, meta in file_manifest(tree).items()},
                      "build": build, "tool_kind": "yosys",
                      "tool": {"path": str(Path(sys.executable).resolve()), "sha256": sha(sys.executable)},
                      "checker": checker or {"scope": "full_design", "pass": "proc/check-assert",
                                              "macros": {}, "models": {}, "config": {}},
                      "artifacts": {key: {"path": str(directory / file), "sha256": sha(directory / file)}
                                    for key, file in (("script", "run.ys"), ("log", "tool.log"), ("result", "result.json"))}}
        path = directory / "evidence.json"
        save_json(path, descriptor)
        return path

    def fixture_job(self, name, variant="baseline", log=None, code=0, status="completed", *, kind="yosys", stage="proc", intact=True):
        config = {"kind": kind, "timeout_seconds": 5, "memory_mib": 256}
        if kind == "command":
            config.update(argv=[sys.executable, "-c", "print('fixture')"], purpose="measurement")
        else:
            config.update(tool=sys.executable, stage=stage)
        request = _request(self.workspace, variant, config)
        job = self.workspace / "jobs" / name
        job.mkdir(parents=True)
        save_json(job / "request.json", request | {"job_id": name})
        shutil.copytree(variant_tree(self.workspace, variant), job / "work/tree")
        # Includes use symlink aliases in real jobs; avoid aliases in this minimal
        # fixture while preserving the built-in request's source-bound include dirs.
        if kind == "yosys":
            from experiments import _prepare_include_aliases
            _prepare_include_aliases(request, job)
        (job / "tool.log").write_text(log if log is not None else transcript())
        (job / "experiment.ys").write_text("proc\nopt_clean\ncheck -assert\n")
        save_json(job / "execution.json", {"fixture": "synthetic; no EDA execution"})
        save_json(job / "result.json", {"status": status, "exit_code": code,
            "source_integrity_passed": intact, "verification_passed": False, "elapsed_seconds": 1,
            **{key: request.get(key) for key in ("request_identity", "workspace_id", "variant_identity",
                                                "baseline_identity", "measurement_key")},
            "artifact_hashes": {rel: sha(job / rel) for rel in ("tool.log", "experiment.ys", "execution.json")}})
        return name

    def test_no_evidence_remains_unknown_and_assessment_is_read_only(self):
        before = file_manifest(self.workspace)
        report = health.assess(self.workspace, "changed")
        self.assertEqual(report["summary"]["baseline_state"], "unknown")
        self.assertEqual(report["summary"]["candidate_state"], "unknown")
        self.assertEqual(report["summary"]["whole_design_readiness"], "not_established")
        self.assertEqual(report["identity"], digest({k: v for k, v in report.items() if k != "identity"}))
        self.assertEqual(health.verify_report(self.workspace, "changed", report), report)
        self.assertEqual(before, file_manifest(self.workspace))

    def test_clean_baseline_and_comparable_candidate_issue_is_new(self):
        b = self.evidence("base")
        c = self.evidence("candidate", "changed", transcript([NO_DRIVER], fatal=True), 1, "failed")
        report = health.assess(self.workspace, "changed", baseline_evidence=[b], candidate_evidence=[c])
        self.assertEqual(report["summary"]["baseline_state"], "clean_scoped")
        self.assertEqual(report["summary"]["candidate_state"], "issues")
        self.assertEqual(report["summary"]["candidate_new_count"], 1)
        finding = report["candidate"][0]["findings"][0]
        self.assertEqual(finding["category"], "undriven")
        self.assertEqual(finding["raw_line_ranges"], [[3, 3]])
        self.assertEqual(report["attribution"][0]["attribution"], "candidate_new")
        self.assertEqual(report["candidate"][0]["evidence_times"]["result"]["ended_utc"], "2026-10-25T01:00:01+00:00")
        health.verify_report(self.workspace, "changed", report)

    def test_same_diagnostic_is_existing_and_renamed_diagnostic_is_unknown(self):
        b = self.evidence("base", log=transcript([NO_DRIVER], fatal=True), code=1, status="failed")
        c = self.evidence("candidate", "changed", transcript([NO_DRIVER], fatal=True), 1, "failed")
        report = health.assess(self.workspace, "changed", baseline_evidence=[b], candidate_evidence=[c])
        candidate_findings = [entry for entry in report["attribution"] if entry["side"] == "candidate"]
        self.assertEqual(candidate_findings[0]["attribution"], "baseline_existing")
        self.assertEqual(report["summary"]["candidate_new_count"], 0)
        renamed = self.evidence("renamed", "changed", transcript([OTHER_DRIVER], fatal=True), 1, "failed")
        report = health.assess(self.workspace, "changed", baseline_evidence=[b], candidate_evidence=[renamed])
        self.assertEqual([entry["attribution"] for entry in report["attribution"] if entry["side"] == "candidate"], ["unknown"])

    def test_different_checker_cannot_attribute_issue_or_prove_fix(self):
        b = self.evidence("base", log=transcript([NO_DRIVER], fatal=True), code=1, status="failed")
        checker = {"scope": "one_lane", "pass": "proc/check-assert", "macros": {}, "models": {}, "config": {}}
        c = self.evidence("candidate", "changed", transcript([NO_DRIVER], fatal=True), 1, "failed", checker=checker)
        report = health.assess(self.workspace, "changed", baseline_evidence=[b], candidate_evidence=[c])
        self.assertEqual([entry["attribution"] for entry in report["attribution"] if entry["side"] == "candidate"], ["unknown"])
        clean = self.evidence("clean-candidate", "changed")
        report = health.assess(self.workspace, "changed", baseline_evidence=[b], candidate_evidence=[clean])
        self.assertEqual(report["summary"]["unresolved_count"], 1)
        self.assertTrue(any("does not establish" in line for line in report["limits"]))

    def test_timeout_incomplete_and_frontend_failure_preserve_observations(self):
        for index, (log, code, status) in enumerate((
            (transcript([NO_DRIVER], fatal=True), -15, "timeout"),
            (transcript([NO_DRIVER], complete=False), 1, "failed"),
            (transcript([NO_DRIVER], fatal=True) + "ERROR: syntax error\n", 1, "failed"),
        )):
            path = self.evidence("unknown-" + str(index), log=log, code=code, status=status)
            record = health.assess(self.workspace, "baseline", baseline_evidence=[path])["baseline"][0]
            self.assertEqual(record["state"], "unknown")
            self.assertEqual(len(record["observed_findings"]), 1)

    def test_later_truncated_check_never_borrows_earlier_clean_summary(self):
        path = self.evidence("truncated", log=transcript([NO_DRIVER], count=1)
                             + transcript() + transcript([OTHER_DRIVER], complete=False), code=1, status="failed")
        record = health.assess(self.workspace, "baseline", baseline_evidence=[path])["baseline"][0]
        self.assertEqual(record["state"], "unknown")
        self.assertEqual(record["checks_started"], 3)
        self.assertEqual(len(record["observed_findings"]), 2)
        self.assertEqual(len(record["findings"]), 2)

    def test_final_check_is_deduplicated_not_sum_of_repeated_checks(self):
        path = self.evidence("repeated", log=transcript([NO_DRIVER]) + transcript([NO_DRIVER], fatal=True),
                             code=1, status="failed")
        record = health.assess(self.workspace, "baseline", baseline_evidence=[path])["baseline"][0]
        self.assertEqual(record["state"], "issues")
        self.assertEqual(record["check"]["reported_problem_count"], 1)
        self.assertEqual(len(record["findings"]), 1)
        self.assertEqual(record["findings"][0]["raw_line_ranges"], [[7, 7]])

    def test_multiline_driver_diagnostic_includes_module_and_continuations(self):
        message = "Warning: multiple conflicting drivers for top.\\q:\n    port Q of cell first\n    port Y of cell second"
        path = self.evidence("multiline", log=transcript([message], fatal=True), code=1, status="failed")
        record = health.assess(self.workspace, "baseline", baseline_evidence=[path])["baseline"][0]
        finding = record["findings"][0]
        self.assertEqual(finding["module"], "top")
        self.assertEqual(finding["category"], "multiple_drivers")
        self.assertEqual(finding["raw_line_ranges"], [[3, 5]])
        self.assertIn("cell second", finding["diagnostic"])

    def test_absent_assert_comments_unknown_tool_and_contradictions_are_unknown(self):
        cases = [
            {"script": "# check -assert\nlog check -assert\n"},
            {"log": transcript([NO_DRIVER], count=0)},
            {"log": transcript([NO_DRIVER], fatal=True), "code": 0},
            {"log": transcript() + "ERROR: Found 2 problems in 'check -assert'.\n", "code": 1, "status": "failed"},
            {"log": "Executing CHECK pass\nMystery diagnostic without warning prefix\nFound 0 problems.\n"},
        ]
        for index, kwargs in enumerate(cases):
            with self.subTest(case=index):
                path = self.evidence("bad-" + str(index), **kwargs)
                self.assertEqual(health.assess(self.workspace, "baseline", baseline_evidence=[path])["baseline"][0]["state"], "unknown")
        path = self.evidence("unknown-tool")
        descriptor = read_json(path)
        descriptor["tool_kind"] = "other"
        save_json(path, descriptor)
        self.assertEqual(health.assess(self.workspace, "baseline", baseline_evidence=[path])["baseline"][0]["state"], "unknown")

    def test_unparsed_positive_count_is_observed_but_not_comparable(self):
        path = self.evidence("incomplete-accounting", log=transcript([NO_DRIVER], count=2, fatal=True), code=1, status="failed")
        record = health.assess(self.workspace, "baseline", baseline_evidence=[path])["baseline"][0]
        self.assertEqual(record["state"], "issues")
        self.assertFalse(record["comparable"])

    def test_compiled_include_closure_and_exact_build_are_required(self):
        for index, edit in enumerate((
            lambda descriptor: descriptor["source_files"].pop("rtl/defs.svh"),
            lambda descriptor: descriptor["source_files"].__setitem__("rtl/top.sv", "0" * 64),
            lambda descriptor: descriptor["build"].__setitem__("sources", ["rtl/defs.svh", "rtl/top.sv"]),
        )):
            path = self.evidence("bad-sources-" + str(index))
            descriptor = read_json(path)
            edit(descriptor)
            save_json(path, descriptor)
            with self.subTest(case=index), self.assertRaises(ReadinessError):
                health.assess(self.workspace, "baseline", baseline_evidence=[path])

    def test_old_unused_source_is_allowed_only_with_real_matching_bytes(self):
        path = self.evidence("unused")
        descriptor = read_json(path)
        source = self.root / "historical-source"
        shutil.copytree(variant_tree(self.workspace, "baseline"), source)
        (source / "rtl/unused.svh").write_text("// unused historical file\n")
        descriptor["source_root"] = str(source)
        descriptor["source_files"]["rtl/unused.svh"] = sha(source / "rtl/unused.svh")
        save_json(path, descriptor)
        report = health.assess(self.workspace, "baseline", baseline_evidence=[path])
        self.assertEqual(report["baseline"][0]["state"], "clean_scoped")
        (source / "rtl/unused.svh").write_text("// changed\n")
        with self.assertRaises(ReadinessError):
            health.verify_report(self.workspace, "baseline", report)

    def test_native_failed_receipt_is_readable_only_for_health_and_stays_source_bound(self):
        job = self.fixture_job("failed", log=transcript([NO_DRIVER], fatal=True), code=1, status="failed")
        with self.assertRaises(ReadinessError):
            comparison.receipt(self.workspace, job)
        record = health.assess(self.workspace, "baseline", baseline_jobs=[job])["baseline"][0]
        self.assertEqual(record["state"], "issues")
        self.assertIn("request", record["artifacts"])
        broken = self.fixture_job("broken", code=1, status="failed", intact=False)
        with self.assertRaises(ReadinessError):
            health.assess(self.workspace, "baseline", baseline_jobs=[broken])

    def test_native_elaborate_and_command_exit_zero_never_establish_clean(self):
        for name, kwargs in (("elab", {"stage": "elaborate"}), ("custom", {"kind": "command"})):
            self.fixture_job(name, **kwargs)
            record = health.assess(self.workspace, "baseline", baseline_jobs=[name])["baseline"][0]
            self.assertEqual(record["state"], "unknown")

    def test_native_actual_script_and_terminal_artifacts_are_required(self):
        job_id = self.fixture_job("clean")
        job = self.workspace / "jobs" / job_id
        report = health.assess(self.workspace, "baseline", baseline_jobs=[job_id])
        self.assertEqual(report["baseline"][0]["state"], "clean_scoped")
        for name in ("experiment.ys", "tool.log", "work/tree/rtl/top.sv"):
            path = job / name
            raw = path.read_bytes()
            path.write_bytes(raw + b"changed")
            with self.subTest(name=name), self.assertRaises(ReadinessError):
                health.verify_report(self.workspace, "baseline", report)
            path.write_bytes(raw)

    def test_referenced_receipt_reads_origin_artifacts_without_copying_or_running(self):
        job_id = self.fixture_job("referenced")
        clone = self.root / "clone"
        clone_workspace(self.workspace, clone, [job_id])
        report = health.assess(clone, "baseline", baseline_jobs=[job_id])
        self.assertEqual(report["baseline"][0]["artifacts"]["log"]["path"], str(self.workspace / "jobs/referenced/tool.log"))
        self.assertEqual(report["baseline"][0]["state"], "clean_scoped")
        health.verify_report(clone, "baseline", report)

    def test_revalidation_recomputes_instead_of_trusting_rehashed_claims(self):
        path = self.evidence("base")
        report = health.assess(self.workspace, "baseline", baseline_evidence=[path])
        report["summary"]["whole_design_readiness"] = "passed"
        report["identity"] = digest({k: v for k, v in report.items() if k != "identity"})
        with self.assertRaisesRegex(ReadinessError, "stale"):
            health.verify_report(self.workspace, "baseline", report)
        report = health.assess(self.workspace, "baseline", baseline_evidence=[path])
        descriptor = read_json(path)
        log = Path(descriptor["artifacts"]["log"]["path"])
        log.write_text(transcript([NO_DRIVER], fatal=True))
        with self.assertRaises(ReadinessError):
            health.verify_report(self.workspace, "baseline", report)

    def test_wrong_variant_duplicate_evidence_and_baseline_only_inputs_rejected(self):
        job = self.fixture_job("candidate", "changed")
        with self.assertRaises(ReadinessError):
            health.assess(self.workspace, "baseline", baseline_jobs=[job])
        path = self.evidence("base")
        with self.assertRaises(ReadinessError):
            health.assess(self.workspace, "baseline", baseline_evidence=[path, path])
        with self.assertRaises(ReadinessError):
            health.assess(self.workspace, "baseline", candidate_evidence=[path])

    def test_unknown_baseline_cannot_be_ignored_when_attributing_new_issue(self):
        clean = self.evidence("base-clean")
        unknown = self.evidence("base-timeout", code=-15, status="timeout")
        candidate = self.evidence("candidate", "changed", transcript([NO_DRIVER], fatal=True), 1, "failed")
        report = health.assess(self.workspace, "changed", baseline_evidence=[clean, unknown], candidate_evidence=[candidate])
        self.assertEqual(report["summary"]["candidate_new_count"], 0)
        self.assertEqual(report["attribution"][0]["attribution"], "unknown")

    def test_regional_build_changes_remain_comparable_at_same_checker_boundary(self):
        project = self.root / "regional-project"
        (project / "rtl").mkdir(parents=True)
        (project / "rtl/top.sv").write_text("module top; endmodule\n")
        (project / "sources.f").write_text("rtl/top.sv\n")
        self.workspace = self.root / "regional-workspace"
        init_workspace(project, self.workspace, ["rtl/top.sv"], "top", region_roots=["rtl"], build_files=["sources.f"])
        overlay = self.root / "regional-overlay"
        (overlay / "rtl").mkdir(parents=True)
        (overlay / "rtl/top.sv").write_text("module top; unit u(); endmodule\n")
        (overlay / "rtl/unit.sv").write_text("module unit; endmodule\n")
        (overlay / "sources.f").write_text("rtl/unit.sv\nrtl/top.sv\n")
        create_variant(self.workspace, "changed", overlay, build={"sources": ["rtl/unit.sv", "rtl/top.sv"], "include_dirs": []})
        b = self.evidence("regional-base")
        c = self.evidence("regional-candidate", "changed", transcript([NO_DRIVER], fatal=True), 1, "failed")
        report = health.assess(self.workspace, "changed", baseline_evidence=[b], candidate_evidence=[c])
        self.assertEqual(report["baseline"][0]["checker_key"], report["candidate"][0]["checker_key"])
        self.assertEqual(report["summary"]["candidate_new_count"], 1)


if __name__ == "__main__":
    unittest.main()
