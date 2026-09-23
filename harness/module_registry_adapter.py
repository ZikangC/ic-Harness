#!/usr/bin/env python3
"""Task-02 adapter for the generic RTL module evidence registry.

This module reads current project configuration plus immutable structured run
results. It never treats progress prose as module state.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from design_context_adapter import ensure_design_context
from rtl_skill_bridge import (module_registry_hot_view, module_registry_reconcile,
                              rtl_instantiation_counts)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "status/task02_module_registry.json"
EVENTS_PATH = ROOT / "status/task02_module_events.jsonl"
MEASUREMENTS_PATH = ROOT / "status/task02_module_measurements.jsonl"
PRIMARY_PARTITION = "top_level_sta"


def _timestamp(path: Optional[Path] = None) -> str:
    if path is not None:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).isoformat()
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _aggregate_fingerprint(paths: Iterable[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        if not path.is_file():
            continue
        try:
            label = path.relative_to(root).as_posix()
        except ValueError:
            label = path.as_posix()
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    # v1/v2 snapshots are accepted only as reconciliation inputs.  The Skill
    # immediately rewrites them as v3 without the removed Optimization mirror.
    return value if isinstance(value, dict) and value.get("schema_version") in (1, 2, 3) else {}


def _filelist_paths(project_root: Path) -> tuple[Path, list[Path]]:
    filelist = project_root / "rtl/filelist.f"
    paths: list[Path] = []
    for raw in filelist.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("+", "-")):
            continue
        path = Path(line)
        paths.append(path if path.is_absolute() else project_root / path)
    return filelist, paths


def discover_modules(project_root: Path) -> tuple[list[dict[str, Any]], str, str]:
    filelist, rtl_paths = _filelist_paths(project_root)
    declaration = re.compile(r"(?m)^\s*module\s+(?:automatic\s+)?([A-Za-z_][A-Za-z0-9_$]*)\b")
    modules: list[dict[str, Any]] = []
    for path in rtl_paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in declaration.findall(text):
            modules.append({
                "module_id": name,
                "variant_key": "default",
                "rtl_path": path.relative_to(project_root).as_posix(),
                "module_fingerprint": _sha_file(path),
                "in_filelist": True,
                "body_present": True,
            })
    by_name: dict[str, dict[str, Any]] = {}
    for item in modules:
        # Duplicate declarations are surfaced conservatively as no unique body.
        if item["module_id"] in by_name:
            by_name[item["module_id"]]["body_present"] = False
            by_name[item["module_id"]]["duplicate_declaration"] = True
        else:
            by_name[item["module_id"]] = item
    filelist_fingerprint = _aggregate_fingerprint([filelist, *rtl_paths], project_root)
    rtl_fingerprint = _aggregate_fingerprint(rtl_paths, project_root)
    return list(by_name.values()), filelist_fingerprint, rtl_fingerprint


def read_partition_config(project_root: Path) -> tuple[dict[str, Any], str]:
    path = project_root / "harness/ppa.toml"
    partitions: dict[str, Any] = {}
    current: Optional[dict[str, str]] = None
    assignment = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$')
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "[[partitions]]":
            if current and current.get("name"):
                name = current["name"]
                partitions[name] = {
                    "top": current.get("top", ""),
                    "blackboxes": [value for value in current.get("blackbox", "").split() if value],
                }
            current = {}
            continue
        if current is None:
            continue
        match = assignment.match(line)
        if match:
            current[match.group(1)] = match.group(2).replace(r'\"', '"').replace(r'\\', '\\')
    if current and current.get("name"):
        name = current["name"]
        partitions[name] = {
            "top": current.get("top", ""),
            "blackboxes": [value for value in current.get("blackbox", "").split() if value],
        }
    return partitions, _sha_file(path)


def current_observation(project_root: Path) -> dict[str, Any]:
    modules, filelist_fingerprint, rtl_fingerprint = discover_modules(project_root)
    partitions, config_fingerprint = read_partition_config(project_root)
    design_context = ensure_design_context(project_root)
    graph_modules = (design_context.get("modules")
                     if isinstance(design_context.get("modules"), dict) else {})
    known = {item["module_id"] for item in modules}
    configured_modules = {
        module for config in partitions.values()
        for module in [config.get("top", ""), *config.get("blackboxes", [])]
        if module
    }
    for name in sorted(configured_modules - known):
        modules.append({
            "module_id": name,
            "variant_key": "default",
            "rtl_path": None,
            "module_fingerprint": None,
            "in_filelist": False,
            "body_present": False,
            "discovery_source": "partition_config_only",
        })
    for module in modules:
        name = module["module_id"]
        graph_record = graph_modules.get(name)
        graph_record = graph_record if isinstance(graph_record, dict) else {}
        module["relationships"] = dict(graph_record.get("relationships") or {})
        module["timing_context"] = dict(graph_record.get("timing_context") or {})
        module["semantic_intent"] = dict(graph_record.get("semantic_intent") or {})
        module["design_context_fingerprint"] = (
            design_context.get("graph_fingerprint") if graph_record else None)
        module["partitions"] = {}
        for partition, config in partitions.items():
            if name in config["blackboxes"]:
                representation = "blackbox"
            elif name == config["top"]:
                representation = "rtl_body" if module["body_present"] else "unknown"
            elif partition == PRIMARY_PARTITION and module["body_present"]:
                representation = "rtl_body"
            else:
                representation = "not_in_scope"
            module["partitions"][partition] = {
                "representation": representation,
                "source": "harness/ppa.toml+rtl/filelist.f",
            }
    return {
        "config_fingerprint": config_fingerprint,
        "filelist_fingerprint": filelist_fingerprint,
        "rtl_fingerprint": rtl_fingerprint,
        "design_context": {
            key: design_context.get(key) for key in (
                "status", "hard_gate", "graph_fingerprint", "graph_ref",
                "generation", "source", "source_fingerprints", "coverage",
                "reason_codes",
            ) if design_context.get(key) is not None
        },
        "modules": modules,
    }


def result_paths(project_root: Path) -> list[Path]:
    paths = list((project_root / "runs").glob("agent-02_yosys_ppa--*/result.json"))
    return sorted(paths, key=lambda path: (path.stat().st_mtime_ns, path.as_posix()))


def _functional_passed(result: Mapping[str, Any]) -> bool:
    return (result.get("functional_quick_exit", result.get("functional_exit")) == 0
            and result.get("functional_medium_exit") in (None, 0)
            and result.get("mandatory_full_exit") == 0)


def _verification_status(result: Mapping[str, Any]) -> str:
    """Project normalized status projection; never reclassify raw exit codes."""
    status = str(result.get("measurement_status") or "")
    if status == "complete":
        return "verified"
    if status == "measurement_incomplete":
        return "measurement_incomplete"
    if status in {"infra_failed", "infra_uncertain"}:
        return "infra_uncertain"
    if status in {
            "functional_failed", "preflight_failed", "execution_failed",
            "measurement_failed"}:
        return "deterministic_failed"
    return "unclassified"


def collect_verification_evidence(project_root: Path) -> tuple[list[dict[str, Any]], str]:
    evidence: list[dict[str, Any]] = []
    watermark_items: list[dict[str, str]] = []
    for path in result_paths(project_root):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        run_id = path.parent.name
        artifact_hash = _sha_file(path)
        watermark_items.append({"run_id": run_id, "sha256": artifact_hash})
        module = result.get("expanded_module")
        if (not isinstance(module, str) or not module):
            scope_gate = result.get("sta_scope_gate")
            scope_gate = scope_gate if isinstance(scope_gate, dict) else {}
            if (result.get("optimization_status") == "accepted"
                    and scope_gate.get("scope") in {"top_level_partial", "top_level_complete"}):
                module = result.get("optimization_integration_target")
        if not isinstance(module, str) or not module:
            continue
        audit = result.get("sta_closure_audit") if isinstance(result.get("sta_closure_audit"), dict) else {}
        observed_at = str(result.get("finished_at") or result.get("timestamp") or _timestamp(path))
        evidence.append({
            "evidence_id": f"expansion:{run_id}:{artifact_hash[:16]}",
            "module_id": module,
            "kind": ("optimization_integration_acceptance"
                     if result.get("optimization_status") == "accepted"
                     else "expansion_verification"),
            "status": _verification_status(result),
            "run_id": run_id,
            "artifact": path.relative_to(project_root).as_posix(),
            "sha256": artifact_hash,
            "observed_at": observed_at,
            "functional_passed": _functional_passed(result),
            "preflight_passed": result.get("preflight_exit") == 0,
            "sta_link_result": audit.get("sta_link_result"),
            "sta_timing_measured": audit.get("sta_timing_measured"),
            "sta_status": audit.get("sta_status"),
            "measurement_scope": result.get("measurement_scope") or audit.get("measurement_scope"),
            "ppa_exit": result.get("ppa_exit"),
        })
    return evidence, _sha_bytes(json.dumps(watermark_items, sort_keys=True).encode("utf-8"))


def collect_measurements(project_root: Path, modules: set[str]) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"^partition=(\S+)\s+module=\\?([^\s]+).*?\bArea\s*=\s*([0-9.]+).*?\bDelay\s*=\s*([0-9.]+)\s+ps\b")
    classification = re.compile(
        r"^partition=(\S+)\s+module=\\?([^\s]+).*?\bdelay=([0-9.]+)\s+seq_pct=([0-9.]+)\s+class=(\S+)")
    records: dict[tuple[str, str], dict[str, Any]] = {}
    feedback_paths = sorted((project_root / "runs").glob("02_yosys_ppa--*/feedback.txt"),
                            key=lambda path: (path.stat().st_mtime_ns, path.as_posix()))
    for path in feedback_paths:
        source_run = path.parent.name
        artifact_hash = _sha_file(path)
        observed_at = _timestamp(path)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = pattern.match(line)
            if match:
                partition, module = match.group(1), match.group(2).lstrip("\\")
                if module not in modules and partition in modules:
                    module = partition
                if module not in modules:
                    continue
                key = (source_run, module)
                records[key] = {
                    "measurement_id": f"ppa:{source_run}:{module}:{artifact_hash[:16]}",
                    "module_id": module, "observed_at": observed_at,
                    "method": "yosys_abc", "scope": "partition_proxy",
                    "source_run": source_run,
                    "artifact": path.relative_to(project_root).as_posix(),
                    "artifact_sha256": artifact_hash,
                    "tool": "Yosys/ABC", "library": "Nangate45", "corner": "typical",
                    "area_um2": float(match.group(3)), "delay_ps": float(match.group(4)),
                    "attribution_quality": "direct_partition" if partition == module else "reported_module",
                    "fmax_valid": False,
                    # Historical feedback predates this registry and does not
                    # carry the exact RTL digest used by its run. Do not claim
                    # it matches the current tree merely because it was read now.
                    "design_fingerprint": "unknown",
                    "current_design_match": "unknown",
                    "comparability_key": f"yosys_abc:Nangate45:typical:{partition}:partition_proxy",
                }
                continue
            match = classification.match(line)
            if match:
                partition, module = match.group(1), match.group(2).lstrip("\\")
                if module not in modules and partition in modules:
                    module = partition
                item = records.get((source_run, module))
                if item is not None:
                    item.update({"classified_delay_ps": float(match.group(3)),
                                 "sequential_percent": float(match.group(4)),
                                 "path_class": match.group(5)})
    latest_by_module: dict[str, dict[str, Any]] = {}
    for item in records.values():
        module = str(item["module_id"])
        previous = latest_by_module.get(module)
        if previous is None or str(item.get("observed_at", "")) >= str(previous.get("observed_at", "")):
            latest_by_module[module] = item
    return list(latest_by_module.values())


def registry_mainline(registry: Mapping[str, Any]) -> dict[str, Any]:
    value = registry.get("mainline")
    if isinstance(value, dict):
        return dict(value)
    hot = registry.get("hot_view") if isinstance(registry.get("hot_view"), dict) else {}
    blackboxes = hot.get("current_blackboxes") if isinstance(hot.get("current_blackboxes"), list) else []
    return {"kind": "blackbox_expansion", "target": blackboxes[0] if blackboxes else None,
            "status": "select_next" if blackboxes else "all_configured_blackboxes_expanded"}


def registry_mainline_ref(registry: Mapping[str, Any]) -> dict[str, Any]:
    """Return an immutable reference to the Registry-owned pending mainline."""
    pending = registry.get("pending_transaction")
    pending = pending if isinstance(pending, Mapping) else {}
    module = str(pending.get("module_id") or pending.get("module") or "").strip()
    if not module:
        return {}
    partition = str(pending.get("partition") or PRIMARY_PARTITION)
    kind = str(pending.get("kind") or "blackbox_expansion")
    transaction_id = str(
        pending.get("transaction_id")
        or f"{kind}:{partition}:{module}")
    identity = {
        "transaction_id": transaction_id, "kind": kind,
        "module_id": module, "partition": partition,
    }
    return {
        **identity,
        "identity_fingerprint": _sha_bytes(json.dumps(
            identity, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")),
        "registry_generation": registry.get("generation"),
    }


def optimization_packet_from_registry(registry: Mapping[str, Any], project_root: Path = ROOT,
                                      mainline: Optional[Mapping[str, Any]] = None,
                                      minimum_value_score: float = 0.5) -> dict[str, Any]:
    """Build triage facts from the reconciled projection, not a new history scan."""
    raw_modules = registry.get("modules") if isinstance(registry.get("modules"), dict) else {}
    verified_modules = sorted(
        module for module, record in raw_modules.items()
        if isinstance(record, dict)
        and (record.get("verification") or {}).get("expansion_verified_once") is True)
    replication = rtl_instantiation_counts(project_root / "rtl", verified_modules)
    areas = []
    for module in verified_modules:
        latest = ((raw_modules[module].get("measurements") or {}).get("latest") or {})
        if isinstance(latest.get("area_um2"), (int, float)):
            areas.append(float(latest["area_um2"]))
    max_area = max(areas, default=0.0)
    max_replication = max(replication.values(), default=0)
    candidates = []
    for module in verified_modules:
        record = raw_modules[module]
        verification = record.get("verification") or {}
        derived = record.get("derived") or {}
        latest_verification = verification.get("latest_expansion_attempt") or {}
        measurement = (record.get("measurements") or {}).get("latest") or {}
        state = str(derived.get("integration_state", "never_verified"))
        area = measurement.get("area_um2")
        delay = measurement.get("classified_delay_ps", measurement.get("delay_ps"))
        count = replication.get(module, 0)
        latest_verified = latest_verification.get("status") == "verified"
        representation = (
            (((record.get("observed") or {}).get("partitions") or {}).get(PRIMARY_PARTITION) or {})
            .get("representation"))
        attributable = (
            isinstance(area, (int, float))
            and isinstance(delay, (int, float))
            and float(delay) < 1_000_000.0
            and measurement.get("path_class") != "non_physical_artifact"
            and measurement.get("attribution_quality") in {
                "direct_partition", "reported_module"}
            and measurement.get("current_design_match") is not False
        )
        evidence_reasons: list[str] = []
        if not measurement:
            evidence_reasons.append("module_measurement_missing")
        elif measurement.get("path_class") == "non_physical_artifact":
            evidence_reasons.append("non_physical_measurement")
        elif measurement.get("attribution_quality") not in {
                "direct_partition", "reported_module"}:
            evidence_reasons.append("measurement_not_attributable")
        elif measurement.get("current_design_match") is False:
            evidence_reasons.append("measurement_design_mismatch")
        elif not isinstance(area, (int, float)) or not isinstance(delay, (int, float)):
            evidence_reasons.append("quantitative_measurement_missing")
        candidate: dict[str, Any] = {
            "module": module,
            "expansion_verified_once": True,
            "expanded_verified": latest_verified,
            "representation": representation,
            "currently_blackboxed": representation == "blackbox",
            "integration_state": state,
            "local_optimization_allowed": state != "deterministic_failed",
            "evidence_eligible": attributable,
            "evidence_reason_codes": evidence_reasons,
            "top_level_acceptance_pending": derived.get("top_level_acceptance_pending") is True,
            "latest_expansion_exit": latest_verification.get("ppa_exit"),
            "latest_expansion_measurement_incomplete": latest_verification.get("status") == "measurement_incomplete",
            "evidence_confidence": (
                1.0 if area is not None and delay is not None
                and measurement.get("current_design_match") is True
                else 0.65 if area is not None and delay is not None else 0.55),
            "change_risk": 0.5,
            "verification_cost": 0.7 if state == "measurement_incomplete" else 0.5,
            "prior_failure_weight": 0.0,
            "measured_area_um2": area,
            "measured_delay_ps": delay,
            "measured_path_class": measurement.get("path_class"),
            "source_instance_count": count,
            "value_evidence": measurement,
        }
        if isinstance(area, (int, float)) and max_area > 0:
            candidate["area_value"] = min(1.0, float(area) / max_area)
        if isinstance(delay, (int, float)) and float(delay) < 1_000_000.0:
            candidate["timing_value"] = min(1.0, float(delay) / 10_000.0)
        if count > 0 and max_replication > 0:
            candidate["replication_value"] = min(1.0, count / max_replication)
        candidates.append(candidate)
    return {"mainline": dict(mainline or registry_mainline(registry)), "candidates": candidates,
            "minimum_value_score": float(minimum_value_score), "target_period_ps": 10_000.0,
            "value_policy": "module_registry_measurement_v1"}


def _pending_transaction(registry: Mapping[str, Any],
                         evidence: list[dict[str, Any]],
                         requested: object = None) -> Optional[dict[str, Any]]:
    # Recover unfinished work from immutable structured result
    # evidence, never from progress prose or controller-local pending fields.
    latest_by_module: dict[str, dict[str, Any]] = {}
    for item in evidence:
        latest_by_module[item["module_id"]] = item
    if isinstance(requested, Mapping) and requested.get("operation") == "begin":
        module = str(requested.get("module_id") or "")
        current = (((registry.get("modules") or {}).get(module) or {}).get("observed") or {}).get(
            "partitions", {})
        representation = (current.get(PRIMARY_PARTITION) or {}).get("representation")
        if module and representation == "rtl_body":
            partition = str(requested.get("partition") or PRIMARY_PARTITION)
            return {
                "transaction_id": str(requested.get("transaction_id")
                                      or f"blackbox_expansion:{partition}:{module}"),
                "kind": "blackbox_expansion", "module_id": module,
                "partition": partition, "phase": "verification_pending",
                "owner": "rtl-module-evidence-registry",
                "source_evidence_id": str(requested.get("operation_id") or "controller_request"),
            }
    for item in sorted(latest_by_module.values(), key=lambda value: str(value.get("observed_at", "")),
                       reverse=True):
        module = str(item["module_id"])
        current = (((registry.get("modules") or {}).get(module) or {}).get("observed") or {}).get("partitions", {})
        representation = (current.get(PRIMARY_PARTITION) or {}).get("representation")
        if item.get("status") in {
                "measurement_incomplete", "infra_uncertain", "unclassified"
        } and representation == "rtl_body":
            phase = {
                "measurement_incomplete": "measurement_pending",
                "infra_uncertain": "infra_reverify_pending",
                "unclassified": "evidence_pending",
            }[str(item.get("status"))]
            return {"transaction_id": f"blackbox_expansion:{PRIMARY_PARTITION}:{module}",
                    "kind": "blackbox_expansion", "module_id": module,
                    "partition": PRIMARY_PARTITION,
                    "phase": phase,
                    "owner": "rtl-module-evidence-registry",
                    "source_evidence_id": item.get("evidence_id")}
    # Persisted Registry is structured state
    # and may own unfinished work for which no newer structured result exists.
    # Validate its subject against the current observation. Progress prose is
    # deliberately never considered here.
    previous_pending = registry.get("previous_pending_transaction")
    if isinstance(previous_pending, Mapping):
        module = str(previous_pending.get("module_id") or "")
        latest = latest_by_module.get(module)
        if isinstance(latest, Mapping) and latest.get("status") in {
                "verified", "deterministic_failed"}:
            return None
        current = (((registry.get("modules") or {}).get(module) or {}).get("observed") or {}).get("partitions", {})
        representation = (current.get(PRIMARY_PARTITION) or {}).get("representation")
        if module and representation == "rtl_body":
            transaction_id = str(previous_pending.get("transaction_id") or
                                 f"blackbox_expansion:{PRIMARY_PARTITION}:{module}")
            return {
                "transaction_id": transaction_id,
                "kind": str(previous_pending.get("kind") or "blackbox_expansion"),
                "module_id": module,
                "partition": str(previous_pending.get("partition") or PRIMARY_PARTITION),
                "phase": str(previous_pending.get("phase") or "verification_pending"),
                "owner": "rtl-module-evidence-registry",
                "source_evidence_id": f"registry-generation:{registry.get('previous_generation')}",
            }
    return None


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _append_unique(path: Path, records: Iterable[Mapping[str, Any]], id_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    known: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get(id_key), str):
                known.add(item[id_key])
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            identifier = record.get(id_key)
            if not isinstance(identifier, str) or identifier in known:
                continue
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            known.add(identifier)


def reconcile_project_registry(*, skill_root: Path, session_id: str, cycle_id: int,
                               phase: str, controller_state: Mapping[str, Any],
                               project_root: Path = ROOT,
                               registry_path: Path = REGISTRY_PATH,
                               events_path: Path = EVENTS_PATH,
                               measurements_path: Path = MEASUREMENTS_PATH) -> dict[str, Any]:
    observation = current_observation(project_root)
    verification, watermark = collect_verification_evidence(project_root)
    observation["evidence_watermark"] = watermark
    previous = load_registry(registry_path)
    # Use the just-scanned current observation for pending recovery, never the
    # previous snapshot's representation.
    observed_projection = {item["module_id"]: {"observed": item} for item in observation["modules"]}
    pending = _pending_transaction({
            "modules": observed_projection,
            "previous_pending_transaction": previous.get("pending_transaction"),
            "previous_generation": previous.get("generation"),
        }, verification, controller_state.get("registry_mainline_event"))
    current_blackboxes = sorted(
        item["module_id"] for item in observation["modules"]
        if (item["partitions"].get(PRIMARY_PARTITION) or {}).get("representation") == "blackbox")
    mainline = ({"transaction_id": pending.get("transaction_id"),
                 "kind": "blackbox_expansion", "target": pending.get("module_id"),
                 "partition": pending.get("partition", PRIMARY_PARTITION),
                 "status": pending.get("phase")} if pending else
                {"transaction_id": None, "kind": None, "partition": None,
                 "target": None,
                 "status": ("no_active_transaction" if current_blackboxes
                            else "all_configured_blackboxes_expanded")})
    measurements = collect_measurements(
        project_root, {item["module_id"] for item in observation["modules"]})
    reconciliation_id = (
        f"{session_id}:{cycle_id}:{phase}:{observation['config_fingerprint'][:12]}:"
        f"{observation['filelist_fingerprint'][:12]}:{observation['rtl_fingerprint'][:12]}:{watermark[:12]}"
    )
    packet = {
        "project_id": project_root.name,
        "session_id": session_id,
        "cycle_id": cycle_id,
        "phase": phase,
        "observed_at": _timestamp(),
        "reconciliation_id": reconciliation_id,
        "primary_partition": PRIMARY_PARTITION,
        "previous_registry": previous,
        "observation": observation,
        "verification_evidence": verification,
        "measurements": measurements,
        "pending_transaction": pending,
        "mainline": mainline,
    }
    registry = module_registry_reconcile(skill_root, packet)
    freshness = module_registry_hot_view(skill_root, registry, {
        "session_id": session_id,
        "cycle_id": cycle_id,
        "phase": phase,
        "evidence_watermark": watermark,
        "fingerprints": {
            "config_fingerprint": observation["config_fingerprint"],
            "filelist_fingerprint": observation["filelist_fingerprint"],
            "rtl_fingerprint": observation["rtl_fingerprint"],
            "evidence_watermark": watermark,
        },
    })
    if (registry.get("registry_status") != "current"
            or registry.get("hot_view", {}).get("hard_gate") != "pass"
            or freshness.get("hard_gate") != "pass"):
        raise RuntimeError(f"module registry reconciliation held: {registry.get('conflicts')}")
    emitted_events = registry.get("emitted_events") or []
    new_measurements = registry.get("new_measurements") or []
    # These are transport deltas for append-only ledgers, not current-state
    # fields. Keeping them in the snapshot would duplicate history every cycle.
    persistent_registry = {
        key: value for key, value in registry.items()
        if key not in {"emitted_events", "new_measurements"}
    }
    _atomic_json(registry_path, persistent_registry)
    _append_unique(events_path, emitted_events, "event_id")
    _append_unique(measurements_path, new_measurements, "measurement_id")
    return persistent_registry
