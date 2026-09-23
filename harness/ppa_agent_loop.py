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
from flash_campaign_contract import (CampaignContractError, actual_changes_error,
                                     load_campaign_contract, parse_work_package)
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
MANAGED_PATHS = (
    Path("rtl"), Path("harness"), Path("scripts"), Path("tasks"), Path("tests"), Path("docs"),
    Path("tb"), Path("tools"), Path("archive"), Path("projects"), Path("reference.py"),
    Path("README.md"), Path("harness.toml"), Path("questionAndsolution.md"), Path(".gitignore"),
)
INFRA_EXIT = 75
INFRA_UNCERTAIN_EXIT = 76
MEASUREMENT_TIMEOUT_EXIT = 124
INFRA_RETRIES = 2
DEFAULT_MEASUREMENT_BUDGET_SECONDS = 180
DEFAULT_MEASUREMENT_BUDGET_LEVELS = (180, 600, 1800, 3600, 7200)
FULL_MEASUREMENT_MIN_BUDGET_SECONDS = 1800
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


def decide_measurement_plan(
    verification_level: str,
    contract_partitions: List[str],
    rtl_changed_paths: List[str],
    non_rtl_changed_paths: List[str],
    sta_closure_gate: bool = False,
) -> Tuple[str, List[str], str]:
    """Derive the concrete project measurement from the immutable verification level."""
    # A real top-level STA closure failure is a prerequisite failure, not an
    # optimization candidate.  Do not spend a generic full-PPA milestone or a
    # functional-counting point while a deterministic audit says the closure
    # itself cannot be read/linked/timed.  One top_level_sta smoke (at its
    # partition-timeout budget) is the smallest falsifiable verification.
    if sta_closure_gate and verification_level in {"sta_targeted", "sta_full"}:
        return "smoke", ["top_level_sta"], "controller_sta_closure_gate"
    if verification_level == "sta_full":
        return "full", [], "pro_campaign_contract"
    if verification_level != "sta_targeted":
        return ("functional_counting" if verification_level == "functional" else
                "preflight_only" if verification_level == "functional_preflight" else
                "none"), [], "pro_campaign_contract"
    if not rtl_changed_paths and not non_rtl_changed_paths and not contract_partitions:
        return "functional_counting", [], "controller_functional_counting_point"
    if rtl_changed_paths:
        return "targeted", contract_partitions, "pro_campaign_contract"
    if not rtl_changed_paths and non_rtl_changed_paths:
        if len(contract_partitions) == 1:
            return "smoke", contract_partitions, "pro_campaign_contract"
        return "targeted", contract_partitions, "pro_campaign_contract"
    if not rtl_changed_paths and not non_rtl_changed_paths:
        return "targeted", contract_partitions, "pro_campaign_contract"
    return "targeted", contract_partitions, "pro_campaign_contract"


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
        elif result["kind"] == "contract_violation":
            handle.write("### Campaign contract 违规：本轮已停止\n\n")
            handle.write(f"- 原因：{result.get('reason', result.get('detail', '未测得'))}\n")
            handle.write(f"- 检测到 RTL 改动：{', '.join(result.get('rtl_changed_paths', []))}\n")
            handle.write(f"- 详细证据：`{root.relative_to(ROOT)}/result.json`、`changes.diff` 与 AI 日志。\n")
        elif result["kind"] == "audit_step":
            handle.write("### 只读 Flash 工作包完成\n\n")
            handle.write(concise_agent_reply(planner, 1600) + "\n")
            handle.write(f"- 受管输入改动：{result.get('managed_inputs_changed', False)}。\n")
            handle.write(f"- 详细证据：`{root.relative_to(ROOT)}/result.json` 与 `work_package.json`。\n")
        elif result["kind"] == "strategy_revision_required":
            handle.write("### Campaign 已到战略边界\n\n")
            handle.write(f"- 原因：{result.get('reason', '未测得')}。\n")
            handle.write("- 当前 controller 未扩大范围，下一次行动必须由 Pro 重新决策。\n")
        elif result["kind"] == "baseline":
            handle.write("### 基线真实结果\n\n")
            handle.write(f"- 功能快检（{result['quick_seq_len']} token）：exit {result['functional_exit']}\n")
            handle.write(f"- 综合预检：exit {result.get('preflight_exit', '未运行')}；完整 PPA：exit {result.get('ppa_exit', '未运行')}（状态 {result.get('measurement_status', '未记录')}）\n")
            handle.write(f"- 详细证据：`{root.relative_to(ROOT)}/result.json` 与对应工具日志。\n")
        else:
            handle.write("### Controller 边界结果\n\n")
            handle.write(f"- kind={result.get('kind')}；原因={result.get('reason', result.get('note', '未测得'))}。\n")
            handle.write(f"- 详细证据：`{root.relative_to(ROOT)}/result.json`。\n")
        handle.write("\n")


def baseline(seq_len: int, preflight_only: bool, preflight_timeout: int,
             measurement_budget: int,
             measurement_partitions: Optional[List[str]] = None
             ) -> Tuple[int, Optional[int], Optional[int]]:
    root = ROOT / "runs" / f"agent-02_yosys_ppa--{dt.datetime.now():%Y%m%d-%H%M%S}--baseline"
    root.mkdir(parents=True)
    set_stage("baseline_functional", "baseline", f"seq_len={seq_len}")
    functional = run_with_infra_retry(functional_command(seq_len), root / "functional.log")
    preflight, ppa = None, None
    if functional == 0:
        set_stage("synthesis_preflight", "baseline", f"Yosys lowering gate; timeout={preflight_timeout}s")
        preflight = run(preflight_command(preflight_timeout), root / "preflight.log")
        if preflight == 0 and not preflight_only:
            selection = ",".join(measurement_partitions) if measurement_partitions else "all"
            set_stage("baseline_measurement", "baseline",
                      f"Yosys/ABC probe; partitions={selection}; timeout={measurement_budget}s")
            ppa = run(measurement_command(
                measurement_budget, measurement_partitions), root / "ppa.log")
    audit = latest_sta_closure_audit() if ppa is not None else {}
    normalized_outcome = Task02ProjectAdapter.normalize_measurement_outcome(
        results=[{"kind": "baseline", "functional_exit": functional,
                  "preflight_exit": preflight, "ppa_exit": ppa}],
        process_exit=0, closure_audit=audit, preflight_only=preflight_only)
    result = {"kind": "baseline", "time": now(), "quick_seq_len": seq_len, "functional_exit": functional,
              "preflight_exit": preflight, "ppa_exit": ppa, "measurement_timeout_seconds": measurement_budget,
              "measurement_plan_mode": ("targeted" if measurement_partitions else
                                        "full" if ppa is not None else "preflight_only"),
              "measurement_partitions": list(measurement_partitions or []),
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
    if preflight_rc == 0:
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


def main() -> int:
    global CLOSURE_GOVERNANCE_ROOT, MEASUREMENT_BUDGET_CEILING_SECONDS
    signal.signal(signal.SIGINT, stop_active_process)
    signal.signal(signal.SIGTERM, stop_active_process)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model")
    parser.add_argument("--quick-seq-len", type=int, default=8)
    parser.add_argument("--medium-seq-len", type=int, default=16)
    parser.add_argument("--medium-every", type=int, default=3)
    parser.add_argument("--preflight-timeout-seconds", type=int, default=600)
    parser.add_argument("--measurement-default-timeout-seconds", type=int, default=DEFAULT_MEASUREMENT_BUDGET_SECONDS,
                        help="initial Yosys/ABC probe budget; the planner may select a permitted larger tier")
    parser.add_argument("--measurement-budget-levels", default=",".join(map(str, DEFAULT_MEASUREMENT_BUDGET_LEVELS)),
                        help="comma-separated permitted measurement budgets in seconds")
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--campaign-contract", type=Path, required=True,
                        help="immutable Pro campaign contract JSON inside this project")
    parser.add_argument("--forced-blackbox-expansion-module",
                        help="controller-owned verification of one module removed from top_level_sta blackboxes")
    parser.add_argument("--sta-scope-skill", type=Path,
                        default=default_skill_path("rtl-sta-scope", STA_SCOPE_ENV),
                        help=f"RTLskill rtl-sta-scope directory (also configurable via {STA_SCOPE_ENV})")
    parser.add_argument("--closure-governance-skill", type=Path,
                        default=default_skill_path("rtl-closure-governance", CLOSURE_GOVERNANCE_ENV),
                        help=("RTLskill rtl-closure-governance directory "
                              f"(also configurable via {CLOSURE_GOVERNANCE_ENV})"))
    args = parser.parse_args()
    try:
        campaign = load_campaign_contract(args.campaign_contract, ROOT)
    except CampaignContractError as error:
        parser.error(str(error))
    try:
        measurement_budget_levels = tuple(sorted({int(value) for value in args.measurement_budget_levels.split(",")}))
    except ValueError:
        parser.error("--measurement-budget-levels must be comma-separated positive integers")
    if (args.quick_seq_len < 1 or args.medium_seq_len < args.quick_seq_len or args.medium_every < 1
            or args.preflight_timeout_seconds < 1 or not measurement_budget_levels
            or any(value < 1 for value in measurement_budget_levels)
            or args.measurement_default_timeout_seconds not in measurement_budget_levels):
        parser.error("invalid sequence-length policy")
    verification_level = str(campaign["verification_level"])
    ceiling = campaign.get("measurement_budget_ceiling_seconds")
    if verification_level in {"sta_targeted", "sta_full"}:
        if ceiling not in measurement_budget_levels:
            parser.error("campaign measurement ceiling is not in the configured budget ladder")
        measurement_budget_levels = tuple(
            value for value in measurement_budget_levels if value <= int(ceiling))
    MEASUREMENT_BUDGET_CEILING_SECONDS = max(measurement_budget_levels)
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
    task += ("\n\n## 上层 Pro 的不可变 campaign contract\n\n```json\n"
             + json.dumps(campaign, ensure_ascii=False, indent=2) + "\n```\n")
    valid_partitions = configured_partitions()
    if not valid_partitions:
        parser.error("harness/ppa.toml contains no [[partitions]] entries")
    requested_partitions = campaign.get("measurement_partitions")
    requested_partitions = requested_partitions if isinstance(requested_partitions, list) else []
    if any(partition not in valid_partitions for partition in requested_partitions):
        parser.error("campaign contains an unavailable Task-02 measurement partition")

    if args.forced_blackbox_expansion_module:
        if "top_level_sta" not in valid_partitions:
            parser.error("blackbox expansion requires the top_level_sta partition")
        return forced_blackbox_expansion(args.forced_blackbox_expansion_module, args, measurement_budget_levels)

    if not args.skip_baseline and verification_level != "none":
        baseline_partitions = (requested_partitions
                               if verification_level == "sta_targeted" else None)
        baseline_preflight_only = (
            verification_level not in {"sta_targeted", "sta_full"}
            or (verification_level == "sta_targeted" and not baseline_partitions))
        baseline_functional, baseline_preflight, baseline_ppa = baseline(
            args.quick_seq_len, baseline_preflight_only,
            args.preflight_timeout_seconds,
            args.measurement_default_timeout_seconds,
            baseline_partitions)
        if baseline_functional in (INFRA_EXIT, INFRA_UNCERTAIN_EXIT):
            set_stage("infra_failed", "baseline", "functional baseline transport failed or remains uncertain")
            return baseline_functional
        if baseline_functional != 0:
            set_stage("failed", "baseline", "functional baseline failed; no PPA optimization edit was started")
            return 1
        if baseline_preflight != 0 or (baseline_ppa is not None and baseline_ppa != 0):
            set_stage("toolchain_repair", "baseline", "functional baseline passed but Yosys preflight or PPA failed")

    measurement_candidates, accepted = persisted_measurement_counters()
    for attempt in range(1, int(campaign["max_steps"]) + 1):
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
        mode = str(campaign["repair_domain"])
        mutation_policy = str(campaign["mutation_policy"])
        allowed_paths = [str(path) for path in campaign["allowed_paths"]]
        allowed = ", ".join(allowed_paths) if allowed_paths else "只读；禁止修改任何受管输入"
        stage = "auditing" if mutation_policy == "read_only" else "editing"
        if (sta_audit_before.get("strategy_change_required") is True
                or (sta_closure_rerun_gate and mode != "measurement")):
            result = {
                "kind": "strategy_revision_required", "attempt": attempt, "time": now(),
                "mode": mode, "campaign_contract_id": campaign["contract_id"],
                "sta_closure_audit": sta_audit_before,
                "reason": "closure_skill_requires_campaign_strategy_revision",
            }
            record(root, result)
            set_stage("strategy_revision_required", attempt,
                      "Closure evidence crossed the immutable campaign boundary")
            return 0

        # A: review/plan is read-only. Any accidental RTL write is restored.
        before_plan, after_plan = root / "before_plan", root / "after_plan"
        snapshot(before_plan)
        set_stage("planning", attempt, "read-only AI review of controller-produced results")
        sta_gate_instruction = ""
        if sta_closure_gate:
            retry_note = ("当前属于 timeout/truncation 的测量未完成状态，不是 closure 导出缺陷；"
                          "同一测量只能提出严格提高合法预算的 increase_budget，不能修改 RTL。"
                          if sta_closure_rerun_gate else
                          "当前 Closure Skill 硬门仍有效；必须保持 campaign 边界，若所需修复越界则请求 Pro。")
            sta_gate_instruction = ("\n控制器 STA 闭合硬门已开启。当前审计如下；本轮只允许一个能让 phase 状态迁移的假设，"
                                    + retry_note +
                                    " controller 将执行 top_level_sta 单网表 smoke，禁止 full PPA 里程碑。\n```json\n"
                                    + json.dumps(sta_audit_before, ensure_ascii=False, indent=2) + "\n```\n")
        budget_instruction = ""
        if verification_level in {"sta_targeted", "sta_full"}:
            budget_instruction = (
                "\n如果本工作包需要 STA，最后另起一行输出 `measurement_budget_seconds: N`。"
                f"N 只能是 {', '.join(map(str, measurement_budget_levels))} 之一，且不得超过 campaign 上限。"
                "只有新证据证明更长档位必要时才升级；相同测量输入不得无理由降档。\n")
        planner_prompt = f"""你是 Task 02 的只读战术规划者。不得修改文件、运行命令、改变 campaign contract，或扩大修复域。\n\n{task}\n\n上一轮控制器真实结果：\n```json\n{json.dumps(previous, ensure_ascii=False, indent=2)}\n```\n\n最新功能验证 feedback：\n```text\n{latest_functional_feedback()}\n```\n\n最新 PPA feedback：\n```text\n{latest_ppa_feedback()}\n```{sta_gate_instruction}\n只为当前 campaign 提出一个有边界的工作包。可以在一个工作包内阅读上下文、实施相关修改并接受契约规定的验证，但不得改变 repair_domain、mutation_policy、verification_level、允许路径或预算上限。\n\n先用不超过 6 条中文 bullet 说明上一轮事实、唯一因果假设、本轮边界和预期证据。最后必须输出一个 fenced JSON，且不能增加字段：\n```json\n{{\n  "kind": "flash_work_package",\n  "disposition": "execute|escalate",\n  "escalation_reason": "execute 时为 none；否则逐字选择 campaign 的一个 escalation_conditions",\n  "access": "read_only|edit",\n  "verification_request": "none|contract",\n  "objective": "本轮有界目标",\n  "hypothesis": "一个可证伪因果假设",\n  "planned_paths": ["准备修改的项目相对文件；只读工作包为空列表"],\n  "expected_evidence": "完成或证伪本轮假设所需的证据"\n}}\n```\nplanned_paths 必须是具体文件，不能写未获授权路径。local 最多一个文件；read_only 必须为空。edit 必须使用 contract 验证；read_only 可以选择 none 或 contract。若完成目标必须越界，disposition 必须为 escalate、access=read_only、verification_request=none 且 planned_paths 必须为空，controller 将停止当前 campaign 并交回 Pro。{budget_instruction}\n没有真实数字时必须写“未测得”。禁止根据源码猜测面积、时序、频率或吞吐数值。"""
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
        if planner_violation:
            result = {
                "kind": "contract_violation", "attempt": attempt, "time": now(),
                "campaign_contract_id": campaign["contract_id"],
                "reason": "planner_modified_managed_inputs",
                "planner_log": str((root / "planner.log").relative_to(ROOT)),
            }
            record(root, result, planner=tail(root / "planner.log"))
            set_stage("contract_violation", attempt,
                      "read-only planner modified managed inputs; changes restored")
            return 1
        try:
            work_package = parse_work_package(tail(root / "planner.log"), campaign)
        except CampaignContractError as error:
            result = {
                "kind": "contract_violation", "attempt": attempt, "time": now(),
                "campaign_contract_id": campaign["contract_id"],
                "reason": "invalid_flash_work_package", "detail": str(error),
                "planner_log": str((root / "planner.log").relative_to(ROOT)),
            }
            record(root, result, planner=tail(root / "planner.log"))
            set_stage("contract_violation", attempt, str(error))
            return 1
        (root / "work_package.json").write_text(
            json.dumps(work_package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if work_package.get("disposition") == "escalate":
            result = {
                "kind": "strategy_revision_required", "attempt": attempt, "time": now(),
                "mode": mode, "campaign_contract_id": campaign["contract_id"],
                "reason": work_package.get("escalation_reason"),
                "work_package": work_package,
                "planner_log": str((root / "planner.log").relative_to(ROOT)),
            }
            record(root, result, planner=tail(root / "planner.log"))
            set_stage("strategy_revision_required", attempt,
                      str(work_package.get("escalation_reason")))
            return 0
        step_read_only = work_package.get("access") == "read_only"
        step_verification_level = (
            verification_level
            if work_package.get("verification_request") == "contract" else "none")

        # B: controller, not AI, owns every command. Toolchain repair has a deliberately small extra allow-list.
        before_edit, after_edit = root / "before_edit", root / "after_edit"
        snapshot(before_edit)
        set_stage("auditing" if step_read_only else stage, attempt,
                  "executing one validated work package inside the immutable campaign")
        if step_read_only:
            executor_rc = 0
            (root / "executor.log").write_text(
                "read_only campaign: planner work package is the completed audit; executor was not invoked\n",
                encoding="utf-8")
        else:
            executor_prompt = f"""你是 Task 02 实施者。不得运行任何命令，也不得改变 campaign contract。\n\n{task}\n\n本轮允许修改：{allowed}\n本轮已校验工作包：\n```json\n{json.dumps(work_package, ensure_ascii=False, indent=2)}\n```\n\n只实施该工作包。实际修改必须是 planned_paths 的子集；不得顺手修改其他文件、扩大修复域或切换架构层级。可以在这些文件内完成实现所需的相关修改。只输出不超过 4 条中文 bullet：实际修改、与假设的关系、保留的不变量和风险。不要预测 PPA 数字。"""
            executor_rc = run(omp_command(args.model) + [executor_prompt], root / "executor.log", record_command=False)
        snapshot(after_edit)
        changes = diff(before_edit, after_edit)
        changed_paths = changed_relative_paths(before_edit, after_edit)
        rtl_changed_paths, requires_full_regression = regression_requirements(changed_paths)
        non_rtl_changed_paths = [path.as_posix() for path in changed_paths
                                 if path.parts and path.parts[0] != "rtl"]
        # An architecture campaign is inherently a cross-module risk even
        # when filenames do not hit the conservative data-path keyword list.
        # It therefore always gets prompt-0/48 before its full PPA result can
        # become evidence for the next edit.
        requires_full_regression = requires_full_regression or (
            mutation_policy == "architecture" and bool(rtl_changed_paths))
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

        contract_error = actual_changes_error(
            [path.as_posix() for path in changed_paths], work_package, campaign)
        if contract_error is not None:
            restore(before_edit)
            result = {"kind": "contract_violation", "attempt": attempt, "time": now(),
                      "planner_exit": planner_rc, "executor_exit": executor_rc,
                      "rtl_changed_paths": rtl_changed_paths,
                      "campaign_contract_id": campaign["contract_id"],
                      "work_package": work_package, "reason": contract_error,
                      "note": "实际 diff 超出已批准工作包；改动已回退，当前 campaign 停止",
                      "planner_log": str((root / "planner.log").relative_to(ROOT)),
                      "executor_log": str((root / "executor.log").relative_to(ROOT)),
                      "changes": str((root / "changes.diff").relative_to(ROOT))}
            record(root, result, changes, tail(root / "planner.log"), tail(root / "executor.log"))
            set_stage("contract_violation", attempt, contract_error)
            return 1

        if step_verification_level == "none":
            result = {
                "kind": "audit_step", "attempt": attempt, "time": now(),
                "mode": mode, "campaign_contract_id": campaign["contract_id"],
                "work_package": work_package, "managed_inputs_changed": bool(changes),
                "planner_exit": planner_rc, "executor_exit": executor_rc,
                "planner_log": str((root / "planner.log").relative_to(ROOT)),
                "executor_log": str((root / "executor.log").relative_to(ROOT)),
                "changes": str((root / "changes.diff").relative_to(ROOT)),
                "note": "read-only audit work package completed without project verification",
            }
            record(root, result, changes, tail(root / "planner.log"), tail(root / "executor.log"))
            continue

        # C: the immutable contract selects the verification level. Every RTL
        # write still receives the mandatory functional gates before synthesis
        # or STA can become evidence for another tactical step.
        set_stage("functional_quick", attempt, f"seq_len={args.quick_seq_len}")
        quick_rc = run_with_infra_retry(
            functional_command(args.quick_seq_len), root / "functional_quick.log")
        preflight_rc, ppa_rc, medium_rc, mandatory_full_rc = None, None, None, None
        counting_requested_seq_len = None
        counting_baseline_cycles = None
        counting_cycles = None
        counting_marginal = None
        measurement_scope = "UNKNOWN"
        new_measurement_candidate = False
        measurement_fingerprint = measurement_config_fingerprint()
        contract_partitions = [str(item) for item in campaign["measurement_partitions"]]
        measurement_plan_mode, measurement_partitions, measurement_plan_source = decide_measurement_plan(
            step_verification_level, contract_partitions, rtl_changed_paths,
            non_rtl_changed_paths, sta_closure_gate)
        sta_scope_gate = None
        if rtl_changed_paths and step_verification_level in {"sta_targeted", "sta_full"}:
            current_context = design_context_slice(
                ROOT, rtl_paths=rtl_changed_paths, module_hints=[])
            context_modules = list((current_context.get("module_contexts") or {}).keys())
            affected_modules = (context_modules if len(context_modules) == len(rtl_changed_paths)
                                else None)
            scope_packet = conservative_rtl_change_packet(
                changes, rtl_changed_paths, change_mode=mutation_policy,
                remaining_blackboxes=configured_top_level_blackboxes(),
                affected_modules=affected_modules, design_context=current_context)
            try:
                sta_scope_gate = sta_scope_decision(sta_scope_root, scope_packet)
            except SkillGateError as error:
                restore(before_edit)
                result = {"kind": "infrastructure_uncertain", "attempt": attempt, "time": now(),
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
            if (step_verification_level == "sta_targeted"
                    and (measurement_plan_mode not in {"targeted", "smoke"}
                         or not measurement_partitions)):
                restore(before_edit)
                result = {
                    "kind": "strategy_revision_required", "attempt": attempt,
                    "time": now(), "campaign_contract_id": campaign["contract_id"],
                    "reason": "verification_scope_change_required",
                    "sta_scope_gate": sta_scope_gate,
                    "note": (
                        "rtl-sta-scope did not produce a legal targeted partition; "
                        "the controller refused to widen the campaign to full STA"),
                }
                record(root, result, changes, tail(root / "planner.log"),
                       tail(root / "executor.log"))
                set_stage("strategy_revision_required", attempt,
                          "targeted STA scope unavailable inside the immutable campaign")
                return 0
        measurement_budget = None
        measurement_budget_source = "not_requested_by_campaign"
        if measurement_plan_mode == "functional_counting":
            measurement_budget, measurement_budget_source = None, "controller_functional_counting_point"
        elif measurement_plan_mode in {"preflight_only", "none"}:
            measurement_budget, measurement_budget_source = None, "not_requested_by_campaign"
        elif sta_closure_gate:
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
                        "kind": "strategy_revision_required", "attempt": attempt,
                        "time": now(), "sta_closure_audit": sta_audit_before,
                        "campaign_contract_id": campaign["contract_id"],
                        "reason": "campaign_budget_ceiling_blocks_closure_rerun",
                        "note": "Closure Skill admitted a rerun but the campaign has no larger permitted budget",
                    }
                    record(root, result, changes, tail(root / "planner.log"),
                           tail(root / "executor.log"))
                    set_stage("strategy_revision_required", attempt,
                              "closure rerun exceeds the immutable campaign ceiling")
                    return 0
                minimum_budget = max(minimum_budget, larger_budgets[0])
                measurement_budget, measurement_budget_source = measurement_budget_from_plan(
                    tail(root / "planner.log"), measurement_budget_levels,
                    args.measurement_default_timeout_seconds, previous, measurement_fingerprint,
                    minimum=minimum_budget, floor_kind="closure_monotonic_rerun")
            else:
                measurement_budget = minimum_budget
                measurement_budget_source = "controller_sta_closure_gate"
        else:
            if measurement_plan_mode == "full":
                measurement_budget_floor = FULL_MEASUREMENT_MIN_BUDGET_SECONDS
            else:
                raw_timeouts = configured_partition_timeouts()
                required_timeout = max(
                    (raw_timeouts.get(name, 0) for name in measurement_partitions), default=0)
                if required_timeout > max(measurement_budget_levels):
                    result = {
                        "kind": "strategy_revision_required", "attempt": attempt,
                        "time": now(), "campaign_contract_id": campaign["contract_id"],
                        "reason": "campaign_budget_ceiling_below_partition_timeout",
                        "required_timeout_seconds": required_timeout,
                        "campaign_ceiling_seconds": max(measurement_budget_levels),
                    }
                    record(root, result, changes, tail(root / "planner.log"),
                           tail(root / "executor.log"))
                    set_stage("strategy_revision_required", attempt,
                              "selected partition exceeds the immutable campaign ceiling")
                    return 0
                measurement_budget_floor = partition_budget_floor(
                    measurement_partitions, measurement_budget_levels)
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
            needs_preflight = step_verification_level in {
                "functional_preflight", "sta_targeted", "sta_full"}
            needs_sta = step_verification_level in {"sta_targeted", "sta_full"}
            if functional_gate_rc == 0 and needs_preflight \
                    and measurement_plan_mode != "functional_counting":
                set_stage("synthesis_preflight", attempt, f"Yosys lowering gate; timeout={args.preflight_timeout_seconds}s")
                preflight_rc = run(preflight_command(args.preflight_timeout_seconds), root / "preflight.log")
            if (functional_gate_rc == 0 and preflight_rc == 0 and needs_sta
                    and measurement_plan_mode != "functional_counting"):
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
            preflight_only=step_verification_level in {"functional", "functional_preflight"})
        outcome_status = str(normalized_outcome.get("status") or "")

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
                  "change_mode": mutation_policy, "measurement_plan_mode": measurement_plan_mode,
                  "measurement_partitions": measurement_partitions, "measurement_plan_source": measurement_plan_source,
                  "campaign_contract_id": campaign["contract_id"],
                  "campaign_operation_id": campaign["operation_id"],
                  "work_package": work_package,
                  "sta_scope_gate": sta_scope_gate,
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

    set_stage("complete", campaign["max_steps"], "immutable Flash campaign completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
