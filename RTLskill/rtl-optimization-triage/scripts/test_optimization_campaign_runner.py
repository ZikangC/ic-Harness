#!/usr/bin/env python3
"""No-network tests for the project-neutral Optimization campaign runner."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from optimization_campaign_runner import _run_until_boundary, run_campaign
from optimization_governance import initial_state, transition
from optimization_state_store import (OptimizationStateStoreError,
                                      load_governance, save_governance)


MAINLINE = {
    "transaction_id": "blackbox_expansion:top_level_sta:next",
    "kind": "blackbox_expansion", "module_id": "next",
    "partition": "top_level_sta", "identity_fingerprint": "mainline",
}

TRIAGE_INPUT = {
    "minimum_value_score": 0.5,
    "mainline": {"transaction_id": MAINLINE["transaction_id"],
                 "kind": "blackbox_expansion", "target": "next",
                 "status": "pending"},
    "runtime_context": {
        "registry_integrity": "pass", "closure_integrity": "pass",
        "functional_baseline_clean": True, "integration_target": "next",
    },
    "candidates": [{
        "module": "compute", "expanded_verified": True,
        "currently_blackboxed": False, "evidence_eligible": True,
        "timing_value": 1.0, "area_value": 0.8,
        "evidence_confidence": 1.0, "change_risk": 0.1,
        "verification_cost": 0.2,
    }],
}


def dispatch(request):
    if request["executor_ref"] == "optimization.edit":
        state = transition(initial_state(MAINLINE), {
            "kind": "begin_candidate", "module": "compute",
            "campaign_id": "campaign", "checkpoint_ref": "checkpoint",
            "rtl_ref": "rtl/compute.sv", "integration_target": "next",
            "insertion_mode": "decoupled", "interrupted_mainline_ref": MAINLINE,
        })
        state = transition(state, {
            "kind": "edit_completed", "campaign_id": "campaign",
            "rtl_fingerprint": "a" * 64,
        })
        return {"status": "modified_pending_functional", "governance_state": state}
    raise AssertionError(request)


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        state_path = root / "state.json"
        observation_path = root / "observation.json"

        result = _run_until_boundary({
            "state": initial_state(MAINLINE),
            "automation_intent": {
                "operation": "start_optimization", "module": "compute",
                "insertion_plan": {
                    "integration_target": "next", "insertion_mode": "decoupled"},
            },
            "candidate": {"module": "compute"}, "mainline_ref": MAINLINE,
            "campaign_id": "campaign",
        }, dispatch, state_path=state_path, observation_path=observation_path,
            dispatchable_executor_refs={"optimization.edit"})
        assert result["requested_execution"]["executor_ref"] == "project.verify_optimization"
        assert result["boundary_reason"] == "executor_not_dispatched"
        assert len(result["campaign_trace"]) == 1
        assert load_governance(state_path)["phase"] == "modified_pending_functional"

        def verification_dispatch(request):
            if request["executor_ref"] == "project.verify_optimization":
                return {"status": "functional_passed", "observation": {
                    "observation_id": "verification:functional",
                    "campaign_id": "campaign", "module": "compute",
                    "status": "functional_passed",
                    "functional_evidence": {"functional_quick_exit": 0},
                    "preflight_evidence": {"preflight_exit": 0},
                }}
            assert request["executor_ref"] == "capability.resolve_sta_scope"
            return {
                "status": "external_verification_required",
                "observation": {
                    "observation_id": "verification:external", "campaign_id": "campaign",
                    "module": "compute", "status": "external_verification_required",
                    "rtl_fingerprint": "a" * 64,
                    "sta_scope_gate": {"scope": "top_level_partial"},
                },
            }

        verified = _run_until_boundary({
            "state": result["state"], "automation_intent": None,
            "candidate": {}, "mainline_ref": MAINLINE, "campaign_id": None,
        }, verification_dispatch, state_path=state_path,
            observation_path=observation_path,
            dispatchable_executor_refs={"project.verify_optimization",
                                        "capability.resolve_sta_scope"})
        assert verified["handoff"]["kind"] == "external_verification_required"
        assert verified["handoff"]["requested_execution"]["edit_allowed"] is False
        assert verified["state"]["phase"] == "waiting_external_verification"

        deferred = _run_until_boundary({
            "state": result["state"], "automation_intent": None,
            "candidate": {}, "mainline_ref": MAINLINE, "campaign_id": None,
        }, lambda _request: {"boundary": {"kind": "resource_limit"}},
            state_path=state_path, observation_path=observation_path,
            dispatchable_executor_refs={"project.verify_optimization"})
        assert deferred["boundary_reason"] == "resource_limit"
        assert deferred["state"]["phase"] == "modified_pending_functional"

        save_governance(state_path, initial_state(MAINLINE))
        owned = run_campaign({
            "triage_input": TRIAGE_INPUT,
            "mainline_ref": MAINLINE,
            "registry_snapshot": {
                "hard_gate": "pass", "generation": 7,
                "pending_transaction": {"transaction_id": MAINLINE["transaction_id"]}},
            "campaign_id_prefix": "session",
        }, dispatch, state_path=state_path, observation_path=observation_path,
            dispatchable_executor_refs={"optimization.edit"})
        assert owned["triage"]["recommended_candidate"] == "compute"
        assert owned["requested_execution"]["executor_ref"] == "project.verify_optimization"
        assert owned["evidence_projection"]["hard_gate"]["phase"] == \
            "modified_pending_functional"

        save_governance(state_path, verified["state"])
        observation_path.write_text(json.dumps({
            "observation_id": "external:unavailable", "campaign_id": "campaign",
            "module": "compute", "status": "external_verification_unavailable",
            "reason": "no_compatible_measurement", "evidence": {},
        }), encoding="utf-8")
        consumed = run_campaign({
            "triage_input": TRIAGE_INPUT,
            "mainline_ref": MAINLINE,
            "registry_snapshot": {
                "hard_gate": "pass", "generation": 7,
                "pending_transaction": {"transaction_id": MAINLINE["transaction_id"]}},
        }, lambda _request: (_ for _ in ()).throw(AssertionError("must not dispatch")),
            state_path=state_path, observation_path=observation_path,
            dispatchable_executor_refs=set())
        assert consumed["state"]["phase"] == "rollback_required"
        assert consumed["boundary_reason"] == "executor_not_dispatched"
        assert consumed["triage"]["triage_status"] == "active_campaign"
        assert not observation_path.exists()

        resume_state = transition(initial_state(MAINLINE), {
            "kind": "begin_candidate", "module": "compute", "campaign_id": "resume",
            "checkpoint_ref": "checkpoint", "rtl_ref": "rtl/compute.sv",
            "integration_target": "next", "insertion_mode": "decoupled",
            "interrupted_mainline_ref": MAINLINE,
        })
        resume_state = transition(resume_state, {
            "kind": "edit_completed", "campaign_id": "resume",
            "rtl_fingerprint": "b" * 64,
        })
        resume_state = transition(resume_state, {"kind": "functional_passed"})
        resume_state = transition(resume_state, {
            "kind": "sta_passed", "evidence": {
                "measurement_scope": "local_module",
                "sta_scope_gate": {"scope": "local_module"},
                "value_evaluation": {"verdict": "improved", "improved": True},
            },
        })
        save_governance(state_path, resume_state)
        resumed = run_campaign({
            "triage_input": TRIAGE_INPUT, "mainline_ref": MAINLINE,
            "registry_snapshot": {
                "hard_gate": "pass", "generation": 8, "pending_transaction": None},
        }, lambda _request: (_ for _ in ()).throw(AssertionError("must not dispatch")),
            state_path=state_path, observation_path=observation_path,
            dispatchable_executor_refs=set())
        assert resumed["state"]["phase"] == "idle"

        state_path.write_text(json.dumps({
            "governance": initial_state(MAINLINE),
            "pending_observation": {"status": "legacy"},
        }), encoding="utf-8")
        try:
            run_campaign({"triage_input": TRIAGE_INPUT, "mainline_ref": MAINLINE},
                         dispatch, state_path=state_path,
                         observation_path=observation_path)
            raise AssertionError("legacy controller envelope must be rejected")
        except OptimizationStateStoreError:
            pass

    print("optimization campaign runner tests: PASS")


if __name__ == "__main__":
    main()
