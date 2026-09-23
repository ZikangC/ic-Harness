#!/usr/bin/env python3
"""Stateless Task-02 verification operations for one Optimization candidate.

The Optimization Skill selects the operation and owns every lifecycle
transition.  This project port only validates candidate identity, runs the
requested project commands, and records raw facts.  It never edits RTL, calls
a model, resolves STA scope, invokes Closure governance, retries a campaign,
or decides acceptance/rollback.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Mapping

from rtl_skill_bridge import TASK02_MEASUREMENT_INPUTS, content_fingerprint


ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
INFRA_EXITS = {75, 76}


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def run(command: list[str], log: Path) -> int:
    with log.open("w", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(command) + "\n\n")
        process = subprocess.Popen(
            command, cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=handle, stderr=subprocess.STDOUT)
        return process.wait()


def configured_partitions() -> set[str]:
    names: set[str] = set()
    current_block = False
    for line in (ROOT / "harness/ppa.toml").read_text(
            encoding="utf-8").splitlines():
        if line.strip() == "[[partitions]]":
            current_block = True
            continue
        if current_block:
            match = re.match(
                r'\s*name\s*=\s*"([A-Za-z0-9_.-]+)"\s*$', line)
            if match:
                names.add(match.group(1))
                current_block = False
    return names


def latest_measurement_scope() -> str:
    candidates = list(RUNS.glob("02_yosys_ppa--*/feedback.txt"))
    if not candidates:
        return "UNKNOWN"
    path = max(candidates, key=lambda item: item.stat().st_mtime_ns)
    match = re.search(
        r"^measurement_scope=([A-Z_]+)$",
        path.read_text(encoding="utf-8", errors="replace"),
        flags=re.MULTILINE)
    return match.group(1) if match else "UNKNOWN"


def candidate_files(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    relative = Path(args.rtl_ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("rtl-ref must be project-relative")
    current = (ROOT / relative).resolve()
    checkpoint = args.checkpoint.expanduser().resolve()
    baseline = (checkpoint / "preimage" / relative).resolve()
    try:
        current.relative_to(ROOT)
        checkpoint.relative_to(ROOT)
        baseline.relative_to(checkpoint)
    except ValueError as error:
        raise ValueError("candidate paths escape the project or checkpoint") from error
    if not current.is_file() or not baseline.is_file():
        raise ValueError("candidate RTL or checkpoint preimage is missing")
    return relative, current, baseline


def candidate_diff(relative: Path, current: Path, baseline: Path) -> str:
    before = baseline.read_text(
        encoding="utf-8", errors="replace").splitlines(keepends=True)
    after = current.read_text(
        encoding="utf-8", errors="replace").splitlines(keepends=True)
    return "".join(difflib.unified_diff(
        before, after, fromfile=f"baseline/{relative.as_posix()}",
        tofile=f"current/{relative.as_posix()}"))


def write_result(root: Path, value: Mapping[str, object]) -> None:
    (root / "result.json").write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def common_result(args: argparse.Namespace, root: Path, relative: Path,
                  fingerprint: str, changes: str) -> dict[str, object]:
    return {
        "kind": "optimization_verification",
        "run_id": root.name,
        "time": now(),
        "operation": args.operation,
        "campaign_id": args.campaign_id,
        "module": args.module,
        "rtl_changed_paths": [relative.as_posix()] if changes else [],
        "rtl_fingerprint": fingerprint,
        "verification_context_fingerprint": content_fingerprint(
            ROOT, TASK02_MEASUREMENT_INPUTS),
        "managed_inputs_changed": False,
        "changes": (root / "changes.diff").relative_to(ROOT).as_posix(),
    }


def functional_preflight(args: argparse.Namespace, root: Path,
                         base: dict[str, object]) -> int:
    quick = run([
        "bash", "scripts/remote_task02_functional.sh", "--prompt", "1",
        "--seq-len", str(args.quick_seq_len)], root / "functional_quick.log")
    medium = full = preflight = None
    if quick == 0:
        medium = run([
            "bash", "scripts/remote_task02_functional.sh", "--prompt", "1",
            "--seq-len", str(args.medium_seq_len)], root / "functional_medium.log")
    if quick == 0 and medium == 0:
        full = run([
            "bash", "scripts/remote_task02_functional.sh", "--prompt", "0",
            "--seq-len", "48"], root / "functional_full.log")
    if quick == 0 and medium == 0 and full == 0:
        preflight = run([
            "bash", "scripts/remote_yosys_round.sh", "--preflight",
            "--timeout-seconds", str(args.preflight_timeout_seconds)],
            root / "preflight.log")
    exits = [value for value in (quick, medium, full, preflight)
             if isinstance(value, int)]
    if any(value in INFRA_EXITS for value in exits):
        status = "infra_uncertain"
    elif any(value not in {0, *INFRA_EXITS}
             for value in (quick, medium, full) if value is not None):
        status = "functional_failed"
    elif preflight != 0:
        status = "verification_failed"
    else:
        status = "functional_passed"
    result = {
        **base,
        "kind": ("infrastructure_uncertain"
                 if status == "infra_uncertain" else base["kind"]),
        "verification_status": status,
        "functional_quick_exit": quick,
        "functional_medium_exit": medium,
        "mandatory_full_exit": full,
        "preflight_exit": preflight,
        "functional_evidence_reused": False,
        "functional_evidence_source": "fresh_reverification",
        "preflight_evidence_reused": False,
        "preflight_evidence_source": "fresh_reverification",
    }
    write_result(root, result)
    return next((value for value in exits if value in INFRA_EXITS), 0)


def measurement(args: argparse.Namespace, root: Path,
                base: dict[str, object]) -> int:
    context_fingerprint = str(base["verification_context_fingerprint"])
    fingerprint = str(base["rtl_fingerprint"])
    proof_valid = (
        args.functional_proof_rtl_sha256 == fingerprint
        and args.functional_proof_context_sha256 == context_fingerprint
        and args.preflight_proof_rtl_sha256 == fingerprint
        and args.preflight_proof_context_sha256 == context_fingerprint)
    valid_partitions = configured_partitions()
    partitions = list(dict.fromkeys(args.measurement_partition))
    contract_valid = (
        proof_valid and bool(partitions)
        and all(name in valid_partitions for name in partitions)
        and args.measurement_timeout_seconds > 0)
    if not contract_valid:
        write_result(root, {
            **base,
            "verification_status": "verification_contract_invalid",
            "measurement_partitions": partitions,
            "measurement_timeout_seconds": args.measurement_timeout_seconds,
            "proof_valid": proof_valid,
            "partition_contract_valid": bool(partitions) and all(
                name in valid_partitions for name in partitions),
        })
        return 0
    command = [
        "bash", "scripts/remote_yosys_round.sh", "--timeout-seconds",
        str(args.measurement_timeout_seconds), "--partitions", ",".join(partitions)]
    ppa_exit = run(command, root / "ppa.log")
    status = ("infra_uncertain" if ppa_exit in INFRA_EXITS
              else "measurement_executed")
    write_result(root, {
        **base,
        "kind": ("infrastructure_uncertain"
                 if status == "infra_uncertain" else base["kind"]),
        "verification_status": status,
        "ppa_exit": ppa_exit,
        "ppa_log": (root / "ppa.log").relative_to(ROOT).as_posix(),
        "measurement_scope": (
            "UNKNOWN" if ppa_exit in INFRA_EXITS else latest_measurement_scope()),
        "measurement_partitions": partitions,
        "measurement_timeout_seconds": args.measurement_timeout_seconds,
        "measurement_fingerprint": context_fingerprint,
    })
    return ppa_exit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", required=True,
                        choices=("functional_preflight", "measure_candidate"))
    parser.add_argument("--module", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--rtl-ref", required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--expected-rtl-sha256", required=True)
    parser.add_argument("--functional-proof-rtl-sha256")
    parser.add_argument("--functional-proof-context-sha256")
    parser.add_argument("--preflight-proof-rtl-sha256")
    parser.add_argument("--preflight-proof-context-sha256")
    parser.add_argument("--measurement-partition", action="append", default=[])
    parser.add_argument("--measurement-timeout-seconds", type=int, default=180)
    parser.add_argument("--quick-seq-len", type=int, default=8)
    parser.add_argument("--medium-seq-len", type=int, default=16)
    parser.add_argument("--preflight-timeout-seconds", type=int, default=600)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.module):
        parser.error("--module must be one identifier")
    if not re.fullmatch(r"[0-9a-f]{64}", args.expected_rtl_sha256):
        parser.error("--expected-rtl-sha256 must be 64 lowercase hex characters")
    if args.quick_seq_len < 1 or args.medium_seq_len < args.quick_seq_len:
        parser.error("invalid functional sequence lengths")

    suffix = "functional" if args.operation == "functional_preflight" else "measurement"
    root = RUNS / (
        f"agent-02_yosys_ppa--{dt.datetime.now():%Y%m%d-%H%M%S}--"
        f"optimization-{suffix}-{os.getpid()}")
    root.mkdir(parents=True)
    try:
        relative, current, baseline = candidate_files(args)
    except (OSError, ValueError) as error:
        write_result(root, {
            "kind": "infrastructure_uncertain", "run_id": root.name,
            "time": now(), "operation": args.operation,
            "campaign_id": args.campaign_id, "module": args.module,
            "verification_status": "infra_uncertain",
            "error": f"{type(error).__name__}: {error}",
        })
        return 76
    changes = candidate_diff(relative, current, baseline)
    (root / "changes.diff").write_text(changes, encoding="utf-8")
    fingerprint = hashlib.sha256(current.read_bytes()).hexdigest()
    base = common_result(args, root, relative, fingerprint, changes)
    if not changes:
        write_result(root, {**base, "verification_status": "no_change"})
        return 0
    if fingerprint != args.expected_rtl_sha256:
        write_result(root, {
            **base, "verification_status": "candidate_identity_mismatch",
            "expected_rtl_fingerprint": args.expected_rtl_sha256,
        })
        return 0
    return (functional_preflight(args, root, base)
            if args.operation == "functional_preflight"
            else measurement(args, root, base))


if __name__ == "__main__":
    raise SystemExit(main())
