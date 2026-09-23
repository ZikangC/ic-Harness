#!/usr/bin/env python3
"""Deterministic Optimization Skill lifecycle tests; no model or PPA runs."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT.parent / "RTLskill" / "rtl-optimization-triage"
sys.path.insert(0, str(SKILL / "scripts"))
from optimization_governance import initial_state, step, transition  # noqa: E402
from optimization_runner import run_rollback  # noqa: E402


MAINLINE = {
    "transaction_id": "blackbox_expansion:top_level_sta:next",
    "kind": "blackbox_expansion", "module_id": "next",
    "partition": "top_level_sta", "registry_generation": 4,
    "identity_fingerprint": "mainline-fingerprint",
}
BASELINE = {
    "comparability_key": "partition=compute;scope=LOCAL",
    "area_um2": 100.0, "delay_ps": 1000.0, "source_run": "baseline",
}


def candidate_state(*, retries: int = 2) -> dict[str, object]:
    state = initial_state(MAINLINE, max_measurement_retries=retries,
                          max_internal_steps=12)
    state = transition(state, {
        "kind": "begin_candidate", "module": "compute", "campaign_id": "campaign",
        "checkpoint_ref": "checkpoint", "rtl_ref": "rtl/compute.sv",
        "integration_target": "next", "insertion_mode": "decoupled",
        "interrupted_mainline_ref": MAINLINE,
        "baseline_measurement": BASELINE,
    })
    return transition(state, {
        "kind": "edit_completed", "campaign_id": "campaign",
        "rtl_fingerprint": "edited",
    })


def observation(identity: str, status: str, **extra: object) -> dict[str, object]:
    return {
        "observation_id": identity, "campaign_id": "campaign",
        "module": "compute", "status": status, **extra,
    }


def main() -> None:
    state = candidate_state(retries=2)
    first = step({"state": state, "observation": observation(
        "incomplete-1", "measurement_incomplete",
        rtl_fingerprint="edited", measurement_partitions=["compute"],
        measurement_timeout_seconds=600)})
    assert first["state"]["phase"] == "measurement_pending"
    assert first["state"]["current_candidate"]["measurement_retries"] == 0
    assert first["requested_execution"]["executor_ref"] == "project.verify_optimization"
    second = step({"state": first["state"], "observation": observation(
        "incomplete-2", "measurement_incomplete")})
    assert second["state"]["current_candidate"]["measurement_retries"] == 1
    third = step({"state": second["state"], "observation": observation(
        "incomplete-3", "measurement_incomplete")})
    assert third["state"]["phase"] == "rollback_required"
    assert third["requested_execution"]["executor_ref"] == "optimization.rollback"

    mismatch = step({"state": candidate_state(), "observation": observation(
        "complete-mismatch", "measurement_observed",
        measurement={"comparability_key": "different", "area_um2": 90.0,
                     "delay_ps": 900.0, "source_run": "new",
                     "attribution_quality": "direct_partition",
                     "current_design_match": True},
        measurement_artifact_available=True, ppa_exit=0,
        sta_evidence={})})
    assert mismatch["state"]["phase"] == "rollback_required"
    assert mismatch["state"]["current_candidate"][
        "pending_terminal_reason"] == "measurement_incomparable"

    improved = step({"state": candidate_state(), "observation": observation(
        "complete-improved", "measurement_observed",
        measurement={"comparability_key": BASELINE["comparability_key"],
                     "area_um2": 98.0, "delay_ps": 995.0,
                     "source_run": "new",
                     "attribution_quality": "direct_partition",
                     "current_design_match": True},
        measurement_artifact_available=True, ppa_exit=0,
        sta_evidence={"sta_scope_gate": {"scope": "local_module"},
                      "measurement_scope": "LOCAL_MODULE",
                      "covered_modules": ["compute"]})})
    assert improved["state"]["phase"] == "resume_pending"
    assert improved["handoff"]["kind"] == "resume_mainline"

    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary) / "project"
        checkpoint_root = project / "status" / "checkpoints"
        rtl = project / "rtl" / "compute.sv"
        rtl.parent.mkdir(parents=True)
        rtl.write_text("module compute; wire edited; endmodule\n", encoding="utf-8")
        campaign = checkpoint_root / "campaigns" / "campaign"
        preimage = campaign / "preimage" / "rtl" / "compute.sv"
        preimage.parent.mkdir(parents=True)
        preimage.write_text("module compute; endmodule\n", encoding="utf-8")
        rollback_state = candidate_state()
        rollback_state["current_candidate"]["checkpoint_ref"] = campaign.as_posix()
        rollback_state = transition(rollback_state, {"kind": "sta_failed"})
        state_path = project / "status" / "optimization.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(rollback_state), encoding="utf-8")
        result = run_rollback({
            "project_root": project.as_posix(), "state_path": state_path.as_posix(),
            "checkpoint_root": checkpoint_root.as_posix(),
            "operation_id": "rollback-operation", "operation": "rollback_candidate",
        })
        assert result["status"] == "rollback_complete"
        assert rtl.read_text(encoding="utf-8") == preimage.read_text(encoding="utf-8")
        assert result["governance_state"]["phase"] == "resume_pending"

    print("Optimization Skill step tests: PASS")


if __name__ == "__main__":
    main()
