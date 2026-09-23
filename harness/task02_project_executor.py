#!/usr/bin/env python3
"""Execute one Task-02 project mutation without scheduling or policy logic."""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path


EXPANSION_PARTITION = "top_level_sta"


def _read_mapping(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("project execution request must be one JSON object")
    return value


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(dict(value), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _reconcile(operation_root: Path, operation_id: str, *,
               executor_ref: str, module: str) -> dict[str, object] | None:
    path = operation_root / f"{operation_id}.json"
    try:
        value = _read_mapping(path)
    except FileNotFoundError:
        return None
    if value.get("operation_id") != operation_id:
        raise ValueError("saved project execution identity mismatch")
    if value.get("executor_ref") != executor_ref or value.get("module") != module:
        raise ValueError("saved project execution request mismatch")
    return value


def _expand_blackbox(config_path: Path, module: str) -> dict[str, object]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module):
        raise ValueError(f"invalid blackbox module name: {module!r}")
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_partition = False
    current_name = None
    for index, line in enumerate(lines):
        if line.strip() == "[[partitions]]":
            in_partition = True
            current_name = None
            continue
        if not in_partition:
            continue
        name_match = re.match(r'\s*name\s*=\s*"([A-Za-z0-9_.-]+)"\s*$', line)
        if name_match:
            current_name = name_match.group(1)
            continue
        if current_name != EXPANSION_PARTITION:
            continue
        match = re.match(r'(\s*blackbox\s*=\s*")([^"]*)("\s*\n?)$', line)
        if not match:
            continue
        old_modules = [item for item in match.group(2).split() if item]
        if module not in old_modules:
            raise ValueError(
                f"stale decision packet: {module!r} is not a current "
                f"{EXPANSION_PARTITION} blackbox")
        new_modules = [item for item in old_modules if item != module]
        lines[index] = f"{match.group(1)}{' '.join(new_modules)}{match.group(3)}"
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=config_path.parent, delete=False) as handle:
            handle.write("".join(lines))
            temporary = Path(handle.name)
        os.chmod(temporary, config_path.stat().st_mode & 0o777)
        os.replace(temporary, config_path)
        return {
            "module": module,
            "partition": EXPANSION_PARTITION,
            "old_blackbox_count": len(old_modules),
            "new_blackbox_count": len(new_modules),
            "old_blackboxes": old_modules,
            "new_blackboxes": new_modules,
            "config_path": "harness/ppa.toml",
        }
    raise ValueError(f"could not find {EXPANSION_PARTITION} blackbox declaration")


def execute_request(project_root: Path,
                    request: Mapping[str, object]) -> dict[str, object]:
    operation_id = str(request.get("operation_id") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", operation_id):
        raise ValueError("operation_id must be one SHA-256 digest")
    executor_ref = str(request.get("executor_ref") or "")
    if executor_ref != "project.expand_blackbox":
        raise ValueError(f"unsupported project executor_ref: {executor_ref}")
    module = str(request.get("module") or "")
    operation_root = project_root / "status/task02_operations"
    existing = _reconcile(
        operation_root, operation_id, executor_ref=executor_ref, module=module)
    if existing is not None:
        return existing
    try:
        details = _expand_blackbox(
            project_root / "harness/ppa.toml", module)
        result = {
            "operation_id": operation_id, "executor_ref": executor_ref,
            "module": module,
            "status": "success", "exit_code": 0,
            "artifacts": ["harness/ppa.toml"], "evidence_refs": [],
            "details": details,
        }
    except ValueError as error:
        result = {
            "operation_id": operation_id, "executor_ref": executor_ref,
            "module": module,
            "status": "stale", "exit_code": None,
            "artifacts": [], "evidence_refs": [], "detail": str(error),
        }
    except OSError as error:
        result = {
            "operation_id": operation_id, "executor_ref": executor_ref,
            "module": module,
            "status": "infra_uncertain", "exit_code": None,
            "artifacts": [], "evidence_refs": [], "detail": str(error),
        }
    _atomic_json(operation_root / f"{operation_id}.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = _read_mapping(args.request)
        result = execute_request(args.project_root.resolve(), request)
        _atomic_json(args.output, result)
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        operation_id = ""
        executor_ref = ""
        try:
            request = _read_mapping(args.request)
            operation_id = str(request.get("operation_id") or "")
            executor_ref = str(request.get("executor_ref") or "")
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        _atomic_json(args.output, {
            "operation_id": operation_id, "executor_ref": executor_ref,
            "status": "infra_uncertain", "exit_code": 2,
            "artifacts": [], "evidence_refs": [], "detail": str(error),
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
