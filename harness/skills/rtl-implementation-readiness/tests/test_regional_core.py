"""Regional candidate ownership, exact build closure, and legacy compatibility.

These tests execute file/CLI operations only. They do not claim RTL equivalence
or synthesis-cost improvement.
"""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from core import (ReadinessError, context_identity, create_variant, digest,
                  file_manifest, init_workspace, load_variant, load_workspace,
                  read_json, save_json, variant_build, variant_identity, variant_tree)


class RegionalCore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "rtl").mkdir(parents=True)
        (self.project / "rtl/top.sv").write_text("module top(input a, output y); assign y=a; endmodule\n")
        (self.project / "rtl/old.sv").write_text("module old; endmodule\n")
        (self.project / "sources.f").write_text("# baseline\nrtl/old.sv\nrtl/top.sv\n")
        (self.project / "check.py").write_text("print('frozen checker')\n")
        self.workspace = self.root / "workspace"
        self.ws = init_workspace(self.project, self.workspace, ["rtl/old.sv", "rtl/top.sv"], "top",
                                 assets=["check.py"], region_roots=["rtl/region"], build_files=["sources.f"])

    def tearDown(self):
        self.temp.cleanup()

    def overlay(self, files):
        path = self.root / ("overlay-" + str(len(list(self.root.glob("overlay-*")))))
        path.mkdir()
        for rel, content in files.items():
            dest = path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        return path

    def extracted(self, name="extracted"):
        overlay = self.overlay({
            "rtl/region/lane.sv": "module lane(input a, output y); assign y=a; endmodule\n",
            "rtl/top.sv": "module top(input a, output y); lane u(a,y); endmodule\n",
            "sources.f": "# refactored\nrtl/region/lane.sv\nrtl/top.sv\n"})
        return create_variant(self.workspace, name, overlay, delete_paths=["rtl/old.sv"],
                              build={"sources": ["rtl/region/lane.sv", "rtl/top.sv"], "include_dirs": []})

    def test_cross_file_candidate_is_one_sealed_build(self):
        baseline = file_manifest(variant_tree(self.workspace, "baseline"))
        original = file_manifest(self.project)
        candidate = self.extracted()
        self.assertEqual(candidate["operations"], {"add": ["rtl/region/lane.sv"],
                         "modify": ["rtl/top.sv", "sources.f"], "delete": ["rtl/old.sv"]})
        self.assertEqual(candidate, load_variant(self.workspace, "extracted"))
        self.assertEqual(variant_build(self.ws, candidate)["sources"], ["rtl/region/lane.sv", "rtl/top.sv"])
        self.assertEqual(file_manifest(variant_tree(self.workspace, "baseline")), baseline)
        self.assertEqual(file_manifest(self.project), original)
        # Exact source order participates in identity, independently of file bytes.
        reordered = dict(candidate["build"], sources=list(reversed(candidate["build"]["sources"])))
        self.assertNotEqual(candidate["identity"], variant_identity(self.ws, candidate["files"], reordered))

    def test_added_headers_are_legal_only_when_reached(self):
        overlay = self.overlay({"rtl/top.sv": '`include "region/defs.svh"\nmodule top; endmodule\n',
                                "rtl/region/defs.svh": "`define WIDTH 1\n"})
        obj = create_variant(self.workspace, "header", overlay)
        self.assertIn("rtl/region/defs.svh", obj["operations"]["add"])
        dead = self.overlay({"rtl/region/dead.sv": "module dead; endmodule\n"})
        with self.assertRaisesRegex(ReadinessError, "omitted"):
            create_variant(self.workspace, "dead", dead)
        self.assertFalse((self.workspace / "variants/dead").exists())

    def test_new_files_do_not_grant_permission_to_existing_inputs(self):
        for files in ({"other/new.sv": "module new; endmodule\n"},
                      {"rtl/region/check.py": "print('new checker')\n"},
                      {"check.py": "print('changed')\n"}):
            with self.subTest(files=files), self.assertRaises(ReadinessError):
                create_variant(self.workspace, "bad", self.overlay(files))
        with self.assertRaises(ReadinessError):
            create_variant(self.workspace, "asset-delete", self.overlay({}), delete_paths=["check.py"])

    def test_explicit_rtl_asset_stays_frozen_even_with_region_ownership(self):
        workspace = self.root / "asset-workspace"
        ws = init_workspace(self.project, workspace, ["rtl/top.sv"], "top", assets=["rtl/top.sv"],
                            region_roots=["rtl"])
        self.assertEqual(ws["allowed_paths"], [])
        self.assertEqual(ws["frozen_assets"], ["rtl/top.sv"])
        with self.assertRaises(ReadinessError):
            create_variant(workspace, "bad", self.overlay({"rtl/top.sv": "module top; endmodule\n"}))
        with self.assertRaises(ReadinessError):
            init_workspace(self.project, self.root / "bad-assets", ["rtl/top.sv"], "top",
                           assets=["rtl/top.sv"], allowed_paths=["rtl/top.sv"], region_roots=["rtl"])

    def test_filelist_must_represent_exact_ordered_effective_build(self):
        with self.assertRaisesRegex(ReadinessError, "exactly match"):
            create_variant(self.workspace, "order", self.overlay({}),
                           build={"sources": ["rtl/top.sv", "rtl/old.sv"], "include_dirs": []})
        for text in ("-f other.f\n", "+incdir+rtl\n", "@other.f\n", '"rtl/top.sv"\n', "$ROOT/top.sv\n"):
            with self.subTest(text=text), self.assertRaisesRegex(ReadinessError, "Unsupported filelist"):
                create_variant(self.workspace, "syntax", self.overlay({"sources.f": text}))
        # Comment/format changes remain source-bound even when build semantics match.
        obj = create_variant(self.workspace, "comment", self.overlay({"sources.f": "rtl/old.sv # first\n\nrtl/top.sv\n"}))
        self.assertNotEqual(obj["identity"], self.ws["baseline_identity"])

    def test_deletion_only_candidate_uses_its_own_build(self):
        workspace = self.root / "delete-workspace"
        init_workspace(self.project, workspace, ["rtl/old.sv", "rtl/top.sv"], "top", region_roots=["new"])
        obj = create_variant(workspace, "delete", self.overlay({}), delete_paths=["rtl/old.sv"],
                             build={"sources": ["rtl/top.sv"], "include_dirs": []})
        self.assertEqual(obj["operations"], {"add": [], "modify": [], "delete": ["rtl/old.sv"]})
        self.assertEqual(obj, load_variant(workspace, "delete"))
        with self.assertRaisesRegex(ReadinessError, "missing source"):
            create_variant(workspace, "missing", self.overlay({}), delete_paths=["rtl/old.sv"])

    def test_existing_nonstandard_rtl_header_cannot_be_edited_then_omitted(self):
        (self.project / "rtl/defs.inc").write_text("`define WIDTH 1\n")
        (self.project / "rtl/top.sv").write_text('`include "defs.inc"\nmodule top; endmodule\n')
        workspace = self.root / "headers"
        init_workspace(self.project, workspace, ["rtl/top.sv"], "top", region_roots=["new"])
        with self.assertRaisesRegex(ReadinessError, "omitted"):
            create_variant(workspace, "unused", self.overlay({"rtl/top.sv": "module top; endmodule\n",
                           "rtl/defs.inc": "`define WIDTH 2\n"}))
        obj = create_variant(workspace, "removed", self.overlay({"rtl/top.sv": "module top; endmodule\n"}),
                             delete_paths=["rtl/defs.inc"])
        self.assertEqual(obj["operations"]["delete"], ["rtl/defs.inc"])

    def test_rehashed_metadata_cannot_hide_out_of_scope_changes(self):
        self.extracted()
        path = self.workspace / "variants/extracted/variant.json"
        obj = read_json(path)
        obj["operations"]["delete"] = []
        save_json(path, obj)
        with self.assertRaisesRegex(ReadinessError, "operation manifest"):
            load_variant(self.workspace, "extracted")
        obj["operations"]["delete"] = ["rtl/old.sv"]
        (variant_tree(self.workspace, "extracted") / "check.py").write_text("changed\n")
        obj["files"] = file_manifest(variant_tree(self.workspace, "extracted"))
        obj["operations"]["modify"].append("check.py")
        obj["operations"]["modify"].sort()
        obj["changes"].append("check.py")
        obj["changes"].sort()
        obj["identity"] = variant_identity(self.ws, obj["files"], obj["build"])
        save_json(path, obj)
        with self.assertRaisesRegex(ReadinessError, "edit scope"):
            load_variant(self.workspace, "extracted")

    def test_missing_header_changed_top_and_policy_drift_are_rejected(self):
        with self.assertRaisesRegex(ReadinessError, "Unresolved include"):
            create_variant(self.workspace, "include", self.overlay({"rtl/top.sv": '`include "missing.svh"\n'}))
        with self.assertRaisesRegex(ReadinessError, "preserve"):
            create_variant(self.workspace, "top", self.overlay({}),
                           build={"top": "other", "sources": ["rtl/top.sv"], "include_dirs": []})
        ws = read_json(self.workspace / "workspace.json")
        ws["region_roots"].append("unowned")
        save_json(self.workspace / "workspace.json", ws)
        with self.assertRaisesRegex(ReadinessError, "context changed"):
            load_workspace(self.workspace)

    def test_loading_candidate_also_revalidates_immutable_baseline(self):
        self.extracted()
        (variant_tree(self.workspace, "baseline") / "rtl/top.sv").write_text("changed baseline\n")
        with self.assertRaisesRegex(ReadinessError, "Sealed variant was modified: baseline"):
            load_variant(self.workspace, "extracted")

    def test_api_2_context_identity_and_overlay_remain_unchanged(self):
        workspace = self.root / "legacy"
        ws = init_workspace(self.project, workspace, ["rtl/top.sv"], "top")
        self.assertEqual(ws["api_version"], 2)
        self.assertEqual(context_identity(ws), digest({k: ws[k] for k in
                         ("api_version", "top", "sources", "include_dirs", "allowed_paths", "files")}))
        self.assertEqual(ws["baseline_identity"], digest({"workspace_id": ws["workspace_id"], "files": ws["files"]}))
        obj = create_variant(workspace, "old", self.overlay({"rtl/top.sv": "module top; endmodule\n"}))
        self.assertEqual(obj["api_version"], 2)
        self.assertNotIn("build", obj)
        self.assertNotIn("operations", obj)
        self.assertEqual(obj, load_variant(workspace, "old"))
        with self.assertRaises(ReadinessError):
            create_variant(workspace, "new", self.overlay({"rtl/new.sv": "module new; endmodule\n"}))
        with self.assertRaises(ReadinessError):
            create_variant(workspace, "delete", self.overlay({}), delete_paths=["rtl/top.sv"])

    def test_cli_can_seal_regional_candidate_without_other_skills(self):
        cli = Path(__file__).resolve().parents[1] / "scripts/readiness.py"
        workspace = self.root / "cli-workspace"
        proc = subprocess.run([sys.executable, "-B", str(cli), "init", "--project", str(self.project),
                               "--workspace", str(workspace), "--top", "top", "--source", "rtl/top.sv",
                               "--source", "rtl/old.sv", "--region-root", "rtl/region"],
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        build = self.root / "build.json"
        build.write_text(json.dumps({"sources": ["rtl/top.sv"], "include_dirs": []}))
        proc = subprocess.run([sys.executable, "-B", str(cli), "variant", str(workspace), "deleted",
                               "--overlay", str(self.overlay({})), "--delete", "rtl/old.sv", "--build", str(build)],
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["operations"]["delete"], ["rtl/old.sv"])


if __name__ == "__main__":
    unittest.main()
