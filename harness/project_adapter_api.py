#!/usr/bin/env python3
"""Load one project-owned controller adapter behind a stable local API."""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any, Mapping


API_VERSION = 10
DEFAULT_SPEC = "task02_project_adapter:Task02ProjectAdapter"
_SPEC_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*")


class ProjectAdapterError(RuntimeError):
    """The configured project adapter is absent or API-incompatible."""


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _parameter_error(name: str, value: object, rule: Mapping[str, object]) -> str | None:
    permitted = rule.get("enum")
    if isinstance(permitted, list) and value not in permitted:
        return f"parameter {name!r} is outside the allowed enum"
    minimum = rule.get("minimum")
    maximum = rule.get("maximum")
    if isinstance(minimum, int) and (not isinstance(value, int) or value < minimum):
        return f"parameter {name!r} is below the allowed minimum"
    if isinstance(maximum, int) and (not isinstance(value, int) or value > maximum):
        return f"parameter {name!r} is above the allowed maximum"
    return None


def decision_error(decision: Mapping[str, object], packet: Mapping[str, object]) -> str | None:
    """Validate only immutable identity, action membership, and parameters."""
    for controller_field in ("executor_ref", "execution_mode", "operation_id"):
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
    return None


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
