"""Behavioral tests of read-only lexical triage; no tools or EDA required."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from analysis import analyze
from core import ReadinessError, digest, file_manifest, init_workspace, save_json


FIXTURES = Path(__file__).parent / "fixtures" / "analysis"


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.workspace = self.root / "workspace"

    def tearDown(self):
        self.temp.cleanup()

    def initialize(self, files, top="design", sources=("design.sv",), include_dirs=()):
        for name, text in files.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return init_workspace(self.project, self.workspace, list(sources), top, include_dirs=include_dirs)

    def test_ordinary_small_design_does_not_require_restructuring(self):
        self.initialize({"design.sv": (FIXTURES / "small.sv").read_text()}, top="tiny")
        before = file_manifest(self.workspace)
        report = analyze(self.workspace)
        self.assertEqual(report["hypotheses"], [])
        self.assertEqual(report["experiments"], [])
        self.assertTrue(any(o["kind"] == "procedural_block" for o in report["observations"]))
        self.assertFalse(report["scope"]["executes_eda"])
        self.assertEqual(before, file_manifest(self.workspace))

    def test_loop_helper_and_default_width_are_discovered(self):
        self.initialize({"design.sv": (FIXTURES / "repeated.sv").read_text()}, top="lanes")
        report = analyze(self.workspace)
        loops = [o for o in report["observations"] if o["kind"] == "loop_helper_calls"]
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0]["default_lexical_iterations"], 64)
        self.assertEqual(loops[0]["helper_callsite_counts"], {"twist": 1})
        widths = [o["default_lexical_width_bits"] for o in report["observations"] if o["kind"] == "packed_range"]
        self.assertIn(512, widths)
        self.assertIn("loop_helper_expansion", {h["kind"] for h in report["hypotheses"]})
        observations = {o["id"] for o in report["observations"]}
        self.assertTrue(all(set(h["evidence"]) <= observations for h in report["hypotheses"]))
        self.assertTrue(all(h["confidence"] == "heuristic" for h in report["hypotheses"]))
        self.assertEqual(len(report["experiments"]), len(report["hypotheses"]))
        self.assertNotIn("target_effect_met", json.dumps(report))

    def test_same_line_identical_packed_ports_have_distinct_stable_ids(self):
        self.initialize({"design.sv": "module design(input logic [127:0] a, output logic [127:0] y); assign y=a; endmodule\n"})
        first = analyze(self.workspace)
        ranges = [o for o in first["observations"] if o["kind"] == "packed_range"]
        self.assertEqual(len(ranges), 2)
        self.assertEqual({o["line"] for o in ranges}, {1})
        self.assertEqual({o["default_lexical_width_bits"] for o in ranges}, {128})
        self.assertEqual(len({o["id"] for o in ranges}), 2)
        self.assertEqual(len({o["source_character_offset"] for o in ranges}), 2)
        again = [o["id"] for o in analyze(self.workspace)["observations"] if o["kind"] == "packed_range"]
        self.assertEqual([o["id"] for o in ranges], again)
        self.assertEqual(first["hypotheses"], [])

    def test_large_control_structure_and_wide_context(self):
        body = "\n".join(f"if (choose == {i}) result = data ^ {i};" for i in range(40))
        text = "module design(input logic [511:0] data, input logic [5:0] choose, output logic [511:0] result);\nalways_comb begin result=0;\n" + body + "\nend\nendmodule\n"
        self.initialize({"design.sv": text})
        report = analyze(self.workspace)
        process = next(o for o in report["observations"] if o["kind"] == "procedural_block")
        self.assertEqual(process["control_keyword_count"], 40)
        hypothesis = next(h for h in report["hypotheses"] if h["kind"] == "large_control_process")
        self.assertIn(process["id"], hypothesis["evidence"])
        self.assertGreater(len(hypothesis["evidence"]), 1)

    def test_included_helper_and_unknown_loop_bound(self):
        helper = "function automatic [7:0] choose(input [7:0] a); if(a[0]) choose=a+1; else choose=a-1; endfunction\n"
        top = '`include "ops.svh"\nmodule design(input [511:0] a, input integer limit, output reg [511:0] y); always_comb begin for(integer k=0;k<limit;k++) y[k*8+:8]=choose(a[k*8+:8]); end endmodule\n'
        self.initialize({"design.sv": top, "inc/ops.svh": helper}, include_dirs=("inc",))
        report = analyze(self.workspace)
        self.assertEqual(report["scope"]["scanned_files"], ["design.sv", "inc/ops.svh"])
        loop = next(o for o in report["observations"] if o["kind"] == "loop_helper_calls")
        self.assertIsNone(loop["default_lexical_iterations"])
        self.assertIn("loop_iterations_unresolved", {u["code"] for u in report["unknowns"]})
        self.assertTrue(any(o.get("path") == "inc/ops.svh" and o["kind"] == "called_helper_definition" for o in report["observations"]))

    def test_assignment_form_loop_step_and_scope_ambiguity(self):
        text = (FIXTURES / "repeated.sv").read_text().replace("lane++", "lane = lane + 2")
        self.initialize({"design.sv": text}, top="lanes")
        report = analyze(self.workspace)
        loop = next(o for o in report["observations"] if o["kind"] == "loop_helper_calls")
        self.assertEqual(loop["default_lexical_iterations"], 32)
        # Reinitialize another independent workspace with a conflicting lexical
        # parameter name; the scanner must not choose a module's value silently.
        self.workspace = self.root / "ambiguous-workspace"
        duplicate = text + "\nmodule other; parameter LANES=3; endmodule\n"
        (self.project / "design.sv").write_text(duplicate)
        init_workspace(self.project, self.workspace, ["design.sv"], "lanes")
        ambiguous = analyze(self.workspace)
        loop = next(o for o in ambiguous["observations"] if o["kind"] == "loop_helper_calls")
        self.assertIsNone(loop["default_lexical_iterations"])

    def test_safe_parent_include_and_string_directive_text(self):
        self.initialize({"rtl/sub/design.sv": '`include "../ops.svh"\nmodule design; initial $display("`include \\"fake.svh\\""); endmodule\n',
                         "rtl/ops.svh": "function automatic f(input x); f=x; endfunction\n"},
                        sources=("rtl/sub/design.sv",))
        report = analyze(self.workspace)
        self.assertEqual(report["scope"]["scanned_files"], ["rtl/ops.svh", "rtl/sub/design.sv"])
        self.assertNotIn("unresolved_include", {u["code"] for u in report["unknowns"]})
        self.assertNotIn("nonliteral_include", {u["code"] for u in report["unknowns"]})

    def test_comments_strings_and_inactive_branches_do_not_claim_elaboration(self):
        text = '''module design(input [7:0] a, output [7:0] y);
// always_comb begin for (int i=0;i<10000;i++) fake(a); end
/* logic [99999:0] huge; */
`ifdef OPTION
logic [UNRESOLVED-1:0] optional;
`endif
assign y = a;
endmodule
'''
        self.initialize({"design.sv": text})
        report = analyze(self.workspace)
        self.assertEqual(report["hypotheses"], [])
        self.assertFalse(any(o["kind"] == "procedural_block" for o in report["observations"]))
        codes = {u["code"] for u in report["unknowns"]}
        self.assertIn("packed_range_unresolved", codes)
        self.assertIn("conditional_preprocessing_not_evaluated", codes)

    def test_sealed_source_changes_are_rejected(self):
        self.initialize({"design.sv": "module design; endmodule\n"})
        (self.workspace / "variants/baseline/tree/design.sv").write_text("module changed; endmodule\n")
        with self.assertRaises(ReadinessError):
            analyze(self.workspace)

    def test_job_measurements_are_bound_and_flags_do_not_become_proof(self):
        ws = self.initialize({"design.sv": "module design; endmodule\n"})
        request = {"api_version": 2, "workspace_id": ws["workspace_id"], "variant": "baseline",
                   "variant_identity": ws["baseline_identity"], "config": {"kind": "yosys", "stage": "proc"}}
        request["request_identity"] = digest(request)
        request["job_id"] = "existing"
        result = {"workspace_id": ws["workspace_id"], "request_identity": request["request_identity"],
                  "variant_identity": ws["baseline_identity"], "kind": "yosys", "status": "completed",
                  "elapsed_seconds": 2.5, "peak_rss_bytes": 1234, "verification_passed": True,
                  "target_effect_met": True, "statistics": {"design": {"num_cells": 7}}}
        save_json(self.workspace / "jobs/existing/request.json", request)
        save_json(self.workspace / "jobs/existing/result.json", result)
        report = analyze(self.workspace)
        self.assertEqual(len(report["jobs_summary"]), 1)
        item = report["jobs_summary"][0]
        self.assertEqual(item["elapsed_seconds"], 2.5)
        self.assertEqual(item["statistics"]["design"]["num_cells"], 7)
        self.assertNotIn("verification_passed", item)
        self.assertNotIn("target_effect_met", item)
        self.assertEqual(report["hypotheses"], [])
        request["config"]["stage"] = "generic"
        save_json(self.workspace / "jobs/existing/request.json", request)
        invalid = analyze(self.workspace)
        self.assertEqual(invalid["jobs_summary"], [])
        self.assertIn("job_request_identity_invalid", {u["code"] for u in invalid["unknowns"]})


if __name__ == "__main__":
    unittest.main()
