#!/usr/bin/env python3
"""Classify top-level STA-closure evidence without model inference.

Input is a feedback.txt-compatible key/value packet. Output is compact JSON,
so it can enter an agent/controller decision without copying large netlists.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, Mapping, Optional


AUDIT_VERSION = 6
HARNESS_PHASES = {
    "tool_probe_failed", "closure_export_empty", "closure_top_missing", "closure_body_missing",
    "undeclared_blackbox", "sta_parse_error", "sta_link_error",
}
CLOSURE_RETRY_PHASES = {"closure_measurement_incomplete"}
CLOSURE_RESOURCE_PHASES = {"closure_resource_blocked"}
TERMINAL_PHASE = "sta_timing_measured"


def normalize_measurement_descriptor(value: object) -> Optional[Dict[str, object]]:
    """Normalize project-supplied measurement identity without project names.

    Budget is deliberately metadata, not identity: raising a timeout is still
    the same semantic measurement.
    """
    if not isinstance(value, Mapping):
        return None
    fingerprint = str(value.get("semantic_fingerprint") or "").strip()
    scope_kind = str(value.get("scope_kind") or "").strip()
    raw_targets = value.get("targets")
    targets = sorted({str(item).strip() for item in raw_targets
                      if str(item).strip()}) if isinstance(raw_targets, list) else []
    if not fingerprint or not scope_kind:
        return None
    identity = {
        "semantic_fingerprint": fingerprint,
        "scope_kind": scope_kind,
        "targets": targets,
    }
    measurement_key = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **identity,
        "measurement_key": measurement_key,
        "operation_kind": str(value.get("operation_kind") or "measurement"),
        "intent": str(value.get("intent") or "unspecified"),
        "budget_seconds": value.get("budget_seconds"),
    }


def normalized_budget(value: object, *, positive: bool = False) -> Optional[int]:
    """Return a usable raw budget without coercing strings or booleans."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value < (1 if positive else 0):
        return None
    return value


def classify_measurement_relation(exhausted: object, proposed: object) -> str:
    """Compare semantic measurement identity, never model prose or action names."""
    old = normalize_measurement_descriptor(exhausted)
    new = normalize_measurement_descriptor(proposed)
    if old is None or new is None:
        return "unknown"
    same_scope = (old["scope_kind"] == new["scope_kind"]
                  and old["targets"] == new["targets"])
    same_design = old["semantic_fingerprint"] == new["semantic_fingerprint"]
    if same_scope and same_design:
        return "same_measurement"
    if not same_scope and not same_design:
        return "changed_scope_and_design"
    if not same_scope:
        return "changed_scope"
    return "changed_design_or_config"


def measurement_policy(audit: Mapping[str, object],
                       context: Mapping[str, object]) -> Dict[str, object]:
    exhausted = normalize_measurement_descriptor(context.get("exhausted_measurement"))
    proposed = normalize_measurement_descriptor(context.get("proposed_measurement"))
    relation = classify_measurement_relation(exhausted, proposed)
    phase = str(audit.get("phase", "not_attempted"))
    exhausted_budget = normalized_budget(
        exhausted.get("budget_seconds") if exhausted is not None else None,
        positive=True)
    proposed_budget = normalized_budget(
        proposed.get("budget_seconds") if proposed is not None else None,
        positive=True)
    max_budget = normalized_budget(context.get("max_measurement_budget_seconds"), positive=True)
    reasons: list[str] = []
    allowed: Optional[bool]
    if proposed is None:
        allowed = None
        reasons.append("proposed_measurement_missing")
    elif max_budget is not None and proposed_budget is not None and proposed_budget > max_budget:
        allowed = False
        reasons.append("proposed_budget_exceeds_legal_maximum")
    elif phase == "closure_resource_blocked":
        if relation == "same_measurement":
            allowed = False
            reasons.append("same_measurement_resource_exhausted")
        elif relation == "unknown":
            allowed = False
            reasons.append("measurement_relation_unknown")
        else:
            allowed = True
            reasons.append("measurement_strategy_changed")
    elif phase == "closure_measurement_incomplete" and relation == "same_measurement":
        if exhausted_budget is None or proposed_budget is None:
            allowed = None
            reasons.append("measurement_budget_relation_unknown")
        elif proposed_budget <= exhausted_budget:
            allowed = False
            reasons.append("same_measurement_budget_not_increased")
        else:
            allowed = True
            reasons.append("same_measurement_budget_strictly_increased")
    else:
        allowed = relation != "unknown"
        if relation == "unknown":
            reasons.append("measurement_relation_unknown")
    return {
        "schema_version": 1,
        "exhausted_measurement": exhausted,
        "proposed_measurement": proposed,
        "measurement_relation": relation,
        "same_measurement_rerun_admissible": (
            allowed if relation == "same_measurement" else False),
        "strategy_change_allowed": relation in {
            "changed_scope", "changed_design_or_config", "changed_scope_and_design"},
        "proposed_measurement_allowed": allowed,
        "reason_codes": reasons,
    }


def parse_feedback(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.startswith("---"):
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[a-z][a-z0-9_]*", key.strip()):
            values[key.strip()] = value.strip()
    return values


def as_int(values: Mapping[str, str], key: str) -> Optional[int]:
    match = re.match(r"([0-9]+)(?:\s|$)", values.get(key, ""))
    return int(match.group(1)) if match else None


def token(values: Mapping[str, str], key: str, default: str = "") -> str:
    value = values.get(key, default).strip()
    return value.split(None, 1)[0] if value else default


def measurement_timeout_or_truncation(values: Mapping[str, str]) -> bool:
    """Recognize an incomplete measurement without treating it as a repair defect."""
    status = token(values, "measurement_status", "").lower()
    if status in {"incomplete_timeout", "timeout", "timed_out", "truncated", "incomplete"}:
        return True
    text = " ".join(str(values.get(key, "")) for key in (
        "sta_error", "error_signature", "measurement_status", "exit_code", "ppa_exit",
    )).lower()
    return bool(re.search(r"(?:exit(?:ed)?|exit_code|ppa_exit)\s*=?\s*124\b", text)
                or "timeout" in text or "timed out" in text or "truncat" in text)


def no_timing_path_cause(values: Mapping[str, str]) -> str:
    """Classify a real no-path result from deterministic adapter counters."""
    if token(values, "sta_diag_status", "") != "ok":
        return "diagnostic_missing"
    clock_ports = as_int(values, "sta_diag_clock_port_count")
    clocks = as_int(values, "sta_diag_clock_count")
    registers = as_int(values, "sta_diag_register_count")
    if clock_ports == 0:
        return "clock_port_missing"
    if clocks == 0:
        return "clock_not_created"
    if registers == 0:
        return "no_registers_in_linked_netlist"
    return "registers_present_but_no_reported_path"


def phase_flags(phase: str, no_path_cause: str = "not_applicable") -> Dict[str, object]:
    """Expose stable semantic gates so controllers do not mirror phase sets."""
    structural = phase in HARNESS_PHASES
    no_path = phase == "sta_no_timing_path"
    incomplete = phase in CLOSURE_RETRY_PHASES
    resource = phase in CLOSURE_RESOURCE_PHASES
    return {
        "closure_repair_required": structural or no_path,
        "structural_repair_required": structural,
        "diagnostic_required": no_path and no_path_cause == "diagnostic_missing",
        "closure_gate_required": structural or no_path or incomplete or resource,
        "measurement_incomplete": incomplete or resource,
        "measurement_rerun_admissible": None if incomplete else False,
        "measurement_resource_blocked": resource,
        "strategy_change_required": resource,
        "timing_ready": phase == TERMINAL_PHASE,
    }


def classify(values: Mapping[str, str]) -> Dict[str, object]:
    status = token(values, "sta_status", "not_attempted")
    link = token(values, "sta_link_result", "未测得")
    timing = token(values, "sta_timing_measured", "no")
    top_found = token(values, "sta_closure_top_found", "未测得")
    body = values.get("sta_body_check", "未测得")
    read_skip = as_int(values, "sta_read_skip_count")
    closure_bytes = as_int(values, "sta_closure_bytes")
    closure_modules = as_int(values, "sta_closure_module_count")
    error = values.get("sta_error", "").replace("\n", " ").strip()[:240] or "未提供 STA 错误文本"

    phase = "not_attempted"
    required_body_missing = values.get("required_body_missing", "").strip()
    probe_status = token(values, "tool_probe_status", "未测得")
    # The controller must not let the agent use a critical command after its
    # declared minimal capability probe failed.
    if probe_status == "failed":
        phase = "tool_probe_failed"
    # Export facts come directly from the netlist and remain useful before an
    # STA run exists.  The remaining phases require a measurement packet.
    # A comment-only netlist has nonzero bytes but zero declarations: that is
    # an export failure, not a top-name failure.
    elif closure_bytes == 0 or closure_modules == 0:
        phase = ("closure_measurement_incomplete"
                 if measurement_timeout_or_truncation(values) else "closure_export_empty")
    elif top_found == "0":
        phase = "closure_top_missing"
    elif required_body_missing or "missing" in body:
        phase = "closure_body_missing"
    elif status not in ("", "not_attempted", "unavailable"):
        if (read_skip is not None and read_skip > 0) or status == "read_error":
            low = error.lower()
            # STA-0171 is a generic OpenSTA read error and can report either a
            # syntax error or an unknown module.  Only the diagnostic text may
            # establish a missing blackbox contract; the bare code cannot.
            phase = ("undeclared_blackbox"
                     if "blackbox" in low or "unknown module" in low
                     else "sta_parse_error")
        elif link == "error" or status == "link_error":
            phase = "sta_link_error"
        elif timing == "yes":
            phase = TERMINAL_PHASE
        else:
            phase = "sta_no_timing_path"

    no_path_cause = (no_timing_path_cause(values)
                     if phase == "sta_no_timing_path" else "not_applicable")
    if phase == "tool_probe_failed":
        repair_domain = "harness_or_environment"
    elif phase == "closure_measurement_incomplete":
        repair_domain = "measurement_budget"
    elif phase in ("closure_export_empty", "closure_top_missing", "closure_body_missing", "undeclared_blackbox"):
        repair_domain = "harness_or_rtl"
    elif phase in ("sta_parse_error", "sta_link_error"):
        repair_domain = "harness_or_constraints"
    elif phase == "sta_no_timing_path":
        repair_domain = (
            "harness_or_constraints" if no_path_cause in {
                "diagnostic_missing", "clock_port_missing", "clock_not_created",
                "no_registers_in_linked_netlist",
            } else "constraints_or_rtl"
        )
    else:
        repair_domain = "none"
    return {
        "audit_version": AUDIT_VERSION,
        "phase": phase,
        **phase_flags(phase, no_path_cause),
        "next_action": ("increase_budget|change_measurement_strategy"
                        if phase in CLOSURE_RETRY_PHASES else None),
        "repair_domain": repair_domain,
        "closure_bytes": closure_bytes,
        "closure_module_count": closure_modules,
        "closure_top_found": top_found,
        "sta_read_ok_count": as_int(values, "sta_read_ok_count"),
        "sta_read_skip_count": read_skip,
        "sta_link_result": link,
        "sta_timing_measured": timing == "yes",
        "sta_status": status,
        "sta_diag_status": token(values, "sta_diag_status", "未测得"),
        "sta_diag_clock_port_count": as_int(values, "sta_diag_clock_port_count"),
        "sta_diag_clock_count": as_int(values, "sta_diag_clock_count"),
        "sta_diag_register_count": as_int(values, "sta_diag_register_count"),
        "sta_diag_data_input_count": as_int(values, "sta_diag_data_input_count"),
        "no_timing_path_cause": no_path_cause,
        "tool_probe_status": probe_status,
        "error_signature": error,
    }


def apply_policy_context(audit: Mapping[str, object],
                         context: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    """Apply controller resource facts without reclassifying measurement truth.

    The base audit says what the tools observed.  This projection says whether
    a strictly larger legal budget remains available.  It deliberately uses
    explicit raw resource facts instead of persistent retry counters or prose.
    """
    result = dict(audit)
    policy = dict(context or {})
    budget_value = policy.get("measurement_budget_seconds")
    budget_limit_value = policy.get("max_measurement_budget_seconds")
    exhausted = normalize_measurement_descriptor(policy.get("exhausted_measurement"))
    descriptor_budget = (exhausted.get("budget_seconds")
                         if exhausted is not None else None)
    measurement_budget_seconds = (
        normalized_budget(budget_value, positive=True)
        if normalized_budget(budget_value, positive=True) is not None
        else normalized_budget(descriptor_budget, positive=True))
    max_measurement_budget_seconds = normalized_budget(budget_limit_value, positive=True)
    max_budget_exhausted = bool(
        result.get("phase") == "closure_measurement_incomplete"
        and measurement_budget_seconds is not None
        and max_measurement_budget_seconds is not None
        and measurement_budget_seconds >= max_measurement_budget_seconds)
    result.update({
        "measurement_budget_seconds": measurement_budget_seconds,
        "max_measurement_budget_seconds": max_measurement_budget_seconds,
        "max_budget_exhausted": max_budget_exhausted,
    })
    if (result.get("phase") == "closure_measurement_incomplete"
            and max_budget_exhausted):
        result.update({
            "phase": "closure_resource_blocked",
            "repair_domain": "measurement_strategy",
            "next_action": "diagnose_local|change_measurement_scope|stop_for_debug",
        })
    result.update(phase_flags(
        str(result.get("phase", "not_attempted")),
        str(result.get("no_timing_path_cause", "not_applicable")),
    ))
    if result.get("phase") == "closure_measurement_incomplete":
        result["measurement_rerun_admissible"] = (
            measurement_budget_seconds < max_measurement_budget_seconds
            if measurement_budget_seconds is not None
            and max_measurement_budget_seconds is not None
            else None)
    result["measurement_policy"] = measurement_policy(result, policy)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?", help="feedback.txt; stdin when omitted")
    parser.add_argument("--artifact", type=Path, help="artifact JSON from sta_closure_collect.py")
    parser.add_argument("--output", type=Path, help="write JSON atomically to this path")
    args = parser.parse_args()
    text = args.input.read_text(encoding="utf-8", errors="replace") if args.input else sys.stdin.read()
    values = parse_feedback(text)
    if args.artifact:
        artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
        for key, value in artifact.items():
            if isinstance(value, list):
                values[key] = ",".join(str(item) for item in value)
            elif value is not None:
                values[key] = str(value)
    encoded = json.dumps(classify(values), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(args.output)
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
