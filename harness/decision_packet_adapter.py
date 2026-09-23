#!/usr/bin/env python3
"""Build one immutable Pro decision packet from deterministic Skill outputs.

This project adapter maps reusable Skill semantics to Task-02 action names.  It
does not read logs, call a model, execute an action, or change project state.
The supervisor gives the same packet to Pro and to its generic decision
validator, so no post-model policy layer may silently replace the chosen
action.
"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, Mapping, Optional

from flash_campaign_contract import (REQUIRED_ESCALATIONS, SCHEMA_VERSION as CAMPAIGN_SCHEMA_VERSION,
                                     validate_campaign_contract)
from project_adapter_api import decision_error


SCHEMA_VERSION = 1
READ_ONLY_ACTIONS = ("inspect_existing_sta_evidence", "inspect_run_evidence")
PROJECT_FALLBACK_SOURCES = {
    "project_batch_policy", "project_pending_verification",
    "evidence_access_policy", "controller_safety",
}
NO_FLASH_ACTIONS = {
    "expand_blackbox",
    "inspect_existing_sta_evidence", "inspect_run_evidence",
    "revise_task_card", "repair_harness", "record_reusable_knowledge",
    "stop_for_debug",
}
EXECUTOR_BY_ACTION = {
    "stop_for_debug": "controller.stop",
    "inspect_existing_sta_evidence": "evidence.inspect",
    "inspect_run_evidence": "evidence.inspect",
    "expand_blackbox": "project.expand_blackbox",
    "revise_task_card": "project.scoped_update",
    "repair_harness": "project.scoped_update",
    "record_reusable_knowledge": "project.scoped_update",
}
INTERNAL_SKILL_ACTIONS = {
    "optimize_rtl", "run_optimization_edit", "verify_optimization_candidate",
    "retry_measurement", "defer_optimization", "rollback_optimization",
}
MEASUREMENT_BUDGET_LEVELS = [180, 600, 1800, 3600, 7200]
TOOLCHAIN_PATHS = [
    "rtl",
    "harness/ppa_preflight.ys",
    "harness/ppa_measure.ys",
    "harness/ppa.toml",
    "scripts/remote_yosys_round.sh",
]
STANDARD_CAMPAIGN_PROFILES = {
    "rtl_local_targeted": {
        "repair_domain": "rtl_optimization", "mutation_policy": "local",
        "allowed_path_roots": ["rtl"], "verification_level": "sta_targeted",
        "measurement_partitions": [],
        "measurement_budget_ceiling_values": MEASUREMENT_BUDGET_LEVELS,
    },
    "rtl_architecture_full": {
        "repair_domain": "rtl_optimization", "mutation_policy": "architecture",
        "allowed_path_roots": ["rtl"], "verification_level": "sta_full",
        "measurement_partitions": [],
        "measurement_budget_ceiling_values": [1800, 3600, 7200],
    },
    "functional_local": {
        "repair_domain": "functional_repair", "mutation_policy": "local",
        "allowed_path_roots": ["rtl"], "verification_level": "functional",
        "measurement_partitions": [], "measurement_budget_ceiling_values": [],
    },
    "toolchain_scoped_full": {
        "repair_domain": "toolchain_repair", "mutation_policy": "architecture",
        "allowed_path_roots": TOOLCHAIN_PATHS, "verification_level": "sta_full",
        "measurement_partitions": [],
        "measurement_budget_ceiling_values": [1800, 3600, 7200],
    },
    "audit_read_only": {
        "repair_domain": "audit", "mutation_policy": "read_only",
        "allowed_path_roots": [], "verification_level": "none",
        "measurement_partitions": [], "measurement_budget_ceiling_values": [],
    },
    "measurement_read_only": {
        "repair_domain": "measurement", "mutation_policy": "read_only",
        "allowed_path_roots": [], "verification_level": "sta_targeted",
        "measurement_partitions": ["top_level_sta"],
        "measurement_budget_ceiling_values": MEASUREMENT_BUDGET_LEVELS,
    },
}


def _campaign_policy(execution_mode: str) -> Dict[str, object]:
    fixed_objectives = {
        "verify_pending_harness": "验证已实施且尚未测量的 harness 改动，不允许任何文件修改。",
        "diagnose_no_path": "执行一次只读 top_level_sta 结构诊断，不允许任何文件修改。",
        "retry_closure_measurement": "按 Closure Skill 授权重测同一 top_level_sta，不修改测量输入。",
        "increase_budget": "按 Closure Skill 授权重测同一 top_level_sta，不修改测量输入。",
        "execute_mainline_transaction": "验证 Registry 登记的黑盒展开主线，不允许额外修改。",
    }
    if execution_mode in fixed_objectives:
        return {
            "selection": "fixed",
            "profile": {
                "repair_domain": "measurement", "mutation_policy": "read_only",
                "allowed_paths": [], "verification_level": "sta_targeted",
                "measurement_partitions": ["top_level_sta"],
                "measurement_budget_ceiling_seconds": 7200,
            },
            "objective": fixed_objectives[execution_mode],
        }
    return {"selection": "pro", "profiles": STANDARD_CAMPAIGN_PROFILES}


class DecisionPacketError(ValueError):
    """The deterministic action sources are stale, conflicting, or malformed."""


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _validated_contracts(stage_record: object) -> Mapping[str, object]:
    record = _mapping(stage_record)
    review = _mapping(record.get("review"))
    if review.get("integrity_state") != "pass":
        return {}
    return _mapping(review.get("validated_execution_contracts"))


def _strategic_evidence(stage_record: object) -> Dict[str, object]:
    record = _mapping(stage_record)
    packet = _mapping(record.get("packet"))
    review = _mapping(record.get("review"))
    strategic = _mapping(review.get("strategic_view"))
    return {
        "stage": record.get("stage", packet.get("stage")),
        "run_id": record.get("run_id", packet.get("run_id")),
        "integrity_state": review.get("integrity_state"),
        "summary_state": review.get("summary_state"),
        "summary_source": strategic.get("summary_source"),
        "summary": strategic.get("summary", ""),
        "evidence_groups": strategic.get("evidence_groups", []),
    }


def _action(*, purpose: str, executor_ref: str, execution_mode: str,
            parameters: Optional[Mapping[str, object]] = None,
            source: str, campaign_policy: Optional[Mapping[str, object]] = None
            ) -> Dict[str, object]:
    result = {
        "purpose": purpose,
        "executor_ref": executor_ref,
        "execution_mode": execution_mode,
        "parameter_schema": dict(parameters or {}),
        "source": source,
    }
    if campaign_policy is not None:
        result["campaign_policy"] = dict(campaign_policy)
    return result


def _merge_action(actions: Dict[str, object], name: str,
                  spec: Mapping[str, object]) -> None:
    existing = actions.get(name)
    if existing is None:
        actions[name] = dict(spec)
        return
    old_schema = _mapping(existing).get("parameter_schema")
    new_schema = spec.get("parameter_schema")
    old_campaign = _mapping(existing).get("campaign_policy")
    new_campaign = spec.get("campaign_policy")
    if old_schema != new_schema or old_campaign != new_campaign:
        raise DecisionPacketError(f"conflicting execution contracts for action {name!r}")


def _contract_action_spec(action: str, contract: Mapping[str, object],
                          source: str) -> Dict[str, object]:
    schemas = _mapping(contract.get("action_parameters"))
    profiles = _mapping(contract.get("execution_profiles"))
    profile = _mapping(profiles.get(action))
    recommended = contract.get("recommended_actions")
    purpose = action
    if isinstance(recommended, list):
        for item in recommended:
            if isinstance(item, Mapping) and item.get("action") == action:
                purpose = str(item.get("reason") or action)
                break
    parameter_schema = dict(_mapping(schemas.get(action)))
    if action in NO_FLASH_ACTIONS:
        parameter_schema.setdefault("flash_attempts", {"enum": [0]})
    elif action == "increase_budget":
        parameter_schema.setdefault("flash_attempts", {"enum": [1]})
    elif action in {"continue", "optimize_rtl"}:
        parameter_schema.setdefault("flash_attempts", {"minimum": 1, "maximum": 6})
    executor_mode = str(profile.get("execution_mode") or profile.get("operation_kind") or action)
    executor_ref = EXECUTOR_BY_ACTION.get(action, "flash.run")
    return _action(
        purpose=purpose,
        executor_ref=executor_ref,
        execution_mode=executor_mode,
        parameters=parameter_schema,
        source=source,
        campaign_policy=(_campaign_policy(executor_mode)
                         if executor_ref == "flash.run" else None),
    )


def _apply_contract(actions: Dict[str, object], contract_entry: Mapping[str, object],
                    source: str) -> bool:
    contract = _mapping(contract_entry.get("contract"))
    if not contract:
        return False
    allowed = contract.get("allowed_actions")
    forbidden = contract.get("forbidden_actions")
    applies = contract.get("applies_to_actions")
    if not all(isinstance(value, list) for value in (allowed, forbidden, applies)):
        raise DecisionPacketError(f"malformed execution contract from {source}")
    allowed_names = [str(item) for item in allowed]
    forbidden_names = {str(item) for item in forbidden}
    global_contract = contract.get("enforcement") == "hard" and "*" in applies
    if global_contract:
        keep = {"stop_for_debug"}
        actions_copy = {name: spec for name, spec in actions.items() if name in keep}
        actions.clear()
        actions.update(actions_copy)
    else:
        # A scoped hard contract owns the actions named in applies_to_actions;
        # remove a generic project fallback before adding the source-authored
        # refinement. Two independent contracts that refine the same action
        # still conflict in _merge_action below.
        for name in (str(item) for item in applies if str(item) != "*"):
            existing = _mapping(actions.get(name))
            if existing.get("source") in PROJECT_FALLBACK_SOURCES:
                actions.pop(name, None)
    for name in forbidden_names:
        actions.pop(name, None)
    for name in allowed_names:
        if name in INTERNAL_SKILL_ACTIONS:
            continue
        _merge_action(actions, name, _contract_action_spec(name, contract, source))
    return global_contract


def materialize_executor_request(decision: Mapping[str, object], packet: Mapping[str, object],
                                 *, pending_verification: object = None) -> Dict[str, object]:
    """Resolve one validated action to executor text without changing the action."""
    action = str(decision.get("action") or "")
    actions = _mapping(packet.get("allowed_actions"))
    spec = _mapping(actions.get(action))
    executor_ref = str(spec.get("executor_ref") or "")
    execution_mode = str(spec.get("execution_mode") or action)
    result = dict(decision)
    identity = _mapping(packet.get("identity"))
    operation_seed = {
        "identity": dict(identity),
        "action": action,
        "parameters": {
            field: decision.get(field)
            for field in _mapping(spec.get("parameter_schema"))
        },
        "campaign": {
            field: decision.get(field)
            for field in ("campaign_profile", "campaign_objective",
                          "campaign_scope_paths", "measurement_budget_ceiling_seconds")
            if field in decision
        },
    }
    result["operation_id"] = hashlib.sha256(
        json.dumps(operation_seed, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    result["executor_ref"] = executor_ref
    result["execution_mode"] = execution_mode
    campaign_policy = _mapping(spec.get("campaign_policy"))
    if executor_ref == "flash.run":
        if campaign_policy.get("selection") == "fixed":
            profile = dict(_mapping(campaign_policy.get("profile")))
            objective = str(campaign_policy.get("objective") or execution_mode)
            if execution_mode == "verify_pending_harness":
                pending = _mapping(pending_verification)
                objective += (
                    f" paths={pending.get('paths', [])}; summary={pending.get('summary', '')};"
                    f" expected={pending.get('expected_phase', 'sta_timing_measured|sta_no_timing_path')}")
        else:
            profiles = _mapping(campaign_policy.get("profiles"))
            profile = dict(_mapping(profiles.get(str(decision.get("campaign_profile") or ""))))
            objective = str(decision.get("campaign_objective") or "").strip()
            profile["allowed_paths"] = list(decision.get("campaign_scope_paths") or [])
            profile["measurement_budget_ceiling_seconds"] = decision.get(
                "measurement_budget_ceiling_seconds")
        contract = {
            "schema_version": CAMPAIGN_SCHEMA_VERSION,
            "operation_id": result["operation_id"],
            "objective": objective,
            "repair_domain": profile.get("repair_domain"),
            "mutation_policy": profile.get("mutation_policy"),
            "allowed_paths": profile.get("allowed_paths", []),
            "verification_level": profile.get("verification_level"),
            "measurement_partitions": profile.get("measurement_partitions", []),
            "measurement_budget_ceiling_seconds": profile.get(
                "measurement_budget_ceiling_seconds"),
            "max_steps": decision.get("flash_attempts"),
            "escalation_conditions": list(REQUIRED_ESCALATIONS),
        }
        contract["contract_id"] = hashlib.sha256(json.dumps(
            contract, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        result["campaign_contract"] = validate_campaign_contract(contract)
    return result


def build_decision_packet(*, state: Mapping[str, object], stage_record: object,
                          evidence_fingerprint: str, stop_requested: bool,
                          functional_baseline_clean: bool,
                          expandable_blackboxes: list[str],
                          project_actions: Mapping[str, object] | None = None
                          ) -> Dict[str, object]:
    """Project current deterministic facts into one bounded Pro action space."""
    record = _mapping(stage_record)
    review = _mapping(record.get("review"))
    if review.get("integrity_state") != "pass":
        raise DecisionPacketError("stage evidence integrity must pass before building a decision packet")

    registry = _mapping(state.get("module_registry_hot_view"))
    pending = _mapping(registry.get("pending_transaction"))
    module = str(pending.get("module_id") or pending.get("module") or "")
    partition = str(pending.get("partition") or "top_level_sta")
    transaction_id = (pending.get("transaction_id")
                      or (f"blackbox_expansion:{partition}:{module}" if module else None)
                      or "no-active-transaction")
    generation = registry.get("generation", 0)
    stage_fingerprint = review.get("packet_fingerprint") or record.get("manifest_fingerprint")

    actions: Dict[str, object] = {
        "stop_for_debug": _action(
            purpose="在批次边界安全停止", executor_ref="controller.stop",
            execution_mode="stop",
            source="controller_safety"),
    }
    if stop_requested:
        evidence = _strategic_evidence(stage_record)
    else:
        closure = _mapping(state.get("latest_closure_governance_audit"))
        if closure.get("closure_governance_status") == "unavailable":
            raise DecisionPacketError("rtl-closure-governance audit is unavailable")
        for action in READ_ONLY_ACTIONS:
            actions[action] = _action(
                purpose="读取已有不可变 STA 证据" if action == READ_ONLY_ACTIONS[0]
                else "读取指定已完成 run 的有界 STA 路径证据",
                executor_ref="evidence.inspect",
                execution_mode=action,
                parameters=({"flash_attempts": {"enum": [0]} }
                            if action == READ_ONLY_ACTIONS[0] else {
                                "flash_attempts": {"enum": [0]},
                            }),
                source="evidence_access_policy")

        pending = _mapping(state.get("pending_verification"))
        if pending:
            actions["continue"] = _action(
                purpose="验证已经实施且尚未测量的 harness 改动",
                executor_ref="flash.run", execution_mode="verify_pending_harness",
                parameters={"flash_attempts": {"enum": [1]}},
                source="project_pending_verification",
                campaign_policy=_campaign_policy("verify_pending_harness"))
        elif closure.get("diagnostic_required") is True:
            actions["continue"] = _action(
                purpose="执行一次零修改 no-path 结构诊断",
                executor_ref="flash.run", execution_mode="diagnose_no_path",
                parameters={"flash_attempts": {"enum": [1]}},
                source="rtl-closure-governance",
                campaign_policy=_campaign_policy("diagnose_no_path"))
        elif closure.get("measurement_rerun_admissible") is True:
            retry_schema = {"flash_attempts": {"enum": [1]}}
            actions["increase_budget"] = _action(
                purpose="以严格更高且不超过上限的预算验证同一测量",
                executor_ref="flash.run", execution_mode="retry_closure_measurement",
                parameters=retry_schema, source="rtl-closure-governance",
                campaign_policy=_campaign_policy("retry_closure_measurement"))
        elif closure.get("structural_repair_required") is True:
            actions["repair_harness"] = _action(
                purpose="修复确定性的综合/STA harness 结构问题",
                executor_ref="project.scoped_update", execution_mode="repair_harness",
                parameters={"flash_attempts": {"enum": [0]}},
                source="rtl-closure-governance")
            actions["revise_task_card"] = _action(
                purpose="在测量契约本身错误时修订任务卡",
                executor_ref="project.scoped_update", execution_mode="revise_task_card",
                parameters={"flash_attempts": {"enum": [0]}},
                source="rtl-closure-governance")
        elif closure.get("measurement_resource_blocked") is not True:
            actions["continue"] = _action(
                purpose="执行下一批已声明的功能或测量工作",
                executor_ref="flash.run", execution_mode="standard_batch",
                parameters={"flash_attempts": {"minimum": 1, "maximum": 6}},
                source="project_batch_policy",
                campaign_policy=_campaign_policy("standard_batch"))

        contracts = _validated_contracts(stage_record)
        global_contract_seen = False
        for role in sorted(contracts):
            entry = _mapping(contracts.get(role))
            global_contract_seen = _apply_contract(actions, entry, str(role)) or global_contract_seen

        if not global_contract_seen:
            closure_ready_for_expansion = (
                closure.get("sta_link_result") == "ok"
                and closure.get("sta_timing_measured") is True
                and closure.get("sta_status") == "partial"
                and functional_baseline_clean
                and registry.get("hard_gate") == "pass"
            )
            if closure_ready_for_expansion and expandable_blackboxes:
                actions["expand_blackbox"] = _action(
                    purpose="展开一个登记簿确认的当前顶层黑盒",
                    executor_ref="project.expand_blackbox", execution_mode="expand_blackbox",
                    parameters={
                        "module": {"enum": list(expandable_blackboxes)},
                        "flash_attempts": {"enum": [0]},
                    },
                    source="rtl-module-evidence-registry")

        # A project compatibility matcher may expose an already-authorized
        # project action.  The packet builder remains the only owner of the
        # final action set and its identity fingerprint.
        for name, raw_spec in sorted((project_actions or {}).items()):
            if not isinstance(raw_spec, Mapping):
                raise DecisionPacketError(
                    f"malformed project action specification for {name!r}")
            required = {"purpose", "executor_ref", "execution_mode",
                        "parameter_schema", "source"}
            if not required.issubset(raw_spec):
                raise DecisionPacketError(
                    f"incomplete project action specification for {name!r}")
            actions[str(name)] = dict(raw_spec)

        evidence = _strategic_evidence(stage_record)

    identity_seed = {
        "transaction_id": transaction_id,
        "generation": generation,
        "stage_packet_fingerprint": stage_fingerprint,
        "evidence_fingerprint": evidence_fingerprint,
        "allowed_actions": actions,
    }
    packet_fingerprint = hashlib.sha256(
        json.dumps(identity_seed, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "identity": {
            "transaction_id": transaction_id,
            "generation": generation,
            "packet_fingerprint": packet_fingerprint,
        },
        "evidence": evidence,
        "allowed_actions": actions,
    }
