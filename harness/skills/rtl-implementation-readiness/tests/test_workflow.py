"""Offline behavioral tests. Synthetic timings test bookkeeping, not EDA benefit."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from core import ReadinessError, create_variant, init_workspace, load_variant, load_workspace, read_json
from experiments import submit
from comparison import compare, export, verify


class Workflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "rtl").mkdir(parents=True)
        (self.project / "inc").mkdir()
        (self.project / "rtl/top.sv").write_text('`include "defs.svh"\nmodule top(input a,output y); assign y=a; endmodule\n')
        (self.project / "inc/defs.svh").write_text('`include "nested.svh"\n')
        (self.project / "inc/nested.svh").write_text('`define WIDTH 1\n')
        (self.project / "check.py").write_text("print('PAIR_PASSED')\n")
        self.ws = self.root / "workspace"
        self.identity = init_workspace(self.project, self.ws, ["rtl/top.sv"], "top", ["inc"], ["check.py"])
        overlay = self.root / "overlay/rtl"
        overlay.mkdir(parents=True)
        (overlay / "top.sv").write_text('`include "defs.svh"\nmodule top(input a,output y); assign y=a & a; endmodule\n')
        self.variant = create_variant(self.ws, "candidate", overlay.parent)

    def tearDown(self):
        self.temp.cleanup()

    def config(self):
        return {"kind": "command", "argv": [sys.executable, "-c", "import pathlib,time,sys; time.sleep(0.01 if '&' in pathlib.Path(sys.argv[1]).read_text() else 0.6)", "{tree}/rtl/top.sv"], "purpose": "measurement", "timeout_seconds": 5, "memory_mib": 256}

    def measurements(self):
        b, c = [], []
        for i in range(3):
            for variant, jobs, prefix in [("baseline", b, "b"), ("candidate", c, "c")]:
                job = prefix + str(i)
                r = submit(self.ws, variant=variant, config=self.config(), job_id=job, background=False)
                self.assertEqual(r["status"], "completed", r)
                jobs.append(job)
        return b, c

    def verify(self):
        cfg = {"kind": "command", "argv": [sys.executable, "{tree}/check.py", "{baseline_tree}/rtl/top.sv", "{tree}/rtl/top.sv"], "purpose": "regression", "success_markers": ["PAIR_PASSED"], "timeout_seconds": 5, "memory_mib": 256}
        # Synthetic marker tests receipt handling only; actual functional EDA is
        # exercised separately on a real tool, never claimed by this unit fixture.
        return submit(self.ws, variant="candidate", config=cfg, job_id="verify", background=False)

    def test_complete_comparison_and_export(self):
        b, c = self.measurements()
        self.assertTrue(self.verify()["verification_passed"])
        result = compare(self.ws, "candidate", b, c, ["verify"])
        self.assertEqual(result["decision"], "candidate_for_integration", result)
        delivery = export(self.ws, result, self.ws / "delivery")
        self.assertEqual(delivery["status"], "scoped_candidate")
        report_path = self.ws / "comparison.json"
        report_path.write_text(json.dumps(result))
        checked = verify(self.ws, report_path, self.ws / "delivery")
        self.assertEqual(checked["comparison_identity"], result["identity"])
        self.assertEqual(checked["changes"], ["rtl/top.sv"])
        self.assertEqual(checked["baseline_files"], self.identity["files"])
        patch_path = self.ws / "delivery/candidate.patch"
        patch_original = patch_path.read_text()
        patch_path.write_text("forged patch")
        with self.assertRaises(ReadinessError):
            verify(self.ws, report_path, self.ws / "delivery")
        patch_path.write_text(patch_original)
        self.assertIn("+module", (self.ws / "delivery/candidate.patch").read_text())
        self.assertEqual(load_variant(self.ws, "baseline")["files"], self.identity["files"])
        # Raw source remains untouched.
        self.assertNotIn("&", (self.project / "rtl/top.sv").read_text())
        # Stale logs cannot continue serving as receipts.
        (self.ws / "jobs/b0/tool.log").write_text("changed")
        with self.assertRaises(ReadinessError):
            compare(self.ws, "candidate", b, c, ["verify"])
        with self.assertRaises(ReadinessError):
            verify(self.ws, report_path, self.ws / "delivery")

    def test_missing_verification_never_promotes(self):
        b, c = self.measurements()
        result = compare(self.ws, "candidate", b, c)
        self.assertEqual(result["decision"], "evidence_incomplete")
        self.assertEqual(export(self.ws, result, self.ws / "proposal")["status"], "proposal_only_keep_original")

    def test_input_closure_and_seal(self):
        self.assertIn("inc/nested.svh", self.identity["files"])
        (self.ws / "variants/candidate/tree/rtl/top.sv").write_text("changed")
        with self.assertRaises(ReadinessError):
            load_variant(self.ws, "candidate")

    def test_protected_assets_and_context(self):
        bad = self.root / "bad"
        bad.mkdir()
        (bad / "check.py").write_text("print('FAKE')")
        with self.assertRaises(ReadinessError):
            create_variant(self.ws, "bad", bad)
        ws = read_json(self.ws / "workspace.json")
        ws["top"] = "another"
        (self.ws / "workspace.json").write_text(json.dumps(ws))
        with self.assertRaises(ReadinessError):
            load_workspace(self.ws)

    def test_missing_include_and_escape_rejected(self):
        (self.project / "inc/nested.svh").unlink()
        with self.assertRaises(ReadinessError):
            init_workspace(self.project, self.root / "missing", ["rtl/top.sv"], "top", ["inc"])
        with self.assertRaises(ReadinessError):
            init_workspace(self.project, self.root / "escape", ["../else.sv"], "top")

    def test_overlay_cannot_introduce_external_dependency(self):
        overlay = self.root / "external/rtl"
        overlay.mkdir(parents=True)
        extra = self.root / "unbound.svh"
        extra.write_text("`define UNBOUND 1\n")
        (overlay / "top.sv").write_text(f'`include "{extra}"\nmodule top(input a,output y);assign y=a;endmodule\n')
        with self.assertRaises(ReadinessError):
            create_variant(self.ws, "external", overlay.parent)

    def test_symlink_include_is_rejected(self):
        (self.project / "inc/nested.svh").unlink()
        (self.project / "inc/nested.svh").symlink_to(self.project / "check.py")
        with self.assertRaises(ReadinessError):
            init_workspace(self.project, self.root / "symlink", ["rtl/top.sv"], "top", ["inc"])

    def test_cli_alone_in_unrelated_directory(self):
        import shutil
        skill = self.root / "only-this-skill"
        shutil.copytree(Path(__file__).resolve().parents[1], skill, ignore=shutil.ignore_patterns("__pycache__"))
        proc = subprocess.run([sys.executable, "-B", str(skill / "scripts/readiness.py"), "analyze", str(self.ws)], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["workspace_id"], self.identity["workspace_id"])


if __name__ == "__main__":
    unittest.main()
