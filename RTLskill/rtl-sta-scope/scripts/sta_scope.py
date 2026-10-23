#!/usr/bin/env python3
"""Deterministically choose the minimum defensible STA scope."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping


SCOPE_SCHEMA_VERSION = 2
TOP_TRIGGER_FIELDS = (
    "top_module_changed", "filelist_changed", "constraints_changed",
    "blackbox_policy_changed",
)
CHANGE_TOP_FIELDS = (
    "interface_changed", "clock_reset_changed", "parameter_changed",
    "shared_state_changed", "combinational_boundary_changed",
)


def _bool(value: object) -> bool:
    return value is True


def decide(packet: Mapping[str, object]) -> dict[str, object]:
    changes = packet.get("rtl_changes")
    rtl_changes = [item for item in changes if isinstance(item, Mapping)] if isinstance(changes, list) else []
    affected_raw = packet.get("affected_modules")
    affected = [str(item) for item in affected_raw if str(item)] if isinstance(affected_raw, list) else []
    for change in rtl_changes:
        module = str(change.get("module", "")).strip()
        if module and module not in affected:
            affected.append(module)
    remaining_raw = packet.get("remaining_blackboxes")
    remaining = [str(item) for item in remaining_raw if str(item)] if isinstance(remaining_raw, list) else []
    raw_context = packet.get("design_context")
    design_context = raw_context if isinstance(raw_context, Mapping) else {}
    module_contexts = design_context.get("module_contexts")
    module_contexts = module_contexts if isinstance(module_contexts, Mapping) else {}
    context_current = (
        design_context.get("status") == "current"
        and design_context.get("hard_gate") == "pass"
        and isinstance(design_context.get("graph_fingerprint"), str)
        and bool(design_context.get("graph_fingerprint"))
    )

    if not rtl_changes and not any(_bool(packet.get(field)) for field in TOP_TRIGGER_FIELDS):
        return {
            "schema_version": SCOPE_SCHEMA_VERSION,
            "scope": "no_rtl_sta",
            "minimum_scope": "no_rtl_sta",
            "requires_sta": False,
            "reason_codes": ["no_rtl_timing_input_change"],
            "affected_modules": affected,
            "remaining_blackbox_count": len(remaining),
            "design_context_fingerprint": design_context.get("graph_fingerprint"),
            "measurement_plan": "measurement_smoke" if _bool(packet.get("measurement_adapter_changed")) else "none",
        }

    reasons: list[str] = []
    top_required = str(packet.get("change_mode", "local")) == "architecture"
    if top_required:
        reasons.append("architecture_change")
    for field in TOP_TRIGGER_FIELDS:
        if _bool(packet.get(field)):
            top_required = True
            reasons.append(field)
    for change in rtl_changes:
        module = str(change.get("module", "")).strip()
        module_context = module_contexts.get(module) if module else None
        if not context_current:
            top_required = True
            reasons.append("design_context_not_current")
        elif not isinstance(module_context, Mapping):
            top_required = True
            reasons.append("module_context_missing")
        for field in CHANGE_TOP_FIELDS:
            if _bool(change.get(field)):
                top_required = True
                reasons.append(field)
        safety = (
            change.get("impact_known") is True
            and change.get("boundary_registered") is True
            and all(change.get(field) is False for field in CHANGE_TOP_FIELDS)
        )
        if not safety:
            top_required = True
            reasons.append("local_scope_not_proven")

    if top_required:
        scope = "top_level_partial" if remaining else "top_level_complete"
        plan = "top_level_sta"
    elif len(affected) > 1:
        scope = "affected_cone"
        plan = "affected_cone_sta"
        reasons.append("known_dependency_cone")
    elif len(rtl_changes) == 1 and len(affected) == 1:
        scope = "local_module"
        plan = "module_sta"
        reasons.append("registered_boundary_local_change")
    else:
        scope = "top_level_partial" if remaining else "top_level_complete"
        plan = "top_level_sta"
        reasons.append("scope_ambiguous")

    return {
        "schema_version": SCOPE_SCHEMA_VERSION,
        "scope": scope,
        "minimum_scope": scope,
        "requires_sta": True,
        "reason_codes": sorted(set(reasons)),
        "affected_modules": affected,
        "remaining_blackbox_count": len(remaining),
        "design_context_fingerprint": design_context.get("graph_fingerprint"),
        "measurement_plan": plan,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path)
    args = parser.parse_args()
    text = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
    packet = json.loads(text)
    print(json.dumps(decide(packet), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
