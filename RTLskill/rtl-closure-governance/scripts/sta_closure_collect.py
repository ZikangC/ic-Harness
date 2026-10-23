#!/usr/bin/env python3
"""Stream a Verilog closure netlist and emit small, portable audit metadata."""
import argparse
import json
import re
from pathlib import Path
from typing import Iterable, List


# Most emitters put module declarations on their own lines.  The semicolon
# alternative also covers compact/minified netlists without loading a large
# file into memory.
MODULE_RE = re.compile(r"(?:^|;)\s*module\s+(\\?[^\s(]+)")


def logical_name(name: str) -> str:
    return name[1:] if name.startswith("\\") else name


def top_matches(module: str, top: str) -> bool:
    if module == top:
        return True
    # Yosys may retain a parameterized elaboration prefix.  Do not treat an
    # arbitrary substring as a match: retain only the conventional suffix.
    return module.endswith("_" + top) or module.endswith("\\" + top)


def collect(netlist: Path, top: str, required: Iterable[str]) -> dict:
    modules: List[str] = []
    with netlist.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            for match in MODULE_RE.finditer(line):
                modules.append(logical_name(match.group(1)))
    module_set = set(modules)
    missing = [name for name in required if name not in module_set]
    return {
        "artifact_version": 1,
        "sta_closure_bytes": netlist.stat().st_size,
        "sta_closure_module_count": len(modules),
        "sta_closure_top_found": int(any(top_matches(module, top) for module in modules)),
        "required_body_missing": missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--netlist", required=True, type=Path)
    parser.add_argument("--top", required=True)
    parser.add_argument("--required-body", action="append", default=[], metavar="MODULE")
    parser.add_argument("--output", type=Path, help="write JSON atomically to this path")
    args = parser.parse_args()
    if not args.netlist.is_file():
        parser.error("--netlist must name an existing regular file")
    encoded = json.dumps(collect(args.netlist, args.top, args.required_body), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(args.output)
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
