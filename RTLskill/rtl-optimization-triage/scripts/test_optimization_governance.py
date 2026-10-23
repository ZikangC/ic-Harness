#!/usr/bin/env python3
"""Policy tests for autonomous optimization triage and recovery."""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from optimization_gate import run_migrate, run_transition, run_triage, run_validate
from scripts.optimization_governance import initial_state, measurement_verdict, step


MAINLINE_REF = {
    "transaction_id": "blackbox_expansion:top_level_sta:next_block",
    "kind": "blackbox_expansion", "module_id": "next_block",
    "partition": "top_level_sta",
    "identity_fingerprint": "sha256:registry-mainline",
    "registry_generation": 7,
}
packet = {
    "minimum_value_score": 0.5,
    "mainline": {
        "transaction_id": MAINLINE_REF["transaction_id"],
        "kind": "blackbox_expansion", "target": "next_block", "status": "pending",
    },
    "candidates": [
        {"module": "fast_small", "expanded_verified": True,
         "currently_blackboxed": False, "evidence_eligible": True,
         "timing_value": 0.1, "area_value": 0.1, "evidence_confidence": 1.0,
         "change_risk": 0.8, "verification_cost": 0.8},
        {"module": "critical_big", "expanded_verified": True,
         "currently_blackboxed": False, "evidence_eligible": True,
         "timing_value": 1.0, "area_value": 0.8, "replication_value": 0.5,
         "evidence_confidence": 1.0, "change_risk": 0.2, "verification_cost": 0.3},
        {"module": "bad_evidence", "expanded_verified": True,
         "currently_blackboxed": False, "evidence_eligible": False,
         "evidence_reason_codes": ["measurement_not_attributable"]},
        {"module": "still_blackboxed", "expanded_verified": False,
         "currently_blackboxed": True},
    ],
}


triaged = run_triage(packet)
assert triaged["recommended_candidate"] == "critical_big"
assert triaged["triage_status"] == "candidate_auto_selected"
assert triaged["automation_intent"]["operation"] == "start_optimization"
assert "optimize_rtl" not in triaged["execution_contract"]["allowed_actions"]
assert run_validate({"packet": packet, "module": "critical_big"})["selection_valid"]
bad = next(item for item in triaged["ineligible_candidates"]
           if item["module"] == "bad_evidence")
assert bad["reason_codes"] == ["measurement_not_attributable"]

blocked_packet = {
    **packet,
    "runtime_context": {
        "registry_integrity": "pass", "closure_integrity": "pass",
        "pending_harness_verification": True,
        "functional_baseline_clean": True,
        "integration_target": "next_block",
    },
}
blocked = run_triage(blocked_packet)
assert blocked["insertion_gate"]["allowed"] is False
assert "pending_harness_verification" in blocked["insertion_gate"]["reason_codes"]
assert blocked["automation_intent"] is None

resume = run_triage({
    **packet,
    "runtime_context": {
        "registry_integrity": "pass", "closure_integrity": "pass",
        "functional_baseline_clean": True,
        "optimization_phase": "resume_pending",
        "current_blackboxes": ["next_block"],
        "pending_mainline": {
            "kind": "blackbox_expansion", "module": "next_block",
            "partition": "top_level_sta",
            "transaction_id": MAINLINE_REF["transaction_id"],
        },
        "measurement_policy": {},
    },
})
assert resume["execution_contract"]["allowed_actions"] == ["expand_blackbox"]
assert resume["execution_contract"]["execution_profiles"][
    "expand_blackbox"]["operation_kind"] == "configuration"

no_mainline = run_triage({**packet, "mainline": {"status": "no_active_transaction"}})
assert no_mainline["automation_intent"] is None
assert "mainline_reference_missing" in no_mainline["insertion_gate"]["reason_codes"]


def begin(module="critical_big", checkpoint="cp:base"):
    state = initial_state(MAINLINE_REF, 2, 2, 2)
    return run_transition({"state": state, "event": {
        "kind": "begin_candidate", "event_id": f"begin:{module}",
        "module": module, "campaign_id": f"campaign:{module}",
        "checkpoint_ref": checkpoint, "rtl_ref": f"rtl/{module}.sv",
        "hypothesis": "reduce measured critical logic",
        "integration_target": "next_block", "insertion_mode": "decoupled",
        "interrupted_mainline_ref": MAINLINE_REF,
        "top_level_acceptance_required": True,
    }})


state = begin()
assert state["phase"] == "editing"
assert state["execution_contract"]["allowed_actions"][0] == "run_optimization_edit"
state = run_transition({"state": state, "event": {
    "kind": "edit_completed", "event_id": "edit:1",
    "campaign_id": "campaign:critical_big", "operation_id": "op:1",
    "rtl_fingerprint": "sha256:edited", "diff_ref": "diff:1",
}})
assert state["phase"] == "modified_pending_functional"
assert state["execution_contract"]["allowed_actions"][0] == "verify_optimization_candidate"
assert state["execution_contract"]["hard_constraints"]["no_edit_during_measurement"] is True
state = run_transition({"state": state, "event": {
    "kind": "functional_failed", "repair_attempt": True,
}})
assert state["phase"] == "functional_repair"
assert state["current_candidate"]["repair_attempts"] == 1
state = run_transition({"state": state, "event": {
    "kind": "edit_failed", "reason": "model_made_no_change",
}})
assert state["current_candidate"]["edit_attempts"] == 2
assert state["phase"] == "rollback_required"
state = run_transition({"state": state, "event": {
    "kind": "rollback_complete",
    "rollback_proof": {"checkpoint_restored": True,
                       "rtl_fingerprint_restored": True,
                       "configuration_restored": True},
}})
assert state["phase"] == "resume_pending"
transaction_id = state["resume_request"]["mainline_ref"]["transaction_id"]
state = run_transition({"state": state, "event": {
    "kind": "mainline_resumed", "transaction_id": transaction_id,
    "registry_confirmed": True,
}})
assert state["phase"] == "idle"


measurement = begin(checkpoint="cp:measure")
measurement = run_transition({"state": measurement, "event": {
    "kind": "edit_completed", "campaign_id": "campaign:critical_big",
    "operation_id": "measure-edit", "rtl_fingerprint": "sha256:rtl",
}})
measurement = run_transition({"state": measurement, "event": {"kind": "functional_passed"}})
measurement = run_transition({"state": measurement, "event": {
    "kind": "measurement_incomplete", "retry_attempt": False,
    "functional_evidence": {"quick": 0, "medium": 0, "full": 0},
    "measurement_partitions": ["critical_big"], "measurement_timeout_seconds": 600,
}})
assert measurement["current_candidate"]["measurement_retries"] == 0
for attempt in (1, 2):
    measurement = run_transition({"state": measurement, "event": {
        "kind": "measurement_incomplete", "retry_attempt": True,
        "event_id": f"retry:{attempt}",
    }})
assert measurement["phase"] == "rollback_required"
assert measurement["current_candidate"]["pending_terminal_outcome"] == "deferred"


accepted = begin(checkpoint="cp:accepted")
accepted = run_transition({"state": accepted, "event": {
    "kind": "edit_completed", "campaign_id": "campaign:critical_big",
    "operation_id": "accepted-edit", "rtl_fingerprint": "sha256:accepted",
}})
accepted = run_transition({"state": accepted, "event": {"kind": "functional_passed"}})
accepted = run_transition({"state": accepted, "event": {
    "kind": "sta_passed", "evidence": {
        "measurement_scope": "LOCAL_MODULE", "sta_scope_gate": {"scope": "local_module"},
        "value_evaluation": {"verdict": "improved", "improved": True}},
}})
assert accepted["accepted"][-1]["acceptance"] == "local_accepted"
assert accepted["acceptance_debt"][-1]["module"] == "critical_big"
assert accepted["phase"] == "resume_pending"

improved = measurement_verdict(
    {"comparability_key": "same", "area_um2": 100.0, "delay_ps": 1000.0},
    {"comparability_key": "same", "area_um2": 95.0, "delay_ps": 1005.0})
assert improved["verdict"] == "improved"
regressed = measurement_verdict(
    {"comparability_key": "same", "area_um2": 100.0, "delay_ps": 1000.0},
    {"comparability_key": "same", "area_um2": 95.0, "delay_ps": 1030.0})
assert regressed["verdict"] == "not_improved"
assert measurement_verdict({}, {})["verdict"] == "incomparable"


legacy = run_migrate({"state": {
    "schema_version": 4, "phase": "measurement_pending",
    "interrupted_transaction_ref": {
        "transaction_id": "old", "ledger_generation": 1,
        "transaction_fingerprint": "old-fingerprint"},
    "current_candidate": {"module": "critical_big", "checkpoint_id": "legacy"},
}})
assert legacy["schema_version"] == 9
assert legacy["migration_hold"] == "legacy_ledger_reference_requires_registry_rebind"
assert legacy["current_candidate"]["checkpoint_ref"] == "legacy"
assert "checkpoint_id" not in legacy["current_candidate"]

stepped = begin(checkpoint="cp:step")
stepped = run_transition({"state": stepped, "event": {
    "kind": "edit_completed", "campaign_id": "campaign:critical_big",
    "rtl_fingerprint": "sha256:step",
}})
functional_stepped = step({"state": stepped, "observation": {
    "observation_id": "step-functional", "campaign_id": "campaign:critical_big",
    "module": "critical_big", "status": "functional_passed",
    "functional_evidence": {"functional_quick_exit": 0},
    "preflight_evidence": {"preflight_exit": 0},
}})
assert functional_stepped["requested_execution"]["executor_ref"] == \
    "capability.resolve_sta_scope"
scoped_stepped = step({"state": functional_stepped["state"], "observation": {
    "observation_id": "step-scope", "campaign_id": "campaign:critical_big",
    "module": "critical_big", "status": "sta_scope_resolved",
    "sta_scope_gate": {"scope": "local_module"},
    "measurement_partitions": ["critical_big"],
    "measurement_timeout_seconds": 180,
}})
incomplete = step({"state": scoped_stepped["state"], "observation": {
    "observation_id": "step-incomplete", "campaign_id": "campaign:critical_big",
    "module": "critical_big", "status": "measurement_incomplete",
}})
assert incomplete["state"]["phase"] == "measurement_pending"
assert incomplete["requested_execution"]["executor_ref"] == "project.verify_optimization"

mismatched = step({"state": scoped_stepped["state"], "observation": {
    "observation_id": "step-mismatch", "campaign_id": "campaign:critical_big",
    "module": "critical_big", "status": "measurement_observed",
    "measurement": {"comparability_key": "other", "area_um2": 1.0,
                    "delay_ps": 1.0, "source_run": "new",
                    "attribution_quality": "direct_partition",
                    "current_design_match": True},
    "measurement_artifact_available": True, "ppa_exit": 0,
}})
assert mismatched["state"]["phase"] == "rollback_required"
assert mismatched["requested_execution"]["executor_ref"] == "optimization.rollback"

external = step({"state": functional_stepped["state"], "observation": {
    "observation_id": "external-sta", "campaign_id": "campaign:critical_big",
    "module": "critical_big", "status": "external_verification_required",
    "rtl_fingerprint": "sha256:step",
    "functional_evidence": {"functional_quick_exit": 0,
                            "functional_medium_exit": 0, "mandatory_full_exit": 0},
    "preflight_evidence": {"preflight_exit": 0},
    "sta_scope_gate": {"scope": "top_level_partial"},
}})
assert external["state"]["phase"] == "waiting_external_verification"
assert external["disposition"] == "handoff"
assert external["handoff"]["kind"] == "external_verification_required"
assert external["handoff"]["verification_obligation"]["required_scope"] == "top_level_partial"
combined = step({"state": external["state"], "observation": {
    "observation_id": "combined-sta", "campaign_id": "campaign:critical_big",
    "module": "critical_big", "status": "external_measurement_observed",
    "obligation_id": external["handoff"]["verification_obligation"]["obligation_id"],
    "rtl_fingerprint": "sha256:step",
    "external_measurement": {
        "reported_incomplete": False, "ppa_exit": 0,
        "measurement_scope": "PARTIAL_HIERARCHICAL",
        "timing_target_met": True,
        "sta_closure_audit": {
            "sta_link_result": "ok", "sta_timing_measured": True}},
}})
assert combined["state"]["phase"] == "resume_pending"
assert combined["state"]["accepted"][-1]["acceptance"] == "combined_top_partial"
assert combined["state"]["acceptance_debt"][-1]["reason"] == \
    "final_complete_top_level_sta_pending"

raw_timeout_only = step({"state": external["state"], "observation": {
    "observation_id": "raw-timeout-is-not-closure", "campaign_id": "campaign:critical_big",
    "module": "critical_big", "status": "external_measurement_observed",
    "obligation_id": external["handoff"]["verification_obligation"]["obligation_id"],
    "rtl_fingerprint": "sha256:step",
    "external_measurement": {
        "reported_incomplete": False, "ppa_exit": 124,
        "measurement_scope": "PARTIAL_HIERARCHICAL",
        "timing_target_met": False,
        "sta_closure_audit": {
            "sta_link_result": "ok", "sta_timing_measured": False}},
}})
assert raw_timeout_only["state"]["phase"] == "rollback_required"
assert raw_timeout_only["requested_execution"]["executor_ref"] == "optimization.rollback"

print("rtl optimization governance tests: PASS")
