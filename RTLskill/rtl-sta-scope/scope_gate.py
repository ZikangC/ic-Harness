#!/usr/bin/env python3
"""Stable controller-facing gate for rtl-sta-scope."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
from sta_scope import decide  # noqa: E402


GATE_API_VERSION = 1


def run(packet: Mapping[str, object]) -> dict[str, object]:
    return {**decide(packet), "gate_api_version": GATE_API_VERSION, "skill": "rtl-sta-scope"}


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    text = path.read_text(encoding="utf-8") if path else sys.stdin.read()
    print(json.dumps(run(json.loads(text)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
