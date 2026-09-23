#!/usr/bin/env python3
"""Portable immutable contract and tactical work-package validation for Flash."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, Mapping, Sequence


SCHEMA_VERSION = 1
MAX_FLASH_STEPS = 6
MUTATION_POLICIES = {"read_only", "local", "architecture"}
VERIFICATION_LEVELS = {
    "none", "functional", "functional_preflight", "sta_targeted", "sta_full",
}
REQUIRED_ESCALATIONS = (
    "scope_expansion_required",
    "repair_domain_change_required",
    "architecture_change_required",
    "verification_scope_change_required",
    "contract_objective_invalidated",
)


class CampaignContractError(ValueError):
    """The immutable Pro campaign contract or a Flash work package is invalid."""


def _safe_relative_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip())
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        return None
    return path.as_posix().rstrip("/")


def path_is_within(path: str, scopes: Sequence[str]) -> bool:
    """Return whether one project-relative path is inside an authorized scope."""
    candidate = Path(path).parts
    for raw_scope in scopes:
        scope = Path(raw_scope).parts
        if candidate[:len(scope)] == scope:
            return True
    return False


def validate_campaign_contract(value: object) -> Dict[str, object]:
    """Validate and copy the only strategic contract consumed by Flash."""
    if not isinstance(value, Mapping):
        raise CampaignContractError("campaign contract must be an object")
    contract = dict(value)
    required = {
        "schema_version", "contract_id", "operation_id", "objective",
        "repair_domain", "mutation_policy", "allowed_paths",
        "verification_level", "measurement_partitions",
        "measurement_budget_ceiling_seconds", "max_steps",
        "escalation_conditions",
    }
    if set(contract) != required:
        missing = sorted(required - set(contract))
        extra = sorted(set(contract) - required)
        raise CampaignContractError(
            f"campaign contract fields mismatch; missing={missing}; extra={extra}")
    if contract.get("schema_version") != SCHEMA_VERSION:
        raise CampaignContractError("unsupported campaign contract schema_version")
    for field in ("contract_id", "operation_id"):
        if not isinstance(contract.get(field), str) or not re.fullmatch(
                r"[0-9a-f]{64}", str(contract.get(field))):
            raise CampaignContractError(f"campaign {field} must be a SHA-256 identity")
    objective = contract.get("objective")
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 4000:
        raise CampaignContractError("campaign objective must be 1..4000 characters")
    domain = contract.get("repair_domain")
    if not isinstance(domain, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", domain):
        raise CampaignContractError("campaign repair_domain is invalid")
    mutation = contract.get("mutation_policy")
    if mutation not in MUTATION_POLICIES:
        raise CampaignContractError("campaign mutation_policy is invalid")
    verification = contract.get("verification_level")
    if verification not in VERIFICATION_LEVELS:
        raise CampaignContractError("campaign verification_level is invalid")
    raw_paths = contract.get("allowed_paths")
    if not isinstance(raw_paths, list):
        raise CampaignContractError("campaign allowed_paths must be a list")
    paths = [_safe_relative_path(item) for item in raw_paths]
    if any(path is None for path in paths) or len(set(paths)) != len(paths):
        raise CampaignContractError("campaign allowed_paths contains an unsafe or duplicate path")
    if mutation == "read_only" and paths:
        raise CampaignContractError("read_only campaign must not authorize writable paths")
    if mutation != "read_only" and not paths:
        raise CampaignContractError("writable campaign requires at least one allowed path")
    contract["allowed_paths"] = [str(path) for path in paths]
    raw_partitions = contract.get("measurement_partitions")
    if (not isinstance(raw_partitions, list)
            or any(not isinstance(item, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", item) for item in raw_partitions)
            or len(set(raw_partitions)) != len(raw_partitions)):
        raise CampaignContractError("campaign measurement_partitions is invalid")
    if verification == "sta_full" and raw_partitions:
        raise CampaignContractError("sta_full campaign must not select partitions")
    if verification not in {"sta_targeted", "sta_full"} and raw_partitions:
        raise CampaignContractError("non-STA campaign must not select measurement partitions")
    ceiling = contract.get("measurement_budget_ceiling_seconds")
    if verification in {"sta_targeted", "sta_full"}:
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1:
            raise CampaignContractError("STA campaign requires a positive measurement ceiling")
    elif ceiling is not None:
        raise CampaignContractError("non-STA campaign must use a null measurement ceiling")
    steps = contract.get("max_steps")
    if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= MAX_FLASH_STEPS:
        raise CampaignContractError(f"campaign max_steps must be in 1..{MAX_FLASH_STEPS}")
    if contract.get("escalation_conditions") != list(REQUIRED_ESCALATIONS):
        raise CampaignContractError("campaign escalation_conditions are not canonical")
    identity_seed = {key: contract[key] for key in contract if key != "contract_id"}
    expected = hashlib.sha256(json.dumps(
        identity_seed, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    if contract["contract_id"] != expected:
        raise CampaignContractError("campaign contract_id does not match its immutable content")
    return contract


def load_campaign_contract(path: Path, project_root: Path) -> Dict[str, object]:
    """Load one contract from inside the project without accepting another input path."""
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError as error:
        raise CampaignContractError("campaign contract must be inside the project") from error
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CampaignContractError(f"campaign contract is unreadable: {error}") from error
    return validate_campaign_contract(value)


def parse_work_package(text: str, contract: Mapping[str, object]) -> Dict[str, object]:
    """Parse and enforce one tactical package proposed inside an immutable campaign."""
    matches = re.findall(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    package: object = None
    for raw in reversed(matches):
        try:
            candidate = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, Mapping) and candidate.get("kind") == "flash_work_package":
            package = candidate
            break
    if not isinstance(package, Mapping):
        raise CampaignContractError("planner produced no flash_work_package JSON")
    required = {
        "kind", "disposition", "escalation_reason", "access",
        "verification_request", "objective", "hypothesis", "planned_paths",
        "expected_evidence",
    }
    if set(package) != required:
        raise CampaignContractError("flash work package fields do not match the schema")
    for field in ("objective", "hypothesis", "expected_evidence"):
        value = package.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise CampaignContractError(f"work package {field} must be 1..2000 characters")
    disposition = package.get("disposition")
    escalation = package.get("escalation_reason")
    if disposition not in {"execute", "escalate"}:
        raise CampaignContractError("work package disposition must be execute or escalate")
    if disposition == "execute" and escalation != "none":
        raise CampaignContractError("executable work package must use escalation_reason=none")
    if disposition == "escalate" and escalation not in REQUIRED_ESCALATIONS:
        raise CampaignContractError("work package escalation_reason is not authorized")
    access = package.get("access")
    if access not in {"read_only", "edit"}:
        raise CampaignContractError("work package access must be read_only or edit")
    verification_request = package.get("verification_request")
    if verification_request not in {"none", "contract"}:
        raise CampaignContractError(
            "work package verification_request must be none or contract")
    if access == "edit" and contract.get("mutation_policy") == "read_only":
        raise CampaignContractError("read_only campaign cannot contain an edit work package")
    if access == "edit" and verification_request != "contract":
        raise CampaignContractError("edit work package must request contract verification")
    if disposition == "escalate" and (access != "read_only" or verification_request != "none"):
        raise CampaignContractError("escalation work package must be read_only with no verification")
    raw_paths = package.get("planned_paths")
    if not isinstance(raw_paths, list):
        raise CampaignContractError("work package planned_paths must be a list")
    paths = [_safe_relative_path(item) for item in raw_paths]
    if any(path is None for path in paths) or len(set(paths)) != len(paths):
        raise CampaignContractError("work package planned_paths contains an unsafe or duplicate path")
    normalized_paths = [str(path) for path in paths]
    mutation = contract.get("mutation_policy")
    allowed = contract.get("allowed_paths")
    allowed = [str(item) for item in allowed] if isinstance(allowed, list) else []
    if (mutation == "read_only" or access == "read_only") and normalized_paths:
        raise CampaignContractError("read_only work package requested file edits")
    if mutation == "local" and len(normalized_paths) > 1:
        raise CampaignContractError("local work package requested more than one file")
    if any(not path_is_within(path, allowed) for path in normalized_paths):
        raise CampaignContractError("work package requested a path outside the campaign scope")
    if disposition == "escalate" and normalized_paths:
        raise CampaignContractError("escalation work package must not request edits")
    result = dict(package)
    result["planned_paths"] = normalized_paths
    return result


def actual_changes_error(changed_paths: Sequence[str], package: Mapping[str, object],
                         contract: Mapping[str, object]) -> str | None:
    """Check the real diff against both the tactical package and strategic contract."""
    actual = [str(path) for path in changed_paths]
    planned = package.get("planned_paths")
    planned = [str(path) for path in planned] if isinstance(planned, list) else []
    if contract.get("mutation_policy") == "read_only" and actual:
        return "read_only campaign modified managed inputs"
    if contract.get("mutation_policy") == "local" and len(actual) > 1:
        return "local campaign modified more than one managed file"
    if any(path not in planned for path in actual):
        return "executor modified a path not declared by the accepted work package"
    allowed = contract.get("allowed_paths")
    allowed = [str(item) for item in allowed] if isinstance(allowed, list) else []
    if any(not path_is_within(path, allowed) for path in actual):
        return "executor modified a path outside the immutable campaign scope"
    return None
