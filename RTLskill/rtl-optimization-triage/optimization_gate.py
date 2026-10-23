#!/usr/bin/env python3
"""Stable controller-facing gate for RTL optimization triage and recovery."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
from optimization_governance import (initial_state, measurement_verdict, migrate_state,
                                     step, transition, triage, validate_selection)  # noqa: E402


GATE_API_VERSION = 9


def _decorate(value: dict[str, object]) -> dict[str, object]:
    return {**value, "gate_api_version": GATE_API_VERSION, "skill": "rtl-optimization-triage"}


def run_triage(packet: dict[str, object]) -> dict[str, object]:
    return _decorate(triage(packet))


def run_validate(packet: dict[str, object]) -> dict[str, object]:
    return _decorate(validate_selection(packet["packet"], str(packet.get("module", ""))))


def run_transition(packet: dict[str, object]) -> dict[str, object]:
    return _decorate(transition(packet["state"], packet["event"]))


def run_migrate(packet: dict[str, object]) -> dict[str, object]:
    return _decorate(migrate_state(packet.get("state", packet)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(
        "triage", "validate", "transition", "initial", "migrate", "step",
        "evaluate-measurement"))
    parser.add_argument("input", nargs="?", type=Path)
    args = parser.parse_args()
    text = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
    packet = json.loads(text)
    if args.command == "triage":
        result = run_triage(packet)
    elif args.command == "validate":
        result = run_validate(packet)
    elif args.command == "transition":
        result = run_transition(packet)
    elif args.command == "initial":
        result = _decorate(initial_state(
            packet.get("interrupted_mainline_ref", {}),
            int(packet.get("max_repair_attempts", 2)),
            int(packet.get("max_measurement_retries", 2)),
            int(packet.get("max_edit_attempts", 2)),
            int(packet.get("max_internal_steps", 12))))
    elif args.command == "evaluate-measurement":
        result = _decorate(measurement_verdict(
            packet.get("before"), packet.get("after"),
            minimum_improvement_ratio=float(packet.get("minimum_improvement_ratio", 0.01)),
            maximum_regression_ratio=float(packet.get("maximum_regression_ratio", 0.01))))
    elif args.command == "step":
        result = _decorate(step(packet))
    else:
        result = run_migrate(packet)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
