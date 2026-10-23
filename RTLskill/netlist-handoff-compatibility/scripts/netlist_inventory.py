#!/usr/bin/env python3
"""Stream a Verilog netlist and emit structural handoff evidence."""
import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Dict, Iterator, List, Mapping


INVENTORY_API_VERSION = 1
MODULE_RE = re.compile(r"(?:^|;)\s*module\s+(\\?[^\s#(]+)")
ENDMODULE_RE = re.compile(r"\bendmodule\b")


def _logical_name(name: str) -> str:
    return name[1:] if name.startswith("\\") else name


def _code_lines(path: Path) -> Iterator[str]:
    in_block = False
    with path.open("r", encoding="utf-8", errors="replace") as source:
        for raw in source:
            index = 0
            parts: List[str] = []
            while index < len(raw):
                if in_block:
                    end = raw.find("*/", index)
                    if end < 0:
                        index = len(raw)
                    else:
                        in_block = False
                        index = end + 2
                else:
                    line_comment = raw.find("//", index)
                    block_comment = raw.find("/*", index)
                    candidates = [position for position in (line_comment, block_comment) if position >= 0]
                    if not candidates:
                        parts.append(raw[index:])
                        index = len(raw)
                    else:
                        start = min(candidates)
                        parts.append(raw[index:start])
                        if start == line_comment:
                            index = len(raw)
                        else:
                            in_block = True
                            index = start + 2
            yield "".join(parts)


def inventory(netlist: Path, top: str) -> Dict[str, object]:
    digest = hashlib.sha256()
    with netlist.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)

    modules: List[str] = []
    endmodule_count = 0
    for line in _code_lines(netlist):
        modules.extend(_logical_name(match.group(1)) for match in MODULE_RE.finditer(line))
        endmodule_count += len(ENDMODULE_RE.findall(line))
    counts = Counter(modules)
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    return {
        "inventory_api_version": INVENTORY_API_VERSION,
        "netlist_path": str(netlist),
        "netlist_sha256": digest.hexdigest(),
        "netlist_bytes": netlist.stat().st_size,
        "module_count": len(modules),
        "endmodule_count": endmodule_count,
        "module_balance_ok": len(modules) == endmodule_count,
        "top": top,
        "top_found": top in counts,
        "duplicate_module_names": duplicates,
    }


def write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--netlist", required=True, type=Path)
    parser.add_argument("--top", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.netlist.is_file():
        parser.error("--netlist must name an existing regular file")
    value = inventory(args.netlist, args.top)
    if args.output:
        write_json(args.output, value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
