#!/usr/bin/env python3
"""Optimization candidate triage and recovery state transitions."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Mapping


SCHEMA_VERSION = 9
DEFAULT_MAX_REPAIR_ATTEMPTS = 2
DEFAULT_MAX_MEASUREMENT_RETRIES = 2
DEFAULT_MAX_EDIT_ATTEMPTS = 2
DEFAULT_MAX_INTERNAL_STEPS = 12
READ_ONLY_ACTIONS = (
    "inspect_existing_sta_evidence",
    "inspect_run_evidence",
    "stop_for_debug",
)


def _mainline(value: Mapping[str, object]) -> dict[str, object]:
    result = copy.deepcopy(dict(value))
    if not result.get("transaction_id") and result.get("target"):
        identity = {
            "kind": result.get("kind"), "target": result.get("target"),
            "partition": result.get("partition"),
        }
        digest = hashlib.sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
        result["transaction_id"] = f"mainline:{digest}"
    return result


def _mainline_ref(value: object) -> dict[str, object]:
    """Normalize one Registry-owned mainline into an immutable return reference.

    The Optimization state owns only this captured reference.  Registry remains
    authoritative for the live mainline phase and completion evidence.
    """
    if not isinstance(value, Mapping):
        return {}
    nested = value.get("interrupted_mainline_ref") or value.get("mainline_ref")
    raw = nested if isinstance(nested, Mapping) else value
    transaction_id = str(raw.get("transaction_id") or "").strip()
    module = str(raw.get("module_id") or raw.get("module") or raw.get("target") or "").strip()
    kind = str(raw.get("kind") or "blackbox_expansion").strip()
    partition = str(raw.get("partition") or "top_level_sta").strip()
    fingerprint = str(raw.get("identity_fingerprint") or "").strip()
    if not fingerprint and transaction_id and module:
        identity = {
            "transaction_id": transaction_id, "kind": kind,
            "module_id": module, "partition": partition,
        }
        fingerprint = hashlib.sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
    if not transaction_id or not module or not fingerprint:
        return {}
    result = {
        "transaction_id": transaction_id,
        "kind": kind,
        "module_id": module,
        "partition": partition,
        "identity_fingerprint": fingerprint,
    }
    generation = raw.get("registry_generation")
    if isinstance(generation, int):
        result["registry_generation"] = generation
    return result


def _verification_obligations(module: str, integration_target: str,
                              change_id: object = None) -> list[dict[str, object]]:
    obligations: list[dict[str, object]] = [{
        "obligation_id": f"optimization_effect:{module}",
        "purpose": "optimization_effect", "target_module": module,
        "status": "awaiting_functional", "change_id": change_id,
    }]
    if integration_target:
        obligations.append({
            "obligation_id": f"integration_acceptance:{integration_target}",
            "purpose": "integration_acceptance", "target_module": integration_target,
            "status": "pending_mainline", "change_id": change_id,
        })
    return obligations


def _external_sta_obligation(current: Mapping[str, object],
                             scope_gate: object) -> dict[str, object]:
    """Build a portable global-STA obligation without project scheduling policy."""
    gate = dict(scope_gate) if isinstance(scope_gate, Mapping) else {}
    scope = str(gate.get("scope") or "")
    if scope not in {"top_level_partial", "top_level_complete"}:
        raise ValueError("external STA obligation requires one top-level scope")
    identity = {
        "campaign_id": current.get("campaign_id"),
        "module": current.get("module"),
        "rtl_fingerprint": current.get("rtl_fingerprint"),
        "required_scope": scope,
    }
    digest = hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "schema_version": 1,
        "obligation_id": f"sta:{digest}",
        "kind": "sta_verification",
        "purpose": "optimization_effect_and_integration_acceptance",
        "campaign_id": current.get("campaign_id"),
        "target_module": current.get("module"),
        "integration_target": current.get("integration_target"),
        "rtl_fingerprint": current.get("rtl_fingerprint"),
        "required_scope": scope,
        "scope_gate": gate,
        "shareable_with_compatible_mainline_measurement": True,
        "status": "waiting_external_verification",
    }


def _enter_resume_pending(state: dict[str, object], reason: str,
                          evidence: object = None) -> None:
    reference = _mainline_ref(state.get("interrupted_mainline_ref"))
    state["interrupted_mainline_ref"] = reference or None
    if not reference:
        state["phase"] = "idle"
        state["resume_request"] = None
        state["next_action"] = "select_next"
        return
    state["phase"] = "resume_pending"
    state["resume_request"] = {
        "mainline_ref": copy.deepcopy(reference),
        "reason": reason,
        "evidence": copy.deepcopy(evidence),
    }
    state["next_action"] = "resume_mainline"


def _number(value: object) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def candidate_score(candidate: Mapping[str, object]) -> float:
    benefit = (3.0 * _number(candidate.get("timing_value"))
               + 2.0 * _number(candidate.get("area_value"))
               + _number(candidate.get("replication_value"))
               + _number(candidate.get("evidence_confidence")))
    cost = (2.0 * _number(candidate.get("change_risk"))
            + _number(candidate.get("verification_cost"))
            + 0.5 * _number(candidate.get("prior_failure_weight")))
    return round(benefit - cost, 4)


def measurement_verdict(before: object, after: object, *,
                        minimum_improvement_ratio: float = 0.01,
                        maximum_regression_ratio: float = 0.01) -> dict[str, object]:
    """Deterministically decide whether a comparable PPA measurement improved.

    A successful tool run is not itself an optimization result.  Both area and
    delay must be present under the same non-empty comparability key.  At least
    one metric must improve by the configured minimum and the other may not
    regress beyond the configured tolerance.
    """
    old = before if isinstance(before, Mapping) else {}
    new = after if isinstance(after, Mapping) else {}
    old_key = str(old.get("comparability_key") or "")
    new_key = str(new.get("comparability_key") or "")
    reasons: list[str] = []
    if not old_key or not new_key:
        reasons.append("comparability_key_missing")
    elif old_key != new_key:
        reasons.append("comparability_key_mismatch")
    old_area = old.get("area_um2")
    new_area = new.get("area_um2")
    old_delay = old.get("classified_delay_ps", old.get("delay_ps"))
    new_delay = new.get("classified_delay_ps", new.get("delay_ps"))
    values = (old_area, new_area, old_delay, new_delay)
    if not all(isinstance(value, (int, float)) and float(value) > 0 for value in values):
        reasons.append("quantitative_measurement_missing")
    if reasons:
        return {
            "verdict": "incomparable", "improved": False,
            "reason_codes": reasons,
            "minimum_improvement_ratio": float(minimum_improvement_ratio),
            "maximum_regression_ratio": float(maximum_regression_ratio),
        }
    area_gain = (float(old_area) - float(new_area)) / float(old_area)
    delay_gain = (float(old_delay) - float(new_delay)) / float(old_delay)
    useful = max(area_gain, delay_gain) >= float(minimum_improvement_ratio)
    safe = min(area_gain, delay_gain) >= -float(maximum_regression_ratio)
    return {
        "verdict": "improved" if useful and safe else "not_improved",
        "improved": bool(useful and safe),
        "reason_codes": ([] if useful and safe else [
            "improvement_below_threshold" if not useful else "cross_metric_regression"]),
        "area_gain_ratio": round(area_gain, 8),
        "delay_gain_ratio": round(delay_gain, 8),
        "minimum_improvement_ratio": float(minimum_improvement_ratio),
        "maximum_regression_ratio": float(maximum_regression_ratio),
        "comparability_key": old_key,
        "before_source_run": old.get("source_run"),
        "after_source_run": new.get("source_run"),
    }


def ineligibility_reasons(candidate: Mapping[str, object]) -> list[str]:
    reasons: list[str] = []
    if not bool(str(candidate.get("module", "")).strip()):
        reasons.append("module_missing")
    verified_once = (candidate.get("expansion_verified_once") is True
                     or candidate.get("expanded_verified") is True)
    if not verified_once:
        reasons.append("never_expansion_verified")
    if candidate.get("local_optimization_allowed") is False:
        reasons.append("local_optimization_disallowed")
    if candidate.get("integration_state") == "deterministic_failed":
        reasons.append("deterministic_integration_failure")
    if candidate.get("cooldown") is True:
        reasons.append("cooldown")
    if candidate.get("evidence_eligible") is not True:
        raw = candidate.get("evidence_reason_codes")
        evidence_reasons = [str(item) for item in raw] if isinstance(raw, list) else []
        reasons.extend(evidence_reasons or ["evidence_not_eligible"])
    return reasons


def eligible(candidate: Mapping[str, object]) -> bool:
    """Historical expansion proof permits local work despite a temporary blackbox.

    ``currently_blackboxed`` describes the current integration measurement and
    is not proof that the module body has never been verified.  A temporary
    resource-control blackbox therefore requires later top-level acceptance,
    but does not erase local optimization eligibility.
    """
    return not ineligibility_reasons(candidate)


def _derive_mainline_execution(runtime: Mapping[str, object],
                               mainline: Mapping[str, object]) -> dict[str, object]:
    """Derive the mainline operation from raw project facts inside the Skill."""
    pending_value = runtime.get("pending_mainline")
    pending = pending_value if isinstance(pending_value, Mapping) else {}
    pending_module = str(pending.get("module") or "").strip()
    saved_target = str(mainline.get("target") or pending_module).strip()
    blackboxes_value = runtime.get("current_blackboxes")
    blackboxes = ({str(item) for item in blackboxes_value}
                  if isinstance(blackboxes_value, list) else set())
    resume_pending = runtime.get("optimization_phase") == "resume_pending"
    resume_expand = bool(resume_pending and saved_target in blackboxes)
    policy_value = runtime.get("measurement_policy")
    measurement_policy = (dict(policy_value)
                          if isinstance(policy_value, Mapping) else {})
    return {
        "pending": bool(pending_module or resume_expand),
        "action": "expand_blackbox" if resume_expand else "continue",
        "module": saved_target if resume_expand else pending_module,
        "transaction_id": pending.get("transaction_id"),
        "flash_attempts": None if resume_expand else 1,
        "operation_kind": "configuration" if resume_expand else "measurement",
        "intent": ("resume_saved_blackbox_expansion" if resume_expand
                   else "verify_pending_transaction"),
        "transaction_kind": (
            "blackbox_expansion" if resume_expand
            else str(pending.get("kind") or "integration_verification")),
        "measurement_policy": measurement_policy,
    }


def triage(packet: Mapping[str, object]) -> dict[str, object]:
    raw = packet.get("candidates")
    candidates = [dict(item) for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []
    valid: list[dict[str, object]] = []
    invalid: list[dict[str, object]] = []
    for item in candidates:
        if eligible(item):
            item["value_score"] = candidate_score(item)
            valid.append(item)
        else:
            invalid.append({"module": item.get("module"),
                            "reason_codes": ineligibility_reasons(item)})
    valid.sort(key=lambda item: (-float(item["value_score"]), str(item.get("module", ""))))
    threshold = float(packet.get("minimum_value_score", 0.5))
    recommended = valid[0].get("module") if valid and float(valid[0]["value_score"]) >= threshold else None
    status = ("no_eligible_candidate" if not valid else
              "candidate_auto_selected" if recommended else
              "continue_mainline")
    result = {
        "schema_version": SCHEMA_VERSION,
        "triage_status": status,
        "recommended_candidate": recommended,
        "eligible_candidates": valid,
        "ineligible_candidates": invalid,
        "mainline": _mainline(packet.get("mainline")) if isinstance(packet.get("mainline"), Mapping) else {},
        "next_action": "start_optimization" if recommended else "resume_mainline",
    }
    runtime = packet.get("runtime_context")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    global_reasons: list[str] = []
    if runtime.get("registry_integrity") not in (None, "pass"):
        global_reasons.append("module_registry_hold")
    if runtime.get("closure_integrity") not in (None, "pass"):
        global_reasons.append("closure_evidence_unavailable")
    if runtime.get("structural_harness_repair_pending") is True:
        global_reasons.append("structural_harness_repair_required")
    if runtime.get("pending_harness_verification") is True:
        global_reasons.append("pending_harness_verification")
    if runtime.get("functional_baseline_clean") is False:
        global_reasons.append("functional_baseline_not_clean")
    if runtime.get("optimization_phase") == "resume_pending":
        global_reasons.append("saved_mainline_resume_pending")
    if not _mainline_ref(result.get("mainline")):
        # Stage-1 is an insertion into an existing integration campaign.  When
        # no mainline is active, leave selection to the integration workflow
        # instead of turning the absence into a runner/infrastructure error.
        global_reasons.append("mainline_reference_missing")
    integration_target = str(
        (result.get("mainline") or {}).get("target") or "").strip()
    plans: list[dict[str, object]] = []
    for candidate in valid:
        module = str(candidate.get("module", ""))
        mode = "fused_same_unit" if integration_target and module == integration_target else "decoupled"
        reasons: list[str] = []
        if mode == "decoupled" and candidate.get("currently_blackboxed") is True:
            reasons.append("decoupled_candidate_still_blackboxed")
        plans.append({
            "module": module,
            "integration_target": integration_target or None,
            "insertion_mode": mode,
            "top_level_acceptance_required": bool(
                candidate.get("top_level_acceptance_pending") is True or mode == "fused_same_unit"),
            "allowed": not reasons,
            "reason_codes": reasons,
        })
    allowed_modules = [str(plan["module"]) for plan in plans if plan.get("allowed") is True]
    if not allowed_modules:
        global_reasons.append("no_safe_optimization_candidate")
    insertion_gate = {
        "allowed": not global_reasons and bool(allowed_modules),
        "integration_target": integration_target or None,
        "allowed_modules": allowed_modules if not global_reasons else [],
        "plans": plans,
        "reason_codes": global_reasons,
        "policy": "optimization_skill_runtime_preconditions_v1",
    }
    result["insertion_gate"] = insertion_gate
    selected_plan = next((copy.deepcopy(plan) for plan in plans
                          if plan.get("module") == recommended and plan.get("allowed") is True), None)
    result["automation_intent"] = ({
        "operation": "start_optimization",
        "module": recommended,
        "value_score": next((item.get("value_score") for item in valid
                             if item.get("module") == recommended), None),
        "minimum_value_score": threshold,
        "insertion_plan": selected_plan,
        "reason_codes": ["highest_score_above_hard_threshold"],
    } if selected_plan is not None and insertion_gate["allowed"] else None)
    mainline_execution = _derive_mainline_execution(
        runtime, result.get("mainline")
        if isinstance(result.get("mainline"), Mapping) else {})
    mainline_reasons: list[str] = []
    resume_action = str(mainline_execution.get("action") or "continue")
    resume_module = str(mainline_execution.get("module") or "").strip()
    resume_transaction_id = str(mainline_execution.get("transaction_id") or "").strip()
    resume_pending = mainline_execution.get("pending") is True
    measurement_policy_value = mainline_execution.get("measurement_policy")
    measurement_policy_value = (measurement_policy_value
                                if isinstance(measurement_policy_value, Mapping) else {})
    mainline_target = str((result.get("mainline") or {}).get("target") or "").strip()
    mainline_transaction_id = str(
        (result.get("mainline") or {}).get("transaction_id") or "").strip()
    if not resume_pending:
        mainline_reasons.append("mainline_transaction_not_pending")
    if runtime.get("registry_conflict") is True:
        mainline_reasons.append("module_registry_conflict")
    if mainline_target and resume_module and mainline_target != resume_module:
        mainline_reasons.append("mainline_target_mismatch")
    if (mainline_transaction_id and resume_transaction_id
            and mainline_transaction_id != resume_transaction_id):
        mainline_reasons.append("mainline_transaction_id_mismatch")
    if runtime.get("structural_harness_repair_pending") is True:
        mainline_reasons.append("structural_harness_repair_required")
    if runtime.get("pending_harness_verification") is True:
        mainline_reasons.append("pending_harness_verification")
    if runtime.get("functional_baseline_clean") is False:
        mainline_reasons.append("functional_baseline_not_clean")
    if mainline_execution.get("operation_kind") == "measurement":
        if runtime.get("closure_integrity") not in (None, "pass"):
            mainline_reasons.append("closure_evidence_unavailable")
        if measurement_policy_value.get("proposed_measurement_allowed") is not True:
            raw_policy_reasons = measurement_policy_value.get("reason_codes")
            policy_reasons = (raw_policy_reasons if isinstance(raw_policy_reasons, list)
                              else ["measurement_policy_not_authorized"])
            mainline_reasons.extend(str(item) for item in policy_reasons
                                    if str(item) not in mainline_reasons)
    resume_allowed = resume_pending and not mainline_reasons
    resume_profile = {
        "execution_mode": "execute_mainline_transaction",
        "operation_kind": str(mainline_execution.get("operation_kind") or "unspecified"),
        "intent": str(mainline_execution.get("intent") or "resume_pending_transaction"),
        "transaction_kind": str(mainline_execution.get("transaction_kind") or "other"),
        "transaction_id": resume_transaction_id or None,
        "module": resume_module or None,
        "measurement_policy": copy.deepcopy(dict(measurement_policy_value)),
    }
    # Candidate selection is a deterministic, threshold-gated Skill operation.
    # It is deliberately absent from the strategic model's action space.
    allowed_actions: list[str] = []
    if resume_allowed and resume_action not in allowed_actions:
        allowed_actions.append(resume_action)
    applies_to_actions: list[str] = []
    if resume_pending and resume_action not in applies_to_actions:
        applies_to_actions.append(resume_action)
    action_parameters: dict[str, object] = {}
    execution_profiles: dict[str, object] = {}
    if resume_pending:
        action_parameters[resume_action] = {
            "module": {"enum": [resume_module]},
        }
        if mainline_execution.get("flash_attempts") is not None:
            action_parameters[resume_action]["flash_attempts"] = {
                "enum": [int(mainline_execution.get("flash_attempts"))]
            }
        execution_profiles[resume_action] = resume_profile
    recommended_actions: list[dict[str, object]] = []
    if resume_allowed:
        recommended_actions.append({
            "action": resume_action, "module": resume_module,
            "reason": "resume_saved_mainline_transaction",
        })
    result["execution_contract"] = {
        "schema_version": 1,
        "contract_kind": "optimization_and_mainline_selection",
        "enforcement": "hard",
        "current_phase": "idle",
        "applies_to_actions": applies_to_actions,
        "allowed_actions": allowed_actions,
        "forbidden_actions": ([resume_action] if resume_pending and not resume_allowed else []),
        "action_parameters": action_parameters,
        "execution_profiles": execution_profiles,
        "hard_constraints": {
            "optimization_reason_codes": global_reasons,
            "mainline_reason_codes": mainline_reasons,
        },
        "recommended_actions": recommended_actions,
        "strategic_questions": [],
        "retry_budget": {},
        "resume_context": {
            "interrupted_mainline_ref": _mainline_ref(result.get("mainline", {})) or None,
        },
        "automation_intent": copy.deepcopy(result.get("automation_intent")),
    }
    return result


def validate_selection(packet: Mapping[str, object], module: str) -> dict[str, object]:
    result = triage(packet)
    candidates = [item for item in result["eligible_candidates"]
                  if str(item.get("module")) == module]
    selected = candidates[0] if candidates else {}
    mainline = result.get("mainline") if isinstance(result.get("mainline"), Mapping) else {}
    integration_target = str(mainline.get("target") or mainline.get("module") or "").strip()
    insertion_mode = "fused_same_unit" if integration_target and module == integration_target else "decoupled"
    selection_error = None
    insertion_gate = result.get("insertion_gate")
    insertion_gate = insertion_gate if isinstance(insertion_gate, Mapping) else {}
    allowed_modules = insertion_gate.get("allowed_modules")
    allowed_modules = allowed_modules if isinstance(allowed_modules, list) else []
    if not candidates:
        selection_error = "module_not_eligible"
    elif insertion_mode == "decoupled" and selected.get("currently_blackboxed") is True:
        # Exposing another blackbox would silently start a second integration
        # transaction.  A project may add an explicit local-only adapter later,
        # but the generic gate remains fail closed.
        selection_error = "decoupled_candidate_still_blackboxed"
    elif module not in allowed_modules:
        reason_codes = insertion_gate.get("reason_codes")
        reason_codes = reason_codes if isinstance(reason_codes, list) else []
        selection_error = str(reason_codes[0]) if reason_codes else "runtime_preconditions_rejected"
    return {
        **result,
        "selected_module": module,
        "selection_valid": selection_error is None,
        "selection_error": selection_error,
        "insertion_plan": {
            "optimization_module": module,
            "integration_target": integration_target or None,
            "insertion_mode": insertion_mode,
            "top_level_acceptance_required": bool(
                selected.get("top_level_acceptance_pending") is True
                or insertion_mode == "fused_same_unit"),
            "mainline_resume": copy.deepcopy(dict(mainline)),
        },
    }


def _execution_contract(state: Mapping[str, object]) -> dict[str, object]:
    phase = str(state.get("phase", "idle"))
    current = state.get("current_candidate")
    current = current if isinstance(current, Mapping) else None
    maximum = int(state.get("max_measurement_retries", DEFAULT_MAX_MEASUREMENT_RETRIES))
    used = int(current.get("measurement_retries", 0)) if current else 0
    remaining = max(0, maximum - used)
    read_only = list(READ_ONLY_ACTIONS)
    allowed: list[str]
    forbidden: list[str] = []
    recommended: list[dict[str, object]] = []
    profiles: dict[str, object] = {}
    if current is None and phase == "resume_pending":
        allowed = read_only
        applies = ["*"]
        enforcement = "hard"
        forbidden = ["optimize_rtl", "expand_blackbox"]
        recommended = [{"action": "inspect_existing_sta_evidence",
                        "reason": "saved_mainline_must_resume_before_new_candidate"}]
    elif current is None:
        allowed = []
        applies = []
        enforcement = "advisory"
    elif phase in {"editing", "functional_repair"}:
        allowed = ["run_optimization_edit", *read_only]
        applies = ["*"]
        enforcement = "hard"
        forbidden = ["retry_measurement", "expand_blackbox", "continue"]
        recommended = [{"action": "run_optimization_edit", "module": current.get("module"),
                        "reason": "edit_or_repair_current_candidate_only"}]
        profiles["run_optimization_edit"] = {
            "execution_mode": "optimization_edit_step",
            "module": current.get("module"),
            "campaign_id": current.get("campaign_id"),
            "rtl_ref": current.get("rtl_ref"),
            "checkpoint_ref": current.get("checkpoint_ref"),
        }
    elif phase == "modified_pending_functional":
        allowed = ["verify_optimization_candidate", *read_only]
        applies = ["*"]
        enforcement = "hard"
        forbidden = ["run_optimization_edit", "retry_measurement", "expand_blackbox", "continue"]
        recommended = [{"action": "verify_optimization_candidate",
                        "module": current.get("module"),
                        "reason": "new_single_file_diff_requires_functional_gate"}]
        profiles["verify_optimization_candidate"] = {
            "execution_mode": "verify_existing_candidate",
            "module": current.get("module"),
            "campaign_id": current.get("campaign_id"),
            "expected_rtl_fingerprint": current.get("rtl_fingerprint"),
            "checkpoint_ref": current.get("checkpoint_ref"),
        }
    elif phase in {"sta_required", "measurement_pending", "infra_pending"}:
        allowed = (["retry_measurement"] if remaining > 0 else []) + ["defer_optimization", *read_only]
        applies = ["*"]
        enforcement = "hard"
        forbidden = ["optimize_rtl", "expand_blackbox", "continue"]
        recommended_action = "retry_measurement" if remaining > 0 else "defer_optimization"
        recommended = [{"action": recommended_action,
                        "reason": "verify_unchanged_candidate" if remaining > 0 else "measurement_retry_budget_exhausted"}]
        profiles["retry_measurement"] = {
            "execution_mode": "verify_existing_candidate",
            "module": current.get("module"),
            "candidate_id": current.get("campaign_id"),
            "expected_rtl_fingerprint": current.get("rtl_fingerprint"),
            "functional_evidence": copy.deepcopy(current.get("functional_evidence")),
            "preflight_evidence": copy.deepcopy(current.get("preflight_evidence")),
            "sta_scope_gate": copy.deepcopy(current.get("sta_scope_gate")),
            "measurement_partitions": copy.deepcopy(current.get("measurement_partitions", [])),
            "measurement_timeout_seconds": current.get("measurement_timeout_seconds"),
        }
    elif phase == "waiting_external_verification":
        allowed = read_only
        applies = ["*"]
        enforcement = "hard"
        forbidden = ["optimize_rtl", "retry_measurement", "expand_blackbox", "continue"]
        recommended = [{"action": "inspect_existing_sta_evidence",
                        "reason": "global_sta_obligation_is_owned_by_outer_scheduler"}]
    elif phase == "rollback_required":
        allowed = ["rollback_optimization", *read_only]
        applies = ["*"]
        enforcement = "hard"
        forbidden = ["optimize_rtl", "retry_measurement", "expand_blackbox", "continue"]
        recommended = [{"action": "rollback_optimization", "reason": "candidate_failed_hard_gate"}]
    else:
        allowed = ["defer_optimization", *read_only]
        applies = ["*"]
        enforcement = "hard"
        forbidden = ["optimize_rtl", "expand_blackbox", "continue"]
    module = current.get("module") if current else None
    parameters = {
        action: {"module": {"enum": [module]}} for action in allowed
        if module and action in {"run_optimization_edit", "verify_optimization_candidate",
                                 "retry_measurement", "defer_optimization", "rollback_optimization"}
    }
    if "retry_measurement" in parameters:
        parameters["retry_measurement"]["flash_attempts"] = {"enum": [1]}
    return {
        "schema_version": 1,
        "contract_kind": "active_optimization" if current else "optimization_state",
        "enforcement": enforcement,
        "current_phase": phase,
        "applies_to_actions": applies,
        "allowed_actions": allowed,
        "forbidden_actions": forbidden,
        "action_parameters": parameters,
        "execution_profiles": profiles,
        "hard_constraints": {
            "candidate_module": module,
            "candidate_id": current.get("campaign_id") if current else None,
            "same_candidate_only": current is not None,
            "no_edit_during_measurement": phase in {
                "modified_pending_functional", "sta_required", "measurement_pending", "infra_pending",
                "waiting_external_verification"},
            "mainline_resume_pending": phase == "resume_pending",
        },
        "recommended_actions": recommended,
        "strategic_questions": (["继续有限重测还是冻结候选并恢复主线？"]
                                if phase in {"sta_required", "measurement_pending", "infra_pending"} else []),
        "retry_budget": {
            "measurement_retries_used": used,
            "measurement_retries_max": maximum,
            "measurement_retries_remaining": remaining,
            "infrastructure_failures_consume_measurement_retry": False,
        },
        "resume_context": {
            "interrupted_mainline_ref": copy.deepcopy(
                state.get("interrupted_mainline_ref")),
        },
        "resume_request": copy.deepcopy(state.get("resume_request")),
        "verification_obligation": copy.deepcopy(
            current.get("verification_obligation") if current else None),
    }


def _with_contract(state: dict[str, object]) -> dict[str, object]:
    state["execution_contract"] = _execution_contract(state)
    return state


def migrate_state(state: Mapping[str, object]) -> dict[str, object]:
    result = copy.deepcopy(dict(state))
    result["schema_version"] = SCHEMA_VERSION
    result.setdefault("acceptance_debt", [])
    result.setdefault("accepted", [])
    result.setdefault("rejected", [])
    result.setdefault("deferred", [])
    result.setdefault("max_repair_attempts", DEFAULT_MAX_REPAIR_ATTEMPTS)
    result.setdefault("max_measurement_retries", DEFAULT_MAX_MEASUREMENT_RETRIES)
    result.setdefault("max_edit_attempts", DEFAULT_MAX_EDIT_ATTEMPTS)
    result.setdefault("max_internal_steps", DEFAULT_MAX_INTERNAL_STEPS)
    legacy_mainline = result.pop("mainline", None)
    legacy_transaction_ref = result.pop("interrupted_transaction_ref", None)
    if not result.get("interrupted_mainline_ref"):
        result["interrupted_mainline_ref"] = _mainline_ref(legacy_mainline)
    else:
        result["interrupted_mainline_ref"] = _mainline_ref(
            result.get("interrupted_mainline_ref"))
    # A Ledger-only legacy reference lacks the immutable module identity needed
    # by the new Registry handoff.  Do not silently promote it into a mainline.
    if not result.get("interrupted_mainline_ref") and legacy_transaction_ref:
        result["migration_hold"] = "legacy_ledger_reference_requires_registry_rebind"
    result.setdefault("resume_request", None)
    result.setdefault("processed_event_ids", [])
    current = result.get("current_candidate")
    if isinstance(current, dict):
        if "campaign_id" not in current and current.get("checkpoint_id"):
            current["campaign_id"] = current.get("checkpoint_id")
        if "checkpoint_ref" not in current and current.get("checkpoint_id"):
            current["checkpoint_ref"] = current.get("checkpoint_id")
        current.pop("checkpoint_id", None)
        current.setdefault("edit_attempts", 0)
        current.setdefault("repair_attempts", 0)
        current.setdefault("measurement_retries", 0)
        current.setdefault("execution_steps", 0)
        current.setdefault("rtl_fingerprint", None)
        current.setdefault("functional_evidence", None)
        current.setdefault("preflight_evidence", None)
        current.setdefault("verification_obligation", None)
        current.setdefault("sta_scope_gate", None)
        current.setdefault("measurement_partitions", [])
        current.setdefault("measurement_timeout_seconds", None)
    return _with_contract(result)


def initial_state(interrupted_mainline_ref: Mapping[str, object],
                  max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
                  max_measurement_retries: int = DEFAULT_MAX_MEASUREMENT_RETRIES,
                  max_edit_attempts: int = DEFAULT_MAX_EDIT_ATTEMPTS,
                  max_internal_steps: int = DEFAULT_MAX_INTERNAL_STEPS) -> dict[str, object]:
    return _with_contract({
        "schema_version": SCHEMA_VERSION,
        "phase": "idle",
        "interrupted_mainline_ref": _mainline_ref(interrupted_mainline_ref) or None,
        "current_candidate": None,
        "acceptance_debt": [],
        "accepted": [],
        "rejected": [],
        "deferred": [],
        "max_repair_attempts": max(1, int(max_repair_attempts)),
        "max_measurement_retries": max(0, int(max_measurement_retries)),
        "max_edit_attempts": max(1, int(max_edit_attempts)),
        "max_internal_steps": max(1, int(max_internal_steps)),
        "resume_request": None,
        "processed_event_ids": [],
        "next_action": "resume_mainline",
    })


def transition(state: Mapping[str, object], event: Mapping[str, object]) -> dict[str, object]:
    result = migrate_state(state)
    event_id = str(event.get("event_id") or "").strip()
    processed = result.get("processed_event_ids")
    processed = list(processed) if isinstance(processed, list) else []
    if event_id and event_id in processed:
        return result
    result.pop("execution_contract", None)
    kind = str(event.get("kind", ""))
    current = result.get("current_candidate")

    if kind == "begin_candidate":
        if current is not None:
            raise ValueError("an optimization candidate is already active")
        module = str(event.get("module", "")).strip()
        checkpoint = str(event.get("checkpoint_ref") or "").strip()
        if not module or not checkpoint:
            raise ValueError("begin_candidate requires module and checkpoint_ref")
        integration_target = str(event.get("integration_target", "")).strip()
        insertion_mode = str(event.get("insertion_mode", "fused_same_unit"))
        if insertion_mode not in {"fused_same_unit", "decoupled"}:
            raise ValueError("begin_candidate insertion_mode is invalid")
        if insertion_mode == "fused_same_unit" and integration_target and integration_target != module:
            raise ValueError("fused_same_unit requires optimization module to equal integration target")
        interrupted_ref = _mainline_ref(event.get("interrupted_mainline_ref"))
        if not interrupted_ref:
            raise ValueError("begin_candidate requires one validated interrupted_mainline_ref")
        result["interrupted_mainline_ref"] = interrupted_ref
        result["current_candidate"] = {
            "module": module,
            "hypothesis": str(event.get("hypothesis", "")),
            "campaign_id": str(event.get("campaign_id") or checkpoint),
            "checkpoint_ref": checkpoint,
            "rtl_ref": event.get("rtl_ref"),
            "edit_attempts": 0,
            "repair_attempts": 0,
            "measurement_retries": 0,
            "execution_steps": 0,
            "integration_target": integration_target or None,
            "insertion_mode": insertion_mode,
            "resume_token": copy.deepcopy(event.get("resume_token")),
            "top_level_acceptance_required": bool(event.get("top_level_acceptance_required")),
            "rtl_fingerprint": event.get("rtl_fingerprint"),
            "functional_evidence": copy.deepcopy(event.get("functional_evidence")),
            "preflight_evidence": copy.deepcopy(event.get("preflight_evidence")),
            "sta_scope_gate": copy.deepcopy(event.get("sta_scope_gate")),
            "measurement_partitions": copy.deepcopy(event.get("measurement_partitions", [])),
            "measurement_timeout_seconds": event.get("measurement_timeout_seconds"),
            "baseline_measurement": copy.deepcopy(event.get("baseline_measurement")),
            "baseline_value_score": event.get("baseline_value_score"),
            "verification_obligations": _verification_obligations(
                module, integration_target, event.get("change_id")),
            "pending_terminal_outcome": None,
        }
        result["phase"] = "editing"
        result["next_action"] = "edit_candidate"
    elif kind == "edit_failed":
        if not isinstance(current, dict):
            raise ValueError("edit_failed requires an active candidate")
        if result.get("phase") not in {"editing", "functional_repair"}:
            raise ValueError("edit_failed requires editing or functional_repair")
        current["edit_attempts"] = int(current.get("edit_attempts", 0)) + 1
        current["execution_steps"] = int(current.get("execution_steps", 0)) + 1
        current["last_edit_failure"] = {
            "reason": str(event.get("reason") or "edit_failed"),
            "evidence": copy.deepcopy(event.get("evidence")),
        }
        if int(current["edit_attempts"]) >= int(
                result.get("max_edit_attempts", DEFAULT_MAX_EDIT_ATTEMPTS)):
            current["pending_terminal_outcome"] = "deferred"
            current["pending_terminal_reason"] = "edit_attempt_budget_exhausted"
            result["phase"] = "rollback_required"
            result["next_action"] = "rollback_candidate"
        else:
            result["next_action"] = "retry_edit_candidate"
    elif kind == "edit_completed":
        if not isinstance(current, dict):
            raise ValueError("edit_completed requires an active candidate")
        if result.get("phase") not in {"editing", "functional_repair"}:
            raise ValueError("edit_completed requires editing or functional_repair")
        expected = str(current.get("campaign_id") or "")
        observed = str(event.get("campaign_id") or "")
        if expected and observed != expected:
            raise ValueError("edit_completed campaign identity mismatch")
        fingerprint = str(event.get("rtl_fingerprint") or "")
        if not fingerprint:
            raise ValueError("edit_completed requires rtl_fingerprint")
        current["edit_attempts"] = int(current.get("edit_attempts", 0)) + 1
        current["execution_steps"] = int(current.get("execution_steps", 0)) + 1
        current["last_edit_was_repair"] = result.get("phase") == "functional_repair"
        current["rtl_fingerprint"] = fingerprint
        # Any edit invalidates evidence tied to the previous RTL image.
        current["functional_evidence"] = None
        current["preflight_evidence"] = None
        current["verification_obligation"] = None
        current["diff_ref"] = event.get("diff_ref")
        current["edit_operation_id"] = event.get("operation_id")
        result["phase"] = "modified_pending_functional"
        result["next_action"] = "run_functional"
    elif kind == "edit_infra_uncertain":
        if not isinstance(current, dict):
            raise ValueError("edit_infra_uncertain requires an active candidate")
        if result.get("phase") not in {"editing", "functional_repair"}:
            raise ValueError("edit_infra_uncertain requires an edit phase")
        current["execution_steps"] = int(current.get("execution_steps", 0)) + 1
        current["last_edit_infrastructure_error"] = copy.deepcopy(event.get("evidence"))
        result["next_action"] = (
            "retry_repair_edit" if result.get("phase") == "functional_repair"
            else "retry_edit_candidate")
    elif kind == "infra_uncertain":
        if not isinstance(current, dict):
            raise ValueError("infra_uncertain requires an active candidate")
        current["infra_retry_phase"] = str(
            event.get("retry_phase") or result.get("phase") or "")
        result["phase"] = "infra_pending"
        result["next_action"] = "retry_same_verification"
    elif kind == "measurement_incomplete":
        if not isinstance(current, dict):
            raise ValueError("measurement_incomplete requires an active candidate")
        if event.get("retry_attempt") is True:
            current["measurement_retries"] = int(current.get("measurement_retries", 0)) + 1
        current["measurement_attempted"] = True
        for key in ("rtl_fingerprint", "functional_evidence", "preflight_evidence", "sta_scope_gate",
                    "measurement_partitions", "measurement_timeout_seconds", "measurement_fingerprint"):
            if key in event:
                current[key] = copy.deepcopy(event.get(key))
        if int(current.get("measurement_retries", 0)) >= int(
                result.get("max_measurement_retries", DEFAULT_MAX_MEASUREMENT_RETRIES)):
            current["pending_terminal_outcome"] = "deferred"
            current["pending_terminal_reason"] = "measurement_retry_budget_exhausted"
            result["phase"] = "rollback_required"
            result["next_action"] = "rollback_candidate"
        else:
            result["phase"] = "measurement_pending"
            result["next_action"] = "retry_same_verification"
    elif kind == "functional_failed":
        if not isinstance(current, dict):
            raise ValueError("functional_failed requires an active candidate")
        attempts = int(current.get("repair_attempts", 0))
        if event.get("repair_attempt") is True or current.get("last_edit_was_repair") is True:
            attempts += 1
        current["repair_attempts"] = attempts
        current["last_edit_was_repair"] = False
        limit = int(result.get("max_repair_attempts", DEFAULT_MAX_REPAIR_ATTEMPTS))
        if attempts >= limit:
            current["pending_terminal_outcome"] = "rejected"
            current["pending_terminal_reason"] = "functional_repair_budget_exhausted"
            result["phase"] = "rollback_required"
            result["next_action"] = "rollback_candidate"
        else:
            result["phase"] = "functional_repair"
            result["next_action"] = "repair_same_candidate"
    elif kind == "functional_passed":
        if not isinstance(current, dict):
            raise ValueError("functional_passed requires an active candidate")
        result["phase"] = "sta_required"
        result["next_action"] = "run_sta_scope"
        current["last_edit_was_repair"] = False
        if "functional_evidence" in event:
            current["functional_evidence"] = copy.deepcopy(event.get("functional_evidence"))
        if "preflight_evidence" in event:
            current["preflight_evidence"] = copy.deepcopy(event.get("preflight_evidence"))
        current.pop("infra_retry_phase", None)
        for obligation in current.get("verification_obligations", []):
            if (isinstance(obligation, dict)
                    and obligation.get("purpose") == "optimization_effect"):
                obligation["status"] = "awaiting_sta_scope"
    elif kind == "sta_scope_resolved":
        if not isinstance(current, dict):
            raise ValueError("sta_scope_resolved requires an active candidate")
        if result.get("phase") not in {"sta_required", "infra_pending"}:
            raise ValueError("sta_scope_resolved requires sta_required or infra_pending")
        gate = event.get("sta_scope_gate")
        if not isinstance(gate, Mapping):
            raise ValueError("sta_scope_resolved requires sta_scope_gate")
        partitions = event.get("measurement_partitions")
        if not isinstance(partitions, list) or not partitions:
            raise ValueError("sta_scope_resolved requires measurement_partitions")
        current["sta_scope_gate"] = copy.deepcopy(dict(gate))
        current["measurement_partitions"] = copy.deepcopy(partitions)
        current["measurement_timeout_seconds"] = event.get(
            "measurement_timeout_seconds")
        current["measurement_attempted"] = False
        current.pop("infra_retry_phase", None)
        result["phase"] = "measurement_pending"
        result["next_action"] = "run_measurement"
    elif kind == "external_verification_required":
        if not isinstance(current, dict):
            raise ValueError("external_verification_required requires an active candidate")
        for key in ("rtl_fingerprint", "functional_evidence", "preflight_evidence", "sta_scope_gate"):
            if key in event:
                current[key] = copy.deepcopy(event.get(key))
        current["verification_obligation"] = _external_sta_obligation(
            current, current.get("sta_scope_gate"))
        result["phase"] = "waiting_external_verification"
        result["next_action"] = "yield_external_sta_obligation"
    elif kind == "external_verification_unavailable":
        if not isinstance(current, dict):
            raise ValueError("external_verification_unavailable requires an active candidate")
        current["pending_terminal_outcome"] = "deferred"
        current["pending_terminal_reason"] = str(
            event.get("reason") or "external_verification_unavailable")
        current["external_verification_evidence"] = copy.deepcopy(event.get("evidence"))
        result["phase"] = "rollback_required"
        result["next_action"] = "rollback_candidate"
    elif kind == "external_measurement_incomplete":
        if not isinstance(current, dict):
            raise ValueError("external_measurement_incomplete requires an active candidate")
        current["measurement_retries"] = int(current.get("measurement_retries", 0)) + 1
        current["last_external_measurement"] = copy.deepcopy(event.get("evidence"))
        if int(current["measurement_retries"]) >= int(
                result.get("max_measurement_retries", DEFAULT_MAX_MEASUREMENT_RETRIES)):
            current["pending_terminal_outcome"] = "deferred"
            current["pending_terminal_reason"] = "external_measurement_retry_budget_exhausted"
            result["phase"] = "rollback_required"
            result["next_action"] = "rollback_candidate"
        else:
            result["phase"] = "waiting_external_verification"
            result["next_action"] = "yield_external_sta_obligation"
    elif kind == "combined_sta_passed":
        if not isinstance(current, dict):
            raise ValueError("combined_sta_passed requires an active candidate")
        evidence = copy.deepcopy(event.get("evidence", {}))
        if (not isinstance(evidence, Mapping)
                or evidence.get("combined_guard_passed") is not True):
            raise ValueError("combined_sta_passed requires deterministic combined guard proof")
        scope = str(evidence.get("measurement_scope") or "")
        acceptance = ("combined_top_complete" if scope == "TOP_LEVEL_COMPLETE"
                      else "combined_top_partial")
        accepted_record = {**current, "acceptance": acceptance,
                           "causality_attribution": "not_required",
                           "evidence": evidence}
        result["accepted"].append(accepted_record)
        if scope != "TOP_LEVEL_COMPLETE":
            result["acceptance_debt"].append({
                "module": current.get("module"),
                "integration_target": current.get("integration_target"),
                "campaign_id": current.get("campaign_id"),
                "checkpoint_ref": current.get("checkpoint_ref"),
                "reason": "final_complete_top_level_sta_pending",
                "combined_partial_evidence": evidence,
            })
        else:
            result["acceptance_debt"] = [
                item for item in result["acceptance_debt"]
                if not isinstance(item, Mapping) or item.get("module") != current.get("module")]
        result["current_candidate"] = None
        _enter_resume_pending(result, "combined_global_sta_accepted", accepted_record)
    elif kind == "sta_passed":
        if not isinstance(current, dict):
            raise ValueError("sta_passed requires an active candidate")
        evidence = copy.deepcopy(event.get("evidence", {}))
        value_evaluation = (evidence.get("value_evaluation")
                            if isinstance(evidence, Mapping) else {})
        if (not isinstance(value_evaluation, Mapping)
                or value_evaluation.get("improved") is not True
                or value_evaluation.get("verdict") != "improved"):
            raise ValueError("sta_passed requires deterministic PPA improvement proof")
        scope_gate = evidence.get("sta_scope_gate") if isinstance(evidence, dict) else {}
        scope = str(scope_gate.get("scope", "")) if isinstance(scope_gate, Mapping) else ""
        measurement_scope = str(evidence.get("measurement_scope", "")) if isinstance(evidence, dict) else ""
        sta_audit = evidence.get("sta_closure_audit") if isinstance(evidence, dict) else {}
        top_scope = (scope in {"top_level_partial", "top_level_complete"}
                     or measurement_scope in {"PARTIAL_HIERARCHICAL", "TOP_LEVEL_COMPLETE"})
        top_level_accepted = bool(
            top_scope and isinstance(sta_audit, Mapping)
            and sta_audit.get("sta_link_result") == "ok"
            and sta_audit.get("sta_timing_measured") is True)
        needs_top = current.get("top_level_acceptance_required") is True
        acceptance = "top_accepted" if top_level_accepted else "local_accepted"
        accepted_record = {**current, "acceptance": acceptance, "evidence": evidence}
        result["accepted"].append(accepted_record)
        if needs_top and not top_level_accepted:
            result["acceptance_debt"].append({
                "module": current.get("module"),
                "integration_target": current.get("integration_target"),
                "campaign_id": current.get("campaign_id"),
                "checkpoint_ref": current.get("checkpoint_ref"),
                "reason": "top_level_acceptance_pending",
                "local_evidence": evidence,
            })
        elif top_level_accepted:
            result["acceptance_debt"] = [
                item for item in result["acceptance_debt"]
                if not isinstance(item, Mapping) or item.get("module") != current.get("module")
            ]
        covered_raw = evidence.get("covered_modules") if isinstance(evidence, dict) else []
        covered = {str(item) for item in covered_raw} if isinstance(covered_raw, list) else set()
        if top_level_accepted and not covered:
            covered.add(str(current.get("module") or ""))
            covered.add(str(current.get("integration_target") or ""))
        for obligation in current.get("verification_obligations", []):
            if not isinstance(obligation, dict):
                continue
            target = str(obligation.get("target_module") or "")
            if obligation.get("purpose") == "optimization_effect":
                obligation["status"] = "satisfied"
                obligation["evidence"] = copy.deepcopy(evidence)
            elif (obligation.get("purpose") == "integration_acceptance"
                  and top_level_accepted and target in covered):
                obligation["status"] = "satisfied"
                obligation["evidence"] = copy.deepcopy(evidence)
        result["current_candidate"] = None
        _enter_resume_pending(result, "optimization_sta_accepted", accepted_record)
    elif kind == "sta_failed":
        if not isinstance(current, dict):
            raise ValueError("sta_failed requires an active candidate")
        current["pending_terminal_outcome"] = "rejected"
        current["pending_terminal_reason"] = "sta_failed"
        result["phase"] = "rollback_required"
        result["next_action"] = "rollback_candidate"
    elif kind == "optimization_not_improved":
        if not isinstance(current, dict):
            raise ValueError("optimization_not_improved requires an active candidate")
        evaluation = event.get("evidence")
        evaluation = evaluation if isinstance(evaluation, Mapping) else {}
        current["pending_terminal_outcome"] = "deferred"
        current["pending_terminal_reason"] = (
            "measurement_incomparable"
            if evaluation.get("verdict") == "incomparable"
            else "measured_improvement_below_threshold")
        current["evaluation_evidence"] = copy.deepcopy(evaluation)
        result["phase"] = "rollback_required"
        result["next_action"] = "rollback_candidate"
    elif kind == "rollback_complete":
        if not isinstance(current, dict):
            raise ValueError("rollback_complete requires an active candidate")
        proof = event.get("rollback_proof")
        proof = proof if isinstance(proof, Mapping) else {}
        required_proof = ("checkpoint_restored", "rtl_fingerprint_restored", "configuration_restored")
        if not all(proof.get(key) is True for key in required_proof):
            raise ValueError("rollback_complete requires verified checkpoint/RTL/configuration restoration")
        outcome = str(current.get("pending_terminal_outcome") or event.get("outcome") or "rejected")
        reason = str(current.get("pending_terminal_reason") or event.get("reason") or "candidate_rejected")
        current["execution_steps"] = int(current.get("execution_steps", 0)) + 1
        record = {**current, "reason": reason, "rollback_proof": copy.deepcopy(dict(proof))}
        if outcome == "deferred":
            result["deferred"].append(record)
        else:
            result["rejected"].append(record)
        result["current_candidate"] = None
        _enter_resume_pending(result, f"optimization_{outcome}_after_rollback", record)
    elif kind == "defer_candidate":
        if not isinstance(current, dict):
            raise ValueError("defer_candidate requires an active candidate")
        current["pending_terminal_outcome"] = "deferred"
        current["pending_terminal_reason"] = str(event.get("reason", "strategic_defer"))
        result["phase"] = "rollback_required"
        result["next_action"] = "rollback_candidate"
    elif kind == "mainline_resumed":
        if result.get("phase") != "resume_pending" or isinstance(current, dict):
            raise ValueError("mainline_resumed requires resume_pending without an active candidate")
        request = result.get("resume_request")
        request = request if isinstance(request, Mapping) else {}
        if event.get("registry_confirmed") is not True:
            raise ValueError("mainline_resumed requires registry confirmation")
        reference = request.get("mainline_ref")
        reference = reference if isinstance(reference, Mapping) else {}
        expected = str(reference.get("transaction_id") or "")
        observed = str(event.get("transaction_id") or "")
        if expected and observed != expected:
            raise ValueError("mainline_resumed transaction identity mismatch")
        result["resume_request"] = None
        result["interrupted_mainline_ref"] = None
        result["phase"] = "idle"
        result["next_action"] = "select_next"
    else:
        raise ValueError(f"unknown optimization event: {kind}")
    if event_id:
        processed.append(event_id)
        result["processed_event_ids"] = processed[-64:]
    return _with_contract(result)


def _operation_id(campaign_id: object, phase: object, step_number: int) -> str:
    identity = {
        "campaign_id": str(campaign_id or ""),
        "phase": str(phase or ""),
        "step_number": int(step_number),
    }
    return hashlib.sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _external_verification_request(
        current: Mapping[str, object]) -> dict[str, object]:
    """Expose one portable no-edit request without leaking durable state."""
    obligation_value = current.get("verification_obligation")
    obligation = (dict(obligation_value)
                  if isinstance(obligation_value, Mapping) else {})
    return {
        "executor_ref": "project.verify_optimization",
        "operation": "consume_external_sta_obligation",
        "operation_id": _operation_id(
            current.get("campaign_id"), "waiting_external_verification",
            int(current.get("measurement_retries", 0)) + 1),
        "obligation_id": obligation.get("obligation_id"),
        "campaign_id": current.get("campaign_id"),
        "module": current.get("module"),
        "rtl_ref": current.get("rtl_ref"),
        "checkpoint_ref": current.get("checkpoint_ref"),
        "expected_rtl_fingerprint": current.get("rtl_fingerprint"),
        "functional_evidence": copy.deepcopy(current.get("functional_evidence")),
        "preflight_evidence": copy.deepcopy(current.get("preflight_evidence")),
        "integration_target": current.get("integration_target"),
        "required_scope": obligation.get("required_scope"),
        "edit_allowed": False,
    }


def _apply_execution_observation(state: Mapping[str, object],
                                 observation: Mapping[str, object],
                                 policy: Mapping[str, object]) -> dict[str, object]:
    """Reduce one project-normalized execution observation into Skill state."""
    result = migrate_state(state)
    current = result.get("current_candidate")
    if not isinstance(current, dict):
        raise ValueError("execution observation requires an active optimization candidate")
    observed_campaign = str(observation.get("campaign_id") or "")
    expected_campaign = str(current.get("campaign_id") or "")
    if observed_campaign and observed_campaign != expected_campaign:
        raise ValueError("execution observation campaign identity mismatch")
    observed_module = str(observation.get("module") or "")
    if observed_module and observed_module != str(current.get("module") or ""):
        raise ValueError("execution observation module identity mismatch")
    observation_id = str(observation.get("observation_id") or "").strip()
    processed = result.get("processed_observation_ids")
    processed = list(processed) if isinstance(processed, list) else []
    if observation_id and observation_id in processed:
        return result

    current["execution_steps"] = int(current.get("execution_steps", 0)) + 1
    status = str(observation.get("status") or "")
    prior_phase = str(result.get("phase") or "")
    event_prefix = observation_id or _operation_id(
        expected_campaign, prior_phase, int(current["execution_steps"]))

    if status == "infra_uncertain":
        result = transition(result, {
            "kind": "infra_uncertain", "event_id": f"{event_prefix}:infra",
            "retry_phase": prior_phase,
        })
    elif status == "functional_failed":
        if prior_phase not in {"modified_pending_functional", "infra_pending"}:
            raise ValueError("functional_failed observation arrived outside functional phase")
        result = transition(result, {
            "kind": "functional_failed", "event_id": f"{event_prefix}:functional",
        })
    elif status == "functional_passed":
        if prior_phase not in {"modified_pending_functional", "infra_pending"}:
            raise ValueError("functional_passed observation arrived outside functional phase")
        result = transition(result, {
            "kind": "functional_passed",
            "event_id": f"{event_prefix}:functional-pass",
            "functional_evidence": copy.deepcopy(observation.get("functional_evidence")),
            "preflight_evidence": copy.deepcopy(observation.get("preflight_evidence")),
        })
    elif status == "sta_scope_resolved":
        result = transition(result, {
            "kind": "sta_scope_resolved",
            "event_id": f"{event_prefix}:sta-scope",
            "sta_scope_gate": copy.deepcopy(observation.get("sta_scope_gate")),
            "measurement_partitions": copy.deepcopy(
                observation.get("measurement_partitions", [])),
            "measurement_timeout_seconds": observation.get(
                "measurement_timeout_seconds"),
        })
    elif status == "sta_scope_unavailable":
        if prior_phase not in {"sta_required", "infra_pending"}:
            raise ValueError("STA scope unavailability arrived outside STA scope phase")
        # The scope capability is optional at integration time.  Its absence
        # cannot become an RTL failure or block the framework; conservatively
        # yield a complete top-level STA obligation to the outer scheduler.
        result = transition(result, {
            "kind": "external_verification_required",
            "event_id": f"{event_prefix}:sta-scope-unavailable",
            "rtl_fingerprint": observation.get("rtl_fingerprint"),
            "sta_scope_gate": {
                "scope": "top_level_complete",
                "reason_codes": ["sta_scope_capability_unavailable"],
                "capability_error": observation.get("reason"),
            },
        })
    elif status == "measurement_incomplete":
        if prior_phase not in {"measurement_pending", "infra_pending"}:
            raise ValueError("measurement_incomplete observation arrived outside measurement phase")
        result = transition(result, {
            "kind": "measurement_incomplete",
            "event_id": f"{event_prefix}:measurement-incomplete",
            "retry_attempt": current.get("measurement_attempted") is True,
            "rtl_fingerprint": observation.get("rtl_fingerprint"),
            "functional_evidence": copy.deepcopy(observation.get("functional_evidence")),
            "preflight_evidence": copy.deepcopy(observation.get("preflight_evidence")),
            "sta_scope_gate": copy.deepcopy(observation.get("sta_scope_gate")),
            "measurement_partitions": copy.deepcopy(
                observation.get("measurement_partitions", [])),
            "measurement_timeout_seconds": observation.get("measurement_timeout_seconds"),
            "measurement_fingerprint": observation.get("measurement_fingerprint"),
        })
    elif status == "external_verification_required":
        if prior_phase not in {"sta_required", "infra_pending"}:
            raise ValueError("external verification request arrived outside STA scope phase")
        result = transition(result, {
            "kind": "external_verification_required",
            "event_id": f"{event_prefix}:external-sta",
            "rtl_fingerprint": observation.get("rtl_fingerprint"),
            "functional_evidence": copy.deepcopy(observation.get("functional_evidence")),
            "preflight_evidence": copy.deepcopy(observation.get("preflight_evidence")),
            "sta_scope_gate": copy.deepcopy(observation.get("sta_scope_gate")),
        })
    elif status == "external_verification_unavailable":
        result = transition(result, {
            "kind": "external_verification_unavailable",
            "event_id": f"{event_prefix}:external-sta-unavailable",
            "reason": observation.get("reason"),
            "evidence": copy.deepcopy(observation.get("evidence")),
        })
    elif status == "external_measurement_observed":
        raw_external = observation.get("external_measurement")
        external = (dict(raw_external)
                    if isinstance(raw_external, Mapping) else {})
        if external.get("reported_incomplete") is True:
            result = transition(result, {
                "kind": "external_measurement_incomplete",
                "event_id": f"{event_prefix}:external-incomplete",
                "evidence": external,
            })
        else:
            obligation_value = current.get("verification_obligation")
            obligation = (obligation_value
                          if isinstance(obligation_value, Mapping) else {})
            expected_fingerprint = str(current.get("rtl_fingerprint") or "")
            observed_fingerprint = str(observation.get("rtl_fingerprint") or "")
            expected_obligation = str(obligation.get("obligation_id") or "")
            observed_obligation = str(observation.get("obligation_id") or "")
            required_scope = str(obligation.get("required_scope") or "")
            measured_scope = str(external.get("measurement_scope") or "")
            scope_ok = (measured_scope == "TOP_LEVEL_COMPLETE"
                        if required_scope == "top_level_complete"
                        else measured_scope in {
                            "PARTIAL_HIERARCHICAL", "TOP_LEVEL_COMPLETE"})
            audit_value = external.get("sta_closure_audit")
            audit = audit_value if isinstance(audit_value, Mapping) else {}
            guard_passed = bool(
                expected_fingerprint and observed_fingerprint == expected_fingerprint
                and expected_obligation and observed_obligation == expected_obligation
                and external.get("ppa_exit") == 0
                and audit.get("sta_link_result") == "ok"
                and audit.get("sta_timing_measured") is True
                and scope_ok and external.get("timing_target_met") is True)
            sta_evidence = {
                "combined_guard_passed": guard_passed,
                "causality_attribution": "not_required",
                "measurement_scope": measured_scope,
                "required_scope": required_scope,
                "timing_target_met": external.get("timing_target_met") is True,
                "sta_scope_gate": copy.deepcopy(external.get("sta_scope_gate")),
                "sta_closure_audit": copy.deepcopy(dict(audit)),
                "result_kind": external.get("result_kind"),
            }
            if guard_passed:
                result = transition(result, {
                    "kind": "combined_sta_passed",
                    "event_id": f"{event_prefix}:combined-accepted",
                    "evidence": sta_evidence,
                })
            else:
                result = transition(result, {
                    "kind": "sta_failed",
                    "event_id": f"{event_prefix}:combined-failed",
                })
    elif status == "measurement_observed":
        if prior_phase not in {"measurement_pending", "infra_pending"}:
            raise ValueError("measurement observation arrived outside measurement phase")
        measurement_value = observation.get("measurement")
        measurement = (measurement_value
                       if isinstance(measurement_value, Mapping) else {})
        attributable = bool(
            measurement.get("attribution_quality") in {
                "direct_partition", "reported_module"}
            and measurement.get("current_design_match") is not False
            and measurement.get("path_class") != "non_physical_artifact")
        required_measurement = (
            measurement.get("comparability_key"), measurement.get("area_um2"),
            measurement.get("classified_delay_ps", measurement.get("delay_ps")),
            measurement.get("source_run"))
        measurement_ready = bool(
            observation.get("ppa_exit") == 0
            and observation.get("measurement_artifact_available") is True
            and attributable
            and all(value not in (None, "") for value in required_measurement))
        if not measurement_ready:
            result = transition(result, {
                "kind": "measurement_incomplete",
                "event_id": f"{event_prefix}:measurement-incomplete",
                "retry_attempt": current.get("measurement_attempted") is True,
                "rtl_fingerprint": observation.get("rtl_fingerprint"),
                "functional_evidence": copy.deepcopy(
                    observation.get("functional_evidence")),
                "preflight_evidence": copy.deepcopy(
                    observation.get("preflight_evidence")),
                "sta_scope_gate": copy.deepcopy(observation.get("sta_scope_gate")),
                "measurement_partitions": copy.deepcopy(
                    observation.get("measurement_partitions", [])),
                "measurement_timeout_seconds": observation.get(
                    "measurement_timeout_seconds"),
                "measurement_fingerprint": observation.get(
                    "measurement_fingerprint"),
            })
        else:
            candidate = result.get("current_candidate")
            candidate = candidate if isinstance(candidate, Mapping) else {}
            verdict = measurement_verdict(
                candidate.get("baseline_measurement"), measurement,
                minimum_improvement_ratio=float(
                    policy.get("minimum_improvement_ratio", 0.01)),
                maximum_regression_ratio=float(
                    policy.get("maximum_regression_ratio", 0.01)),
            )
            sta_evidence = observation.get("sta_evidence")
            sta_evidence = (dict(sta_evidence)
                            if isinstance(sta_evidence, Mapping) else {})
            if verdict.get("improved") is True:
                result = transition(result, {
                    "kind": "sta_passed", "event_id": f"{event_prefix}:accepted",
                    "evidence": {**sta_evidence, "value_evaluation": verdict},
                })
            else:
                # A complete measurement with a different comparability key is
                # a deterministic policy mismatch, not a timeout worth repeating.
                result = transition(result, {
                    "kind": "optimization_not_improved",
                    "event_id": f"{event_prefix}:not-improved",
                    "evidence": verdict,
                })
    elif status in {"sta_failed", "verification_failed", "no_change"}:
        event = ({"kind": "optimization_not_improved",
                  "evidence": {"verdict": "not_improved",
                               "reason_codes": ["candidate_has_no_effect"]}}
                 if status == "no_change" else {"kind": "sta_failed"})
        result = transition(result, {
            **event, "event_id": f"{event_prefix}:{status}",
        })
    else:
        raise ValueError(f"unknown optimization execution status: {status}")

    if observation_id:
        processed.append(observation_id)
        result["processed_observation_ids"] = processed[-64:]
    return _with_contract(result)


def step(packet: Mapping[str, object]) -> dict[str, object]:
    """Advance one optimization campaign without project-specific commands.

    The returned execution request is declarative.  A project adapter maps its
    ``executor_ref`` and parameters to paths or commands; the controller only
    validates identity and dispatches it.
    """
    state_value = packet.get("state")
    state = migrate_state(state_value if isinstance(state_value, Mapping) else {})
    policy = packet.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    observation = packet.get("observation")
    if isinstance(observation, Mapping):
        state = _apply_execution_observation(state, observation, policy)

    current = state.get("current_candidate")
    current = current if isinstance(current, Mapping) else None
    phase = str(state.get("phase") or "idle")
    if current is not None:
        maximum = int(state.get("max_internal_steps", DEFAULT_MAX_INTERNAL_STEPS))
        if (int(current.get("execution_steps", 0)) >= maximum
                and phase not in {"rollback_required", "resume_pending",
                                  "waiting_external_verification"}):
            state = transition(state, {
                "kind": "defer_candidate",
                "event_id": f"{current.get('campaign_id')}:internal-step-budget",
                "reason": "optimization_internal_step_budget_exhausted",
            })
            current = state.get("current_candidate")
            current = current if isinstance(current, Mapping) else None
            phase = str(state.get("phase") or "")

    request: dict[str, object] | None = None
    handoff: dict[str, object] | None = None
    if phase == "idle":
        intent = packet.get("automation_intent")
        intent = intent if isinstance(intent, Mapping) else None
        candidate = packet.get("candidate")
        candidate = candidate if isinstance(candidate, Mapping) else {}
        mainline_ref = _mainline_ref(packet.get("mainline_ref"))
        if intent is not None and mainline_ref:
            module = str(intent.get("module") or "")
            insertion = intent.get("insertion_plan")
            insertion = insertion if isinstance(insertion, Mapping) else {}
            campaign_id = str(packet.get("campaign_id") or
                              f"optimization:{mainline_ref.get('transaction_id')}:{module}")
            request = {
                "executor_ref": "optimization.edit",
                "operation": "start_campaign",
                "operation_id": _operation_id(campaign_id, "editing", 1),
                "campaign_id": campaign_id,
                "module": module,
                "candidate": copy.deepcopy(dict(candidate)),
                "mainline_ref": mainline_ref,
                "insertion_plan": copy.deepcopy(dict(insertion)),
            }
    elif current is not None and phase in {"editing", "functional_repair"}:
        request = {
            "executor_ref": "optimization.edit",
            "operation": "repair_candidate" if phase == "functional_repair" else "edit_candidate",
            "operation_id": _operation_id(
                current.get("campaign_id"), phase, int(current.get("execution_steps", 0)) + 1),
            "campaign_id": current.get("campaign_id"),
            "module": current.get("module"),
            "rtl_ref": current.get("rtl_ref"),
            "checkpoint_ref": current.get("checkpoint_ref"),
        }
    elif current is not None and phase == "modified_pending_functional":
        request = {
            "executor_ref": "project.verify_optimization",
            "operation": "functional_preflight",
            "operation_id": _operation_id(
                current.get("campaign_id"), phase, int(current.get("execution_steps", 0)) + 1),
            "campaign_id": current.get("campaign_id"),
            "module": current.get("module"),
            "rtl_ref": current.get("rtl_ref"),
            "checkpoint_ref": current.get("checkpoint_ref"),
            "expected_rtl_fingerprint": current.get("rtl_fingerprint"),
            "functional_evidence": copy.deepcopy(current.get("functional_evidence")),
            "preflight_evidence": copy.deepcopy(current.get("preflight_evidence")),
            "measurement_partitions": copy.deepcopy(current.get("measurement_partitions", [])),
            "measurement_timeout_seconds": current.get("measurement_timeout_seconds"),
            "integration_target": current.get("integration_target"),
            "edit_allowed": False,
        }
    elif current is not None and (
            phase == "sta_required"
            or (phase == "infra_pending"
                and current.get("infra_retry_phase") == "sta_required")):
        request = {
            "executor_ref": "capability.resolve_sta_scope",
            "operation": "resolve_sta_scope",
            "operation_id": _operation_id(
                current.get("campaign_id"), phase,
                int(current.get("execution_steps", 0)) + 1),
            "campaign_id": current.get("campaign_id"),
            "module": current.get("module"),
            "rtl_ref": current.get("rtl_ref"),
            "checkpoint_ref": current.get("checkpoint_ref"),
            "expected_rtl_fingerprint": current.get("rtl_fingerprint"),
            "edit_allowed": False,
        }
    elif current is not None and (
            phase == "measurement_pending"
            or (phase == "infra_pending"
                and current.get("infra_retry_phase") == "measurement_pending")):
        request = {
            "executor_ref": "project.verify_optimization",
            "operation": "measure_candidate",
            "operation_id": _operation_id(
                current.get("campaign_id"), phase,
                int(current.get("execution_steps", 0)) + 1),
            "campaign_id": current.get("campaign_id"),
            "module": current.get("module"),
            "rtl_ref": current.get("rtl_ref"),
            "checkpoint_ref": current.get("checkpoint_ref"),
            "expected_rtl_fingerprint": current.get("rtl_fingerprint"),
            "functional_evidence": copy.deepcopy(current.get("functional_evidence")),
            "preflight_evidence": copy.deepcopy(current.get("preflight_evidence")),
            "sta_scope_gate": copy.deepcopy(current.get("sta_scope_gate")),
            "measurement_partitions": copy.deepcopy(
                current.get("measurement_partitions", [])),
            "measurement_timeout_seconds": current.get(
                "measurement_timeout_seconds"),
            "integration_target": current.get("integration_target"),
            "edit_allowed": False,
        }
    elif current is not None and phase == "infra_pending":
        request = {
            "executor_ref": "project.verify_optimization",
            "operation": "functional_preflight",
            "operation_id": _operation_id(
                current.get("campaign_id"), phase,
                int(current.get("execution_steps", 0)) + 1),
            "campaign_id": current.get("campaign_id"),
            "module": current.get("module"),
            "rtl_ref": current.get("rtl_ref"),
            "checkpoint_ref": current.get("checkpoint_ref"),
            "expected_rtl_fingerprint": current.get("rtl_fingerprint"),
            "edit_allowed": False,
        }
    elif current is not None and phase == "rollback_required":
        request = {
            "executor_ref": "optimization.rollback",
            "operation": "rollback_candidate",
            "operation_id": _operation_id(
                current.get("campaign_id"), phase, int(current.get("execution_steps", 0)) + 1),
            "campaign_id": current.get("campaign_id"),
            "module": current.get("module"),
            "rtl_ref": current.get("rtl_ref"),
            "checkpoint_ref": current.get("checkpoint_ref"),
        }
    elif current is not None and phase == "waiting_external_verification":
        handoff = {
            "kind": "external_verification_required",
            "verification_obligation": copy.deepcopy(current.get("verification_obligation")),
            "mainline_ref": copy.deepcopy(state.get("interrupted_mainline_ref")),
            "requested_execution": _external_verification_request(current),
        }
    elif phase == "resume_pending":
        handoff = {
            "kind": "resume_mainline",
            "resume_request": copy.deepcopy(state.get("resume_request")),
        }

    return {
        "schema_version": 1,
        "state": _with_contract(state),
        "disposition": "execute" if request else "handoff" if handoff else "idle",
        "requested_execution": request,
        "handoff": handoff,
    }
