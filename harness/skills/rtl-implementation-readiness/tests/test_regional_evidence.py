"""Regional evidence plumbing, using Python and synthetic timings, never EDA."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import comparison
import experiments
from core import (ReadinessError, create_variant, digest, file_manifest, init_workspace,
                  load_variant, load_workspace, read_json, save_json, sha, variant_tree)
from receipt_reuse import clone_workspace


class RegionalEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        project = self.root / "project"
        (project / "rtl/old").mkdir(parents=True)
        (project / "rtl/top.sv").write_text(
            "module top(input a,output y); leaf u(a,y); endmodule\n")
        (project / "rtl/old/leaf.sv").write_text(
            '`include "defs.svh"\nmodule leaf(input a,output y);assign y=a;endmodule\n')
        (project / "rtl/old/defs.svh").write_text("`define WIDTH 1\n")
        (project / "sources.f").write_text("rtl/old/leaf.sv\nrtl/top.sv\n")
        (project / "reference.py").write_text("# Frozen functional reference fixture.\n")
        self.workspace = self.root / "workspace"
        self.ws = init_workspace(project, self.workspace, ["rtl/old/leaf.sv", "rtl/top.sv"],
                                 "top", ["rtl/old"], ["reference.py"],
                                 region_roots=["rtl"], build_files=["sources.f"])
        overlay = self.root / "overlay"
        (overlay / "rtl/new").mkdir(parents=True)
        (overlay / "rtl/top.sv").write_text(
            "module top(input a,output y); datapath u(a,y); endmodule\n")
        (overlay / "rtl/new/datapath.sv").write_text(
            '`include "defs.svh"\nmodule datapath(input a,output y);assign y=a&a;endmodule\n')
        (overlay / "rtl/new/defs.svh").write_text("`define WIDTH 1\n")
        (overlay / "sources.f").write_text("rtl/new/datapath.sv\nrtl/top.sv\n")
        self.build = {"top": "top", "sources": ["rtl/new/datapath.sv", "rtl/top.sv"],
                      "include_dirs": ["rtl/new"]}
        self.candidate = create_variant(self.workspace, "candidate", overlay,
                                        delete_paths=["rtl/old/leaf.sv", "rtl/old/defs.svh"],
                                        build=self.build)

    def command(self, *, pair=False, purpose="measurement"):
        args = [sys.executable, "-B", "-c", "print('PAIR_OK')", "{tree}", "{build}"]
        if pair:
            args += ["{baseline_tree}", "{baseline_build}"]
        return {"kind": "command", "argv": args, "purpose": purpose,
                "success_markers": ["PAIR_OK"] if pair else [],
                "timeout_seconds": 3, "memory_mib": 256}

    def measurement(self, name, variant, seconds):
        # Source-bound terminal fixture tests comparison bookkeeping only. It is
        # deliberately labeled synthetic and makes no synthesis-benefit claim.
        request = experiments._request(self.workspace, variant, self.command())
        job = self.workspace / "jobs" / name
        job.mkdir(parents=True)
        save_json(job / "request.json", request | {"job_id": name})
        save_json(job / "build.json", request["build"])
        shutil.copytree(variant_tree(self.workspace, variant), job / "work/tree")
        (job / "tool.log").write_text("Synthetic timing fixture; no EDA was run.\n")
        save_json(job / "result.json", {
            "status": "completed", "exit_code": 0, "source_integrity_passed": True,
            "elapsed_seconds": seconds, "verification_passed": False,
            **{key: request.get(key) for key in ("request_identity", "workspace_id",
                "variant_identity", "baseline_identity", "measurement_key")},
            "artifact_hashes": {name: sha(job / name) for name in ("tool.log", "build.json")}})
        return request

    def finalization(self):
        self.measurement("b", "baseline", 10)
        self.measurement("c", "candidate", 1)
        result = experiments.submit(self.workspace, variant="candidate",
                                    config=self.command(pair=True, purpose="regression"),
                                    job_id="check", background=False)
        self.assertTrue(result["verification_passed"], result)
        return comparison.finalize(self.workspace, "candidate", ["b"], ["c"], ["check"],
                                   destination=self.workspace / "final", min_repeats=1)

    def test_per_side_build_is_sealed_but_unequal_lists_remain_comparable(self):
        config = {"kind": "yosys", "tool": sys.executable, "stage": "proc",
                  "timeout_seconds": 3, "memory_mib": 256}
        baseline = experiments._request(self.workspace, "baseline", config)
        candidate = experiments._request(self.workspace, "candidate", config)
        self.assertEqual(candidate["api_version"], 3)
        self.assertEqual(candidate["build"], self.build)
        self.assertNotEqual(baseline["sources"], candidate["sources"])
        self.assertNotEqual(baseline["include_dirs"], candidate["include_dirs"])
        self.assertEqual(baseline["measurement_key"], candidate["measurement_key"])
        self.assertNotEqual(baseline["verification_identity"], candidate["verification_identity"])
        self.assertNotEqual(baseline["request_identity"], candidate["request_identity"])
        different_budget = experiments._request(self.workspace, "candidate", config | {"memory_mib": 512})
        self.assertNotEqual(candidate["measurement_key"], different_budget["measurement_key"])

    def test_proof_script_and_aliases_read_each_actual_build(self):
        request = experiments._request(self.workspace, "candidate", {
            "kind": "equiv", "tool": sys.executable, "timeout_seconds": 3, "memory_mib": 256})
        job = self.root / "script job"
        for name, target in (("baseline", "baseline_tree"), ("candidate", "tree")):
            shutil.copytree(variant_tree(self.workspace, name), job / "work" / target)
        aliases = experiments._prepare_include_aliases(request, job)
        script = experiments._yosys_script(request, job).read_text()
        reads = [line for line in script.splitlines() if line.startswith("read_verilog ")]
        self.assertEqual(len(reads), 2)
        self.assertIn("baseline_tree/rtl/old/leaf.sv", reads[0])
        self.assertNotIn("datapath.sv", reads[0])
        self.assertIn("tree/rtl/new/datapath.sv", reads[1])
        self.assertNotIn("old/leaf.sv", reads[1])
        self.assertIn("-I../include_paths/baseline_tree/0", reads[0])
        self.assertIn("-I../include_paths/tree/0", reads[1])
        records = {record["alias"]: record["target"] for record in aliases}
        self.assertEqual(records["work/include_paths/baseline_tree/0"], "work/baseline_tree/rtl/old")
        self.assertEqual(records["work/include_paths/tree/0"], "work/tree/rtl/new")
        experiments._verify_include_aliases(job, aliases)

    def test_pair_command_gets_actual_build_json_and_distinct_file_closures(self):
        code = ("import json,pathlib,sys; a=json.load(open(sys.argv[1])); "
                "b=json.load(open(sys.argv[2])); "
                "assert a['sources'][0]=='rtl/new/datapath.sv'; "
                "assert b['sources'][0]=='rtl/old/leaf.sv'; "
                "assert not (pathlib.Path(sys.argv[3])/'rtl/old/leaf.sv').exists(); "
                "assert (pathlib.Path(sys.argv[4])/'rtl/old/leaf.sv').is_file(); print('PAIR_OK')")
        config = self.command(pair=True, purpose="regression")
        config["argv"] = [sys.executable, "-B", "-c", code, "{build}", "{baseline_build}",
                          "{tree}", "{baseline_tree}"]
        result = experiments.submit(self.workspace, variant="candidate", config=config,
                                    job_id="actual-builds", background=False)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["verification_passed"])
        self.assertIn("build.json", result["artifact_hashes"])
        self.assertIn("baseline-build.json", result["artifact_hashes"])
        comparison.receipt(self.workspace, "actual-builds")

    def test_re_signed_request_with_stale_build_is_refused(self):
        self.measurement("candidate", "candidate", 1)
        job = self.workspace / "jobs/candidate"
        request = read_json(job / "request.json")
        request["build"] = {key: self.ws[key] for key in ("top", "sources", "include_dirs")}
        request["request_identity"] = digest({key: value for key, value in request.items()
                                              if key not in ("request_identity", "job_id")})
        save_json(job / "request.json", request)
        result = read_json(job / "result.json")
        save_json(job / "result.json", result | {"request_identity": request["request_identity"]})
        with self.assertRaisesRegex(ReadinessError, "build"):
            comparison.receipt(self.workspace, "candidate")

    def test_command_cannot_change_the_bound_build_manifest(self):
        config = self.command()
        config["argv"] = [sys.executable, "-B", "-c",
                          "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('{}');print('PAIR_OK')",
                          "{build}"]
        result = experiments.submit(self.workspace, variant="candidate", config=config,
                                    job_id="build-mutation", background=False)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertFalse(result["source_integrity_passed"])
        self.assertIn("build manifest changed", result["error"])

    def test_add_delete_export_is_whole_candidate_and_rechecked(self):
        record = self.finalization()
        self.assertEqual(record["decision"], "candidate_for_integration")
        checked = comparison.verify_finalization(self.workspace, self.workspace / "final")
        self.assertEqual(checked["build"], self.build)
        self.assertEqual(checked["operations"], self.candidate["operations"])
        self.assertEqual(checked["frozen_assets"], ["reference.py"])
        self.assertEqual(checked["build_files"], ["sources.f"])
        self.assertEqual(checked["region_roots"], ["rtl"])
        self.assertNotIn("rtl/old/leaf.sv", checked["files"])
        self.assertIn("rtl/new/datapath.sv", checked["files"])
        patch = (self.workspace / "final/candidate.patch").read_text()
        self.assertIn("--- a/rtl/old/leaf.sv\n+++ /dev/null", patch)
        self.assertIn("--- /dev/null\n+++ b/rtl/new/datapath.sv", patch)
        self.assertIn("--- a/sources.f\n+++ b/sources.f", patch)
        delivery = self.workspace / "final/delivery.json"
        value = read_json(delivery)
        value["operations"]["delete"] = []
        save_json(delivery, value)
        with self.assertRaisesRegex(ReadinessError, "binding"):
            comparison.verify_finalization(self.workspace, self.workspace / "final")

    def test_clone_keeps_regional_build_operations_and_rechecks_origin(self):
        self.finalization()
        target = self.root / "clone"
        before = file_manifest(self.workspace)
        clone_workspace(self.workspace, target, ["b", "c", "check"])
        self.assertEqual(load_workspace(target), self.ws)
        self.assertEqual(load_variant(target, "candidate"), self.candidate)
        comparison.finalize(target, "candidate", ["b"], ["c"], ["check"],
                            destination=target / "final", min_repeats=1)
        checked = comparison.verify_finalization(target, target / "final")
        self.assertEqual(checked["operations"], self.candidate["operations"])
        self.assertEqual(checked["build"], self.build)
        self.assertEqual(before, file_manifest(self.workspace))
        origin = self.workspace / "jobs/check/build.json"
        origin.write_text("{}\n")
        with self.assertRaisesRegex(ReadinessError, "build manifest changed"):
            comparison.receipt(target, "check")
        with self.assertRaises(ReadinessError):
            comparison.verify_finalization(target, target / "final")


if __name__ == "__main__":
    unittest.main()
