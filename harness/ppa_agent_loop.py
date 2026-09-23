#!/usr/bin/env python3
"""Task-02 closed loop: baseline, read-only plan, bounded edit, real measurement."""
import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from environment import REMOTE_HOST, REMOTE_PROJECT
from design_context_adapter import design_context_slice
from rtl_skill_bridge import (CLOSURE_GOVERNANCE_ENV, STA_SCOPE_ENV, TASK02_MEASUREMENT_INPUTS, SkillGateError,
                              closure_governance_result,
                              conservative_rtl_change_packet, content_fingerprint,
                              default_skill_path, sta_scope_decision)
from task02_project_adapter import Task02ProjectAdapter

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks/02_yosys_ppa.md"
REPORT = ROOT / "reports/02_yosys_ppa/progress.md"
STATUS = ROOT / "status/task02_agent.status"
STRATEGY_ARCHIVE = ROOT / "runs/strategy_archive"
REMOTE = REMOTE_HOST
REMOTE_STATUS = f"{REMOTE_PROJECT}/status/task02_agent.status"
MANAGED_PATHS = (Path("rtl"), Path("harness/ppa_measure.ys"), Path("harness/ppa_preflight.ys"), Path("harness/ppa.toml"), Path("scripts/remote_yosys_round.sh"))
INFRA_EXIT = 75
INFRA_UNCERTAIN_EXIT = 76
MEASUREMENT_TIMEOUT_EXIT = 124
INFRA_RETRIES = 2
DEFAULT_MEASUREMENT_BUDGET_SECONDS = 180
DEFAULT_MEASUREMENT_BUDGET_LEVELS = (180, 600, 1800, 3600, 7200)
FULL_MEASUREMENT_MIN_BUDGET_SECONDS = 1800
MAX_TARGETED_PARTITIONS = 4
TARGETED_MEASUREMENTS_BEFORE_FULL = 2
# A change to any RTL needs more than the 8-token smoke test.  These filename
# tokens identify control/data-path changes that also require the full prompt-0
# regression before their PPA numbers may guide another RTL edit.
FULL_REGRESSION_RTL_TOKENS = (
    "fsm", "cache", "arbiter", "controller", "loader", "stream", "datapath",
    "adapter", "attention", "matvec", "expert", "norm", "conv", "residual", "kda", "descriptor",
)
ACTIVE_PROCESS = None  # type: Optional[subprocess.Popen]
CURRENT_STAGE = "starting"
CURRENT_ATTEMPT = "unknown"
CLOSURE_GOVERNANCE_ROOT = default_skill_path("rtl-closure-governance", CLOSURE_GOVERNANCE_ENV)
MEASUREMENT_BUDGET_CEILING_SECONDS = max(DEFAULT_MEASUREMENT_BUDGET_LEVELS)


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def set_stage(stage: str, attempt, detail: str = "") -> None:
    global CURRENT_STAGE, CURRENT_ATTEMPT
    CURRENT_STAGE, CURRENT_ATTEMPT = stage, attempt
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    content = f"task=02_yosys_ppa\nstage={stage}\nattempt={attempt}\nupdated_at={now()}\ndetail={detail}\n"
    STATUS.write_text(content, encoding="utf-8")
    # A tiny status packet makes server-side monitoring useful without returning logs or artifacts.
    remote_command = f"mkdir -p {shlex.quote(str(Path(REMOTE_STATUS).parent))}; printf %s {shlex.quote(content)} > {shlex.quote(REMOTE_STATUS)}"
    try:
        subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", REMOTE, remote_command],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def run(command: List[str], log: Path, *, record_command: bool = True) -> int:
    global ACTIVE_PROCESS
    with log.open("w", encoding="utf-8") as handle:
        if record_command:
            handle.write("$ " + " ".join(command) + "\n\n")
        else:
            handle.write("# OMP 可见回复（完整 prompt 见任务卡与 result.json，不在本日志重复保存）\n\n")
        ACTIVE_PROCESS = subprocess.Popen(
            command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=handle,
            stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return ACTIVE_PROCESS.wait()
        finally:
            ACTIVE_PROCESS = None


def stop_active_process(signum: int, _frame: object) -> None:
    """Stop the controller-owned child process tree before leaving the controller."""
    if ACTIVE_PROCESS is not None and ACTIVE_PROCESS.poll() is None:
        try:
            os.killpg(ACTIVE_PROCESS.pid, signum)
        except ProcessLookupError:
            pass
    cancel_detail = "remote cancellation not needed"
    if CURRENT_STAGE.startswith("functional") or CURRENT_STAGE in {
            "ppa", "baseline", "synthesis_preflight"}:
        try:
            completed = subprocess.run(
                ["bash", "scripts/remote_cancel.sh"], cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                timeout=45, check=False)
            cancel_detail = f"remote_cancel_exit={completed.returncode}"
        except (OSError, subprocess.TimeoutExpired) as error:
            cancel_detail = f"remote_cancel_uncertain={type(error).__name__}"
    try:
        set_stage("cancelled", CURRENT_ATTEMPT,
                  f"controller received signal {signum} while stage={CURRENT_STAGE}; {cancel_detail}")
    except OSError:
        pass
    raise SystemExit(128 + signum)


def run_with_infra_retry(command: List[str], log: Path) -> int:
    """Retry transport-only failures without spending another AI edit round."""
    rc = INFRA_EXIT
    for attempt in range(1, INFRA_RETRIES + 1):
        attempt_log = log if attempt == 1 else log.with_name(f"{log.stem}.infra_retry{attempt}{log.suffix}")
        rc = run(command, attempt_log)
        if rc != INFRA_EXIT:
            return rc
    return rc


def tail(path: Path, limit: int = 8000) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[-limit:] if path.exists() else "（无输出）"


def concise_agent_reply(text: str, limit: int) -> str:
    """Keep progress readable; raw OMP logs remain in the per-run directory."""
    reply = text.rsplit("Working...", 1)[-1].strip()
    if len(reply) > limit:
        reply = reply[:limit].rstrip() + "\n\n（完整 AI 回复见本轮 planner.log / executor.log。）"
    return reply or "AI 未产生可见回复。"


def agent_failure_reason(log: Path, exit_code: int) -> Optional[str]:
    """Classify terminal OMP/model failures that invalidate a plan or edit.

    A controller-owned verification run is meaningful only after a successful
    planner and executor.  In particular, do not treat a quota/runtime error
    as an empty plan and then continue into functional or PPA verification.
    """
    text = tail(log).lower()
    signatures = (
        ("token plan quota exhausted", "token_plan_quota_exhausted"),
        ("\"type\":\"runtime_error\"", "model_runtime_error"),
        ("\"type\": \"runtime_error\"", "model_runtime_error"),
        ("\"error\":{\"message\"", "model_error_response"),
        ("\"error\": {\"message\"", "model_error_response"),
    )
    for needle, reason in signatures:
        if needle in text:
            return reason
    if exit_code != 0:
        return f"omp_exit_{exit_code}"
    return None


def snapshot(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for relative in MANAGED_PATHS:
        source, target = ROOT / relative, destination / relative
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def restore(snapshot_root: Path) -> None:
    for relative in MANAGED_PATHS:
        source, target = snapshot_root / relative, ROOT / relative
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def restore_one(snapshot_root: Path, relative: Path) -> None:
    source, target = snapshot_root / relative, ROOT / relative
    if not source.is_file():
        raise OSError(f"candidate checkpoint source missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def diff(before: Path, after: Path) -> str:
    result = []  # type: List[str]
    old_files = {path.relative_to(before) for path in before.rglob("*") if path.is_file()}
    new_files = {path.relative_to(after) for path in after.rglob("*") if path.is_file()}
    for relative in sorted(old_files | new_files):
        old, new = before / relative, after / relative
        if old.exists() and new.exists() and old.read_bytes() == new.read_bytes():
            continue
        old_lines = old.read_text(errors="replace").splitlines(True) if old.exists() else []
        new_lines = new.read_text(errors="replace").splitlines(True) if new.exists() else []
        result.extend(difflib.unified_diff(old_lines, new_lines, fromfile=f"before/{relative}", tofile=f"after/{relative}"))
    return "".join(result)


def changed_relative_paths(before: Path, after: Path) -> List[Path]:
    """Return managed files whose content changed between two snapshots."""
    old_files = {path.relative_to(before) for path in before.rglob("*") if path.is_file()}
    new_files = {path.relative_to(after) for path in after.rglob("*") if path.is_file()}
    return [relative for relative in sorted(old_files | new_files)
            if not (before / relative).exists() or not (after / relative).exists()
            or (before / relative).read_bytes() != (after / relative).read_bytes()]


def regression_requirements(changed_paths: List[Path]) -> Tuple[List[str], bool]:
    """Classify post-edit functional gates from actual changed RTL paths."""
    rtl_paths = [path.as_posix() for path in changed_paths if path.parts and path.parts[0] == "rtl"]
    needs_full = any(any(token in Path(path).name.lower() for token in FULL_REGRESSION_RTL_TOKENS)
                     for path in rtl_paths)
    return rtl_paths, needs_full


def functional_command(seq_len: int) -> List[str]:
    return ["bash", "scripts/remote_task02_functional.sh", "--prompt", "1", "--seq-len", str(seq_len)]


def full_functional_command() -> List[str]:
    return ["bash", "scripts/remote_task02_functional.sh", "--prompt", "0", "--seq-len", "48"]


def preflight_command(timeout_seconds: int) -> List[str]:
    return ["bash", "scripts/remote_yosys_round.sh", "--preflight", "--timeout-seconds", str(timeout_seconds)]


def measurement_command(timeout_seconds: int, partitions: Optional[List[str]] = None) -> List[str]:
    command = ["bash", "scripts/remote_yosys_round.sh", "--timeout-seconds", str(timeout_seconds)]
    if partitions:
        command += ["--partitions", ",".join(partitions)]
    return command


def configured_partitions() -> List[str]:
    """Read exact PPA partition names without depending on Python's TOML version."""
    names, in_partition = [], False
    for line in (ROOT / "harness/ppa.toml").read_text(encoding="utf-8").splitlines():
        if line.strip() == "[[partitions]]":
            in_partition = True
            continue
        if in_partition:
            match = re.match(r'\s*name\s*=\s*"([A-Za-z0-9_.-]+)"\s*$', line)
            if match:
                names.append(match.group(1))
                in_partition = False
    return names


def configured_top_level_blackboxes() -> List[str]:
    """Read the current top-level compute blackbox list for hard expansion mode."""
    in_partition = False
    current_name: Optional[str] = None
    for line in (ROOT / "harness/ppa.toml").read_text(encoding="utf-8").splitlines():
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
        if current_name == "top_level_sta":
            blackbox_match = re.match(r'\s*blackbox\s*=\s*"([^"]*)"\s*$', line)
            if blackbox_match:
                return [item for item in blackbox_match.group(1).split() if item]
    return []


def rtl_file_for_module(module: str) -> Optional[Path]:
    """Resolve one module declaration to one project-relative RTL source."""
    declaration = re.compile(rf"^\s*module\s+{re.escape(module)}\b", re.MULTILINE)
    matches = []
    for path in sorted((ROOT / "rtl").rglob("*")):
        if path.is_file() and path.suffix.lower() in (".v", ".sv"):
            try:
                if declaration.search(path.read_text(encoding="utf-8", errors="replace")):
                    matches.append(path.relative_to(ROOT))
            except OSError:
                continue
    return matches[0] if len(matches) == 1 else None


def configured_partition_timeouts() -> Dict[str, int]:
    """Read each partition's declared tool budget (``timeout_s``) from harness/ppa.toml.

    ``configured_partitions`` already proves the partition block grammar; this
    mirrors it to also capture the per-partition tool window the round script
    reads.  A partition's ``timeout_s`` is its authoritative tool budget, and a
    smoke/targeted round must not be handed a smaller controller budget that
    would truncate it (Round-78 evidence: a 180 s top_level_sta smoke capped the
    closure partition below its 600 s window and left build/ppa_closure_top.v
    empty -> STA_NO_COMBINED_NETLIST).
    """
    timeouts: Dict[str, int] = {}
    in_partition = False
    current: Optional[str] = None
    for line in (ROOT / "harness/ppa.toml").read_text(encoding="utf-8").splitlines():
        if line.strip() == "[[partitions]]":
            in_partition = True
            current = None
            continue
        if not in_partition:
            continue
        name_match = re.match(r'\s*name\s*=\s*"([A-Za-z0-9_.-]+)"\s*$', line)
        if name_match:
            current = name_match.group(1)
            continue
        if current is not None:
            timeout_match = re.match(r'\s*timeout_s\s*=\s*(\d+)\s*$', line)
            if timeout_match:
                timeouts[current] = int(timeout_match.group(1))
    return timeouts


def partition_budget_floor(partitions: List[str], levels: Tuple[int, ...]) -> int:
    """Smallest permitted budget that covers every selected partition's ``timeout_s``.

    ``remote_yosys_round.sh`` caps each partition to the round budget, so a
    targeted/smoke round must carry at least the largest declared ``timeout_s``
    of its partitions.  The result is raised to the next permitted ladder rung.
    """
    timeouts = configured_partition_timeouts()
    required = max((timeouts.get(name, 0) for name in partitions), default=0)
    return next((level for level in levels if level >= required), levels[-1])


def planning_policy_from_reply(planner: str, valid_partitions: List[str]) -> Tuple[str, str, List[str], str]:
    """Parse a deliberately small, controller-validated measurement contract.

    Missing or malformed directives remain safe: preserve the historic full
    measurement instead of guessing a partition from prose.
    """
    change_match = re.search(r"^`?\s*change_mode:\s*(local|architecture)\s*`?\s*$", planner, re.MULTILINE)
    measurement_match = re.search(r"^`?\s*measurement_mode:\s*(targeted|full)\s*`?\s*$", planner, re.MULTILINE)
    affected_match = re.search(r"^`?\s*affected_partitions:\s*(.*?)\s*`?\s*$", planner, re.MULTILINE)
    change_mode = change_match.group(1) if change_match else "local"
    measurement_mode = measurement_match.group(1) if measurement_match else "full"
    raw = affected_match.group(1).strip() if affected_match else ""
    requested = [item.strip() for item in raw.split(",") if item.strip() and item.strip().lower() != "none"]
    valid = set(valid_partitions)
    if measurement_mode != "targeted":
        return change_mode, "full", [], "planner_full_or_default"
    if change_mode != "local":
        return change_mode, "full", [], "controller_architecture_requires_full"
    if (not requested or len(requested) > MAX_TARGETED_PARTITIONS or len(set(requested)) != len(requested)
            or any(item not in valid for item in requested)):
        return change_mode, "full", [], "controller_invalid_targeted_partition_request"
    return change_mode, "targeted", requested, "planner_targeted_request"


def targeted_measurements_since_full() -> int:
    """Count completed targeted measurements since the last completed full one."""
    candidates = sorted(agent_result_paths(), key=lambda path: path.stat().st_mtime, reverse=True)
    count = 0
    for path in candidates:
        result = json.loads(path.read_text(encoding="utf-8"))
        plan = result.get("measurement_plan_mode")
        ppa_rc = result.get("ppa_exit")
        if plan == "full" and ppa_rc not in (None, INFRA_EXIT):
            return count
        if plan == "targeted" and ppa_rc not in (None, INFRA_EXIT):
            count += 1
    return count


def decide_measurement_plan(
    planned_change_mode: str,
    planned_measurement_mode: str,
    planned_partitions: List[str],
    planned_measurement_source: str,
    rtl_changed_paths: List[str],
    non_rtl_changed_paths: List[str],
    force_full: bool,
    targeted_count: int,
    sta_closure_gate: bool = False,
) -> Tuple[str, List[str], str]:
    """Controller-owned measurement plan, independent of AI prose.

    Full configured-partition coverage remains the safe default.  Targeted PPA
    is permitted only for a real, single-file local RTL hypothesis (with a
    periodic full milestone).  A script-only change -- a measurement harness or
    scheduler edit with zero RTL edits -- never forces a full repeat: when the
    planner supplies a validated targeted partition request, the controller runs
    exactly those partitions (one requested partition as a single-netlist smoke,
    several as a targeted set), so a closure/STA harness fix actually reaches the
    partition it modified.  An edit round that produced no managed-file change at
    all (no RTL edit and no harness/script edit) normally carries no new
    evidence, so it routes to the 16-token functional counting point; the one
    exception is an explicit, validated targeted PPA request from the planner to
    re-verify an already-landed measurement/closure hypothesis (for example a
    previously applied harness fix whose PPA/STA verification never completed).
    That is a concrete PPA hypothesis, not a pure counting point, so it is
    honored as a targeted measurement of the requested partitions instead of
    being silently downgraded to the counting point.
    """
    # A real top-level STA closure failure is a prerequisite failure, not an
    # optimization candidate.  Do not spend a generic full-PPA milestone or a
    # functional-counting point while a deterministic audit says the closure
    # itself cannot be read/linked/timed.  One top_level_sta smoke (at its
    # partition-timeout budget) is the smallest falsifiable verification.
    if sta_closure_gate:
        return "smoke", ["top_level_sta"], "controller_sta_closure_gate"
    if force_full:
        return "full", [], "supervisor_forced_full_milestone"
    if (not rtl_changed_paths and not non_rtl_changed_paths
            and planned_measurement_mode != "targeted"):
        return "functional_counting", [], "controller_functional_counting_point"
    if planned_measurement_mode != "targeted":
        return "full", [], planned_measurement_source
    if planned_change_mode == "local" and len(rtl_changed_paths) == 1:
        if targeted_count >= TARGETED_MEASUREMENTS_BEFORE_FULL:
            return "full", [], "controller_periodic_full_after_targeted"
        return "targeted", planned_partitions, planned_measurement_source
    if not rtl_changed_paths and non_rtl_changed_paths:
        if len(planned_partitions) == 1:
            return "smoke", planned_partitions, planned_measurement_source
        return "targeted", planned_partitions, planned_measurement_source
    if not rtl_changed_paths and not non_rtl_changed_paths:
        return "targeted", planned_partitions, planned_measurement_source
    return "full", [], "controller_targeted_requires_single_rtl_local_change"


def measurement_config_fingerprint() -> str:
    """Fingerprint only inputs that can alter the PPA measurement outcome."""
    return content_fingerprint(ROOT, TASK02_MEASUREMENT_INPUTS)


def measurement_budget_from_plan(planner: str, allowed: Tuple[int, ...], default: int,
                                 previous: Optional[dict] = None,
                                 current_fingerprint: Optional[str] = None,
                                 minimum: Optional[int] = None,
                                 *, floor_kind: str = "full_measurement") -> Tuple[int, str]:
    """Accept only a planner-selected budget from the controller's fixed ladder.

    ``minimum`` is the hard floor for the selected plan: full configured-partition
    coverage, or the declared per-partition tool budgets for a smoke/targeted set.
    The low probe default is only valid for targeted smoke measurements.
    ``floor_kind`` names that floor in the raised-to-floor source string so a
    partition-timeout floor is not mislabeled as a full-measurement floor.
    """
    raised_label = f"controller_raised_to_{floor_kind}_floor"
    floor_label = f"controller_{floor_kind}_floor"
    match = re.search(r"^`?\s*measurement_budget_seconds:\s*(\d+)\s*`?\s*$", planner, flags=re.MULTILINE)
    if not match:
        return ((minimum if minimum is not None else default),
                (floor_label if minimum is not None else "default_no_valid_plan_value"))
    requested = int(match.group(1))
    if requested not in allowed:
        return ((minimum if minimum is not None else default),
                (f"controller_{floor_kind}_floor_invalid_{requested}" if minimum is not None
                 else f"default_invalid_request_{requested}"))
    # A too-small request is raised to the effective floor.  That floor is the
    # selected-plan minimum, and the no-unexplained-downgrade rule raises it
    # further to the prior incomplete run's budget when the measurement inputs
    # are unchanged.  Both are resolved before the final comparison so a
    # below-floor request cannot silently downgrade an incomplete run.
    effective_floor = minimum
    effective_source = raised_label
    if previous and current_fingerprint and previous.get("measurement_fingerprint") == current_fingerprint:
        old_budget = previous.get("measurement_timeout_seconds")
        if isinstance(old_budget, int) and requested < old_budget and previous.get("ppa_exit") != 0:
            if effective_floor is None or old_budget > effective_floor:
                effective_floor = old_budget
                effective_source = "controller_prevented_unexplained_downgrade"
    if effective_floor is not None and requested < effective_floor:
        return effective_floor, effective_source
    return requested, "planner_evidence_request"


def latest_result() -> dict:
    candidates = sorted(agent_result_paths(), key=lambda path: path.stat().st_mtime)
    return json.loads(candidates[-1].read_text(encoding="utf-8")) if candidates else {"kind": "none", "note": "尚无真实测量结果。"}


def agent_result_paths() -> List[Path]:
    """Include compacted historical runs as well as live run directories."""
    return (list((ROOT / "runs").glob("agent-02_yosys_ppa--*/result.json"))
            + list(STRATEGY_ARCHIVE.glob("agent-02_yosys_ppa--*/result.json")))


def latest_non_infrastructure_result() -> dict:
    """Return the last result containing a real functional verdict, not exit 75."""
    candidates = sorted(agent_result_paths(), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        result = json.loads(path.read_text(encoding="utf-8"))
        functional_rc = result.get("functional_exit", result.get("functional_quick_exit"))
        if functional_rc != INFRA_EXIT:
            return result
    return {"kind": "none", "note": "尚无非基础设施结果。"}


def latest_ppa_feedback() -> str:
    candidates = sorted((ROOT / "runs").glob("02_yosys_ppa--*/feedback.txt"), key=lambda path: path.stat().st_mtime)
    archived = STRATEGY_ARCHIVE / "latest_ppa_feedback.txt"
    return tail(candidates[-1]) if candidates else (tail(archived) if archived.exists() else "（尚无 PPA feedback）")


def latest_ppa_feedback_path() -> Optional[Path]:
    candidates = sorted((ROOT / "runs").glob("02_yosys_ppa--*/feedback.txt"), key=lambda path: path.stat().st_mtime)
    if candidates:
        return candidates[-1]
    archived = STRATEGY_ARCHIVE / "latest_ppa_feedback.txt"
    return archived if archived.exists() else None


def latest_sta_closure_audit() -> Dict[str, object]:
    """Classify the newest real STA packet through the sole portable gate."""
    path = latest_ppa_feedback_path()
    result = latest_result()
    current_budget = result.get("measurement_timeout_seconds")
    return closure_governance_result(
        CLOSURE_GOVERNANCE_ROOT, feedback_path=path,
        policy_context={
            "measurement_budget_seconds": (
                current_budget if isinstance(current_budget, int)
                and not isinstance(current_budget, bool) and current_budget > 0 else None),
            "max_measurement_budget_seconds": MEASUREMENT_BUDGET_CEILING_SECONDS,
        })


def closure_governance_unavailable(audit: object) -> bool:
    return (not isinstance(audit, dict)
            or audit.get("closure_governance_status") == "unavailable")


def latest_functional_feedback() -> str:
    candidates = sorted((ROOT / "runs").glob("02_yosys_ppa_functional--*/feedback.txt"), key=lambda path: path.stat().st_mtime)
    archived = STRATEGY_ARCHIVE / "latest_functional_feedback.txt"
    return tail(candidates[-1]) if candidates else (tail(archived) if archived.exists() else "（尚无功能验证 feedback）")


def latest_functional_feedback_path() -> Optional[Path]:
    candidates = sorted((ROOT / "runs").glob("02_yosys_ppa_functional--*/feedback.txt"),
                        key=lambda path: path.stat().st_mtime)
    return candidates[-1] if candidates else None


def parse_functional_counting_feedback(text: str) -> Dict[str, Optional[int]]:
    """Extract requested_seq_len and PASS cycles from a functional feedback packet.

    The remote functional script writes summary.json first (requested_seq_len),
    then the simulation.log tail (PASS cycles=N).  Both come only from real
    controller logs; a missing field stays None and is never guessed.
    """
    requested = None
    match = re.search(r'"requested_seq_len"\s*:\s*(\d+)', text)
    if match:
        requested = int(match.group(1))
    cycles = None
    match = re.search(r"PASS cycles=(\d+)", text)
    if match:
        cycles = int(match.group(1))
    return {"requested_seq_len": requested, "pass_cycles": cycles}


def functional_counting_marginal(baseline_cycles: Optional[int], measured_cycles: Optional[int],
                                 baseline_seq_len: int, measured_seq_len: Optional[int]) -> Optional[float]:
    """Return m=(C(n2)-C(n1))/(n2-n1) cycles/token, or None when not falsifiable.

    The counting point is only a decision when the measured sequence length is
    strictly longer than the baseline; otherwise the marginal is 未测得 and the
    controller reports the raw cycle counts without deriving a slope.
    """
    if (baseline_cycles is None or measured_cycles is None or measured_seq_len is None
            or measured_seq_len <= baseline_seq_len):
        return None
    return (measured_cycles - baseline_cycles) / (measured_seq_len - baseline_seq_len)


def latest_measurement_scope() -> str:
    candidates = sorted((ROOT / "runs").glob("02_yosys_ppa--*/feedback.txt"), key=lambda path: path.stat().st_mtime)
    path = candidates[-1] if candidates else STRATEGY_ARCHIVE / "latest_ppa_feedback.txt"
    if not path.exists():
        return "UNKNOWN"
    match = re.search(r"^measurement_scope=([A-Z_]+)$", path.read_text(encoding="utf-8", errors="replace"), flags=re.MULTILINE)
    return match.group(1) if match else "UNKNOWN"


def load_supervisor_directive(path: Optional[Path]) -> str:
    """Load a bounded project-local Pro instruction without bypassing policy."""
    if path is None:
        return ""
    resolved = path.resolve()
    if ROOT not in resolved.parents or not resolved.is_file():
        raise ValueError("--supervisor-directive must be an existing file inside this project")
    return resolved.read_text(encoding="utf-8", errors="replace")[:12000]


def persisted_measurement_counters() -> Tuple[int, int]:
    """Resume task-level cadence counters across separate controller launches."""
    candidates = sorted(agent_result_paths(), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        result = json.loads(path.read_text(encoding="utf-8"))
        if "measurement_candidates" in result or "accepted_measurements" in result:
            return int(result.get("measurement_candidates") or 0), int(result.get("accepted_measurements") or 0)
    return 0, 0


def record(root: Path, result: dict, changes: str = "", planner: str = "", executor: str = "") -> None:
    (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with REPORT.open("a", encoding="utf-8") as handle:
        handle.write(f"## {result['kind']}：{result.get('time', now())}\n\n")
        if result["kind"] == "iteration":
            handle.write("### 本轮计划摘要\n\n")
            handle.write(concise_agent_reply(planner, 1600))
            handle.write("\n\n### 本轮实施摘要\n\n")
            handle.write(concise_agent_reply(executor, 900))
            handle.write("\n\n### 本轮真实结果（下一轮的输入）\n\n")
            handle.write(f"- 模式：{result['mode']}；受管输入改动：{result['managed_inputs_changed']}\n")
            handle.write(f"- 功能快检（{result['quick_seq_len']} token）：exit {result['functional_quick_exit']}\n")
            if result.get("rtl_changed_paths"):
                handle.write(f"- RTL 改动：{', '.join(result['rtl_changed_paths'])}\n")
                handle.write(f"- 强制 16-token 回归：exit {result.get('mandatory_medium_exit', '未运行')}；"
                             f"强制 48-token 回归：exit {result.get('mandatory_full_exit', '不需要')}\n")
            plan = result.get("measurement_plan_mode", "full")
            if plan == "functional_counting":
                handle.write(f"- 验证策略：{result.get('change_mode', '未声明')} 假设；功能计数点，不运行 PPA。\n")
                handle.write(f"- 功能计数点（{result.get('medium_seq_len', '未运行')} token）：exit {result.get('functional_medium_exit', '未运行')}\n")
                requested = result.get("functional_counting_requested_seq_len")
                cycles = result.get("functional_counting_cycles")
                baseline = result.get("functional_counting_baseline_cycles")
                marginal = result.get("functional_counting_marginal_cycles_per_token")
                if cycles is not None:
                    handle.write(f"- C({requested if requested is not None else '未测得'}) = {cycles} cycles；"
                                 f"基准 C({result.get('quick_seq_len')}) = {baseline if baseline is not None else '未测得'} cycles；"
                                 f"m = {marginal if marginal is not None else '未测得'} cycles/token。\n")
                else:
                    handle.write("- 功能计数点：C(n) 未测得（simulation.log 无 PASS cycles 行）。\n")
                handle.write("- 综合预检/PPA：未运行（功能计数点路由，跳过重复测量）。\n")
            else:
                partitions = result.get("measurement_partitions") or []
                selection = ", ".join(partitions) if partitions else "全部已配置分区"
                handle.write(f"- PPA 策略：{result.get('change_mode', '未声明')} 假设；{plan} 测量（{selection}，{result.get('measurement_plan_source', '无来源')}）。\n")
                handle.write(f"- 综合预检：exit {result.get('preflight_exit', '未运行')}；PPA 测量：exit {result.get('ppa_exit', '未运行')}（状态 {result.get('measurement_status', '未记录')}；预算 {result.get('measurement_timeout_seconds', '未运行')} s，范围 {result.get('measurement_scope', 'UNKNOWN')}，{result.get('measurement_budget_source', '无来源')}）\n")
                if result.get("mode") == "黑盒展开验证":
                    handle.write(f"- 展开模块：{result.get('expanded_module', '未测得')}；强制 16-token 回归：exit {result.get('functional_medium_exit', '未运行')}；"
                                 f"强制 48-token 回归：exit {result.get('mandatory_full_exit', '未运行')}。\n")
                audit = result.get("sta_closure_audit")
                if isinstance(audit, dict):
                    handle.write(f"- STA 闭合审计：phase={audit.get('phase', '未测得')}；"
                                 f"read_skip={audit.get('sta_read_skip_count', '未测得')}；"
                                 f"link={audit.get('sta_link_result', '未测得')}；"
                                 f"timing_measured={audit.get('sta_timing_measured', '未测得')}。\n")
            handle.write(f"- 详细证据：`{root.relative_to(ROOT)}/result.json`、`changes.diff`、功能/预检/PPA日志。\n")
        elif result["kind"] == "agent_unavailable":
            handle.write("### AI 阶段失败：本轮未验证、未保留半成品修改\n\n")
            handle.write(f"- 阶段：{result['agent_stage']}；原因：{result['agent_failure_reason']}；exit {result['agent_exit']}。\n")
            handle.write(f"- planner 是否违规写入：{result['planner_modified_managed_inputs']}；executor 半成品是否已回退：{result.get('executor_partial_changes_reverted', False)}。\n")
            handle.write(f"- 按 fail-fast 政策：未运行功能快检、综合预检或 PPA，等待模型/配额恢复后从新的回合重启。\n")
            handle.write(f"- 详细证据：`{root.relative_to(ROOT)}/result.json`、planner/executor 日志与可能的 `changes.diff`。\n")
        elif result["kind"] == "policy_violation":
            handle.write("### 修改范围违反策略：本轮已回退，未运行验证\n\n")
            handle.write(f"- 原因：{result['note']}\n")
            handle.write(f"- 检测到 RTL 改动：{', '.join(result.get('rtl_changed_paths', []))}\n")
            handle.write(f"- 详细证据：`{root.relative_to(ROOT)}/result.json`、`changes.diff` 与 AI 日志。\n")
        else:
            handle.write("### 基线真实结果\n\n")
            handle.write(f"- 功能快检（{result['quick_seq_len']} token）：exit {result['functional_exit']}\n")
            handle.write(f"- 综合预检：exit {result.get('preflight_exit', '未运行')}；完整 PPA：exit {result.get('ppa_exit', '未运行')}（状态 {result.get('measurement_status', '未记录')}）\n")
            handle.write(f"- 详细证据：`{root.relative_to(ROOT)}/result.json` 与对应工具日志。\n")
        handle.write("\n")


def baseline(seq_len: int, preflight_only: bool, preflight_timeout: int, measurement_budget: int) -> Tuple[int, Optional[int], Optional[int]]:
    root = ROOT / "runs" / f"agent-02_yosys_ppa--{dt.datetime.now():%Y%m%d-%H%M%S}--baseline"
    root.mkdir(parents=True)
    set_stage("baseline_functional", "baseline", f"seq_len={seq_len}")
    functional = run_with_infra_retry(functional_command(seq_len), root / "functional.log")
    preflight, ppa = None, None
    if functional == 0:
        set_stage("synthesis_preflight", "baseline", f"Yosys lowering gate; timeout={preflight_timeout}s")
        preflight = run(preflight_command(preflight_timeout), root / "preflight.log")
        if preflight == 0 and not preflight_only:
            set_stage("baseline_measurement", "baseline", f"Yosys/ABC probe; timeout={measurement_budget}s")
            ppa = run(measurement_command(measurement_budget), root / "ppa.log")
    audit = latest_sta_closure_audit() if ppa is not None else {}
    normalized_outcome = Task02ProjectAdapter.normalize_measurement_outcome(
        results=[{"kind": "baseline", "functional_exit": functional,
                  "preflight_exit": preflight, "ppa_exit": ppa}],
        process_exit=0, closure_audit=audit, preflight_only=preflight_only)
    result = {"kind": "baseline", "time": now(), "quick_seq_len": seq_len, "functional_exit": functional,
              "preflight_exit": preflight, "ppa_exit": ppa, "measurement_timeout_seconds": measurement_budget,
              "measurement_status": normalized_outcome["status"],
              "sta_closure_audit": audit,
              "measurement_budget_source": "baseline_default",
              "functional_log": str((root / "functional.log").relative_to(ROOT)),
              "preflight_log": str((root / "preflight.log").relative_to(ROOT)),
              "ppa_log": str((root / "ppa.log").relative_to(ROOT)) if ppa is not None else None}
    record(root, result)
    return functional, preflight, ppa


def forced_blackbox_expansion(module: str, args: argparse.Namespace,
                              measurement_budget_levels: Tuple[int, ...]) -> int:
    """Verify one controller-selected blackbox expansion without another AI edit.

    The supervisor has already removed ``module`` from top_level_sta's list.
    This path deliberately skips Flash planning/execution: a structural
    expansion is a controller operation, and letting Flash edit during its
    verification would mix the expansion with an unrelated RTL hypothesis.
    """
    if module in configured_top_level_blackboxes():
        root = ROOT / "runs" / f"agent-02_yosys_ppa--{dt.datetime.now():%Y%m%d-%H%M%S}--expansion-rejected"
        root.mkdir(parents=True, exist_ok=True)
        message = f"module {module!r} is still in top_level_sta blackbox list; refusing implicit expansion\n"
        (root / "planner.log").write_text(message, encoding="utf-8")
        (root / "executor.log").write_text("controller expansion verification not started\n", encoding="utf-8")
        (root / "changes.diff").write_text("", encoding="utf-8")
        set_stage("blocked", "expansion", message.strip())
        return 2

    root = ROOT / "runs" / f"agent-02_yosys_ppa--{dt.datetime.now():%Y%m%d-%H%M%S}--expansion-{module}"
    root.mkdir(parents=True, exist_ok=True)
    planner_text = (
        "controller 强制模块展开验证\n"
        f"module={module}\npartition=top_level_sta\n"
        "Flash 不得在本验证批次中新增 RTL 或 harness 修改。\n"
    )
    executor_text = "controller 已完成黑盒名单变更；本批只运行功能门、预检和 targeted PPA。\n"
    (root / "planner.log").write_text(planner_text, encoding="utf-8")
    (root / "executor.log").write_text(executor_text, encoding="utf-8")
    (root / "changes.diff").write_text(
        f"# controller expansion: removed {module} from top_level_sta blackbox list\n", encoding="utf-8")

    before_audit = latest_sta_closure_audit()
    if closure_governance_unavailable(before_audit):
        result = {
            "kind": "infrastructure_uncertain", "attempt": 1, "time": now(),
            "mode": "黑盒展开验证", "expanded_module": module,
            "sta_closure_audit": before_audit,
            "note": "rtl-closure-governance unavailable; expansion verification was not started",
        }
        record(root, result, (root / "changes.diff").read_text(encoding="utf-8"),
               planner_text, executor_text)
        set_stage("infra_uncertain", "expansion", "rtl-closure-governance unavailable")
        return INFRA_UNCERTAIN_EXIT
    quick_rc = medium_rc = full_rc = preflight_rc = ppa_rc = None
    set_stage("functional_quick", "expansion", f"module={module}; seq_len={args.quick_seq_len}")
    quick_rc = run_with_infra_retry(functional_command(args.quick_seq_len), root / "functional_quick.log")
    if quick_rc == 0:
        set_stage("functional_medium", "expansion", f"module={module}; seq_len={args.medium_seq_len}")
        medium_rc = run_with_infra_retry(functional_command(args.medium_seq_len), root / "functional_medium.log")
    if medium_rc == 0:
        set_stage("functional_full", "expansion", f"module={module}; prompt=0; seq_len=48")
        full_rc = run_with_infra_retry(full_functional_command(), root / "functional_full.log")
    if full_rc == 0:
        set_stage("synthesis_preflight", "expansion", f"module={module}; timeout={args.preflight_timeout_seconds}s")
        preflight_rc = run(preflight_command(args.preflight_timeout_seconds), root / "preflight.log")
    if preflight_rc == 0 and not args.preflight_only:
        budget = partition_budget_floor(["top_level_sta"], measurement_budget_levels)
        set_stage("ppa", "expansion", f"module={module}; partition=top_level_sta; timeout={budget}s")
        ppa_rc = run(measurement_command(budget, ["top_level_sta"]), root / "ppa.log")

    scope = latest_measurement_scope() if ppa_rc not in (None, INFRA_EXIT, INFRA_UNCERTAIN_EXIT) else "UNKNOWN"
    candidates, accepted = persisted_measurement_counters()
    if ppa_rc not in (None, INFRA_EXIT, INFRA_UNCERTAIN_EXIT):
        candidates += 1
        if ppa_rc == 0 and scope == "TOP_LEVEL_COMPLETE":
            accepted += 1
    after_audit = latest_sta_closure_audit() if ppa_rc is not None else before_audit
    result = {
        "kind": "iteration", "attempt": 1, "time": now(), "planner_exit": 0, "executor_exit": 0,
        "mode": "黑盒展开验证", "managed_inputs_changed": True, "expanded_module": module,
        "rtl_changed_paths": [], "mandatory_medium_exit": medium_rc, "mandatory_full_exit": full_rc,
        "quick_seq_len": args.quick_seq_len, "functional_quick_exit": quick_rc,
        "preflight_exit": preflight_rc, "ppa_exit": ppa_rc, "measurement_scope": scope,
        "sta_closure_audit_before": before_audit, "sta_closure_audit": after_audit,
        "measurement_timeout_seconds": partition_budget_floor(["top_level_sta"], measurement_budget_levels),
        "measurement_budget_source": "controller_expansion_partition_floor",
        "change_mode": "module_expansion", "measurement_plan_mode": "targeted",
        "measurement_partitions": ["top_level_sta"],
        "measurement_plan_source": "controller_forced_blackbox_expansion",
        "measurement_fingerprint": measurement_config_fingerprint(),
        "measurement_candidates": candidates, "accepted_measurements": accepted,
        "functional_medium_exit": medium_rc,
        "planner_log": str((root / "planner.log").relative_to(ROOT)),
        "executor_log": str((root / "executor.log").relative_to(ROOT)),
        "changes": str((root / "changes.diff").relative_to(ROOT)),
        "functional_log": str((root / "functional_quick.log").relative_to(ROOT)),
        "preflight_log": str((root / "preflight.log").relative_to(ROOT)),
        "ppa_log": str((root / "ppa.log").relative_to(ROOT)) if ppa_rc is not None else None,
    }
    normalized_outcome = Task02ProjectAdapter.normalize_measurement_outcome(
        results=[result], process_exit=0, closure_audit=after_audit,
        require_timing=True)
    outcome_status = str(normalized_outcome.get("status") or "")
    result["measurement_status"] = outcome_status
    result["measurement_reason"] = normalized_outcome.get("reason")
    record(root, result, (root / "changes.diff").read_text(encoding="utf-8"), planner_text, executor_text)
    if closure_governance_unavailable(after_audit):
        set_stage("infra_uncertain", "expansion", "rtl-closure-governance unavailable after measurement")
        return INFRA_UNCERTAIN_EXIT
    if outcome_status == "infra_uncertain":
        set_stage("infra_uncertain", "expansion", f"module={module}; expansion verification transport uncertain")
        return INFRA_UNCERTAIN_EXIT
    if outcome_status == "infra_failed":
        set_stage("infra_failed", "expansion", f"module={module}; expansion verification transport failed")
        return INFRA_EXIT
    if outcome_status == "measurement_incomplete":
        set_stage("measurement_incomplete", "expansion",
                  f"module={module}; reason={normalized_outcome.get('reason')}; next guidance cycle may reassess")
        return MEASUREMENT_TIMEOUT_EXIT
    if outcome_status == "verification_evidence_missing":
        set_stage("verification_evidence_missing", "expansion",
                  f"module={module}; required timing evidence was not established")
        return 0
    if outcome_status != "complete":
        set_stage("failed", "expansion", f"module={module}; functional/preflight/PPA verification failed")
        return 1
    set_stage("complete", "expansion", f"module={module}; targeted top_level_sta verification completed")
    return 0


def omp_command(model: Optional[str]) -> List[str]:
    command = ["omp", "-p", "--cwd", str(ROOT)]
    return command + (["--model", model] if model else [])


def verify_existing_optimization(args: argparse.Namespace, optimization_rtl_path: Path,
                                 optimization_baseline: Path,
                                 measurement_budget_levels: Tuple[int, ...],
                                 valid_partitions: List[str]) -> int:
    """Verify the preserved candidate without invoking planner or executor."""
    root = ROOT / "runs" / f"agent-02_yosys_ppa--{dt.datetime.now():%Y%m%d-%H%M%S}--verify"
    root.mkdir(parents=True)
    baseline_file = optimization_baseline / optimization_rtl_path
    current_file = ROOT / optimization_rtl_path
    if not baseline_file.is_file() or not current_file.is_file():
        parser_note = "optimization verify-only baseline or RTL source is missing"
        (root / "planner.log").write_text(parser_note + "\n", encoding="utf-8")
        (root / "executor.log").write_text("no edit was attempted\n", encoding="utf-8")
        result = {
            "kind": "infrastructure_uncertain", "attempt": 1, "time": now(),
            "optimization_candidate_id": args.optimization_candidate_id,
            "optimization_module": args.optimization_module,
            "optimization_status": "preserved_infra_uncertain",
            "note": parser_note,
        }
        record(root, result)
        set_stage("infra_uncertain", "verify", parser_note)
        return INFRA_UNCERTAIN_EXIT

    before_lines = baseline_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    after_lines = current_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    changes = "".join(difflib.unified_diff(
        before_lines, after_lines,
        fromfile=f"baseline/{optimization_rtl_path.as_posix()}",
        tofile=f"current/{optimization_rtl_path.as_posix()}",
    ))
    (root / "changes.diff").write_text(changes, encoding="utf-8")
    (root / "planner.log").write_text(
        "controller verify-only：复用已保存候选，不调用规划模型，不产生新编辑。\n",
        encoding="utf-8")
    (root / "executor.log").write_text(
        "controller verify-only：executor 被跳过；只运行契约要求的验证。\n",
        encoding="utf-8")
    if not changes:
        result = {
            "kind": "iteration", "attempt": 1, "time": now(),
            "planner_exit": 0, "executor_exit": 0, "managed_inputs_changed": False,
            "rtl_changed_paths": [], "optimization_candidate_id": args.optimization_candidate_id,
            "optimization_module": args.optimization_module,
            "optimization_status": "no_change", "mainline_resume": False,
            "changes": str((root / "changes.diff").relative_to(ROOT)),
            "note": "candidate equals checkpoint; no optimization remains to verify",
        }
        record(root, result, changes, tail(root / "planner.log"), tail(root / "executor.log"))
        set_stage("optimization_complete", "verify", "candidate equals checkpoint; resume mainline")
        return 0

    current_fingerprint = hashlib.sha256(current_file.read_bytes()).hexdigest()
    verification_context_fingerprint = measurement_config_fingerprint()
    identity_matches = bool(args.optimization_expected_rtl_sha256
                            and current_fingerprint == args.optimization_expected_rtl_sha256)
    # Candidate identity is necessary but never sufficient evidence.  Reuse is
    # legal only when the caller supplies an explicit successful proof bound to
    # this exact RTL image.
    functional_evidence_reused = bool(
        identity_matches
        and args.optimization_functional_proof_rtl_sha256 == current_fingerprint
        and args.optimization_functional_proof_context_sha256
            == verification_context_fingerprint)
    preflight_evidence_reused = bool(
        identity_matches
        and args.optimization_preflight_proof_rtl_sha256 == current_fingerprint
        and args.optimization_preflight_proof_context_sha256
            == verification_context_fingerprint)
    quick_rc = medium_rc = mandatory_full_rc = preflight_rc = None
    if not functional_evidence_reused:
        set_stage("functional_quick", "verify", "candidate has no matching functional proof; seq_len=8")
        quick_rc = run_with_infra_retry(functional_command(args.quick_seq_len), root / "functional_quick.log")
        if quick_rc == 0:
            set_stage("functional_medium", "verify", f"candidate re-verification; seq_len={args.medium_seq_len}")
            medium_rc = run_with_infra_retry(functional_command(args.medium_seq_len), root / "functional_medium.log")
        if quick_rc == 0 and medium_rc == 0:
            set_stage("functional_full", "verify", "candidate re-verification; prompt=0 seq_len=48")
            mandatory_full_rc = run_with_infra_retry(full_functional_command(), root / "functional_full.log")
    else:
        quick_rc = medium_rc = mandatory_full_rc = 0
        (root / "functional_quick.log").write_text(
            "functional evidence reused: explicit passing proof matches candidate RTL SHA-256\n", encoding="utf-8")
        (root / "functional_medium.log").write_text(
            "functional evidence reused: no RTL edit occurred\n", encoding="utf-8")
        (root / "functional_full.log").write_text(
            "functional evidence reused: no RTL edit occurred\n", encoding="utf-8")
    if all(value == 0 for value in (quick_rc, medium_rc, mandatory_full_rc)):
        if preflight_evidence_reused:
            preflight_rc = 0
            (root / "preflight.log").write_text(
                "preflight evidence reused: explicit passing proof matches candidate RTL SHA-256\n",
                encoding="utf-8")
        else:
            set_stage("synthesis_preflight", "verify", "candidate has no matching Yosys preflight proof")
            preflight_rc = run(preflight_command(args.preflight_timeout_seconds), root / "preflight.log")

    scope_gate = None
    measurement_partitions = [name for name in args.optimization_measurement_partition
                              if name in valid_partitions]
    if not measurement_partitions:
        current_context = design_context_slice(
            ROOT, rtl_paths=[optimization_rtl_path.as_posix()],
            module_hints=[args.optimization_module])
        scope_packet = conservative_rtl_change_packet(
            changes, [optimization_rtl_path.as_posix()], change_mode="local",
            remaining_blackboxes=configured_top_level_blackboxes(),
            affected_modules=[args.optimization_module], design_context=current_context)
        try:
            scope_gate = sta_scope_decision(args.sta_scope_skill, scope_packet)
        except SkillGateError as error:
            result = {
                "kind": "infrastructure_uncertain", "attempt": 1, "time": now(),
                "optimization_candidate_id": args.optimization_candidate_id,
                "optimization_module": args.optimization_module,
                "optimization_status": "preserved_infra_uncertain",
                "sta_scope_gate_error": str(error), "mainline_resume": False,
            }
            record(root, result, changes, tail(root / "planner.log"), tail(root / "executor.log"))
            set_stage("infra_uncertain", "verify", "rtl-sta-scope gate unavailable")
            return INFRA_UNCERTAIN_EXIT
        scope = scope_gate.get("scope")
        if scope == "local_module" and args.optimization_module in valid_partitions:
            measurement_partitions = [args.optimization_module]
        elif scope == "affected_cone":
            measurement_partitions = [name for name in scope_gate.get("affected_modules", [])
                                      if name in valid_partitions]
        if not measurement_partitions:
            measurement_partitions = ["top_level_sta"]
    else:
        scope_gate = {"scope": "contract_reuse", "affected_modules": [args.optimization_module],
                      "reason_codes": ["preserved_candidate_measurement_retry"]}

    external_verification_required = bool(
        isinstance(scope_gate, dict)
        and scope_gate.get("scope") in ("top_level_partial", "top_level_complete")
        and not args.optimization_consume_external_sta)
    ppa_rc = None
    measurement_scope = "UNKNOWN"
    deterministic_functional_failure = any(
        value not in (None, 0, INFRA_EXIT, INFRA_UNCERTAIN_EXIT)
        for value in (quick_rc, medium_rc, mandatory_full_rc))
    if (not deterministic_functional_failure and preflight_rc == 0
            and not external_verification_required):
        budget_floor = partition_budget_floor(measurement_partitions, measurement_budget_levels)
        requested = args.measurement_default_timeout_seconds
        measurement_budget = requested if requested in measurement_budget_levels else budget_floor
        measurement_budget = max(measurement_budget, budget_floor)
        set_stage("ppa", "verify",
                  f"verify-only partitions={','.join(measurement_partitions)} timeout={measurement_budget}s")
        ppa_rc = run(measurement_command(measurement_budget, measurement_partitions), root / "ppa.log")
        measurement_scope = latest_measurement_scope() if ppa_rc != INFRA_EXIT else "UNKNOWN"
    else:
        measurement_budget = max(args.measurement_default_timeout_seconds,
                                 partition_budget_floor(measurement_partitions, measurement_budget_levels))

    audit = latest_sta_closure_audit() if ppa_rc is not None else {}
    outcome_facts = {
        "kind": "iteration", "planner_exit": 0, "executor_exit": 0,
        "functional_quick_exit": quick_rc, "functional_medium_exit": medium_rc,
        "mandatory_full_exit": mandatory_full_rc,
        "preflight_exit": preflight_rc, "ppa_exit": ppa_rc,
    }
    normalized_outcome = Task02ProjectAdapter.normalize_measurement_outcome(
        results=[outcome_facts], process_exit=0, closure_audit=audit)
    outcome_status = str(normalized_outcome.get("status") or "")
    if outcome_status in {"infra_failed", "infra_uncertain"}:
        optimization_status = "preserved_infra_uncertain"
    elif outcome_status == "functional_failed":
        optimization_status = "functional_repair_required"
    elif outcome_status == "preflight_failed":
        optimization_status = "verification_failed"
    elif external_verification_required and preflight_rc == 0:
        optimization_status = "external_verification_required"
    elif outcome_status == "measurement_incomplete":
        optimization_status = "measurement_incomplete"
    elif outcome_status == "complete":
        optimization_status = "accepted"
    elif outcome_status in {"measurement_failed", "execution_failed"}:
        optimization_status = "verification_failed"
    else:
        optimization_status = "preserved_infra_uncertain"
    result = {
        "kind": "iteration", "attempt": 1, "time": now(), "planner_exit": 0, "executor_exit": 0,
        "mode": "RTL 优化候选无编辑验证", "managed_inputs_changed": False,
        "rtl_changed_paths": [optimization_rtl_path.as_posix()],
        "functional_quick_exit": quick_rc, "functional_medium_exit": medium_rc,
        "mandatory_medium_exit": medium_rc, "mandatory_full_exit": mandatory_full_rc,
        "rtl_fingerprint": current_fingerprint,
        "verification_context_fingerprint": verification_context_fingerprint,
        "functional_evidence_reused": functional_evidence_reused,
        "functional_evidence_source": ("explicit_matching_proof" if functional_evidence_reused
                                       else "fresh_reverification"),
        "preflight_evidence_reused": preflight_evidence_reused,
        "preflight_evidence_source": ("explicit_matching_proof" if preflight_evidence_reused
                                      else "fresh_reverification"),
        "preflight_exit": preflight_rc, "ppa_exit": ppa_rc,
        "measurement_status": outcome_status,
        "measurement_scope": measurement_scope, "measurement_timeout_seconds": measurement_budget,
        "measurement_budget_source": "optimization_contract_reuse",
        "measurement_plan_mode": "targeted", "measurement_partitions": measurement_partitions,
        "measurement_plan_source": "optimization_execution_contract",
        "sta_scope_gate": scope_gate, "sta_closure_audit": audit,
        "optimization_candidate_id": args.optimization_candidate_id,
        "optimization_module": args.optimization_module,
        "optimization_integration_target": args.optimization_integration_target,
        "optimization_status": optimization_status,
        "optimization_repair_attempts": 0,
        # Verification only reports facts.  Rollback and mainline resume are
        # owned by the Optimization Skill lifecycle.
        "mainline_resume": False,
        "measurement_fingerprint": measurement_config_fingerprint(),
        "changes": str((root / "changes.diff").relative_to(ROOT)),
        "planner_log": str((root / "planner.log").relative_to(ROOT)),
        "executor_log": str((root / "executor.log").relative_to(ROOT)),
        "functional_log": str((root / "functional_quick.log").relative_to(ROOT)),
        "preflight_log": str((root / "preflight.log").relative_to(ROOT)),
        "ppa_log": str((root / "ppa.log").relative_to(ROOT)) if ppa_rc is not None else None,
    }
    record(root, result, changes, tail(root / "planner.log"), tail(root / "executor.log"))
    if ppa_rc is not None and closure_governance_unavailable(audit):
        set_stage("infra_uncertain", "verify", "rtl-closure-governance unavailable after measurement")
        return INFRA_UNCERTAIN_EXIT
    if outcome_status == "infra_uncertain":
        set_stage("infra_uncertain", "verify", "candidate verification transport uncertain")
        return INFRA_UNCERTAIN_EXIT
    if outcome_status == "infra_failed":
        set_stage("infra_failed", "verify", "candidate verification transport failed")
        return INFRA_EXIT
    if optimization_status == "measurement_incomplete":
        set_stage("measurement_incomplete", "verify", "candidate preserved; bounded retry consumed")
        return MEASUREMENT_TIMEOUT_EXIT
    if optimization_status == "external_verification_required":
        set_stage("external_verification_required", "verify",
                  "candidate passed functional/preflight gates; top-level STA obligation yielded")
        return 0
    terminal_stage = (
        "optimization_complete" if optimization_status == "accepted" else
        "optimization_no_change" if optimization_status == "no_change" else
        "optimization_functional_repair" if optimization_status == "functional_repair_required" else
        "optimization_verification_failed")
    set_stage(terminal_stage, "verify",
              f"candidate={args.optimization_module}; status={optimization_status}")
    # A deterministic functional mismatch is a domain transition, not a
    # controller/process failure.  The supervisor must remain alive so the
    # bounded repair-or-rollback contract can govern the next cycle.
    return 0


def main() -> int:
    global CLOSURE_GOVERNANCE_ROOT, MEASUREMENT_BUDGET_CEILING_SECONDS
    signal.signal(signal.SIGINT, stop_active_process)
    signal.signal(signal.SIGTERM, stop_active_process)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--model")
    parser.add_argument("--quick-seq-len", type=int, default=8)
    parser.add_argument("--medium-seq-len", type=int, default=16)
    parser.add_argument("--medium-every", type=int, default=3)
    parser.add_argument("--preflight-timeout-seconds", type=int, default=600)
    parser.add_argument("--measurement-default-timeout-seconds", type=int, default=DEFAULT_MEASUREMENT_BUDGET_SECONDS,
                        help="initial Yosys/ABC probe budget; the planner may select a permitted larger tier")
    parser.add_argument("--measurement-budget-levels", default=",".join(map(str, DEFAULT_MEASUREMENT_BUDGET_LEVELS)),
                        help="comma-separated permitted measurement budgets in seconds")
    parser.add_argument("--preflight-only", action="store_true", help="run functional + Yosys lowering gate, but skip full ABC PPA")
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--final-regression", action="store_true", help="after all rounds, run prompt-0 full 48-token regression")
    parser.add_argument("--force-full-measurement", action="store_true",
                        help="supervisor milestone: disallow targeted PPA for every attempt in this invocation")
    parser.add_argument("--supervisor-directive", type=Path,
                        help="bounded Pro instruction file inside this project; advisory only")
    parser.add_argument("--forced-blackbox-expansion-module",
                        help="controller-owned verification of one module removed from top_level_sta blackboxes")
    parser.add_argument("--optimization-module",
                        help="controller-selected expanded module; restrict this invocation to its RTL source")
    parser.add_argument("--optimization-candidate-id",
                        help="stable supervisor candidate/checkpoint identifier")
    parser.add_argument("--optimization-integration-target",
                        help="saved mainline integration target E for optimization target O")
    parser.add_argument("--optimization-baseline", type=Path,
                        help="controller-owned candidate baseline directory for retry/rollback")
    parser.add_argument("--optimization-verify-only", action="store_true",
                        help="verify the preserved optimization candidate without planner or executor")
    parser.add_argument("--optimization-consume-external-sta", action="store_true",
                        help="consume a previously yielded top-level STA obligation without editing")
    parser.add_argument("--optimization-expected-rtl-sha256",
                        help="expected candidate identity; never sufficient by itself to reuse evidence")
    parser.add_argument("--optimization-functional-proof-rtl-sha256",
                        help="reuse passing 8/16/48 functional proof bound to this RTL SHA-256")
    parser.add_argument("--optimization-functional-proof-context-sha256",
                        help="managed-input fingerprint for the passing functional proof")
    parser.add_argument("--optimization-preflight-proof-rtl-sha256",
                        help="reuse passing Yosys preflight proof bound to this RTL SHA-256")
    parser.add_argument("--optimization-preflight-proof-context-sha256",
                        help="managed-input fingerprint for the passing Yosys proof")
    parser.add_argument("--optimization-measurement-partition", action="append", default=[],
                        help="validated measurement partition for a preserved candidate; repeat as needed")
    parser.add_argument("--max-rtl-repair-attempts", type=int, default=2,
                        help="deterministic functional repair attempts before candidate rollback")
    parser.add_argument("--sta-scope-skill", type=Path,
                        default=default_skill_path("rtl-sta-scope", STA_SCOPE_ENV),
                        help=f"RTLskill rtl-sta-scope directory (also configurable via {STA_SCOPE_ENV})")
    parser.add_argument("--closure-governance-skill", type=Path,
                        default=default_skill_path("rtl-closure-governance", CLOSURE_GOVERNANCE_ENV),
                        help=("RTLskill rtl-closure-governance directory "
                              f"(also configurable via {CLOSURE_GOVERNANCE_ENV})"))
    args = parser.parse_args()
    try:
        measurement_budget_levels = tuple(sorted({int(value) for value in args.measurement_budget_levels.split(",")}))
    except ValueError:
        parser.error("--measurement-budget-levels must be comma-separated positive integers")
    if (args.quick_seq_len < 1 or args.medium_seq_len < args.quick_seq_len or args.medium_every < 1
            or args.preflight_timeout_seconds < 1 or not measurement_budget_levels
            or any(value < 1 for value in measurement_budget_levels)
            or args.measurement_default_timeout_seconds not in measurement_budget_levels
            or args.max_rtl_repair_attempts < 1):
        parser.error("invalid sequence-length policy")
    MEASUREMENT_BUDGET_CEILING_SECONDS = max(measurement_budget_levels)
    optimization_rtl_path = None
    sta_scope_root = args.sta_scope_skill.expanduser()
    if not sta_scope_root.is_absolute():
        sta_scope_root = ROOT / sta_scope_root
    sta_scope_root = sta_scope_root.resolve()
    if not (sta_scope_root / "scope_gate.py").is_file():
        parser.error(f"rtl-sta-scope scope_gate.py not found: {sta_scope_root / 'scope_gate.py'}")
    closure_root = args.closure_governance_skill.expanduser()
    if not closure_root.is_absolute():
        closure_root = ROOT / closure_root
    closure_root = closure_root.resolve()
    if not (closure_root / "closure_gate.py").is_file():
        parser.error(f"rtl-closure-governance closure_gate.py not found: {closure_root / 'closure_gate.py'}")
    CLOSURE_GOVERNANCE_ROOT = closure_root
    if args.optimization_module:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.optimization_module):
            parser.error("--optimization-module must be one valid module name")
        if not args.optimization_candidate_id:
            parser.error("--optimization-module requires --optimization-candidate-id")
        if (args.optimization_integration_target is not None
                and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.optimization_integration_target)):
            parser.error("--optimization-integration-target must be one valid module name")
        optimization_rtl_path = rtl_file_for_module(args.optimization_module)
        if optimization_rtl_path is None:
            parser.error("optimization module must resolve to exactly one rtl/*.v or rtl/*.sv source")
    if args.optimization_verify_only and not args.optimization_module:
        parser.error("--optimization-verify-only requires --optimization-module")
    if args.optimization_consume_external_sta and not args.optimization_verify_only:
        parser.error("--optimization-consume-external-sta requires --optimization-verify-only")
    for option_name, digest in (
            ("--optimization-expected-rtl-sha256", args.optimization_expected_rtl_sha256),
            ("--optimization-functional-proof-rtl-sha256",
             args.optimization_functional_proof_rtl_sha256),
            ("--optimization-functional-proof-context-sha256",
             args.optimization_functional_proof_context_sha256),
            ("--optimization-preflight-proof-rtl-sha256",
             args.optimization_preflight_proof_rtl_sha256),
            ("--optimization-preflight-proof-context-sha256",
             args.optimization_preflight_proof_context_sha256)):
        if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
            parser.error(f"{option_name} must be 64 lowercase hex characters")

    # A prior local controller may have died while its server-side functional
    # command was still alive.  Take over only after that exact recorded run is
    # stopped and verified.  This is deliberately before any planner/executor
    # invocation: a fresh controller must not make an AI decision on top of an
    # unaccounted-for remote verification.
    startup_root = ROOT / "runs" / f"agent-02_yosys_ppa--{dt.datetime.now():%Y%m%d-%H%M%S}--startup"
    startup_root.mkdir(parents=True)
    set_stage("remote_reconcile", "startup", "safely reconciling any stale exact remote functional run")
    reconcile_rc = run(["bash", "scripts/remote_reconcile.sh"], startup_root / "remote_reconcile.log")
    if reconcile_rc != 0:
        set_stage("infra_uncertain", "startup",
                  "could not prove any stale exact remote functional run was stopped; no AI or verification started")
        return INFRA_UNCERTAIN_EXIT

    startup_closure_audit = latest_sta_closure_audit()
    if closure_governance_unavailable(startup_closure_audit):
        set_stage("infra_uncertain", "startup", "rtl-closure-governance unavailable; no AI or verification started")
        return INFRA_UNCERTAIN_EXIT

    task = TASK.read_text(encoding="utf-8")
    try:
        directive = load_supervisor_directive(args.supervisor_directive)
    except ValueError as error:
        parser.error(str(error))
    if directive:
        task += "\n\n## 上层 Pro 的本批次策略指令\n\n" + directive
    valid_partitions = configured_partitions()
    if not valid_partitions:
        parser.error("harness/ppa.toml contains no [[partitions]] entries")

    if args.forced_blackbox_expansion_module:
        if "top_level_sta" not in valid_partitions:
            parser.error("blackbox expansion requires the top_level_sta partition")
        return forced_blackbox_expansion(args.forced_blackbox_expansion_module, args, measurement_budget_levels)

    optimization_baseline = (args.optimization_baseline.expanduser().resolve()
                             if args.optimization_baseline else startup_root / "optimization_candidate_baseline")
    optimization_failures = 0
    if args.optimization_module:
        if args.optimization_baseline:
            if not (optimization_baseline / optimization_rtl_path).is_file():
                parser.error(f"optimization baseline does not contain {optimization_rtl_path}")
        else:
            (optimization_baseline / optimization_rtl_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / optimization_rtl_path, optimization_baseline / optimization_rtl_path)

    if args.optimization_verify_only:
        args.sta_scope_skill = sta_scope_root
        return verify_existing_optimization(
            args, optimization_rtl_path, optimization_baseline,
            measurement_budget_levels, valid_partitions)

    if not args.skip_baseline:
        baseline_functional, baseline_preflight, baseline_ppa = baseline(
            args.quick_seq_len, args.preflight_only, args.preflight_timeout_seconds,
            args.measurement_default_timeout_seconds)
        if baseline_functional in (INFRA_EXIT, INFRA_UNCERTAIN_EXIT):
            set_stage("infra_failed", "baseline", "functional baseline transport failed or remains uncertain")
            return baseline_functional
        if baseline_functional != 0:
            set_stage("failed", "baseline", "functional baseline failed; no PPA optimization edit was started")
            return 1
        if baseline_preflight != 0 or (baseline_ppa is not None and baseline_ppa != 0):
            set_stage("toolchain_repair", "baseline", "functional baseline passed but Yosys preflight or PPA failed")

    measurement_candidates, accepted = persisted_measurement_counters()
    for attempt in range(1, args.max_attempts + 1):
        root = ROOT / "runs" / f"agent-02_yosys_ppa--{dt.datetime.now():%Y%m%d-%H%M%S}--attempt{attempt}"
        root.mkdir(parents=True)
        previous = latest_result()
        functional_rc = previous.get("functional_exit", previous.get("functional_quick_exit"))
        functional_recovery_exit = None  # type: Optional[int]
        if functional_rc == INFRA_UNCERTAIN_EXIT:
            result = {"kind": "infrastructure_uncertain", "time": now(), "quick_seq_len": args.quick_seq_len,
                      "functional_exit": functional_rc, "preflight_exit": None, "ppa_exit": None,
                      "note": "remote functional run may still be active; controller refused to start a replacement run"}
            record(root, result)
            set_stage("infra_uncertain", attempt, "remote functional liveness is unknown; no AI edit was consumed")
            return INFRA_UNCERTAIN_EXIT
        # Exit 75 means that no RTL verdict exists. Recover the functional gate
        # before asking AI to plan or edit, then resume from the last real result.
        if functional_rc == INFRA_EXIT:
            set_stage("functional_recovery", attempt, "retrying only the interrupted functional gate; no AI edit")
            functional_recovery_exit = run_with_infra_retry(functional_command(args.quick_seq_len), root / "functional_recovery.log")
            if functional_recovery_exit in (INFRA_EXIT, INFRA_UNCERTAIN_EXIT):
                result = {"kind": "infrastructure_recovery", "time": now(), "quick_seq_len": args.quick_seq_len,
                          "functional_exit": functional_recovery_exit, "preflight_exit": None, "ppa_exit": None,
                          "functional_log": str((root / "functional_recovery.log").relative_to(ROOT)),
                          "note": "functional transport retry exhausted; no AI plan or edit was run"}
                record(root, result)
                stage = "infra_uncertain" if functional_recovery_exit == INFRA_UNCERTAIN_EXIT else "infra_failed"
                set_stage(stage, attempt, "functional recovery did not produce a safe RTL verdict; no AI edit was consumed")
                return functional_recovery_exit
            if functional_recovery_exit == 0:
                previous = latest_non_infrastructure_result()
                previous = {**previous, "infrastructure_recovery_exit": 0,
                            "infrastructure_recovery_log": str((root / "functional_recovery.log").relative_to(ROOT))}
                functional_rc = previous.get("functional_exit", previous.get("functional_quick_exit"))
            else:
                previous = {**previous, "functional_quick_exit": functional_recovery_exit,
                            "infrastructure_recovery_exit": functional_recovery_exit,
                            "infrastructure_recovery_log": str((root / "functional_recovery.log").relative_to(ROOT))}
                functional_rc = functional_recovery_exit
        functional_repair = functional_rc not in (None, 0, INFRA_EXIT, INFRA_UNCERTAIN_EXIT)
        preflight_rc = previous.get("preflight_exit")
        previous_measurement_scope = previous.get("measurement_scope", "UNKNOWN")
        sta_audit_before = latest_sta_closure_audit()
        if closure_governance_unavailable(sta_audit_before):
            result = {"kind": "infrastructure_uncertain", "attempt": attempt, "time": now(),
                      "sta_closure_audit": sta_audit_before,
                      "note": "rtl-closure-governance unavailable; no AI edit was consumed"}
            record(root, result)
            set_stage("infra_uncertain", attempt, "rtl-closure-governance unavailable")
            return INFRA_UNCERTAIN_EXIT
        sta_closure_rerun_gate = sta_audit_before.get("measurement_rerun_admissible") is True
        sta_closure_gate = sta_audit_before.get("closure_gate_required") is True
        top_level_measurement_pending = (
            functional_rc == 0 and preflight_rc == 0
            and previous_measurement_scope not in ("UNKNOWN", "TOP_LEVEL_COMPLETE"))
        toolchain_repair = not functional_repair and functional_rc == 0 and (
            preflight_rc not in (None, 0)
            or (preflight_rc == 0 and previous.get("ppa_exit") not in (None, 0)
                and previous_measurement_scope == "UNKNOWN")
        )
        if args.optimization_module:
            if optimization_failures:
                mode, allowed, stage = ("RTL 优化候选功能修复",
                                        optimization_rtl_path.as_posix(), "optimization_functional_repair")
            else:
                mode, allowed, stage = ("RTL 优化候选",
                                        optimization_rtl_path.as_posix(), "optimization_editing")
        elif functional_repair:
            mode, allowed, stage = "功能/Verilator 可用性修复", "rtl/", "functional_repair"
        elif sta_closure_rerun_gate:
            mode, allowed, stage = "STA 闭合测量预算提升", "harness/ppa.toml、scripts/remote_yosys_round.sh", "closure_measurement_rerun"
        elif toolchain_repair:
            mode, allowed, stage = "综合可用性修复", "rtl/、harness/ppa_preflight.ys、harness/ppa_measure.ys、harness/ppa.toml、scripts/remote_yosys_round.sh", "toolchain_repair"
        elif (sta_audit_before.get("structural_repair_required") is True
              and "harness" in str(sta_audit_before.get("repair_domain", ""))):
            mode, allowed, stage = "STA 顶层闭合修复", "harness/ppa_measure.ys、harness/ppa.toml、scripts/remote_yosys_round.sh", "sta_closure_repair"
        elif top_level_measurement_pending:
            mode, allowed, stage = "测量完善/端到端收敛", "rtl/、harness/ppa_preflight.ys、harness/ppa_measure.ys、harness/ppa.toml、scripts/remote_yosys_round.sh", "measurement_completion"
        else:
            mode, allowed, stage = "PPA 优化", "rtl/", "editing"

        # A: review/plan is read-only. Any accidental RTL write is restored.
        before_plan, after_plan = root / "before_plan", root / "after_plan"
        snapshot(before_plan)
        set_stage("planning", attempt, "read-only AI review of controller-produced results")
        sta_gate_instruction = ""
        if sta_closure_gate:
            if args.optimization_module:
                retry_note = (
                    "上层 controller 已通过 Optimization Skill 与插入硬门授权本次候选编辑；"
                    "closure 状态只限制闭合结论、预算和重复测量，不得把测量未完成解释成 RTL 失败，"
                    "也不得撤销当前候选编辑。功能门通过后，必须按实际 diff 的 STA scope 执行验收。")
            else:
                retry_note = ("当前属于 timeout/truncation 的测量未完成状态，不是 closure 导出缺陷；"
                              "同一测量只能提出严格提高合法预算的 increase_budget，不能修改 RTL。"
                              if sta_closure_rerun_gate else
                              "当前属于真实闭合/解析/链路修复状态；若 repair_domain 包含 harness，禁止改 RTL。")
            sta_gate_instruction = ("\n控制器 STA 闭合硬门已开启。当前审计如下；本轮只允许一个能让 phase 状态迁移的假设，"
                                    + retry_note +
                                    " controller 将执行 top_level_sta 单网表 smoke，禁止 full PPA 里程碑。\n```json\n"
                                    + json.dumps(sta_audit_before, ensure_ascii=False, indent=2) + "\n```\n")
        planner_prompt = f"""你是 Task 02 的只读复盘与规划者。不得修改任何文件，也不得运行命令。\n\n{task}\n\n当前模式：{mode}\n上一轮控制器真实结果：\n```json\n{json.dumps(previous, ensure_ascii=False, indent=2)}\n```\n\n最新功能验证 feedback：\n```text\n{latest_functional_feedback()}\n```\n\n最新 PPA feedback：\n```text\n{latest_ppa_feedback()}\n```{sta_gate_instruction}\n用不超过 8 条中文 bullet 输出，严格按以下顺序：\n1. 上一轮真实结果；\n2. 本轮要解决的问题与根因证据；\n3. 本轮唯一因果假设；\n4. 准备修改的文件、联动边界和必须保持的接口/功能不变量；\n5. 预期验证结果与功能风险。\n\n“一个问题”指一个可验证因果假设，不等于只能修改一个文件。普通局部优化选择 local：只能改一个 RTL 文件；若假设跨模块且确有必要，选择 architecture：可把多个 RTL 文件作为一个原子变更集，但必须列出全部文件和不变量，controller 将强制全量 PPA。\n\n最后必须单独输出以下三行（字段值严格小写）：\n`change_mode: local|architecture`\n`measurement_mode: targeted|full`\n`affected_partitions: 逗号分隔的分区名；full 时写 none`\n只有 local 且确实修改 RTL 时 targeted 才得到多分区 targeted（最多 {MAX_TARGETED_PARTITIONS} 个分区，候选只能是：{', '.join(valid_partitions)}）；连续 {TARGETED_MEASUREMENTS_BEFORE_FULL} 次有效 targeted 后 controller 自动插入一次 full。脚本/测量 harness 改动且无 RTL 改动时，targeted 会按你声明的合法分区跑单网表 smoke（一个分区）或 targeted 分区集（多个分区）；architecture 或字段不合法均自动改为 full。\n\n最后还必须单独输出一行 `measurement_budget_seconds: N`。N 只能是 {', '.join(map(str, measurement_budget_levels))} 之一。measurement_mode=full 时 N 不得低于 {FULL_MEASUREMENT_MIN_BUDGET_SECONDS}、优先 3600（历史上唯一闭环全部已配置分区的档位）；targeted（含脚本 smoke）才允许 180/600 等短档。默认先选 {args.measurement_default_timeout_seconds}；只有日志证明短预算已稳定推进、且更长时间会获得新的可验证证据时，才可升级。7200 仅用于重点模块测量或顶层里程碑验证，不能因“想再等一会”而选。若上一轮测量未完成且 RTL、PPA 脚本、分区或黑盒配置不变，不得选择比上一轮更短的预算；只有实际改了这些测量输入并说明新探针为何更短，才可降档。\n没有真实数字时必须写“未测得”。禁止根据源码猜测面积、时序、频率或吞吐数值。"""
        planner_rc = run(omp_command(args.model) + [planner_prompt], root / "planner.log", record_command=False)
        snapshot(after_plan)
        planner_violation = diff(before_plan, after_plan)
        if planner_violation:
            (root / "planner_violation.diff").write_text(planner_violation, encoding="utf-8")
            restore(before_plan)
        planner_failure = agent_failure_reason(root / "planner.log", planner_rc)
        if planner_failure:
            result = {"kind": "agent_unavailable", "attempt": attempt, "time": now(),
                      "agent_stage": "planner", "agent_failure_reason": planner_failure,
                      "agent_exit": planner_rc, "planner_exit": planner_rc,
                      "planner_modified_managed_inputs": bool(planner_violation),
                      "executor_exit": None, "executor_partial_changes_reverted": False,
                      "planner_log": str((root / "planner.log").relative_to(ROOT)),
                      "executor_log": None, "changes": None,
                      "note": "planner failed; controller intentionally skipped executor and all verification"}
            record(root, result, planner=tail(root / "planner.log"))
            set_stage("agent_unavailable", attempt,
                      f"planner failed: {planner_failure}; executor and verification intentionally skipped")
            return planner_rc or 1

        planned_change_mode, planned_measurement_mode, planned_partitions, planned_measurement_source = \
            planning_policy_from_reply(tail(root / "planner.log"), valid_partitions)

        # B: controller, not AI, owns every command. Toolchain repair has a deliberately small extra allow-list.
        before_edit, after_edit = root / "before_edit", root / "after_edit"
        snapshot(before_edit)
        set_stage(stage, attempt, "AI may edit only the controller allow-list; verification has not started")
        executor_prompt = f"""你是 Task 02 实施者。不得运行任何命令。\n\n{task}\n\n当前模式：{mode}\n本轮允许修改：{allowed}\n控制器已接受的变更类型：{planned_change_mode}；PPA 策略：{planned_measurement_mode}（{', '.join(planned_partitions) if planned_partitions else '全部分区'}）。\n本轮规划：\n{tail(root / 'planner.log')}\n\n只实施这一个因果假设，且不得修改允许范围之外的文件。local 模式至多修改一个 RTL 文件；architecture 模式的多文件变更必须是同一原子假设，不能顺手重构。只输出不超过 4 条中文 bullet：修改文件、具体改动、为何能解决本轮问题、功能风险。不要预测 PPA 数字。"""
        executor_rc = run(omp_command(args.model) + [executor_prompt], root / "executor.log", record_command=False)
        snapshot(after_edit)
        changes = diff(before_edit, after_edit)
        changed_paths = changed_relative_paths(before_edit, after_edit)
        rtl_changed_paths, requires_full_regression = regression_requirements(changed_paths)
        non_rtl_changed_paths = [path.as_posix() for path in changed_paths
                                 if path.parts and path.parts[0] != "rtl"]
        # An architecture experiment is inherently a cross-module risk even
        # when filenames do not hit the conservative data-path keyword list.
        # It therefore always gets prompt-0/48 before its full PPA result can
        # become evidence for the next edit.
        requires_full_regression = requires_full_regression or (
            planned_change_mode == "architecture" and bool(rtl_changed_paths))
        (root / "changes.diff").write_text(changes, encoding="utf-8")
        executor_failure = agent_failure_reason(root / "executor.log", executor_rc)
        if executor_failure:
            # A failed agent can leave a truncated write.  Keep its diff as
            # evidence, but restore the verified round-start inputs before
            # returning so the next launch cannot inherit a half edit.
            restore(before_edit)
            result = {"kind": "agent_unavailable", "attempt": attempt, "time": now(),
                      "agent_stage": "executor", "agent_failure_reason": executor_failure,
                      "agent_exit": executor_rc, "planner_exit": planner_rc,
                      "planner_modified_managed_inputs": bool(planner_violation),
                      "executor_exit": executor_rc,
                      "executor_partial_changes_reverted": bool(changes),
                      "planner_log": str((root / "planner.log").relative_to(ROOT)),
                      "executor_log": str((root / "executor.log").relative_to(ROOT)),
                      "changes": str((root / "changes.diff").relative_to(ROOT)),
                      "note": "executor failed; partial managed-input changes were restored before exit"}
            record(root, result, planner=tail(root / "planner.log"), executor=tail(root / "executor.log"))
            set_stage("agent_unavailable", attempt,
                      f"executor failed: {executor_failure}; partial changes restored; verification intentionally skipped")
            return executor_rc or 1

        if args.optimization_module:
            unexpected = [path.as_posix() for path in changed_paths if path != optimization_rtl_path]
            if unexpected:
                restore_one(optimization_baseline, optimization_rtl_path)
                result = {"kind": "policy_violation", "attempt": attempt, "time": now(),
                          "optimization_candidate_id": args.optimization_candidate_id,
                          "optimization_module": args.optimization_module,
                          "optimization_integration_target": args.optimization_integration_target,
                          "optimization_status": "rolled_back",
                          "mainline_resume": True, "unexpected_paths": unexpected,
                          "note": "优化候选越过单模块文件边界；controller 已恢复候选基线",
                          "planner_log": str((root / "planner.log").relative_to(ROOT)),
                          "executor_log": str((root / "executor.log").relative_to(ROOT)),
                          "changes": str((root / "changes.diff").relative_to(ROOT))}
                record(root, result, changes, tail(root / "planner.log"), tail(root / "executor.log"))
                set_stage("optimization_rolled_back", attempt, "candidate edited paths outside its hard scope")
                return 0

        # A local hypothesis is deliberately narrow, but an architecture
        # hypothesis may be a coherent multi-file atomic edit.  Enforce the
        # distinction after the actual diff rather than trusting prose.
        if planned_change_mode == "local" and len(rtl_changed_paths) > 1:
            restore(before_edit)
            result = {"kind": "policy_violation", "attempt": attempt, "time": now(),
                      "planner_exit": planner_rc, "executor_exit": executor_rc,
                      "rtl_changed_paths": rtl_changed_paths,
                      "note": "local 模式实际修改了多个 RTL 文件；已回退，必须由 planner 明确选择 architecture 原子实验后重试",
                      "planner_log": str((root / "planner.log").relative_to(ROOT)),
                      "executor_log": str((root / "executor.log").relative_to(ROOT)),
                      "changes": str((root / "changes.diff").relative_to(ROOT))}
            record(root, result, changes, tail(root / "planner.log"), tail(root / "executor.log"))
            set_stage("policy_violation", attempt, "local hypothesis edited multiple RTL files; changes restored")
            return 1

        # C: every RTL write receives an immediate 16-token gate.  Changes to
        # control/data-path modules additionally receive prompt-0/48-token
        # regression before any synthesis result is allowed to influence the
        # next RTL edit.  PPA-only changes retain the cheaper cadence below.
        set_stage("functional_quick", attempt, f"seq_len={args.quick_seq_len}")
        quick_rc = run_with_infra_retry(functional_command(args.quick_seq_len), root / "functional_quick.log")
        preflight_rc, ppa_rc, medium_rc, mandatory_full_rc = None, None, None, None
        counting_requested_seq_len = None
        counting_baseline_cycles = None
        counting_cycles = None
        counting_marginal = None
        measurement_scope = "UNKNOWN"
        new_measurement_candidate = False
        measurement_fingerprint = measurement_config_fingerprint()
        # Full PPA is the safe default.  Targeted PPA is permitted only for a
        # real, single-file local RTL hypothesis; every two useful targeted
        # probes are followed by full configured-partition coverage.  A
        # script-only change honors the planner's validated partition request
        # as a single-netlist smoke (one partition) or a targeted set, never a
        # full repeat.
        measurement_plan_mode, measurement_partitions, measurement_plan_source = decide_measurement_plan(
            planned_change_mode, planned_measurement_mode, planned_partitions, planned_measurement_source,
            rtl_changed_paths, non_rtl_changed_paths, args.force_full_measurement,
            targeted_measurements_since_full(), sta_closure_gate)
        sta_scope_gate = None
        if rtl_changed_paths:
            context_hints = [args.optimization_module] if args.optimization_module else []
            current_context = design_context_slice(
                ROOT, rtl_paths=rtl_changed_paths, module_hints=context_hints)
            context_modules = list((current_context.get("module_contexts") or {}).keys())
            affected_modules = (context_modules if len(context_modules) == len(rtl_changed_paths)
                                else None)
            scope_packet = conservative_rtl_change_packet(
                changes, rtl_changed_paths, change_mode=planned_change_mode,
                remaining_blackboxes=configured_top_level_blackboxes(),
                affected_modules=affected_modules, design_context=current_context)
            try:
                sta_scope_gate = sta_scope_decision(sta_scope_root, scope_packet)
            except SkillGateError as error:
                if args.optimization_module:
                    restore_one(optimization_baseline, optimization_rtl_path)
                else:
                    restore(before_edit)
                result = {"kind": "infrastructure_uncertain", "attempt": attempt, "time": now(),
                          "optimization_candidate_id": args.optimization_candidate_id,
                          "optimization_module": args.optimization_module,
                          "optimization_status": "preserved_infra_uncertain" if args.optimization_module else None,
                          "sta_scope_gate_error": str(error),
                          "note": "rtl-sta-scope gate 不可用；未启动 STA，当前改动已恢复到安全基线"}
                record(root, result, changes, tail(root / "planner.log"), tail(root / "executor.log"))
                set_stage("infra_uncertain", attempt, "rtl-sta-scope gate unavailable")
                return INFRA_UNCERTAIN_EXIT
            scope = sta_scope_gate.get("scope")
            if scope in ("top_level_partial", "top_level_complete"):
                measurement_plan_mode, measurement_partitions = "targeted", ["top_level_sta"]
            elif scope == "local_module":
                local_partition = Path(rtl_changed_paths[0]).stem
                if local_partition in valid_partitions:
                    measurement_plan_mode, measurement_partitions = "targeted", [local_partition]
                else:
                    measurement_plan_mode, measurement_partitions = "targeted", ["top_level_sta"]
                    sta_scope_gate["adapter_fallback"] = "local_partition_unavailable_use_top_level_sta"
            elif scope == "affected_cone":
                selected = [name for name in sta_scope_gate.get("affected_modules", []) if name in valid_partitions]
                measurement_plan_mode, measurement_partitions = ("targeted", selected or ["top_level_sta"])
            measurement_plan_source = "rtl-sta-scope"
        if measurement_plan_mode == "functional_counting":
            measurement_budget, measurement_budget_source = None, "controller_functional_counting_point"
        elif sta_closure_gate and not args.optimization_module:
            # The closure partition maps the whole control-plane closure; a smoke
            # must carry its declared timeout_s (not a hardcoded 600 s) or the
            # round caps the partition below its window (Round-87 evidence: 09-15
            # exit 124 at the 600 s edge).
            minimum_budget = partition_budget_floor(["top_level_sta"], measurement_budget_levels)
            if sta_closure_rerun_gate:
                previous_budget = previous.get("measurement_timeout_seconds")
                larger_budgets = (
                    [value for value in measurement_budget_levels
                     if isinstance(previous_budget, int) and value > previous_budget])
                if not larger_budgets:
                    result = {
                        "kind": "closure_measurement_blocked", "attempt": attempt,
                        "time": now(), "sta_closure_audit": sta_audit_before,
                        "note": "Closure Skill admitted a rerun but no strictly larger configured budget exists",
                    }
                    record(root, result, changes, tail(root / "planner.log"),
                           tail(root / "executor.log"))
                    set_stage("infra_uncertain", attempt,
                              "closure rerun contract conflicts with configured budget ladder")
                    return INFRA_UNCERTAIN_EXIT
                minimum_budget = max(minimum_budget, larger_budgets[0])
                measurement_budget, measurement_budget_source = measurement_budget_from_plan(
                    tail(root / "planner.log"), measurement_budget_levels,
                    args.measurement_default_timeout_seconds, previous, measurement_fingerprint,
                    minimum=minimum_budget, floor_kind="closure_monotonic_rerun")
            else:
                measurement_budget = minimum_budget
                measurement_budget_source = "controller_sta_closure_gate"
        else:
            measurement_budget_floor = (FULL_MEASUREMENT_MIN_BUDGET_SECONDS
                                        if measurement_plan_mode == "full"
                                        else partition_budget_floor(measurement_partitions, measurement_budget_levels))
            measurement_budget, measurement_budget_source = measurement_budget_from_plan(
                tail(root / "planner.log"), measurement_budget_levels, args.measurement_default_timeout_seconds,
                previous, measurement_fingerprint, minimum=measurement_budget_floor,
                floor_kind=("full_measurement" if measurement_plan_mode == "full" else "partition_timeout"))
        if quick_rc == 0:
            functional_gate_rc = 0
            if rtl_changed_paths:
                set_stage("functional_medium", attempt,
                          f"mandatory after RTL edit; seq_len={args.medium_seq_len}; files={','.join(rtl_changed_paths)}")
                medium_rc = run_with_infra_retry(functional_command(args.medium_seq_len), root / "functional_medium.log")
                functional_gate_rc = medium_rc
            elif measurement_plan_mode == "functional_counting":
                set_stage("functional_medium", attempt,
                          f"functional counting point; seq_len={args.medium_seq_len}; no managed-input change")
                # Capture the quick gate's own C(n1) before the longer count
                # overwrites the newest functional feedback slot.  This is the
                # marginal reference, never a planner-supplied number.
                quick_feedback = latest_functional_feedback_path()
                if quick_feedback is not None:
                    counting_baseline_cycles = parse_functional_counting_feedback(
                        quick_feedback.read_text(encoding="utf-8", errors="replace")).get("pass_cycles")
                medium_rc = run_with_infra_retry(functional_command(args.medium_seq_len), root / "functional_medium.log")
                functional_gate_rc = medium_rc
                if medium_rc == 0:
                    medium_feedback = latest_functional_feedback_path()
                    if medium_feedback is not None:
                        point = parse_functional_counting_feedback(
                            medium_feedback.read_text(encoding="utf-8", errors="replace"))
                        counting_requested_seq_len = point.get("requested_seq_len")
                        counting_cycles = point.get("pass_cycles")
                        counting_marginal = functional_counting_marginal(
                            counting_baseline_cycles, counting_cycles,
                            args.quick_seq_len, counting_requested_seq_len)
            if functional_gate_rc == 0 and requires_full_regression:
                set_stage("functional_full", attempt,
                          "mandatory prompt=0 48-token regression after control/data-path RTL edit")
                mandatory_full_rc = run_with_infra_retry(full_functional_command(), root / "functional_full.log")
                functional_gate_rc = mandatory_full_rc
            if functional_gate_rc == 0 and measurement_plan_mode != "functional_counting":
                set_stage("synthesis_preflight", attempt, f"Yosys lowering gate; timeout={args.preflight_timeout_seconds}s")
                preflight_rc = run(preflight_command(args.preflight_timeout_seconds), root / "preflight.log")
            if functional_gate_rc == 0 and preflight_rc == 0 and not args.preflight_only and measurement_plan_mode != "functional_counting":
                selection = ",".join(measurement_partitions) if measurement_partitions else "all"
                set_stage("ppa", attempt, f"Yosys/ABC {measurement_plan_mode}; partitions={selection}; timeout={measurement_budget}s; source={measurement_budget_source}")
                ppa_rc = run(measurement_command(measurement_budget, measurement_partitions), root / "ppa.log")
                # A total-budget timeout may happen after earlier partitions
                # completed.  Preserve their declared scope instead of
                # discarding real partial evidence solely because rc=124.
                measurement_scope = latest_measurement_scope() if ppa_rc != INFRA_EXIT else "UNKNOWN"
                if measurement_scope != "UNKNOWN":
                    measurement_candidates += 1
                    new_measurement_candidate = True
                if ppa_rc == 0 and measurement_scope == "TOP_LEVEL_COMPLETE":
                    accepted += 1
                if medium_rc is None and new_measurement_candidate and measurement_candidates % args.medium_every == 0:
                    set_stage("functional_medium", attempt, f"seq_len={args.medium_seq_len}")
                    medium_rc = run(functional_command(args.medium_seq_len), root / "functional_medium.log")

        sta_audit_after = latest_sta_closure_audit() if ppa_rc is not None else sta_audit_before
        (root / "sta_closure_audit.json").write_text(
            json.dumps(sta_audit_after, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if closure_governance_unavailable(sta_audit_after):
            result = {"kind": "infrastructure_uncertain", "attempt": attempt, "time": now(),
                      "sta_closure_audit_before": sta_audit_before,
                      "sta_closure_audit": sta_audit_after,
                      "note": "rtl-closure-governance unavailable after measurement"}
            record(root, result, changes, tail(root / "planner.log"), tail(root / "executor.log"))
            set_stage("infra_uncertain", attempt, "rtl-closure-governance unavailable after measurement")
            return INFRA_UNCERTAIN_EXIT

        optimization_status = None
        mainline_resume = False
        optimization_top_scope = bool(
            args.optimization_module
            and isinstance(sta_scope_gate, dict)
            and sta_scope_gate.get("scope") in ("top_level_partial", "top_level_complete"))
        normalized_outcome = Task02ProjectAdapter.normalize_measurement_outcome(
            results=[{
                "kind": "iteration", "planner_exit": planner_rc,
                "executor_exit": executor_rc,
                "functional_recovery_exit": functional_recovery_exit,
                "functional_quick_exit": quick_rc,
                "functional_medium_exit": medium_rc,
                "mandatory_full_exit": mandatory_full_rc,
                "preflight_exit": preflight_rc, "ppa_exit": ppa_rc,
            }],
            process_exit=0, closure_audit=sta_audit_after,
            preflight_only=args.preflight_only,
            require_timing=optimization_top_scope)
        outcome_status = str(normalized_outcome.get("status") or "")
        if args.optimization_module:
            if outcome_status == "functional_failed":
                optimization_failures += 1
                repair_attempts = max(0, optimization_failures - 1)
                if repair_attempts >= args.max_rtl_repair_attempts or attempt >= args.max_attempts:
                    restore_one(optimization_baseline, optimization_rtl_path)
                    optimization_status, mainline_resume = "rolled_back", True
                else:
                    optimization_status = "functional_repair_required"
            elif outcome_status == "measurement_incomplete":
                optimization_status = "measurement_incomplete"
            elif outcome_status in {"measurement_not_run", "verification_evidence_missing"}:
                optimization_status = "external_verification_required"
            elif outcome_status == "complete":
                optimization_status, mainline_resume = "accepted", True
            elif outcome_status in {
                    "preflight_failed", "execution_failed", "measurement_failed"}:
                restore_one(optimization_baseline, optimization_rtl_path)
                optimization_status, mainline_resume = "rolled_back", True
            elif outcome_status in {"infra_failed", "infra_uncertain"}:
                optimization_status = "preserved_infra_uncertain"
            elif not changes:
                restore_one(optimization_baseline, optimization_rtl_path)
                optimization_status, mainline_resume = "rolled_back_no_change", True

        result = {"kind": "iteration", "attempt": attempt, "time": now(), "planner_exit": planner_rc,
                  "planner_modified_managed_inputs": bool(planner_violation), "executor_exit": executor_rc,
                  "mode": mode, "managed_inputs_changed": bool(changes),
                  "rtl_changed_paths": rtl_changed_paths, "mandatory_medium_exit": medium_rc if rtl_changed_paths else None,
                  "mandatory_full_exit": mandatory_full_rc,
                  "quick_seq_len": args.quick_seq_len, "functional_quick_exit": quick_rc,
                  "functional_recovery_exit": functional_recovery_exit,
                  "preflight_exit": preflight_rc, "ppa_exit": ppa_rc,
                  "measurement_status": outcome_status,
                  "measurement_scope": measurement_scope,
                  "sta_closure_audit_before": sta_audit_before,
                  "sta_closure_audit": sta_audit_after,
                  "measurement_timeout_seconds": measurement_budget, "measurement_budget_source": measurement_budget_source,
                  "change_mode": planned_change_mode, "measurement_plan_mode": measurement_plan_mode,
                  "measurement_partitions": measurement_partitions, "measurement_plan_source": measurement_plan_source,
                  "sta_scope_gate": sta_scope_gate,
                  "optimization_candidate_id": args.optimization_candidate_id,
                  "optimization_module": args.optimization_module,
                  "optimization_integration_target": args.optimization_integration_target,
                  "optimization_status": optimization_status,
                  "optimization_repair_attempts": max(0, optimization_failures - 1) if args.optimization_module else None,
                  "mainline_resume": mainline_resume,
                  "measurement_fingerprint": measurement_fingerprint,
                  "measurement_candidates": measurement_candidates, "accepted_measurements": accepted,
                  "medium_seq_len": args.medium_seq_len if medium_rc is not None else None, "functional_medium_exit": medium_rc,
                  "functional_counting_requested_seq_len": counting_requested_seq_len,
                  "functional_counting_baseline_cycles": counting_baseline_cycles,
                  "functional_counting_cycles": counting_cycles,
                  "functional_counting_marginal_cycles_per_token": counting_marginal,
                  "planner_log": str((root / "planner.log").relative_to(ROOT)), "executor_log": str((root / "executor.log").relative_to(ROOT)),
                  "changes": str((root / "changes.diff").relative_to(ROOT)), "functional_log": str((root / "functional_quick.log").relative_to(ROOT)),
                  "preflight_log": str((root / "preflight.log").relative_to(ROOT)),
                  "ppa_log": str((root / "ppa.log").relative_to(ROOT)) if ppa_rc is not None else None}
        record(root, result, changes, tail(root / "planner.log"), tail(root / "executor.log"))
        if any(rc in (INFRA_EXIT, INFRA_UNCERTAIN_EXIT) for rc in (quick_rc, medium_rc, mandatory_full_rc, preflight_rc, ppa_rc)):
            uncertain = any(rc == INFRA_UNCERTAIN_EXIT for rc in (quick_rc, medium_rc, mandatory_full_rc, preflight_rc, ppa_rc))
            set_stage("infra_uncertain" if uncertain else "infra_failed", attempt,
                      "remote status uncertain or transport retries failed; RTL was not classified as failing")
            return INFRA_UNCERTAIN_EXIT if uncertain else INFRA_EXIT
        if args.optimization_module:
            if optimization_status in ("rolled_back", "rolled_back_no_change", "accepted"):
                set_stage("optimization_complete", attempt,
                          f"candidate={args.optimization_module}; status={optimization_status}; resume mainline")
                return 0
            if optimization_status == "measurement_incomplete":
                set_stage("measurement_incomplete", attempt,
                          f"candidate={args.optimization_module}; measurement timeout; candidate preserved")
                return MEASUREMENT_TIMEOUT_EXIT

    if args.final_regression:
        final_root = ROOT / "runs" / f"agent-02_yosys_ppa--{dt.datetime.now():%Y%m%d-%H%M%S}--final"
        final_root.mkdir(parents=True)
        set_stage("functional_full", "final", "prompt=0 full 48-token regression")
        final_command = ["bash", "scripts/remote_task02_functional.sh", "--prompt", "0", "--seq-len", "48"]
        final_log = final_root / "functional_full.log"
        final_launch_error = None
        try:
            final_rc = run_with_infra_retry(final_command, final_log)
        except (OSError, subprocess.SubprocessError) as error:
            final_rc = INFRA_EXIT
            final_launch_error = f"could not launch final regression: {type(error).__name__}: {error}"
            with final_log.open("a", encoding="utf-8") as handle:
                handle.write(f"\n[controller launch error] {final_launch_error}\n")
        (final_root / "final_regression.json").write_text(
            json.dumps({
                "kind": "final_regression",
                "time": now(),
                "exit": final_rc,
                "command": final_command,
                "log": str(final_log.relative_to(ROOT)),
                "launch_error": final_launch_error,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if final_rc:
            if final_rc == INFRA_UNCERTAIN_EXIT:
                stage = "infra_uncertain"
            elif final_rc == INFRA_EXIT:
                stage = "infra_failed"
            else:
                stage = "failed"
            detail = (final_launch_error if final_launch_error else
                      f"48-token regression failed; exit={final_rc}; log={final_log.relative_to(ROOT)}")
            set_stage(stage, "final", detail)
            return final_rc
    set_stage("complete", args.max_attempts, "controller completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
