#!/usr/bin/env python3
"""Stable controller API for synthesized-netlist handoff compatibility."""
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional


SCRIPTS = Path(__file__).resolve().parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from handoff_audit import classify  # noqa: E402
from handoff_probe import run_probe_config, write_json  # noqa: E402
from netlist_inventory import inventory  # noqa: E402


GATE_API_VERSION = 1


def _load_object(path: Path) -> Dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(str(path) + " must contain a JSON object")
    return value


def run(*, netlist_path: Optional[Path] = None, top: Optional[str] = None,
        inventory_path: Optional[Path] = None,
        probe_config_path: Optional[Path] = None,
        probe_path: Optional[Path] = None,
        inventory_output: Optional[Path] = None,
        probe_output: Optional[Path] = None) -> Dict[str, object]:
    if (netlist_path is None) == (inventory_path is None):
        raise ValueError("supply exactly one of netlist_path or inventory_path")
    if (probe_config_path is not None) and (probe_path is not None):
        raise ValueError("supply at most one of probe_config_path or probe_path")
    if netlist_path is not None:
        if not top:
            raise ValueError("top is required with netlist_path")
        inventory_data = inventory(netlist_path, top)
        if inventory_output:
            write_json(inventory_output, inventory_data)
    else:
        inventory_data = _load_object(inventory_path)  # type: ignore[arg-type]

    probe_data = None
    if probe_config_path is not None:
        probe_data = run_probe_config(_load_object(probe_config_path))
        if probe_output:
            write_json(probe_output, probe_data)
    elif probe_path is not None:
        probe_data = _load_object(probe_path)
    result = classify(inventory_data, probe_data)
    result["gate_api_version"] = GATE_API_VERSION
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--netlist", type=Path)
    parser.add_argument("--top")
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--probe-config", type=Path)
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--inventory-output", type=Path)
    parser.add_argument("--probe-output", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    for path, label in ((args.netlist, "--netlist"), (args.inventory, "--inventory"),
                        (args.probe_config, "--probe-config"), (args.probe, "--probe")):
        if path is not None and not path.is_file():
            parser.error(label + " must name an existing regular file")
    try:
        result = run(netlist_path=args.netlist, top=args.top, inventory_path=args.inventory,
                     probe_config_path=args.probe_config, probe_path=args.probe,
                     inventory_output=args.inventory_output, probe_output=args.probe_output)
    except (ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    if args.output:
        write_json(args.output, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["handoff_compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
