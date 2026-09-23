#!/usr/bin/env python3
"""Create a reference vector, run a configured simulator, and score its dump."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class RunPaths:
    root: Path
    golden: Path
    image: Path
    image_bin: Path
    meta: Path
    dump: Path
    compile_log: Path
    sim_log: Path
    score_log: Path
    summary: Path


def load_config() -> dict:
    with (PROJECT / "harness.toml").open("rb") as handle:
        return tomllib.load(handle)


def run_command(command: list[str], *, cwd: Path, log_path: Path, timeout: int) -> int:
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + shlex.join(command) + "\n\n")
        try:
            process = subprocess.run(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                     text=True, timeout=timeout, check=False)
            return process.returncode
        except subprocess.TimeoutExpired:
            log.write(f"\nHARNESS_TIMEOUT after {timeout}s\n")
            return 124
        except FileNotFoundError as error:
            log.write(f"\nHARNESS_EXEC_ERROR: {error}\n")
            return 127


def parse_score(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {"pass": False, "reason": "scorer did not emit JSON"}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as error:
        return {"pass": False, "reason": f"invalid scorer JSON: {error}"}


def make_paths(task: str, run_id: str | None) -> RunPaths:
    stamp = run_id or f"{task}-{time.strftime('%Y%m%d-%H%M%S')}"
    root = PROJECT / "runs" / stamp
    root.mkdir(parents=True, exist_ok=False)
    return RunPaths(root, root / "golden.npz", root / "image.hex", root / "image.bin",
                    root / "meta.json", root / "rtl_memory.bin", root / "compile.log", root / "simulation.log",
                    root / "score.log", root / "summary.json")


def verification_cache_key(cfg: dict) -> str:
    """Hash every compile-time input, but intentionally exclude runtime images/dumps."""
    digest = hashlib.sha256()
    digest.update(repr(cfg["verification"]).encode())
    for directory in (PROJECT / "rtl", PROJECT / "tb"):
        for source in sorted(path for path in directory.rglob("*") if path.is_file()):
            digest.update(str(source.relative_to(PROJECT)).encode() + b"\0")
            digest.update(source.read_bytes())
    return digest.hexdigest()[:20]


def run_verification(cfg: dict, paths: RunPaths) -> tuple[int, str, bool]:
    verification = cfg["verification"]
    cache_key = verification_cache_key(cfg)
    cache_dir = PROJECT / verification.get("cache_dir", "build/verilator-cache") / cache_key
    executable = cache_dir / "obj_dir" / "Vtb_nano_golden_decoder_top"
    cache_hit = executable.is_file() and os.access(executable, os.X_OK)
    values = {
        "cache_dir": cache_dir,
        "image": paths.image,
        "dump": paths.dump,
        "verilator_jobs": int(verification.get("verilator_jobs", 32)),
    }
    timeout = int(verification.get("timeout_seconds", 900))
    if not cache_hit:
        build = verification["build_command"].format(**values)
        build_rc = run_command(["/bin/sh", "-lc", build], cwd=PROJECT,
                               log_path=paths.compile_log, timeout=timeout)
        if build_rc:
            return build_rc, cache_key, False
        if not executable.is_file():
            paths.compile_log.write_text(paths.compile_log.read_text(encoding="utf-8", errors="replace") +
                                         "\nHARNESS_BUILD_ERROR: Verilator finished without the expected executable\n",
                                         encoding="utf-8")
            return 1, cache_key, False
    else:
        paths.compile_log.write_text(
            f"VERILATOR_CACHE_HIT key={cache_key}\nexecutable={executable}\n",
            encoding="utf-8")
    simulation = verification["simulation_command"].format(**values)
    return run_command(["/bin/sh", "-lc", simulation], cwd=PROJECT,
                       log_path=paths.sim_log, timeout=timeout), cache_key, cache_hit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, help="task name used only to label runs")
    parser.add_argument("--seed", type=int, default=20260706)
    parser.add_argument("--prompt", type=int, choices=(0, 1), default=1)
    parser.add_argument("--group-size", type=int, choices=(64, 128), default=128)
    parser.add_argument("--seq-len", type=int, help="run only the first N causal tokens of the selected prompt")
    parser.add_argument("--run-id", help="unique directory name below runs/")
    parser.add_argument("--prepare-only", action="store_true", help="generate golden/image but skip simulation")
    args = parser.parse_args()

    cfg, paths = load_config(), make_paths(args.task, args.run_id)
    ref, acceptance = cfg["reference"], cfg["acceptance"]
    common = ["--seed", str(args.seed), "--prompt", str(args.prompt), "--group-size", str(args.group_size)]

    ref_rc = run_command([sys.executable, ref["script"], *common, "--output", str(paths.golden),
                          "--dump-intermediates"], cwd=PROJECT, log_path=paths.root / "reference.log", timeout=300)
    image_command = [sys.executable, ref["image_builder"], "--output", str(paths.image),
                     "--binary", str(paths.image_bin), "--meta", str(paths.meta), *common]
    if args.seq_len is not None:
        image_command.extend(["--seq-len", str(args.seq_len)])
    image_rc = run_command(image_command, cwd=PROJECT, log_path=paths.root / "image.log", timeout=300)
    summary: dict = {"task": args.task, "run_dir": str(paths.root), "seed": args.seed, "prompt": args.prompt,
                     "requested_seq_len": args.seq_len,
                     "group_size": args.group_size, "thresholds": acceptance,
                     "reference_exit": ref_rc, "image_exit": image_rc, "pass": False}
    if ref_rc or image_rc:
        summary["reason"] = "reference/image generation failed"
    elif args.prepare_only:
        summary["reason"] = "prepared only"
    else:
        sim_rc, cache_key, cache_hit = run_verification(cfg, paths)
        summary["verilator_cache_key"] = cache_key
        summary["verilator_cache_hit"] = cache_hit
        summary["simulation_exit"] = sim_rc
        if sim_rc:
            summary["reason"] = "simulation command failed; see simulation.log"
        elif not paths.dump.is_file():
            summary["reason"] = "simulation exited successfully but did not create rtl_memory.bin"
        else:
            score_rc = run_command([sys.executable, ref["scorer"], "--result", str(paths.dump), "--meta", str(paths.meta),
                                    *common], cwd=PROJECT, log_path=paths.score_log, timeout=120)
            score = parse_score(paths.score_log)
            summary.update(score)
            summary["score_exit"] = score_rc
            summary["pass"] = bool(score.get("pass")) and score_rc == 0
    paths.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
