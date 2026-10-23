#!/usr/bin/env python3
"""Stable JSON stdin/stdout entry point for module registry controllers."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from module_registry import (RegistryError, can_expand, module_context, reconcile,
                             validate_hot_view, value_evidence)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=(
        "reconcile", "hot-view", "can-expand", "value-evidence", "module-context"))
    args = parser.parse_args()
    try:
        packet = json.load(sys.stdin)
        if args.command == "reconcile":
            result = reconcile(packet)
        elif args.command == "hot-view":
            result = validate_hot_view(packet.get("registry", {}), packet.get("expected", {}))
        elif args.command == "can-expand":
            result = can_expand(packet.get("registry", {}), str(packet.get("module", "")),
                                str(packet.get("partition", "top_level_sta")))
        elif args.command == "value-evidence":
            result = value_evidence(packet.get("registry", {}), str(packet.get("module", "")))
        else:
            result = module_context(packet.get("registry", {}), str(packet.get("module", "")))
    except (RegistryError, ValueError, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({"hard_gate": "hold", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
