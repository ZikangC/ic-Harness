#!/usr/bin/env python3
"""Pure reconciliation logic for the RTL module evidence registry."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping


REPRESENTATIONS = {"blackbox", "rtl_body", "not_in_scope", "unknown"}
VERIFICATION_STATUSES = {
    "verified", "measurement_incomplete", "deterministic_failed", "infra_uncertain",
    "unclassified",
}
DESIGN_CONTEXT_STATUSES = {"current", "stale", "unavailable", "conflict"}


class RegistryError(ValueError):
    pass


def stable_id(prefix: str, value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _object(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RegistryError(f"{label} must be an object")
    return copy.deepcopy(value)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{label} must be a non-empty string")
    return value.strip()


def _verification_ref(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in (
        "evidence_id", "status", "run_id", "artifact", "sha256", "observed_at",
        "functional_passed", "preflight_passed", "sta_link_result",
        "sta_timing_measured", "sta_status", "measurement_scope", "ppa_exit",
    ) if item.get(key) is not None}


def _measurement_ref(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in (
        "measurement_id", "observed_at", "method", "scope", "source_run", "artifact",
        "artifact_sha256", "tool", "current_design_match",
        "area_um2", "delay_ps", "path_class", "instance_count", "library", "corner",
        "attribution_quality", "comparability_key", "fmax_valid", "design_fingerprint",
    ) if item.get(key) is not None}


def _event(base: Mapping[str, Any], kind: str, **fields: Any) -> dict[str, Any]:
    value = {
        "event_kind": kind,
        "project_id": base["project_id"],
        "session_id": base["session_id"],
        "cycle_id": base["cycle_id"],
        "phase": base["phase"],
        "observed_at": base["observed_at"],
        "reconciliation_id": base["reconciliation_id"],
        **fields,
    }
    value["event_id"] = stable_id("evt", value)
    return value


def _integration_state(record: Mapping[str, Any], partition: str) -> str:
    representation = ((record.get("observed") or {}).get("partitions") or {}).get(partition, {}).get("representation")
    verification = record.get("verification") or {}
    verified_once = verification.get("expansion_verified_once") is True
    latest = verification.get("latest_expansion_attempt") or {}
    latest_status = latest.get("status")
    if representation == "blackbox" and verified_once:
        return "temporarily_blackboxed"
    if representation == "rtl_body" and latest_status == "verified":
        return "integrated_verified"
    if representation == "rtl_body" and latest_status == "measurement_incomplete":
        return "measurement_incomplete"
    if representation == "rtl_body" and latest_status in {"infra_uncertain", "unclassified"}:
        return "pending_reverify"
    if verified_once:
        return "pending_reverify"
    if latest_status == "deterministic_failed":
        return "deterministic_failed"
    return "never_verified"


def _design_context(raw: Any, fingerprints: Mapping[str, str]) -> dict[str, Any]:
    """Validate a project-built design graph reference without constructing it."""
    if raw is None:
        return {
            "status": "unavailable", "hard_gate": "hold",
            "reason_codes": ["design_context_unavailable"],
        }
    value = _object(raw, "observation.design_context")
    declared_status = str(value.get("status") or "unavailable")
    if declared_status not in DESIGN_CONTEXT_STATUSES:
        raise RegistryError(f"invalid design context status: {declared_status}")
    source_fingerprints = _object(
        value.get("source_fingerprints"), "observation.design_context.source_fingerprints")
    mismatches = [
        key for key in ("config_fingerprint", "filelist_fingerprint", "rtl_fingerprint")
        if source_fingerprints.get(key) != fingerprints.get(key)
    ]
    graph_fingerprint = value.get("graph_fingerprint")
    reasons = [str(item) for item in value.get("reason_codes", [])
               if isinstance(item, str) and item]
    status = declared_status
    if mismatches:
        status = "stale"
        reasons.extend(f"source_fingerprint_mismatch:{key}" for key in mismatches)
    if not isinstance(graph_fingerprint, str) or not graph_fingerprint:
        status = "unavailable"
        reasons.append("design_graph_fingerprint_missing")
    return {
        "status": status,
        "hard_gate": "pass" if status == "current" else "hold",
        "graph_fingerprint": graph_fingerprint,
        "graph_ref": value.get("graph_ref"),
        "generation": value.get("generation"),
        "source": value.get("source"),
        "source_fingerprints": source_fingerprints,
        "coverage": copy.deepcopy(value.get("coverage")),
        "reason_codes": sorted(set(reasons)),
    }


def reconcile(packet: Mapping[str, Any]) -> dict[str, Any]:
    packet = _object(packet, "packet")
    required = ("project_id", "session_id", "phase", "observed_at", "reconciliation_id")
    base = {key: _text(packet.get(key), key) for key in required}
    cycle = packet.get("cycle_id")
    if not isinstance(cycle, int) or cycle < 0:
        raise RegistryError("cycle_id must be a non-negative integer")
    base["cycle_id"] = cycle
    observation = _object(packet.get("observation"), "observation")
    fingerprints = {
        key: _text(observation.get(key), f"observation.{key}")
        for key in ("config_fingerprint", "filelist_fingerprint", "rtl_fingerprint", "evidence_watermark")
    }
    design_context = _design_context(observation.get("design_context"), fingerprints)
    raw_modules = observation.get("modules")
    if not isinstance(raw_modules, list):
        raise RegistryError("observation.modules must be an array")
    previous = _object(packet.get("previous_registry"), "previous_registry")
    previous_modules = previous.get("modules") if isinstance(previous.get("modules"), dict) else {}
    conflicts: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    modules: dict[str, dict[str, Any]] = {}

    for raw in raw_modules:
        item = _object(raw, "module")
        module = _text(item.get("module_id"), "module.module_id")
        if module in modules:
            raise RegistryError(f"duplicate module_id: {module}")
        partitions = _object(item.get("partitions"), f"module {module}.partitions")
        normalized_partitions: dict[str, Any] = {}
        for partition, raw_partition in partitions.items():
            value = _object(raw_partition, f"module {module} partition {partition}")
            representation = value.get("representation", "unknown")
            if representation not in REPRESENTATIONS:
                raise RegistryError(f"invalid representation for {module}/{partition}: {representation}")
            normalized_partitions[str(partition)] = {**value, "representation": representation}
        observed = {
            "variant_key": str(item.get("variant_key") or "default"),
            "rtl_path": item.get("rtl_path"),
            "module_fingerprint": item.get("module_fingerprint"),
            "in_filelist": item.get("in_filelist") is True,
            "body_present": item.get("body_present") is True,
            "partitions": normalized_partitions,
            "relationships": _object(item.get("relationships"), f"module {module}.relationships"),
            "timing_context": _object(item.get("timing_context"), f"module {module}.timing_context"),
            "semantic_intent": _object(item.get("semantic_intent"), f"module {module}.semantic_intent"),
            "design_context_fingerprint": item.get("design_context_fingerprint"),
        }
        prior = copy.deepcopy(previous_modules.get(module, {})) if isinstance(previous_modules, dict) else {}
        record = {
            "module_id": module,
            "observed": observed,
            "verification": _object(prior.get("verification"), "previous verification"),
            "measurements": _object(prior.get("measurements"), "previous measurements"),
        }
        modules[module] = record
        old_observed = prior.get("observed") if isinstance(prior, dict) else None
        if old_observed is None:
            events.append(_event(base, "module_observed", module_id=module, after=observed))
        elif old_observed.get("partitions") != normalized_partitions:
            events.append(_event(base, "representation_changed", module_id=module,
                                 before=old_observed.get("partitions"), after=normalized_partitions))

    for raw in packet.get("verification_evidence") or []:
        item = _object(raw, "verification_evidence item")
        module = _text(item.get("module_id"), "verification_evidence.module_id")
        if module not in modules:
            conflicts.append({"kind": "verification_for_unknown_module", "module_id": module})
            continue
        status = item.get("status")
        if status not in VERIFICATION_STATUSES:
            conflicts.append({"kind": "invalid_verification_status", "module_id": module, "status": status})
            continue
        evidence_id = item.get("evidence_id") or stable_id("verify", item)
        item = {**item, "evidence_id": evidence_id}
        verification = modules[module]["verification"]
        known = verification.get("evidence_ids")
        evidence_ids = list(known) if isinstance(known, list) else []
        if evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
            events.append(_event(base, "verification_observed", module_id=module,
                                 after=_verification_ref(item)))
        verification["evidence_ids"] = evidence_ids
        latest = verification.get("latest_expansion_attempt")
        if not isinstance(latest, dict) or str(item.get("observed_at", "")) >= str(latest.get("observed_at", "")):
            verification["latest_expansion_attempt"] = _verification_ref(item)
        if status == "verified":
            verification["expansion_verified_once"] = True
            if not isinstance(verification.get("first_verified"), dict):
                verification["first_verified"] = _verification_ref(item)
            verification["latest_verified"] = _verification_ref(item)
        else:
            verification.setdefault("expansion_verified_once", False)

    measurements_by_module: dict[str, list[dict[str, Any]]] = {}
    for raw in packet.get("measurements") or []:
        item = _object(raw, "measurement item")
        module = _text(item.get("module_id"), "measurement.module_id")
        if module not in modules:
            conflicts.append({"kind": "measurement_for_unknown_module", "module_id": module})
            continue
        required_measurement = ("method", "scope", "source_run", "artifact", "observed_at")
        if any(not isinstance(item.get(key), str) or not item.get(key) for key in required_measurement):
            conflicts.append({"kind": "measurement_missing_provenance", "module_id": module})
            continue
        measurement_id = item.get("measurement_id") or stable_id("measure", item)
        normalized = {**item, "measurement_id": measurement_id}
        measurements_by_module.setdefault(module, []).append(normalized)

    new_measurements: list[dict[str, Any]] = []
    for module, items in measurements_by_module.items():
        record = modules[module]
        summary = record["measurements"]
        known_ids = list(summary.get("measurement_ids") or [])
        previous_count = int(summary.get("count", len(known_ids)))
        added_count = 0
        for item in sorted(items, key=lambda value: str(value.get("observed_at", ""))):
            if item["measurement_id"] not in known_ids:
                known_ids.append(item["measurement_id"])
                new_measurements.append(item)
                added_count += 1
            summary["latest"] = _measurement_ref(item)
        # Exact history lives in the append-only measurement ledger. The
        # current projection keeps only a bounded deduplication window.
        summary["measurement_ids"] = known_ids[-16:]
        summary["count"] = previous_count + added_count

    primary_partition = str(packet.get("primary_partition") or "top_level_sta")
    pending = packet.get("pending_transaction")
    if pending is not None and not isinstance(pending, dict):
        raise RegistryError("pending_transaction must be null or an object")
    if isinstance(pending, dict):
        target = pending.get("module_id") or pending.get("module")
        if target not in modules:
            conflicts.append({"kind": "pending_transaction_unknown_module", "module_id": target})
    for record in modules.values():
        state = _integration_state(record, primary_partition)
        partitions = record["observed"]["partitions"]
        blackboxed = sorted(name for name, value in partitions.items()
                            if value.get("representation") == "blackbox")
        blocked_target = ((pending or {}).get("module_id") or (pending or {}).get("module")) if pending else None
        record["derived"] = {
            "integration_state": state,
            "currently_blackboxed_partitions": blackboxed,
            "can_expand_partitions": (
                [] if not record["observed"].get("body_present")
                or blocked_target and blocked_target != record["module_id"] else blackboxed),
            "top_level_acceptance_pending": state in {
                "measurement_incomplete", "temporarily_blackboxed", "pending_reverify", "never_verified"
            },
        }

    current_blackboxes = sorted(
        module for module, record in modules.items()
        if ((record["observed"]["partitions"].get(primary_partition) or {}).get("representation") == "blackbox")
    )
    body_available_blackboxes = sorted(
        module for module in current_blackboxes
        if modules[module]["observed"].get("body_present") is True
    )
    expandable_blackboxes = sorted(
        module for module in body_available_blackboxes
        if not pending or ((pending.get("module_id") or pending.get("module")) == module)
    )
    pending_modules = sorted(
        module for module, record in modules.items()
        if record["derived"]["integration_state"] in {"measurement_incomplete", "pending_reverify"})
    mainline = packet.get("mainline")
    if pending_modules and not pending:
        conflicts.append({
            "kind": "verification_pending_without_transaction",
            "modules": pending_modules,
        })
    if (pending_modules and isinstance(mainline, dict)
            and mainline.get("status") == "select_next"):
        conflicts.append({
            "kind": "mainline_skips_pending_verification",
            "modules": pending_modules,
            "mainline_target": mainline.get("target"),
        })
    generation = int(previous.get("generation", 0))
    if previous.get("last_reconciliation", {}).get("reconciliation_id") != base["reconciliation_id"]:
        generation += 1
        events.append(_event(base, "cycle_reconciled", after={"fingerprints": fingerprints}))
    old_pending = previous.get("pending_transaction")
    if old_pending != pending:
        events.append(_event(base, "pending_transaction_changed", before=old_pending, after=pending))
    old_design_context = previous.get("design_context")
    if ((old_design_context or {}).get("graph_fingerprint")
            != design_context.get("graph_fingerprint")):
        events.append(_event(base, "design_context_changed",
                             before=copy.deepcopy(old_design_context),
                             after=copy.deepcopy(design_context)))
    status = "conflict" if conflicts else "current"
    hot_view = {
        "schema_version": 2,
        "registry_status": status,
        "hard_gate": "hold" if conflicts else "pass",
        "project_id": base["project_id"],
        "session_id": base["session_id"],
        "cycle_id": cycle,
        "phase": base["phase"],
        "generation": generation,
        "fingerprints": fingerprints,
        "evidence_watermark": fingerprints["evidence_watermark"],
        "primary_partition": primary_partition,
        "current_blackboxes": current_blackboxes,
        "body_available_blackboxes": body_available_blackboxes,
        "expandable_blackboxes": expandable_blackboxes,
        "verification_pending_modules": pending_modules,
        "pending_transaction": copy.deepcopy(pending),
        "mainline": copy.deepcopy(mainline),
        "design_context": copy.deepcopy(design_context),
        "module_summary": {
            module: {
                "representation": ((record["observed"]["partitions"].get(primary_partition) or {}).get("representation", "unknown")),
                "integration_state": record["derived"]["integration_state"],
                "expansion_verified_once": record["verification"].get("expansion_verified_once") is True,
                "latest_verification": (record["verification"].get("latest_expansion_attempt") or {}).get("status"),
                "latest_measurement": (record["measurements"].get("latest") or None),
            } for module, record in sorted(modules.items())
        },
        "conflicts": conflicts,
    }
    return {
        "schema_version": 3,
        "project_id": base["project_id"],
        "generation": generation,
        "registry_status": status,
        "conflicts": conflicts,
        "fingerprints": fingerprints,
        "evidence_watermark": fingerprints["evidence_watermark"],
        "last_reconciliation": base,
        "primary_partition": primary_partition,
        "modules": modules,
        "pending_transaction": copy.deepcopy(pending),
        "mainline": copy.deepcopy(mainline),
        "design_context": copy.deepcopy(design_context),
        "hot_view": hot_view,
        "emitted_events": events,
        "new_measurements": new_measurements,
    }


def validate_hot_view(registry: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    registry = _object(registry, "registry")
    expected = _object(expected, "expected")
    hot = registry.get("hot_view")
    if not isinstance(hot, dict):
        return {"hard_gate": "hold", "reason": "hot_view_missing"}
    mismatches = []
    for key in ("session_id", "cycle_id", "phase", "evidence_watermark"):
        if key in expected and hot.get(key) != expected.get(key):
            mismatches.append(key)
    expected_fingerprints = expected.get("fingerprints")
    if isinstance(expected_fingerprints, dict):
        actual = hot.get("fingerprints") or {}
        mismatches.extend(f"fingerprints.{key}" for key, value in expected_fingerprints.items()
                          if actual.get(key) != value)
    if hot.get("hard_gate") != "pass" or registry.get("registry_status") != "current":
        mismatches.append("registry_status")
    return {"hard_gate": "hold" if mismatches else "pass",
            "reason": "stale_or_conflicting_hot_view" if mismatches else "current",
            "mismatches": sorted(set(mismatches)), "hot_view": hot}


def can_expand(registry: Mapping[str, Any], module: str, partition: str) -> dict[str, Any]:
    registry = _object(registry, "registry")
    record = (registry.get("modules") or {}).get(module)
    if registry.get("registry_status") != "current":
        return {"allowed": False, "hard_gate": "hold", "reason": "registry_not_current"}
    if not isinstance(record, dict):
        return {"allowed": False, "hard_gate": "hold", "reason": "module_not_registered"}
    representation = (((record.get("observed") or {}).get("partitions") or {}).get(partition) or {}).get("representation")
    pending = registry.get("pending_transaction")
    pending_target = ((pending or {}).get("module_id") or (pending or {}).get("module")) if isinstance(pending, dict) else None
    if pending_target and pending_target != module:
        return {"allowed": False, "hard_gate": "hold", "reason": "conflicting_pending_transaction",
                "pending_module": pending_target}
    if (record.get("observed") or {}).get("body_present") is not True:
        return {"allowed": False, "hard_gate": "hold", "reason": "rtl_body_unavailable",
                "module": module, "partition": partition, "representation": representation}
    allowed = representation == "blackbox"
    return {"allowed": allowed, "hard_gate": "pass" if allowed else "hold",
            "reason": "current_blackbox" if allowed else "module_not_current_blackbox",
            "module": module, "partition": partition, "representation": representation}


def value_evidence(registry: Mapping[str, Any], module: str) -> dict[str, Any]:
    record = ((registry or {}).get("modules") or {}).get(module)
    if not isinstance(record, dict):
        return {"available": False, "reason": "module_not_registered", "module": module}
    return {"available": bool((record.get("measurements") or {}).get("latest")),
            "module": module, "integration_state": (record.get("derived") or {}).get("integration_state"),
            "expansion_verified_once": (record.get("verification") or {}).get("expansion_verified_once") is True,
            "measurement": (record.get("measurements") or {}).get("latest")}


def module_context(registry: Mapping[str, Any], module: str) -> dict[str, Any]:
    """Return a bounded current design-context slice for one registered module."""
    registry = _object(registry, "registry")
    context = registry.get("design_context")
    context = context if isinstance(context, dict) else {}
    record = (registry.get("modules") or {}).get(module)
    if registry.get("registry_status") != "current":
        return {"hard_gate": "hold", "reason": "registry_not_current", "module": module}
    if context.get("status") != "current" or context.get("hard_gate") != "pass":
        return {
            "hard_gate": "hold", "reason": "design_context_not_current", "module": module,
            "design_context": copy.deepcopy(context),
        }
    if not isinstance(record, dict):
        return {"hard_gate": "hold", "reason": "module_not_registered", "module": module}
    observed = record.get("observed") or {}
    if observed.get("design_context_fingerprint") != context.get("graph_fingerprint"):
        return {
            "hard_gate": "hold", "reason": "module_context_fingerprint_mismatch",
            "module": module, "design_context": copy.deepcopy(context),
        }
    return {
        "hard_gate": "pass", "reason": "current", "module": module,
        "graph_fingerprint": context.get("graph_fingerprint"),
        "graph_ref": context.get("graph_ref"),
        "generation": context.get("generation"),
        "relationships": copy.deepcopy(observed.get("relationships") or {}),
        "timing_context": copy.deepcopy(observed.get("timing_context") or {}),
        "semantic_intent": copy.deepcopy(observed.get("semantic_intent") or {}),
    }
