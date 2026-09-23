#!/usr/bin/env python3
"""Project adapter tests for portable Optimization Skill requests."""
from __future__ import annotations

import tempfile
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
from task02_project_adapter import Task02ProjectAdapter  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary)
        rtl = project / "rtl" / "compute.sv"
        rtl.parent.mkdir()
        rtl.write_text("module compute; endmodule\n", encoding="utf-8")
        adapter = Task02ProjectAdapter(project_root=project)
        incomplete_audit = {
            "closure_governance_status": "ready",
            "phase": "closure_measurement_incomplete",
            "measurement_incomplete": True,
        }
        normalized = adapter.normalize_measurement_outcome(
            results=[{"kind": "iteration", "functional_quick_exit": 0,
                      "preflight_exit": 0, "ppa_exit": 124}],
            process_exit=124, closure_audit=incomplete_audit)
        assert normalized["status"] == "measurement_incomplete"
        assert normalized["measurement_incomplete"] is True
        assert adapter.normalize_measurement_outcome(
            results=[{"kind": "iteration", "functional_quick_exit": 1,
                      "preflight_exit": 0, "ppa_exit": 124}],
            process_exit=124, closure_audit=incomplete_audit)["status"] == \
            "functional_failed"
        assert adapter.normalize_measurement_outcome(
            results=[{"kind": "iteration", "functional_quick_exit": 0,
                      "preflight_exit": 1, "ppa_exit": 124}],
            process_exit=124, closure_audit=incomplete_audit)["status"] == \
            "preflight_failed"
        assert adapter.normalize_measurement_outcome(
            results=[{"kind": "iteration", "functional_quick_exit": 0,
                      "preflight_exit": 0, "ppa_exit": 124}],
            process_exit=124,
            closure_audit={"closure_governance_status": "ready",
                           "measurement_incomplete": False})["status"] == \
            "measurement_failed"
        assert adapter.normalize_measurement_outcome(
            results=[{"kind": "iteration", "functional_quick_exit": 0,
                      "preflight_exit": 0, "ppa_exit": 0}],
            process_exit=0,
            closure_audit={"closure_governance_status": "ready",
                           "sta_link_result": "ok", "sta_timing_measured": True},
            require_timing=True)["status"] == "complete"
        preflight_only = adapter.normalize_measurement_outcome(
            results=[{"kind": "iteration", "functional_quick_exit": 0,
                      "preflight_exit": 0, "ppa_exit": None}],
            process_exit=0, closure_audit={}, preflight_only=True)
        assert preflight_only["status"] == "measurement_not_run"
        assert preflight_only["measurement_incomplete"] is False
        missing_timing = adapter.normalize_measurement_outcome(
            results=[{"kind": "iteration", "functional_quick_exit": 0,
                      "preflight_exit": 0, "ppa_exit": 0}],
            process_exit=0,
            closure_audit={"closure_governance_status": "ready",
                           "sta_link_result": "ok", "sta_timing_measured": False,
                           "measurement_incomplete": False},
            require_timing=True)
        assert missing_timing["status"] == "verification_evidence_missing"
        assert missing_timing["measurement_incomplete"] is False
        assert adapter.normalize_measurement_outcome(
            results=[{"kind": "iteration", "functional_quick_exit": 1,
                      "preflight_exit": 1, "ppa_exit": 124}],
            process_exit=76, closure_audit=incomplete_audit)["status"] == \
            "infra_uncertain"
        common = {
            "operation_id": "operation", "campaign_id": "campaign",
            "module": "compute",
        }
        edit = adapter.materialize_optimization_execution(
            {**common, "executor_ref": "optimization.edit",
             "operation": "start_campaign", "candidate": {"value_score": 2.0},
             "mainline_ref": {"transaction_id": "mainline"},
             "insertion_plan": {"integration_target": "next"}},
            state_path=project / "state.json",
            checkpoint_root=project / "status/checkpoints", model="flash")
        assert edit["runner_request"]["rtl_path"] == "rtl/compute.sv"
        assert edit["runner_request"]["integration_target"] == "next"

        checkpoint = project / "status/checkpoints/campaign"
        verify = adapter.materialize_optimization_execution(
            {**common, "executor_ref": "project.verify_optimization",
             "checkpoint_ref": checkpoint.as_posix(),
             "expected_rtl_fingerprint": "a" * 64,
             "functional_evidence": {
                 "functional_quick_exit": 0, "functional_medium_exit": 0,
                 "mandatory_full_exit": 0, "rtl_fingerprint": "a" * 64,
                 "verification_context_fingerprint": "b" * 64},
             "preflight_evidence": {
                 "preflight_exit": 0, "rtl_fingerprint": "a" * 64,
                 "verification_context_fingerprint": "b" * 64},
             "measurement_partitions": ["compute"],
             "measurement_timeout_seconds": 1800},
            directive_path=project / "directive.md",
            ppa_controller=project / "ppa_agent_loop.py",
            sta_scope_skill=project / "sta-scope",
            closure_skill=project / "closure", model="flash",
            max_repair_attempts=2)
        command = verify["command"]
        assert "--optimization-verify-only" in command
        assert "--optimization-functional-proof-rtl-sha256" in command
        assert "--optimization-functional-proof-context-sha256" in command
        assert "--optimization-preflight-proof-rtl-sha256" in command
        assert "--optimization-preflight-proof-context-sha256" in command
        assert command[command.index("--optimization-measurement-partition") + 1] == "compute"
        assert not hasattr(adapter, "materialize_optimization_plan")
        external_plan = adapter.materialize_external_optimization_plan(
            decision={"action": "continue"},
            decision_packet={"allowed_actions": {"continue": {
                "executor_ref": "project.optimization_external_sta"}}},
            optimization_handoff={
                "kind": "external_verification_required",
                "resolution": {
                    "status": "standalone_ready", "obligation_id": "sta:one"},
                "requested_execution": {
                    "executor_ref": "project.verify_optimization",
                    "operation": "consume_external_sta_obligation",
                    "operation_id": "b" * 64,
                    "obligation_id": "sta:one", "campaign_id": "campaign",
                    "module": "compute", "rtl_ref": "rtl/compute.sv",
                    "checkpoint_ref": checkpoint.as_posix(),
                    "expected_rtl_fingerprint": "a" * 64,
                    "functional_evidence": {}, "preflight_evidence": {},
                    "integration_target": "next",
                    "required_scope": "top_level_partial", "edit_allowed": False,
                },
            },
            directive_path=project / "directive.md",
            ppa_controller=project / "ppa_agent_loop.py",
            sta_scope_skill=project / "sta-scope", closure_skill=project / "closure",
            model="flash", max_repair_attempts=2)
        assert external_plan is not None and external_plan["kind"] == "external"
        assert "--optimization-consume-external-sta" in external_plan["command"]
        assert external_plan["execution"]["operation_id"] == "b" * 64
        assert adapter.materialize_external_optimization_plan(
            decision={"action": "continue"},
            decision_packet={"allowed_actions": {"continue": {
                "executor_ref": "flash.run"}}}, optimization_handoff=None,
            directive_path=project / "directive.md",
            ppa_controller=project / "ppa_agent_loop.py",
            sta_scope_skill=project / "sta-scope", closure_skill=project / "closure",
            model="flash", max_repair_attempts=2) is None

        execution = {**common, "executor_ref": "project.verify_optimization",
                     "rtl_ref": "rtl/compute.sv"}
        raw = {
            "optimization_status": "accepted", "ppa_exit": 0,
            "ppa_log": "runs/run-a/ppa.log", "time": "run-a",
            "functional_quick_exit": 0, "functional_medium_exit": 0,
            "mandatory_full_exit": 0,
        }
        incomplete = adapter.normalize_optimization_observation(
            execution, results=[raw], process_exit=0,
            measurement={"comparability_key": "same", "area_um2": 90.0,
                         "delay_ps": 900.0, "source_run": "run-a",
                         "attribution_quality": "direct_partition",
                         "current_design_match": True})
        assert incomplete["status"] == "measurement_observed"
        assert incomplete["measurement_artifact_available"] is False
        ppa_log = project / "runs/run-a/ppa.log"
        ppa_log.parent.mkdir(parents=True)
        ppa_log.write_text("measured\nsta_credible_meets_10ns=yes\n", encoding="utf-8")
        complete = adapter.normalize_optimization_observation(
            execution, results=[raw], process_exit=0,
            measurement={"comparability_key": "same", "area_um2": 90.0,
                         "delay_ps": 900.0, "source_run": "run-a",
                         "attribution_quality": "direct_partition",
                         "current_design_match": True})
        assert complete["status"] == "measurement_observed"
        assert complete["measurement_artifact_available"] is True
        failed = adapter.normalize_optimization_observation(
            execution, results=[{**raw, "optimization_status": "verification_failed"}],
            process_exit=0)
        assert failed["status"] == "verification_failed"
        external = adapter.normalize_optimization_observation(
            execution, results=[{**raw,
                "optimization_status": "external_verification_required",
                "sta_scope_gate": {"scope": "top_level_partial"},
                "preflight_exit": 0}], process_exit=0)
        assert external["status"] == "external_verification_required"
        assert external["preflight_evidence"]["rtl_fingerprint"] is not None

        external_execution = {
            **execution, "checkpoint_ref": checkpoint.as_posix(),
            "expected_rtl_fingerprint": __import__("hashlib").sha256(
                rtl.read_bytes()).hexdigest(),
            "integration_target": "next",
        }
        external_command = adapter.materialize_external_optimization_sta(
            external_execution, directive_path=project / "directive.md",
            ppa_controller=project / "ppa_agent_loop.py",
            sta_scope_skill=project / "sta-scope", closure_skill=project / "closure",
            model="flash", max_repair_attempts=2)["command"]
        assert "--optimization-consume-external-sta" in external_command
        assert external_command[external_command.index(
            "--optimization-integration-target") + 1] == "next"
        combined = adapter.normalize_external_optimization_observation(
            external_execution,
            {"obligation_id": "sta:one", "required_scope": "top_level_partial"},
            results=[{**raw, "measurement_scope": "PARTIAL_HIERARCHICAL",
                      "sta_closure_audit": {"sta_link_result": "ok",
                                            "sta_timing_measured": True}}],
            process_exit=0)
        assert combined["status"] == "external_measurement_observed"
        assert combined["external_measurement"]["timing_target_met"] is True
        assert "combined_guard_passed" not in combined["external_measurement"]

    print("Task-02 optimization adapter tests: PASS")


if __name__ == "__main__":
    main()
