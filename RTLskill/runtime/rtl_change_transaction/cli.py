#!/usr/bin/env python3
"""Thin CLI for rtl_change_transaction recovery and inspection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import (
    TransactionError, begin_transaction, commit_transaction,
    inspect_transaction, rollback_transaction,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    begin = subparsers.add_parser("begin")
    begin.add_argument("--project-root", type=Path, required=True)
    begin.add_argument("--transaction-root", type=Path, required=True)
    begin.add_argument("--operation-id", required=True)
    begin.add_argument("--managed-path", action="append", required=True)
    begin.add_argument("--allowed-path", action="append", required=True)
    begin.add_argument("--max-changed-files", type=int, default=3)
    for name in ("inspect", "commit", "rollback"):
        child = subparsers.add_parser(name)
        child.add_argument("--transaction-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "begin":
            result = begin_transaction(
                project_root=args.project_root,
                transaction_root=args.transaction_root,
                operation_id=args.operation_id,
                managed_paths=args.managed_path,
                allowed_paths=args.allowed_path,
                max_changed_files=args.max_changed_files)
        elif args.command == "inspect":
            result = inspect_transaction(args.transaction_root)
        elif args.command == "commit":
            result = commit_transaction(args.transaction_root)
        else:
            result = rollback_transaction(args.transaction_root)
    except TransactionError as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
