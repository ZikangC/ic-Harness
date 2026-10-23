#!/usr/bin/env python3
"""Stable JSON interface for the RTL transaction ledger."""
from __future__ import annotations

import argparse
import json
import sys

from scripts.transaction_ledger import (hot_view, initial_state, transition,
                                        validate_ref)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("initial", "transition", "validate-ref", "hot-view"))
    args = parser.parse_args()
    packet = json.load(sys.stdin)
    if args.command == "initial":
        result = initial_state(str(packet["project_id"]))
    elif args.command == "transition":
        result = transition(packet["ledger"], packet["event"])
    elif args.command == "validate-ref":
        result = validate_ref(packet["ledger"], packet["transaction_ref"])
    else:
        result = hot_view(packet["ledger"])
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
