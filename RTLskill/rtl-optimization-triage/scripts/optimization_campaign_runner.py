#!/usr/bin/env python3
"""Drive Optimization Skill-owned stages until a project or strategy boundary.

The runner is intentionally project-neutral.  A caller supplies a dispatcher
for executable requests.  Skill-owned edit/rollback stages may be completed in
one call; project measurement and external STA obligations remain explicit
boundaries unless the caller elects to dispatch them too.
"""
from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from optimization_governance import initial_state, migrate_state, step, transition, triage
from optimization_state_store import (clear_observation, load_governance,
                                      load_observation, save_governance)


Dispatch = Callable[[Mapping[str, object]], Mapping[str, object]]
ResolveHandoff = Callable[[Mapping[str, object]], Mapping[str, object]]


def _run_until_boundary(packet: Mapping[str, object], dispatch: Dispatch, *,
                        state_path: Path, observation_path: Path,
                        dispatchable_executor_refs: set[str] | None = None,
                        max_dispatches: int = 12) -> dict[str, object]:
    """Run deterministic campaign stages and return at the next outer boundary.

    Dispatcher results must contain either ``governance_state`` (for a Skill
    runner that already applied its transition) or one normalized
    ``observation``.  The function never invents project commands or interprets
    raw tool logs.
    """
    allowed = (set(dispatchable_executor_refs) if dispatchable_executor_refs is not None
               else {"optimization.edit", "optimization.rollback",
                     "project.verify_optimization", "capability.resolve_sta_scope"})
    current_packet: dict[str, Any] = copy.deepcopy(dict(packet))
    trace: list[dict[str, object]] = []
    for _ in range(max(1, int(max_dispatches)) + 1):
        consumes_envelope = (
            observation_path.is_file()
            and isinstance(current_packet.get("observation"), Mapping))
        result = step(current_packet)
        state = result.get("state")
        if not isinstance(state, Mapping):
            raise ValueError("Optimization step returned no governance state")
        save_governance(state_path, state)
        if consumes_envelope:
            # Persist-before-delete makes replay safe.  A crash between these
            # operations replays an observation idempotently by observation_id.
            clear_observation(observation_path)
        request = result.get("requested_execution")
        if not isinstance(request, Mapping):
            return {**result, "campaign_trace": trace,
                    "boundary_reason": (result.get("handoff") or {}).get("kind")
                    if isinstance(result.get("handoff"), Mapping) else "idle"}
        executor_ref = str(request.get("executor_ref") or "")
        if executor_ref not in allowed:
            return {**result, "campaign_trace": trace,
                    "boundary_reason": "executor_not_dispatched"}
        response = dispatch(request)
        if not isinstance(response, Mapping):
            raise ValueError("campaign dispatcher must return one mapping")
        trace.append({
            "executor_ref": executor_ref,
            "operation_id": request.get("operation_id"),
            "response_status": response.get("status"),
        })
        boundary = response.get("boundary")
        if isinstance(boundary, Mapping):
            kind = str(boundary.get("kind") or "execution_boundary")
            return {**result, "campaign_trace": trace,
                    "boundary_reason": kind,
                    "execution_boundary": copy.deepcopy(dict(boundary))}
        updated = response.get("governance_state")
        if isinstance(updated, Mapping):
            save_governance(state_path, updated)
            current_packet["state"] = copy.deepcopy(dict(updated))
            current_packet.pop("observation", None)
            # An automation intent starts one campaign only.  Keeping it after
            # rollback/resume could accidentally begin a second candidate.
            current_packet["automation_intent"] = None
            continue
        observation = response.get("observation")
        if not isinstance(observation, Mapping):
            raise ValueError(
                "campaign dispatcher response requires governance_state or observation")
        current_packet["state"] = copy.deepcopy(dict(result["state"]))
        current_packet["observation"] = copy.deepcopy(dict(observation))
        current_packet["automation_intent"] = None
        if observation.get("status") == "infra_uncertain":
            terminal = step(current_packet)
            terminal_state = terminal.get("state")
            if not isinstance(terminal_state, Mapping):
                raise ValueError("Optimization infra boundary returned no state")
            save_governance(state_path, terminal_state)
            return {**terminal, "campaign_trace": trace,
                    "boundary_reason": "infra_uncertain"}
    raise RuntimeError("optimization campaign dispatch budget exhausted")


def _resume_completed_mainline(
        state: Mapping[str, object], registry: Mapping[str, object]) -> dict[str, object]:
    """Let the Skill consume one Registry-confirmed mainline completion."""
    migrated = migrate_state(state)
    if migrated.get("phase") != "resume_pending" or isinstance(
            migrated.get("current_candidate"), Mapping):
        return migrated
    request = migrated.get("resume_request")
    request = request if isinstance(request, Mapping) else {}
    reference = request.get("mainline_ref")
    reference = reference if isinstance(reference, Mapping) else {}
    if not reference:
        return migrated
    if registry.get("hard_gate") not in (None, "pass") or registry.get("conflicts"):
        return migrated
    if isinstance(registry.get("pending_transaction"), Mapping):
        return migrated
    transaction_id = str(reference.get("transaction_id") or "")
    return transition(migrated, {
        "kind": "mainline_resumed",
        "event_id": f"registry:{registry.get('generation')}:{transaction_id}:resumed",
        "transaction_id": transaction_id,
        "registry_confirmed": True,
        "registry_generation": registry.get("generation"),
    })


def _bind_runtime_policy(state: Mapping[str, object],
                         packet: Mapping[str, object]) -> dict[str, object]:
    """Apply configured limits and conservatively bind legacy active state."""
    live_ref = packet.get("mainline_ref")
    live_ref = live_ref if isinstance(live_ref, Mapping) else {}
    result = (migrate_state(state) if state
              else initial_state(live_ref))
    limits = packet.get("limits")
    limits = limits if isinstance(limits, Mapping) else {}
    for field, minimum in (
            ("max_repair_attempts", 1), ("max_measurement_retries", 0),
            ("max_edit_attempts", 1), ("max_internal_steps", 1)):
        value = limits.get(field)
        if isinstance(value, int):
            result[field] = max(minimum, value)

    saved_ref = result.get("interrupted_mainline_ref")
    saved_ref = saved_ref if isinstance(saved_ref, Mapping) else {}
    active = bool(result.get("phase") != "idle" or result.get("current_candidate"))
    saved_id = str(saved_ref.get("transaction_id") or "")
    live_id = str(live_ref.get("transaction_id") or "")
    if active and saved_id and live_id and saved_id != live_id:
        raise ValueError("active optimization mainline conflicts with Module Registry")
    if active and not saved_ref:
        if not live_ref:
            raise ValueError("active optimization state has no Registry mainline reference")
        result["interrupted_mainline_ref"] = copy.deepcopy(dict(live_ref))
        if result.get("phase") == "resume_pending":
            resume = result.get("resume_request")
            resume = dict(resume) if isinstance(resume, Mapping) else {}
            resume["mainline_ref"] = copy.deepcopy(dict(live_ref))
            result["resume_request"] = resume
    return migrate_state(result)


def _active_triage_projection(state: Mapping[str, object]) -> dict[str, object]:
    current = state.get("current_candidate")
    current = current if isinstance(current, Mapping) else {}
    return {
        "triage_status": "active_campaign",
        "recommended_candidate": current.get("module"),
        "next_action": state.get("next_action"),
        "mainline": None,
        "eligible_candidates": [],
        "ineligible_candidates": [],
        "automation_intent": None,
        "execution_contract": copy.deepcopy(state.get("execution_contract")),
    }


def _evidence_projection(result: Mapping[str, object],
                         triage_result: Mapping[str, object]) -> dict[str, object]:
    """Expose bounded evidence without exporting the durable governance object."""
    state_value = result.get("state")
    state = state_value if isinstance(state_value, Mapping) else {}
    current_value = state.get("current_candidate")
    current = current_value if isinstance(current_value, Mapping) else {}
    raw_candidates = triage_result.get("eligible_candidates")
    raw_candidates = raw_candidates if isinstance(raw_candidates, list) else []
    candidates = []
    for item in raw_candidates[:8]:
        if isinstance(item, Mapping):
            candidates.append({key: item.get(key) for key in (
                "module", "integration_state", "expanded_verified",
                "expansion_verified_once", "representation", "currently_blackboxed",
                "top_level_acceptance_pending", "value_score",
                "measured_area_um2", "measured_delay_ps", "measured_path_class",
                "source_instance_count", "latest_expansion_exit",
                "latest_expansion_measurement_incomplete",
            )})
    compact_candidate = ({key: current.get(key) for key in (
        "module", "campaign_id", "rtl_fingerprint", "integration_target",
        "insertion_mode", "top_level_acceptance_required", "edit_attempts",
        "repair_attempts", "measurement_retries", "execution_steps",
    )} if current else None)
    contract = (state.get("execution_contract") if current
                else triage_result.get("execution_contract"))
    resolution = result.get("handoff_resolution")
    resolution = copy.deepcopy(dict(resolution)) if isinstance(resolution, Mapping) else None
    return {
        "schema_version": 1,
        "hard_gate": {
            "phase": state.get("phase"),
            "recommended_candidate": triage_result.get("recommended_candidate"),
            "eligible_modules": [item.get("module") for item in candidates
                                 if item.get("module")],
            "next_action": state.get("next_action") or triage_result.get("next_action"),
        },
        "summary_input": {
            "triage": {
                "triage_status": triage_result.get("triage_status"),
                "recommended_candidate": triage_result.get("recommended_candidate"),
                "next_action": triage_result.get("next_action"),
                "mainline": triage_result.get("mainline"),
                "eligible_candidates": candidates,
                "ineligible_candidates": triage_result.get("ineligible_candidates", []),
            },
            "campaign": {
                "phase": state.get("phase"),
                "current_candidate": compact_candidate,
                "acceptance_debt": copy.deepcopy(state.get("acceptance_debt", [])),
                "resume_request": copy.deepcopy(state.get("resume_request")),
                "next_action": state.get("next_action"),
            },
            "obligation_match": resolution,
        },
        "execution_contract": (copy.deepcopy(dict(contract))
                               if isinstance(contract, Mapping) else None),
    }


def _finalize(result: Mapping[str, object],
              triage_result: Mapping[str, object]) -> dict[str, object]:
    finalized = dict(result)
    finalized["triage"] = copy.deepcopy(dict(triage_result))
    state_value = finalized.get("state")
    state = state_value if isinstance(state_value, Mapping) else {}
    finalized["active_candidate"] = isinstance(
        state.get("current_candidate"), Mapping)
    finalized["evidence_projection"] = _evidence_projection(
        finalized, triage_result)
    return finalized


def run_campaign(packet: Mapping[str, object], dispatch: Dispatch, *,
                 state_path: Path, observation_path: Path,
                 dispatchable_executor_refs: set[str] | None = None,
                 max_dispatches: int = 12,
                 resolve_handoff: ResolveHandoff | None = None) -> dict[str, object]:
    """Own triage and lifecycle progression up to one strategic boundary.

    Project code supplies current facts and execution callbacks.  It does not
    select a candidate, call ``step``/``transition``, or mutate campaign state.
    """
    if "state" in packet or "observation" in packet:
        raise ValueError(
            "campaign packet must not carry durable state or pending observation")
    state = _bind_runtime_policy(load_governance(state_path), packet)
    registry = packet.get("registry_snapshot")
    registry = registry if isinstance(registry, Mapping) else {}
    state = _resume_completed_mainline(state, registry)

    triage_value = packet.get("triage_input")
    if not isinstance(triage_value, Mapping):
        raise ValueError("campaign requires one triage_input mapping")
    triage_input = copy.deepcopy(dict(triage_value))
    runtime_value = triage_input.get("runtime_context")
    runtime = dict(runtime_value) if isinstance(runtime_value, Mapping) else {}
    runtime["optimization_phase"] = state.get("phase")
    triage_input["runtime_context"] = runtime
    active = bool(state.get("phase") != "idle" or state.get("current_candidate"))
    triage_result = _active_triage_projection(state) if active else triage(triage_input)
    intent = triage_result.get("automation_intent")
    intent = intent if isinstance(intent, Mapping) else None
    candidate_module = str((intent or {}).get("module") or "")
    eligible = triage_result.get("eligible_candidates")
    eligible = eligible if isinstance(eligible, list) else []
    candidate = next((copy.deepcopy(dict(item)) for item in eligible
                      if isinstance(item, Mapping)
                      and item.get("module") == candidate_module), {})
    campaign_prefix = str(packet.get("campaign_id_prefix") or "campaign")
    step_packet: dict[str, Any] = {
        "state": state,
        "automation_intent": copy.deepcopy(dict(intent)) if intent else None,
        "candidate": candidate,
        "mainline_ref": copy.deepcopy(packet.get("mainline_ref")),
        "campaign_id": (f"{campaign_prefix}-optimization-{candidate_module}"
                        if candidate_module else None),
        "policy": copy.deepcopy(dict(packet.get("policy") or {})),
    }
    observation = load_observation(observation_path)
    if isinstance(observation, Mapping):
        step_packet["observation"] = copy.deepcopy(dict(observation))

    result = _run_until_boundary(
        step_packet, dispatch,
        state_path=state_path, observation_path=observation_path,
        dispatchable_executor_refs=dispatchable_executor_refs,
        max_dispatches=max_dispatches)
    handoff = result.get("handoff")
    handoff = handoff if isinstance(handoff, Mapping) else {}
    if handoff.get("kind") != "external_verification_required" or resolve_handoff is None:
        return _finalize(result, triage_result)

    resolution = resolve_handoff(handoff)
    if not isinstance(resolution, Mapping):
        raise ValueError("handoff resolver must return one mapping")
    result["handoff_resolution"] = copy.deepcopy(dict(resolution))
    result["external_handoff"] = {
        **copy.deepcopy(dict(handoff)),
        "resolution": copy.deepcopy(dict(resolution)),
    }
    status = str(resolution.get("status") or "")
    if status == "hold":
        result["boundary_reason"] = "handoff_hold"
        return _finalize(result, triage_result)
    if status != "unavailable":
        return _finalize(result, triage_result)

    obligation = handoff.get("verification_obligation")
    obligation = obligation if isinstance(obligation, Mapping) else {}
    unavailable_observation = {
        "observation_id": f"{obligation.get('obligation_id')}:unavailable",
        "campaign_id": obligation.get("campaign_id"),
        "module": obligation.get("target_module"),
        "status": "external_verification_unavailable",
        "reason": (resolution.get("reason_codes") or
                   ["external_verification_unavailable"])[0],
        "evidence": copy.deepcopy(dict(resolution)),
    }
    follow_up = _run_until_boundary(
        {
            "state": result["state"],
            "automation_intent": None,
            "candidate": {},
            "mainline_ref": copy.deepcopy(packet.get("mainline_ref")),
            "campaign_id": None,
            "policy": copy.deepcopy(dict(packet.get("policy") or {})),
            "observation": unavailable_observation,
        },
        dispatch,
        state_path=state_path, observation_path=observation_path,
        dispatchable_executor_refs=dispatchable_executor_refs,
        max_dispatches=max_dispatches)
    follow_up["handoff_resolution"] = copy.deepcopy(dict(resolution))
    follow_up["campaign_trace"] = (
        list(result.get("campaign_trace") or [])
        + list(follow_up.get("campaign_trace") or []))
    return _finalize(follow_up, triage_result)
