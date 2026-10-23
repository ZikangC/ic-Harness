#!/usr/bin/env python3
"""Run short, declared capability probes and emit deterministic JSON evidence.

Commands are argv arrays from JSON, never shell strings.  A probe passes only
when its process exits zero and all configured success markers occur in its
combined, bounded output.
"""
import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Mapping


PROBE_API_VERSION = 1
OUTPUT_TAIL_CHARS = 1200


def _tail(text: str) -> str:
    return text[-OUTPUT_TAIL_CHARS:]


def _validate_probe(raw: Mapping[str, object]) -> Dict[str, object]:
    name = raw.get("name")
    argv = raw.get("argv")
    if not isinstance(name, str) or not name:
        raise ValueError("probe.name must be a nonempty string")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError("probe.argv must be a nonempty array of nonempty strings")
    timeout = raw.get("timeout_seconds", 10)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("probe.timeout_seconds must be positive")
    markers = raw.get("success_markers", [])
    if not isinstance(markers, list) or not all(isinstance(item, str) and item for item in markers):
        raise ValueError("probe.success_markers must be an array of nonempty strings")
    cwd = raw.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd):
        raise ValueError("probe.cwd must be an optional nonempty string")
    return {"name": name, "argv": argv, "timeout_seconds": timeout,
            "success_markers": markers, "cwd": cwd}


def run_probe(raw: Mapping[str, object]) -> Dict[str, object]:
    probe = _validate_probe(raw)
    start = time.monotonic()
    output = ""
    timed_out = False
    exit_code = None
    launch_error = ""
    try:
        completed = subprocess.run(probe["argv"], cwd=probe["cwd"], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=probe["timeout_seconds"], check=False)
        output = completed.stdout or ""
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        output = (error.stdout or "") + (error.stderr or "")
    except OSError as error:
        launch_error = str(error)
    missing = [marker for marker in probe["success_markers"] if marker not in output]
    passed = exit_code == 0 and not timed_out and not launch_error and not missing
    return {
        "name": probe["name"],
        "status": "passed" if passed else "failed",
        "argv": probe["argv"],
        "exit_code": exit_code,
        "timed_out": timed_out,
        "elapsed_ms": round((time.monotonic() - start) * 1000),
        "missing_success_markers": missing,
        "launch_error": launch_error,
        "output_tail": _tail(output),
    }


def run_probe_config(config: Mapping[str, object]) -> Dict[str, object]:
    raw_probes = config.get("probes")
    if not isinstance(raw_probes, list) or not raw_probes:
        raise ValueError("config.probes must be a nonempty array")
    if not all(isinstance(probe, dict) for probe in raw_probes):
        raise ValueError("each config.probes entry must be an object")
    results: List[Dict[str, object]] = []
    names = set()
    for raw in raw_probes:
        result = run_probe(raw)  # type: ignore[arg-type]
        if result["name"] in names:
            raise ValueError("probe.name values must be unique")
        names.add(result["name"])
        results.append(result)
    failed = [result["name"] for result in results if result["status"] != "passed"]
    return {"probe_api_version": PROBE_API_VERSION,
            "status": "failed" if failed else "passed",
            "failed_probes": failed,
            "probes": results}


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="JSON probe configuration")
    parser.add_argument("--output", type=Path, help="write probe evidence JSON atomically")
    args = parser.parse_args()
    if not args.config.is_file():
        parser.error("--config must name an existing regular file")
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("config must be a JSON object")
        result = run_probe_config(config)
    except (ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    if args.output:
        _write_json(args.output, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
