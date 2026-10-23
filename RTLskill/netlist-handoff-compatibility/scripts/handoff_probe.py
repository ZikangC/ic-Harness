#!/usr/bin/env python3
"""Run bounded backend availability/read/link probes without a shell."""
import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Mapping, Optional


PROBE_API_VERSION = 1
STAGE_ORDER = ("availability", "read", "link")
OUTPUT_TAIL_CHARS = 2000
ALLOWED_DIAGNOSTIC_KINDS = {
    "backend_unavailable", "unsupported_hdl_construct",
    "blackbox_contract_missing", "library_coverage_missing",
    "backend_read_error", "backend_link_error",
}
DEFAULT_DIAGNOSTIC_RULES = (
    ("unsupported_hdl_construct", r"syntax error|parse error|unsupported|unexpected token"),
    ("blackbox_contract_missing", r"unknown module|undefined module|blackbox|not part of the design"),
    ("library_coverage_missing", r"not found in (?:the )?librar|unknown cell|unresolved cell|cell .* not found"),
)


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _rules(config: Mapping[str, object]):
    custom = config.get("diagnostic_rules", [])
    if not isinstance(custom, list):
        raise ValueError("diagnostic_rules must be an array")
    compiled = []
    for rule in custom:
        if not isinstance(rule, dict):
            raise ValueError("each diagnostic rule must be an object")
        kind, pattern = rule.get("kind"), rule.get("pattern")
        if kind not in ALLOWED_DIAGNOSTIC_KINDS or not isinstance(pattern, str) or not pattern:
            raise ValueError("diagnostic rule has invalid kind or pattern")
        try:
            compiled.append((kind, re.compile(pattern, re.IGNORECASE)))
        except re.error as error:
            raise ValueError("invalid diagnostic regex: " + str(error))
    compiled.extend((kind, re.compile(pattern, re.IGNORECASE)) for kind, pattern in DEFAULT_DIAGNOSTIC_RULES)
    return compiled


def _validate_stage(raw: Mapping[str, object]) -> Dict[str, object]:
    name, argv = raw.get("name"), raw.get("argv")
    if name not in STAGE_ORDER:
        raise ValueError("stage.name must be availability, read, or link")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError("stage.argv must be a nonempty array of strings")
    timeout = raw.get("timeout_seconds", 10)
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
        raise ValueError("stage.timeout_seconds must be in (0, 60]")
    markers = raw.get("success_markers")
    if not isinstance(markers, list) or not markers or not all(isinstance(item, str) and item for item in markers):
        raise ValueError("stage.success_markers must be a nonempty array of strings")
    cwd = raw.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd):
        raise ValueError("stage.cwd must be an optional nonempty string")
    return {"name": name, "argv": argv, "timeout_seconds": timeout,
            "success_markers": markers, "cwd": cwd}


def _failure_kind(stage: str, output: str, rules) -> str:
    if stage == "availability":
        return "backend_unavailable"
    for kind, pattern in rules:
        if pattern.search(output):
            return kind
    if stage == "read":
        return "backend_read_error"
    return "backend_link_error"


def _run_stage(raw: Mapping[str, object], rules) -> Dict[str, object]:
    stage = _validate_stage(raw)
    started = time.monotonic()
    output, launch_error = "", ""
    exit_code: Optional[int] = None
    timed_out = False
    try:
        completed = subprocess.run(stage["argv"], cwd=stage["cwd"], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=stage["timeout_seconds"], check=False)
        output, exit_code = completed.stdout or "", completed.returncode
    except subprocess.TimeoutExpired as error:
        timed_out, output = True, _text(error.stdout) + _text(error.stderr)
    except OSError as error:
        launch_error = str(error)
    missing = [marker for marker in stage["success_markers"] if marker not in output]
    passed = exit_code == 0 and not timed_out and not launch_error and not missing
    diagnostic_text = output + "\n" + launch_error
    return {
        "name": stage["name"], "status": "passed" if passed else "failed",
        "argv": stage["argv"], "exit_code": exit_code, "timed_out": timed_out,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "missing_success_markers": missing, "launch_error": launch_error,
        "diagnostic_kind": "none" if passed else _failure_kind(stage["name"], diagnostic_text, rules),
        "output_tail": output[-OUTPUT_TAIL_CHARS:],
    }


def run_probe_config(config: Mapping[str, object]) -> Dict[str, object]:
    backend, scope = config.get("backend"), config.get("scope", "unspecified")
    if not isinstance(backend, str) or not backend:
        raise ValueError("backend must be a nonempty string")
    if scope not in ("complete", "partial_hierarchical", "unspecified"):
        raise ValueError("scope must be complete, partial_hierarchical, or unspecified")
    stages = config.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("stages must be a nonempty array")
    validated = [_validate_stage(stage) for stage in stages if isinstance(stage, dict)]
    if len(validated) != len(stages):
        raise ValueError("each stage must be an object")
    names = [stage["name"] for stage in validated]
    if len(set(names)) != len(names) or names != sorted(names, key=STAGE_ORDER.index):
        raise ValueError("stages must be unique and ordered availability, read, link")
    rules = _rules(config)
    results: List[Dict[str, object]] = []
    failed_stage = "none"
    diagnostic_kind = "none"
    for stage in validated:
        result = _run_stage(stage, rules)
        results.append(result)
        if result["status"] == "failed":
            failed_stage, diagnostic_kind = str(result["name"]), str(result["diagnostic_kind"])
            break
    statuses = {name: "not_attempted" for name in STAGE_ORDER}
    statuses.update({str(result["name"]): str(result["status"]) for result in results})
    return {
        "probe_api_version": PROBE_API_VERSION, "backend": backend, "scope": scope,
        "status": "failed" if failed_stage != "none" else "passed",
        "failed_stage": failed_stage, "diagnostic_kind": diagnostic_kind,
        "stage_status": statuses, "stages": results,
    }


def write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.config.is_file():
        parser.error("--config must name an existing regular file")
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        result = run_probe_config(config)
    except (ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    if args.output:
        write_json(args.output, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
