"""Consume real delivery patches with Git; local bookkeeping fixtures, no EDA.

Git is an independent test consumer only. Production export and verification
continue to use the Python standard library.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import comparison
import experiments
from core import (create_variant, init_workspace, read_json, save_json, sha,
                  validate_variant_build, variant_build, variant_tree)


@unittest.skipUnless(shutil.which("git"), "git required as an independent delivery patch consumer")
class PatchRoundtripTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="readiness-patch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def write_files(self, root, files):
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def evidence(self, workspace):
        config = {"kind": "command", "argv": [sys.executable, "-B", "-c",
                  "print('LOCAL_PATCH_FIXTURE')", "{tree}"], "purpose": "measurement",
                  "timeout_seconds": 3, "memory_mib": 256}
        for job_id, variant, seconds in (("b", "baseline", 10), ("c", "candidate", 1)):
            # Source-bound synthetic receipts exercise compare/export/finalize
            # bookkeeping. These times are not measured RTL cost claims.
            request = experiments._request(workspace, variant, config)
            job = workspace / "jobs" / job_id
            job.mkdir(parents=True)
            save_json(job / "request.json", request | {"job_id": job_id})
            shutil.copytree(variant_tree(workspace, variant), job / "work/tree")
            (job / "tool.log").write_text("Synthetic patch fixture; no EDA was run.\n")
            artifacts = {"tool.log": sha(job / "tool.log")}
            if request["api_version"] == 3:
                save_json(job / "build.json", request["build"])
                artifacts["build.json"] = sha(job / "build.json")
            save_json(job / "result.json", {
                "status": "completed", "exit_code": 0, "source_integrity_passed": True,
                "elapsed_seconds": seconds, "verification_passed": False,
                **{key: request.get(key) for key in ("request_identity", "workspace_id",
                   "variant_identity", "baseline_identity", "measurement_key")},
                "artifact_hashes": artifacts})
        result = experiments.submit(workspace, variant="candidate", job_id="check", background=False,
            config={"kind": "command", "argv": [sys.executable, "-B", "-c", "print('PAIR_OK')",
                    "{baseline_tree}", "{tree}"], "purpose": "regression",
                    "success_markers": ["PAIR_OK"], "timeout_seconds": 3, "memory_mib": 256})
        self.assertTrue(result["verification_passed"], result)

    def assert_deliveries_roundtrip(self, workspace, ws, candidate, *, expected_build):
        self.evidence(workspace)
        exported, finalized = workspace / "exported", workspace / "finalized"
        report = comparison.compare(workspace, "candidate", ["b"], ["c"], ["check"], min_repeats=1)
        comparison.export(workspace, report, exported)
        comparison.finalize(workspace, "candidate", ["b"], ["c"], ["check"],
                            destination=finalized, min_repeats=1)
        self.assertEqual(report["decision"], "candidate_for_integration")
        sealed = variant_tree(workspace, "candidate")
        expected_files = {p.relative_to(sealed).as_posix(): p.read_bytes()
                          for p in sealed.rglob("*") if p.is_file()}
        for delivery in (exported, finalized):
            with self.subTest(delivery=delivery.name):
                # Apply the delivered artifact to a fresh baseline, without
                # calling _patch or trusting verify's regenerated patch.
                checkout = self.root / (workspace.name + "-" + delivery.name + "-applied")
                shutil.copytree(variant_tree(workspace, "baseline"), checkout)
                applied = subprocess.run(["git", "apply", str(delivery / "candidate.patch")],
                                         cwd=checkout, capture_output=True, text=True)
                self.assertEqual(applied.returncode, 0, applied.stderr)
                actual_files = {p.relative_to(checkout).as_posix(): p.read_bytes()
                                for p in checkout.rglob("*") if p.is_file()}
                self.assertEqual(actual_files, expected_files)
                metadata = read_json(delivery / "candidate.json")
                delivered_build = variant_build(ws, metadata)
                self.assertEqual(delivered_build, expected_build)
                if ws["api_version"] == 3:
                    # Recheck exact ordered sources/filelists and include closure
                    # against the independently reconstructed filesystem.
                    self.assertEqual(read_json(delivery / "delivery.json")["build"], expected_build)
                    self.assertEqual(validate_variant_build(ws, checkout, delivered_build), expected_build)
                self.assertEqual(metadata, candidate)
        return exported, finalized

    def test_regional_export_and_finalization_restore_all_file_operations(self):
        project = self.root / "project"
        baseline_sources = ["rtl/old.sv", "rtl/no_newline.sv", "rtl/top.sv"]
        self.write_files(project, {
            "rtl/top.sv": ('`include "old includes/空 header.svh"\n'
                           '`include "filled_to_empty.svh"\n'
                           '`include "empty_to_filled.svh"\nmodule top; endmodule\n').encode(),
            "rtl/old includes/空 header.svh": b"",
            "rtl/filled_to_empty.svh": b"`define WIDTH 1\n",
            "rtl/empty_to_filled.svh": b"",
            "rtl/old.sv": b"module old; endmodule",
            "rtl/no_newline.sv": b"module helper; endmodule",
            "sources.f": ("\n".join(baseline_sources) + "\n").encode(),
            "reference.bin": b"frozen\x00\xff\r\n"})
        workspace = self.root / "regional"
        ws = init_workspace(project, workspace, baseline_sources, "top",
                            ["rtl/old includes", "rtl"], ["reference.bin"],
                            region_roots=["rtl"], build_files=["sources.f"])
        build = {"top": "top", "sources": ["rtl/top.sv", "rtl/no_newline.sv", "rtl/new.sv"],
                 "include_dirs": ["rtl/new includes", "rtl"]}
        overlay = self.root / "overlay"
        self.write_files(overlay, {
            "rtl/top.sv": ('`include "new includes/新 header.svh"\n'
                           '`include "filled_to_empty.svh"\n'
                           '`include "empty_to_filled.svh"\nmodule top; wire a; endmodule').encode(),
            "rtl/new includes/新 header.svh": b"",
            "rtl/filled_to_empty.svh": b"",
            "rtl/empty_to_filled.svh": b"`define WIDTH 2",
            "rtl/no_newline.sv": b"module helper; wire a; endmodule",
            "rtl/new.sv": b"module new_leaf; endmodule\n",
            "sources.f": ("\n".join(build["sources"]) + "\n").encode()})
        candidate = create_variant(workspace, "candidate", overlay,
                                   delete_paths=["rtl/old.sv", "rtl/old includes/空 header.svh"], build=build)
        self.assertIn("rtl/new includes/新 header.svh", candidate["operations"]["add"])
        self.assertIn("rtl/old includes/空 header.svh", candidate["operations"]["delete"])
        self.assert_deliveries_roundtrip(workspace, ws, candidate, expected_build=build)

    def test_empty_operations_use_git_path_quoting_for_legal_names(self):
        names = ['space name', '中文', 'double"quote', 'tab\tname', 'line\nname']
        old_paths = ["rtl/old " + name + ".sv" for name in names]
        new_paths = ["rtl/new " + name + ".sv" for name in names]
        project, overlay = self.root / "quoted-project", self.root / "quoted-overlay"
        self.write_files(project, {"rtl/top.sv": b"module top; endmodule\n",
                                   **{rel: b"" for rel in old_paths}})
        workspace = self.root / "quoted"
        ws = init_workspace(project, workspace, [*old_paths, "rtl/top.sv"], "top", region_roots=["rtl"])
        self.write_files(overlay, {rel: b"" for rel in new_paths})
        build = {"top": "top", "sources": ["rtl/top.sv", *new_paths], "include_dirs": []}
        candidate = create_variant(workspace, "candidate", overlay, delete_paths=old_paths, build=build)
        self.assert_deliveries_roundtrip(workspace, ws, candidate, expected_build=build)

    def test_api_2_existing_nonempty_patch_format_and_roundtrip(self):
        project, overlay = self.root / "legacy-project", self.root / "legacy-overlay"
        before = b"module top; endmodule\n"
        after = b"module top; wire a; endmodule\n"
        self.write_files(project, {"top.sv": before})
        workspace = self.root / "legacy"
        ws = init_workspace(project, workspace, ["top.sv"], "top")
        self.write_files(overlay, {"top.sv": after})
        candidate = create_variant(workspace, "candidate", overlay)
        self.assertEqual(ws["api_version"], 2)
        outputs = self.assert_deliveries_roundtrip(workspace, ws, candidate,
            expected_build={"top": "top", "sources": ["top.sv"], "include_dirs": []})
        expected_patch = ("--- a/top.sv\n+++ b/top.sv\n@@ -1 +1 @@\n"
                          "-module top; endmodule\n+module top; wire a; endmodule\n")
        for output in outputs:
            self.assertEqual((output / "candidate.patch").read_text(), expected_patch)


if __name__ == "__main__":
    unittest.main()
