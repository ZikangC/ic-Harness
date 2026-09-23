#!/usr/bin/env python3
"""Read and summarize an existing remote OpenSTA report without starting EDA.

The PPA worker deliberately transfers only compact feedback to the local host.
When a strategic decision needs path-level facts, this helper reads the bounded
``STA_TIMING_BEGIN..END`` section from one already-completed, immutable remote
run.  It never invokes Yosys, OpenROAD, Verilator, or modifies the server.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from environment import REMOTE_HOST, REMOTE_PROJECT


ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
MAX_RETURNED_PATHS = 6
MAX_PATH_QUERY_LINES = 240
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
PPA_RUN_ID_RE = re.compile(r"02_yosys_ppa--\d{8}-\d{6}--\d+\Z")
NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _feedback_fields(path: Path) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key.strip()] = value.strip()
    except OSError:
        pass
    return fields


def latest_measured_sta_run(runs: Path = RUNS) -> Optional[str]:
    """Return the newest local PPA run that recorded a real linked STA path."""
    candidates = sorted(runs.glob("02_yosys_ppa--*/feedback.txt"), key=lambda path: path.stat().st_mtime,
                        reverse=True)
    for feedback in candidates:
        fields = _feedback_fields(feedback)
        if not fields.get("sta_timing_measured", "").startswith("yes") or not fields.get("sta_link_result", "").startswith("ok"):
            continue
        run_id = feedback.parent.name
        if RUN_ID_RE.fullmatch(run_id):
            return run_id
    return None


def _first_number(line: str) -> Optional[float]:
    match = NUMBER_RE.search(line)
    return float(match.group(0)) if match else None


def _record_path(lines: Iterable[str]) -> Optional[Dict[str, object]]:
    record: Dict[str, object] = {}
    arrivals: List[float] = []
    required: List[float] = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("Startpoint:"):
            record["startpoint"] = line.partition(":")[2].strip()
        elif line.startswith("Endpoint:"):
            record["endpoint"] = line.partition(":")[2].strip()
        elif line.startswith("Path Group:"):
            record["path_group"] = line.partition(":")[2].strip()
        elif "data arrival time" in line:
            value = _first_number(line)
            if value is not None:
                arrivals.append(value)
        elif "data required time" in line:
            value = _first_number(line)
            if value is not None:
                required.append(value)
        elif "slack" in line:
            value = _first_number(line)
            if value is not None:
                record["slack_ns"] = value
    if not record.get("startpoint") or not record.get("endpoint"):
        return None
    # OpenSTA prints arrival/required values once in their positive report
    # sections and may print a signed accounting value later.  The first one
    # is the physical quantity associated with this path.
    if arrivals:
        record["arrival_ns"] = arrivals[0]
    if required:
        record["required_ns"] = required[0]
    return record


def parse_timing_paths(text: str) -> List[Dict[str, object]]:
    """Parse the bounded report into path-associated, not globally mixed, facts."""
    paths: List[Dict[str, object]] = []
    current: List[str] = []
    in_report = False
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if line.strip() == "STA_TIMING_BEGIN":
            in_report = True
            continue
        if line.strip() == "STA_TIMING_END":
            break
        if not in_report and "Startpoint:" not in line:
            continue
        if line.lstrip().startswith("Startpoint:"):
            previous = _record_path(current)
            if previous is not None:
                paths.append(previous)
            current = [line]
        elif current:
            current.append(line)
    previous = _record_path(current)
    if previous is not None:
        paths.append(previous)
    return paths


def timing_path_blocks(text: str) -> List[Dict[str, object]]:
    """Keep every raw timing-report block paired with its parsed summary."""
    blocks: List[Dict[str, object]] = []
    current: List[str] = []
    in_report = False
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if line.strip() == "STA_TIMING_BEGIN":
            in_report = True
            continue
        if line.strip() == "STA_TIMING_END":
            break
        if not in_report:
            continue
        if line.lstrip().startswith("Startpoint:"):
            parsed = _record_path(current)
            if parsed is not None:
                blocks.append({"summary": parsed, "report": "\n".join(current).rstrip()})
            current = [line]
        elif current:
            current.append(line)
    parsed = _record_path(current)
    if parsed is not None:
        blocks.append({"summary": parsed, "report": "\n".join(current).rstrip()})
    return blocks


def _remote_timing_text(run_id: str, *, timeout_seconds: int = 20) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("invalid immutable run id")
    log = f"{REMOTE_PROJECT}/runs/{run_id}/sta.log"
    remote_command = "sed -n '/STA_TIMING_BEGIN/,/STA_TIMING_END/p' " + shlex.quote(log)
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", REMOTE_HOST, remote_command],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_seconds, check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"ssh exit={result.returncode}"
        raise RuntimeError(f"remote immutable STA read failed: {detail[:500]}")
    if "STA_TIMING_BEGIN" not in result.stdout or "STA_TIMING_END" not in result.stdout:
        raise RuntimeError("remote sta.log has no complete STA_TIMING_BEGIN..END block")
    return result.stdout


def inspect_latest_measured_sta(*, runs: Path = RUNS) -> Dict[str, object]:
    """Read one existing run and return a small, auditable timing evidence packet."""
    run_id = latest_measured_sta_run(runs)
    if run_id is None:
        return {"status": "unavailable", "reason": "no local feedback with linked measured STA"}
    try:
        paths = parse_timing_paths(_remote_timing_text(run_id))
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as error:
        return {"status": "unavailable", "source_run_id": run_id, "reason": str(error)}
    if not paths:
        return {"status": "unavailable", "source_run_id": run_id, "reason": "no parseable timing paths"}
    by_slack = [path for path in paths if isinstance(path.get("slack_ns"), float)]
    by_arrival = [path for path in paths if isinstance(path.get("arrival_ns"), float)]
    return {
        "status": "ok",
        "source_run_id": run_id,
        "source": f"remote immutable runs/{run_id}/sta.log:STA_TIMING_BEGIN..END",
        "path_count": len(paths),
        "worst_slack_path": min(by_slack, key=lambda path: float(path["slack_ns"])) if by_slack else None,
        "max_arrival_path": max(by_arrival, key=lambda path: float(path["arrival_ns"])) if by_arrival else None,
        "paths": paths[:MAX_RETURNED_PATHS],
        "truncated_path_count": max(0, len(paths) - MAX_RETURNED_PATHS),
    }


def query_sta_timing_paths(run_id: str, selector: str, max_lines: int) -> Dict[str, object]:
    """Return bounded raw paths from one immutable remote STA report.

    The small selector vocabulary prevents a model request from becoming an
    arbitrary remote shell command.  This function never starts EDA or writes
    to the remote host.
    """
    if not PPA_RUN_ID_RE.fullmatch(run_id):
        return {"status": "invalid_request", "reason": "run_id must name one Task-02 PPA run"}
    if selector not in {"worst_slack", "max_arrival", "all"}:
        return {"status": "invalid_request", "reason": "selector must be worst_slack|max_arrival|all"}
    if not isinstance(max_lines, int) or not 1 <= max_lines <= MAX_PATH_QUERY_LINES:
        return {"status": "invalid_request", "reason": f"max_lines must be 1..{MAX_PATH_QUERY_LINES}"}
    try:
        blocks = timing_path_blocks(_remote_timing_text(run_id))
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as error:
        return {"status": "unavailable", "source_run_id": run_id, "reason": str(error)}
    if not blocks:
        return {"status": "unavailable", "source_run_id": run_id, "reason": "no parseable timing paths"}
    by_slack = [block for block in blocks if isinstance(block["summary"].get("slack_ns"), float)]
    by_arrival = [block for block in blocks if isinstance(block["summary"].get("arrival_ns"), float)]
    if selector == "worst_slack":
        selected = [min(by_slack, key=lambda block: float(block["summary"]["slack_ns"]))] if by_slack else []
    elif selector == "max_arrival":
        selected = [max(by_arrival, key=lambda block: float(block["summary"]["arrival_ns"]))] if by_arrival else []
    else:
        selected = blocks
    used = 0
    bounded: List[Dict[str, object]] = []
    for block in selected:
        lines = str(block["report"]).splitlines()
        remaining = max_lines - used
        if remaining <= 0:
            break
        kept = lines[:remaining]
        bounded.append({"summary": block["summary"], "report": "\n".join(kept),
                        "report_truncated": len(kept) < len(lines)})
        used += len(kept)
        if len(kept) < len(lines):
            break
    return {
        "status": "ok",
        "source_run_id": run_id,
        "source": f"remote immutable runs/{run_id}/sta.log:STA_TIMING_BEGIN..END",
        "selector": selector,
        "max_lines": max_lines,
        "available_path_count": len(blocks),
        "returned_path_count": len(bounded),
        "path_blocks": bounded,
        "truncated": len(bounded) < len(selected) or any(block["report_truncated"] for block in bounded),
    }


if __name__ == "__main__":
    print(json.dumps(inspect_latest_measured_sta(), ensure_ascii=False, indent=2))
