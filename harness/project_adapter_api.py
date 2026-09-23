#!/usr/bin/env python3
"""Load one project-owned controller adapter behind a stable local API."""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any, Mapping


API_VERSION = 12
DEFAULT_SPEC = "task02_project_adapter:Task02ProjectAdapter"
_SPEC_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*")


class ProjectAdapterError(RuntimeError):
    """The configured project adapter is absent or API-incompatible."""


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _parameter_error(name: str, value: object, rule: Mapping[str, object]) -> str | None:
    if (any(isinstance(rule.get(bound), int) for bound in ("minimum", "maximum"))
            and isinstance(value, bool)):
        return f"parameter {name!r} must be an integer, not a boolean"
    permitted = rule.get("enum")
    if (isinstance(permitted, list) and permitted
            and all(isinstance(item, int) and not isinstance(item, bool) for item in permitted)
            and isinstance(value, bool)):
        return f"parameter {name!r} must be an integer, not a boolean"
    if isinstance(permitted, list) and value not in permitted:
        return f"parameter {name!r} is outside the allowed enum"
    minimum = rule.get("minimum")
    maximum = rule.get("maximum")
    if isinstance(minimum, int) and (not isinstance(value, int) or value < minimum):
        return f"parameter {name!r} is below the allowed minimum"
    if isinstance(maximum, int) and (not isinstance(value, int) or value > maximum):
        return f"parameter {name!r} is above the allowed maximum"
    return None


def _safe_scope_path(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip())
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        return None
    return path.parts


def _campaign_error(decision: Mapping[str, object], spec: Mapping[str, object]) -> str | None:
    fields = {
        "campaign_profile", "campaign_objective", "campaign_scope_paths",
        "measurement_budget_ceiling_seconds",
    }
    policy = _mapping(spec.get("campaign_policy"))
    if not policy:
        if any(field in decision for field in fields):
            return "non-Flash action must not supply campaign fields"
        return None
    if policy.get("selection") == "fixed":
        if any(field in decision for field in fields):
            return "fixed Flash action must not replace its campaign contract"
        return None
    if policy.get("selection") != "pro":
        return "Flash action contains a malformed campaign policy"
    profiles = _mapping(policy.get("profiles"))
    profile_name = decision.get("campaign_profile")
    if not isinstance(profile_name, str):
        return "campaign_profile must be a string selected from the immutable packet"
    profile = _mapping(profiles.get(profile_name))
    if not profile:
        return "campaign_profile is not allowed by the immutable packet"
    objective = decision.get("campaign_objective")
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 4000:
        return "campaign_objective must be a non-empty string no longer than 4000 characters"
    raw_paths = decision.get("campaign_scope_paths")
    if not isinstance(raw_paths, list):
        return "campaign_scope_paths must be a list"
    scopes = [_safe_scope_path(item) for item in raw_paths]
    if any(path is None for path in scopes) or len(set(scopes)) != len(scopes):
        return "campaign_scope_paths contains an unsafe or duplicate path"
    mutation = profile.get("mutation_policy")
    if mutation == "read_only" and scopes:
        return "read_only campaign_profile must not request writable paths"
    if mutation != "read_only" and not scopes:
        return "writable campaign_profile requires at least one scope path"
    roots = [_safe_scope_path(item) for item in profile.get("allowed_path_roots", [])]
    if any(root is None for root in roots):
        return "campaign profile contains an invalid allowed path root"
    for scope in scopes:
        assert scope is not None
        if not any(scope[:len(root)] == root for root in roots if root is not None):
            return "campaign_scope_paths exceeds the selected profile"
    ceiling = decision.get("measurement_budget_ceiling_seconds")
    permitted = profile.get("measurement_budget_ceiling_values")
    permitted = permitted if isinstance(permitted, list) else []
    if permitted and ceiling not in permitted:
        return "measurement_budget_ceiling_seconds is outside the selected profile"
    if not permitted and ceiling is not None:
        return "non-STA campaign_profile must not request a measurement budget"
    return None


def decision_error(decision: Mapping[str, object], packet: Mapping[str, object]) -> str | None:
    """Validate immutable identity, action parameters, and Pro-selected campaign bounds."""
    for controller_field in (
            "executor_ref", "execution_mode", "operation_id", "campaign_contract",
            "flash_directive", "change_mode", "measurement_mode", "affected_partitions"):
        if controller_field in decision:
            return f"decision must not supply controller-owned field {controller_field!r}"
    identity = _mapping(packet.get("identity"))
    for field in ("transaction_id", "generation", "packet_fingerprint"):
        if decision.get(field) != identity.get(field):
            return f"decision {field} does not match the immutable packet"
    action = str(decision.get("action") or "")
    spec = _mapping(_mapping(packet.get("allowed_actions")).get(action))
    if not spec:
        return f"action {action!r} is not allowed by the immutable packet"
    for field, raw_rule in _mapping(spec.get("parameter_schema")).items():
        if not isinstance(raw_rule, Mapping):
            return f"parameter schema for {field!r} is malformed"
        error = _parameter_error(str(field), decision.get(field), raw_rule)
        if error is not None:
            return error
    return _campaign_error(decision, spec)


def configured_adapter_spec(config_path: Path) -> str:
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ProjectAdapterError(f"project config is unreadable: {error}") from error
    in_controller = False
    spec = DEFAULT_SPEC
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        section = re.fullmatch(r"\[([^]]+)\]", line)
        if section:
            in_controller = section.group(1).strip() == "controller"
            continue
        if not in_controller:
            continue
        match = re.fullmatch(r'project_adapter\s*=\s*"([^"]+)"', line)
        if match:
            spec = match.group(1)
            break
    if not _SPEC_RE.fullmatch(spec):
        raise ProjectAdapterError("controller.project_adapter must be module:Class")
    return spec


def load_project_adapter(config_path: Path, project_root: Path) -> Any:
    spec = configured_adapter_spec(config_path)
    module_name, class_name = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        adapter_type = getattr(module, class_name)
        adapter = adapter_type(project_root=project_root)
    except (ImportError, AttributeError, TypeError) as error:
        raise ProjectAdapterError(f"could not load project adapter {spec}: {error}") from error
    if getattr(adapter, "api_version", None) != API_VERSION:
        raise ProjectAdapterError(
            f"project adapter {spec} has incompatible api_version="
            f"{getattr(adapter, 'api_version', None)!r}; expected {API_VERSION}")
    for method in (
            "prepare_cycle", "materialize_request", "materialize_project_execution",
            "normalize_project_execution", "normalize_measurement_outcome",
            "prepare_optimization_input", "materialize_optimization_execution",
            "materialize_external_optimization_plan", "normalize_optimization_plan",
            "resolve_optimization_handoff", "materialize_stage_evidence_execution",
            "normalize_stage_evidence_execution"):
        if not callable(getattr(adapter, method, None)):
            raise ProjectAdapterError(f"project adapter {spec} lacks callable {method}()")
    return adapter
