#!/usr/bin/env python3
"""Classify deterministic netlist inventory and backend probe evidence."""
import argparse
import json
from pathlib import Path
from typing import Dict, Mapping, Optional


AUDIT_API_VERSION = 1
NEXT_ACTION = {
    "source_artifact_empty": "repair the front-end selection/export and regenerate a separate candidate netlist",
    "source_artifact_malformed": "repair the front-end export; do not text-patch the truncated or duplicate artifact",
    "source_top_missing": "repair top selection or elaboration and regenerate the candidate netlist",
    "not_attempted": "run the declared bounded backend availability/read/link probe",
    "backend_unavailable": "repair the execution environment or backend command adapter, then rerun the probe",
    "unsupported_hdl_construct": "derive a normalized netlist with a declared front-end transformation, then rerun read/link",
    "blackbox_contract_missing": "supply an exact declared blackbox/body contract for the intended scope",
    "library_coverage_missing": "load the matching backend library/model set or correct the mapped-cell contract",
    "backend_read_error": "isolate the first backend read diagnostic and repair the read adapter or export",
    "backend_link_error": "repair backend top/library/model linkage and rerun the same bounded link probe",
    "backend_link_not_attempted": "add and run a real backend link stage before formal STA or implementation",
    "handoff_compatible": "pass the unchanged artifact, backend identity, scope, and evidence to the STA closure gate",
}
REPAIR_DOMAIN = {
    "source_artifact_empty": "frontend_export",
    "source_artifact_malformed": "frontend_export",
    "source_top_missing": "frontend_hierarchy",
    "not_attempted": "backend_adapter",
    "backend_unavailable": "environment_or_adapter",
    "unsupported_hdl_construct": "normalization_adapter",
    "blackbox_contract_missing": "blackbox_contract",
    "library_coverage_missing": "library_adapter",
    "backend_read_error": "backend_adapter",
    "backend_link_error": "backend_adapter_or_constraints",
    "backend_link_not_attempted": "backend_adapter",
    "handoff_compatible": "none",
}


def classify(inventory: Mapping[str, object], probe: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    if int(inventory.get("netlist_bytes", 0) or 0) == 0 or int(inventory.get("module_count", 0) or 0) == 0:
        phase = "source_artifact_empty"
    elif not inventory.get("module_balance_ok", False) or inventory.get("duplicate_module_names", []):
        phase = "source_artifact_malformed"
    elif not inventory.get("top_found", False):
        phase = "source_top_missing"
    elif probe is None:
        phase = "not_attempted"
    elif probe.get("status") == "failed":
        candidate = str(probe.get("diagnostic_kind", ""))
        phase = candidate if candidate in REPAIR_DOMAIN else "backend_link_error"
    else:
        stage_status = probe.get("stage_status", {})
        link_status = stage_status.get("link") if isinstance(stage_status, dict) else None
        phase = "handoff_compatible" if link_status == "passed" else "backend_link_not_attempted"
    return {
        "audit_api_version": AUDIT_API_VERSION,
        "phase": phase,
        "handoff_compatible": phase == "handoff_compatible",
        "repair_domain": REPAIR_DOMAIN[phase],
        "next_action": NEXT_ACTION[phase],
        "netlist_sha256": inventory.get("netlist_sha256"),
        "netlist_bytes": inventory.get("netlist_bytes"),
        "module_count": inventory.get("module_count"),
        "top": inventory.get("top"),
        "top_found": inventory.get("top_found"),
        "backend": probe.get("backend", "unmeasured") if probe else "unmeasured",
        "scope": probe.get("scope", "unspecified") if probe else "unspecified",
        "failed_stage": probe.get("failed_stage", "none") if probe else "none",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.inventory.is_file():
        parser.error("--inventory must name an existing regular file")
    if args.probe and not args.probe.is_file():
        parser.error("--probe must name an existing regular file")
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    probe = json.loads(args.probe.read_text(encoding="utf-8")) if args.probe else None
    result = classify(inventory, probe)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        from netlist_inventory import write_json
        write_json(args.output, result)
    else:
        print(encoded, end="")
    return 0 if result["handoff_compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
