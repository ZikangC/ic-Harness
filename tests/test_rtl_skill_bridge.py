#!/usr/bin/env python3
"""Pure adapter tests; no controller, SSH, synthesis, or STA is started."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
from rtl_skill_bridge import (compact_module_registry_hot_view,
                              conservative_rtl_change_packet, content_fingerprint,
                              default_skill_path, measurement_descriptor,
                              module_registry_can_expand, module_registry_reconcile,
                              optimization_execute_request,
                              optimization_run_campaign,
                              partition_value_evidence, rtl_instantiation_counts,
                              sta_scope_decision, stage_evidence_envelope)


def main() -> None:
    sta_root = default_skill_path("rtl-sta-scope", "RTLSKILL_STA_SCOPE")
    optimization_root = default_skill_path(
        "rtl-optimization-triage", "RTLSKILL_OPTIMIZATION_TRIAGE")
    registry_root = default_skill_path(
        "rtl-module-evidence-registry", "RTLSKILL_MODULE_EVIDENCE_REGISTRY")
    registered_diff = """--- before/rtl/stage.sv
+++ after/rtl/stage.sv
-      sum_q <= sum_q + value;
+      sum_q <= value + sum_q;
"""
    packet = conservative_rtl_change_packet(
        registered_diff, ["rtl/stage.sv"], change_mode="local", remaining_blackboxes=[],
        design_context={
            "status": "current", "hard_gate": "pass", "graph_fingerprint": "graph-1",
            "module_contexts": {"stage": {"relationships": {}, "timing_context": {}}},
        })
    decision = sta_scope_decision(sta_root, packet)
    assert decision["scope"] == "local_module"

    combinational_diff = """--- before/rtl/stage.sv
+++ after/rtl/stage.sv
-  assign out = a + b;
+  assign out = b + a;
"""
    packet = conservative_rtl_change_packet(
        combinational_diff, ["rtl/stage.sv"], change_mode="local",
        remaining_blackboxes=["later_stage"], design_context={
            "status": "current", "hard_gate": "pass", "graph_fingerprint": "graph-1",
            "module_contexts": {"stage": {"relationships": {}, "timing_context": {}}},
        })
    decision = sta_scope_decision(sta_root, packet)
    assert decision["scope"] == "top_level_partial"

    transaction_ref = {
        "transaction_id": "blackbox_expansion:top_level_sta:next",
        "kind": "blackbox_expansion", "module_id": "next",
        "partition": "top_level_sta", "registry_generation": 1,
        "identity_fingerprint": "sha256:mainline",
    }
    with tempfile.TemporaryDirectory() as temporary:
        state_path = Path(temporary) / "optimization.json"
        observation_path = Path(temporary) / "observation.json"
        campaign_boundary = optimization_run_campaign(
            optimization_root, {
            "triage_input": {
                "candidates": [], "minimum_value_score": 0.5,
                "mainline": {"transaction_id": transaction_ref["transaction_id"],
                             "kind": "blackbox_expansion", "target": "next",
                             "status": "pending"},
                "runtime_context": {"registry_integrity": "pass",
                                    "closure_integrity": "pass",
                                    "functional_baseline_clean": True,
                                    "integration_target": "next"},
            },
            "mainline_ref": transaction_ref,
            "registry_snapshot": {"hard_gate": "pass", "generation": 1,
                                  "pending_transaction": {
                                      "transaction_id": transaction_ref["transaction_id"]}},
            },
            lambda _request: (_ for _ in ()).throw(AssertionError("must not dispatch")),
            dispatchable_executor_refs=set(), max_dispatches=1,
            state_path=state_path, observation_path=observation_path)
        assert campaign_boundary["boundary_reason"] == "idle"
        assert state_path.is_file()
        assert campaign_boundary["evidence_projection"]["hard_gate"]["phase"] == "idle"
    captured_plan = {}

    class ProjectPort:
        @staticmethod
        def materialize_optimization_execution(request, **_context):
            assert request["executor_ref"] == "project.verify_optimization"
            return {"command": ["project-controller", "--verify"],
                    "directive": "verify only\n"}

    def execute_project(plan):
        captured_plan.update(plan)
        return {"status": "measurement_incomplete",
                "observation": {"status": "measurement_incomplete"}}

    project_response = optimization_execute_request(
        optimization_root, ProjectPort(),
        {"executor_ref": "project.verify_optimization", "operation_id": "op"},
        execute_project_plan=execute_project,
        directive_path=ROOT / "status/test-directive.md")
    assert project_response["status"] == "measurement_incomplete"
    assert captured_plan["kind"] == "local"
    assert captured_plan["execution"]["operation_id"] == "op"
    assert captured_plan["directive_path"].endswith("test-directive.md")
    registry = module_registry_reconcile(registry_root, {
        "project_id": "bridge", "session_id": "s", "cycle_id": 1,
        "phase": "pre_decision", "observed_at": "2026-09-16T12:00:00+08:00",
        "reconciliation_id": "bridge-1", "observation": {
            "config_fingerprint": "c", "filelist_fingerprint": "f",
            "rtl_fingerprint": "r", "evidence_watermark": "e",
            "modules": [{"module_id": "stage", "in_filelist": True,
                         "body_present": True, "partitions": {
                             "top_level_sta": {"representation": "blackbox"}}}],
        },
    })
    assert module_registry_can_expand(
        registry_root, registry, "stage", "top_level_sta")["allowed"] is True
    projected_registry = compact_module_registry_hot_view({
        "hard_gate": "pass", "generation": 4,
        "current_blackboxes": ["stage"], "body_available_blackboxes": ["stage"],
        "expandable_blackboxes": ["stage"], "pending_transaction": None,
    })
    assert projected_registry["generation"] == 4
    assert projected_registry["expandable_blackboxes"] == ["stage"]
    envelope = stage_evidence_envelope(
        skill="rtl-optimization-triage", identity={"generation": 4},
        hard_gate={"status": "pass"},
        summary_input={"triage": {"recommended_candidate": "stage"}})
    assert envelope["scope"] == "current_stage"
    assert envelope["summary_input"]["triage"]["recommended_candidate"] == "stage"
    descriptor = measurement_descriptor(
        semantic_fingerprint="design-a", scope_kind="targeted", targets=["stage"],
        intent="verify_pending_transaction", budget_seconds=600)
    assert descriptor["targets"] == ["stage"] and descriptor["budget_seconds"] == 600
    comment_only_boundary = conservative_rtl_change_packet(
        """--- before/rtl/stage.sv
+++ after/rtl/stage.sv
-  // parameter and clock interface note
+  // clock, reset, parameter, interface note only
""", ["rtl/stage.sv"], change_mode="local", remaining_blackboxes=[],
        affected_modules=["stage_impl"])
    assert comment_only_boundary["rtl_changes"][0]["module"] == "stage_impl"
    assert comment_only_boundary["rtl_changes"][0]["interface_changed"] is False
    assert comment_only_boundary["rtl_changes"][0]["clock_reset_changed"] is False
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        feedback = root / "run-a" / "feedback.txt"
        feedback.parent.mkdir()
        feedback.write_text(
            "partition=mac module=\\mac ABC: Area = 1234.50 (100.0 %) Delay = 12000.00 ps\n"
            "partition=mac module=\\mac delay=12000.00 seq_pct=25.00 class=registered_path\n",
            encoding="utf-8")
        values = partition_value_evidence([feedback], ["mac"])["mac"]
        assert values["area_um2"] == 1234.5 and values["path_class"] == "registered_path"
        rtl = root / "rtl"
        rtl.mkdir()
        (rtl / "top.sv").write_text("mac u0();\nmac #(.W(8)) u1();\n", encoding="utf-8")
        assert rtl_instantiation_counts(rtl, ["mac"])["mac"] == 2
        first_fingerprint = content_fingerprint(root, [Path("rtl")])
        (rtl / "top.sv").write_text("mac u0();\n", encoding="utf-8")
        assert content_fingerprint(root, [Path("rtl")]) != first_fingerprint
    print("RTLskill bridge policy tests: PASS")


if __name__ == "__main__":
    main()
