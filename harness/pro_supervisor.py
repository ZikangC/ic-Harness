#!/usr/bin/env python3
"""Schedule Task-02 guidance, Skill campaigns, and bounded project execution.

A stop request is just a file, so it never kills an in-flight functional/PPA
command: the supervisor observes it at the next execution boundary.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional

from sta_evidence_extract import (PPA_RUN_ID_RE, inspect_latest_measured_sta, latest_measured_sta_run,
                                  query_sta_timing_paths)
from rtl_skill_bridge import (CLOSURE_GOVERNANCE_ENV, MODULE_EVIDENCE_REGISTRY_ENV,
                              OPTIMIZATION_TRIAGE_ENV, STA_SCOPE_ENV, SkillGateError,
                              TASK02_MEASUREMENT_INPUTS, compact_stage_evidence_record,
                              content_fingerprint,
                              closure_governance_result, default_skill_path,
                              measurement_descriptor,
                              optimization_execute_request,
                              optimization_run_campaign)
from module_registry_adapter import (reconcile_project_registry,
                                     registry_mainline)
from pro_guidance_runner import (EXPAND_BLACKBOX_ACTION,
                                 RUN_EVIDENCE_ACTION, ProDecisionRejected,
                                 ProGuidanceError, ProGuidanceInputError,
                                 ProGuidanceRunner, ProModelInvocationError,
                                 ProOutputError)
from project_adapter_api import ProjectAdapterError, load_project_adapter


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks/02_yosys_ppa.md"
REPORT = ROOT / "reports/02_yosys_ppa/progress.md"
PRO_REPORT = ROOT / "reports/02_yosys_ppa/pro_progress.md"
RUNS = ROOT / "runs"
STATUS = ROOT / "status/pro_supervisor.status"
PENDING_VERIFICATION = ROOT / "status/task02_pending_verification.json"
OPTIMIZATION_STATE = ROOT / "status/task02_optimization_state.json"
OPTIMIZATION_OBSERVATION = ROOT / "status/task02_optimization_pending_observation.json"
PPA_CONTROLLER = ROOT / "harness/ppa_agent_loop.py"
OPTIMIZATION_EXECUTOR = ROOT / "harness/task02_optimization_executor.py"
DEFAULT_EVIDENCE_CONTRACT = ROOT / "harness/evidence_contract.json"
PRO_ACTION_RATIONALE_CHARS = 3200
READ_ONLY_EVIDENCE_ACTION = "inspect_existing_sta_evidence"
EXPANSION_PARTITION = "top_level_sta"
STAGE_EVIDENCE_WORKFLOW_ENV = "RTLSKILL_STAGE_EVIDENCE_WORKFLOW"
STAGE_EVIDENCE_MAX_ARTIFACT_BYTES = 262144
STAGE_EVIDENCE_PROMPT_MAX_CHARS = 180000
STAGE_EVIDENCE_MAX_FACTS_PER_SKILL = 16
STAGE_EVIDENCE_MAX_TOTAL_FACTS = 60
PRO_STAGE_EVIDENCE_CONTEXT_CHARS = 3600
INFRA_EXIT = 75
INFRA_UNCERTAIN_EXIT = 76
INCREASE_BUDGET_ACTION = "increase_budget"
MAX_CLOSURE_MEASUREMENT_BUDGET_SECONDS = 7200
CLOSURE_GATE_TIMEOUT_SECONDS = 120
ACTION_SCOPES = {
    "revise_task_card": (Path("tasks/02_yosys_ppa.md"), Path("questionAndsolution.md")),
    "record_reusable_knowledge": (Path("questionAndsolution.md"),),
    "repair_harness": (
        Path("harness/environment.py"), Path("harness/ppa_agent_loop.py"), Path("harness/pro_supervisor.py"),
        Path("harness/rtl_skill_bridge.py"), Path("harness/evidence_contract.json"),
        Path("harness/ppa_measure.ys"), Path("harness/ppa_preflight.ys"), Path("harness/ppa.toml"),
        Path("harness/ppa_leaves_blackbox.v"), Path("harness/parse_partition_memory.awk"),
        Path("harness/parse_sta_timing.awk"),
        Path("scripts/remote_yosys_round.sh"), Path("scripts/remote_task02_functional.sh"),
        Path("scripts/remote_reconcile.sh"),
        Path("scripts/remote_status.sh"), Path("scripts/remote_cancel.sh"),
        Path("scripts/remote_diagnose.sh"), Path("scripts/request_pro_supervisor_stop.sh"),
        Path("tests/test_ppa_scheduler.sh"), Path("tests/test_remote_functional_transport.sh"),
        Path("docs/pro_supervisor.md"), Path("questionAndsolution.md"),
    ),
}
GUARDED_ITEMS = (
    Path("harness"), Path("scripts"), Path("tasks"), Path("rtl"), Path("tb"), Path("tools"), Path("docs"),
    Path("reports"), Path("reference.py"), Path("harness.toml"), Path("questionAndsolution.md"),
)
ACTIVE: Optional[subprocess.Popen] = None
CURRENT_SESSION: Optional[str] = None
CLOSURE_GOVERNANCE_ROOT: Optional[Path] = None
CLOSURE_FEEDBACK_PATH: Optional[Path] = None
CLOSURE_ARTIFACT_PATH: Optional[Path] = None
CLOSURE_PROBE_PATH: Optional[Path] = None
CLOSURE_NETLIST_PATH: Optional[Path] = None
CLOSURE_TOP: Optional[str] = None
CLOSURE_REQUIRED_BODIES: tuple[str, ...] = ()
STA_SCOPE_ROOT: Optional[Path] = None
OPTIMIZATION_TRIAGE_ROOT: Optional[Path] = None


def timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def write_status(session: str, stage: str, detail: str) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(
        f"task=02_yosys_ppa\nsession={session}\nstage={stage}\nupdated_at={timestamp()}\ndetail={detail}\n",
        encoding="utf-8",
    )


def load_pending_verification() -> Dict[str, object]:
    """Load a durable harness change that must be measured before more edits."""
    try:
        value = json.loads(PENDING_VERIFICATION.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict) or value.get("kind") != "harness":
        return {}
    paths = value.get("paths")
    summary = value.get("summary")
    if not isinstance(paths, list) or not paths or not all(isinstance(path, str) for path in paths):
        return {}
    if not isinstance(summary, str) or not summary.strip():
        return {}
    return value


def save_pending_verification(paths: Iterable[str], summary: str, expected_phase: str) -> Dict[str, object]:
    allowed = {path.as_posix() for path in ACTION_SCOPES["repair_harness"]}
    clean_paths = sorted({path for path in paths if path in allowed})
    clean_summary = summary.strip()
    if not clean_paths:
        raise ValueError("pending harness verification requires at least one allowed harness path")
    if not clean_summary or len(clean_summary) > 1200:
        raise ValueError("pending harness verification summary must contain 1..1200 characters")
    value: Dict[str, object] = {
        "kind": "harness",
        "paths": clean_paths,
        "summary": clean_summary,
        "expected_phase": expected_phase.strip() or "sta_timing_measured|sta_no_timing_path",
        "partition": "top_level_sta",
        "timeout_seconds": 1800,  # top_level_sta smoke budget = partition timeout floor
        "declared_at": timestamp(),
    }
    PENDING_VERIFICATION.parent.mkdir(parents=True, exist_ok=True)
    PENDING_VERIFICATION.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return value


def clear_pending_verification() -> None:
    try:
        PENDING_VERIFICATION.unlink()
    except FileNotFoundError:
        pass


def save_optimization_observation(observation: Mapping[str, object]) -> None:
    """Atomically deliver one opaque project result to the owning Skill."""
    OPTIMIZATION_OBSERVATION.parent.mkdir(parents=True, exist_ok=True)
    if OPTIMIZATION_OBSERVATION.exists():
        raise RuntimeError(
            "unconsumed Optimization observation envelope already exists")
    temporary = OPTIMIZATION_OBSERVATION.with_name(
        f".{OPTIMIZATION_OBSERVATION.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(dict(observation), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        os.replace(temporary, OPTIMIZATION_OBSERVATION)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def tail(path: Path, limit: int = 16000) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[-limit:] if path.exists() else "（无记录）"


def bounded_text(text: str, limit: int, *, marker: str = "（前文已截断）\n") -> str:
    """Keep model context bounded while retaining the newest evidence."""
    if len(text) <= limit:
        return text
    return marker + text[-max(0, limit - len(marker)):]


def append_pro_progress(report: Path, session: str, cycle: int, phase: str, *,
                        raw_output: str = "", details: Optional[Dict[str, object]] = None) -> None:
    """Append a human-readable Pro timeline without replacing raw run evidence."""
    report.parent.mkdir(parents=True, exist_ok=True)
    if not report.exists():
        report.write_text(
            "# Task 02 Pro supervisor progress\n\n"
            "本文件是 Pro 战略判断与受限动作的累积摘要；每条原始证据仍在对应 session 的 runs 目录。\n",
            encoding="utf-8",
        )
    entry = [f"\n## {timestamp()} · session `{session}` · cycle {cycle} · {phase}\n"]
    if details is not None:
        entry.extend(("\n### 结构化记录\n\n", "```json\n",
                      json.dumps(details, ensure_ascii=False, indent=2), "\n```\n"))
    if raw_output:
        # Avoid allowing an OMP response containing a Markdown fence to terminate this block early.
        safe_output = raw_output.replace("```", "``\\`")
        entry.extend(("\n### 原始 Pro 输出\n\n", "```text\n", safe_output.rstrip(), "\n```\n"))
    with report.open("a", encoding="utf-8") as handle:
        handle.write("".join(entry))


def run_logged(command: list[str], log: Path, *, record_command: bool = True) -> int:
    """Run a child while preserving only useful, unambiguous log content.

    OMP prompts contain the same fenced JSON schema used for model decisions.
    Recording that full prompt after an unflushed child response makes a naive
    "last JSON block" parser select its own template rather than the model's
    answer.  Pro calls therefore retain raw model output but not their prompt.
    """
    global ACTIVE
    with log.open("w", encoding="utf-8") as handle:
        if record_command:
            handle.write("$ " + " ".join(command) + "\n\n")
        else:
            handle.write("# OMP 模型原始输出（调用 prompt 不在本日志重复保存）\n\n")
        # The child inherits this descriptor.  Flush before it starts so a
        # parent-side buffered header cannot be written after child output.
        handle.flush()
        ACTIVE = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                  stdout=handle, stderr=subprocess.STDOUT,
                                  start_new_session=True)
        try:
            return ACTIVE.wait()
        finally:
            ACTIVE = None


def default_evidence_workflow_path() -> Path:
    """Locate the portable RTLskill workflow without coupling the projects."""
    configured = os.environ.get(STAGE_EVIDENCE_WORKFLOW_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return ROOT.parent / "RTLskill" / "stage-evidence-workflow"


def default_closure_governance_path() -> Path:
    """Locate the portable RTLskill closure gate without coupling projects."""
    return default_skill_path("rtl-closure-governance", CLOSURE_GOVERNANCE_ENV)


def configure_closure_governance(*, skill_root: Path,
                                 feedback_path: Optional[Path] = None,
                                 artifact_path: Optional[Path] = None,
                                 probe_path: Optional[Path] = None,
                                 netlist_path: Optional[Path] = None,
                                 top: Optional[str] = None,
                                 required_bodies: Iterable[str] = ()) -> None:
    """Set the evidence paths used by the external closure gate.

    Paths are caller-owned inputs.  The gate only reads them; it never launches
    synthesis/STA, performs transport, or copies a netlist into a prompt.
    """
    global CLOSURE_GOVERNANCE_ROOT, CLOSURE_FEEDBACK_PATH, CLOSURE_ARTIFACT_PATH
    global CLOSURE_PROBE_PATH, CLOSURE_NETLIST_PATH, CLOSURE_TOP, CLOSURE_REQUIRED_BODIES
    CLOSURE_GOVERNANCE_ROOT = skill_root.expanduser().resolve()
    CLOSURE_FEEDBACK_PATH = feedback_path.expanduser().resolve() if feedback_path else None
    CLOSURE_ARTIFACT_PATH = artifact_path.expanduser().resolve() if artifact_path else None
    CLOSURE_PROBE_PATH = probe_path.expanduser().resolve() if probe_path else None
    CLOSURE_NETLIST_PATH = netlist_path.expanduser().resolve() if netlist_path else None
    CLOSURE_TOP = top.strip() if isinstance(top, str) and top.strip() else None
    CLOSURE_REQUIRED_BODIES = tuple(item.strip() for item in required_bodies if item.strip())


def closure_governance_paths() -> Dict[str, object]:
    """Return configured paths without embedding raw logs or netlist content."""
    root = CLOSURE_GOVERNANCE_ROOT or default_closure_governance_path().expanduser().resolve()
    feedback = CLOSURE_FEEDBACK_PATH or newest_feedback_path()
    return {
        "skill_root": root.as_posix(),
        "gate": (root / "closure_gate.py").as_posix(),
        "feedback": feedback.as_posix() if feedback else None,
        "artifact": CLOSURE_ARTIFACT_PATH.as_posix() if CLOSURE_ARTIFACT_PATH else None,
        "probe": CLOSURE_PROBE_PATH.as_posix() if CLOSURE_PROBE_PATH else None,
        "netlist": CLOSURE_NETLIST_PATH.as_posix() if CLOSURE_NETLIST_PATH else None,
        "top": CLOSURE_TOP,
        "required_bodies": list(CLOSURE_REQUIRED_BODIES),
    }


def resolve_evidence_model(evidence_model: Optional[str], flash_model: str) -> str:
    """Use the execution model by default, while allowing an explicit override."""
    return (evidence_model or flash_model).strip()


def stop_child(signum: int, _frame: object) -> None:
    cleanup = "no active child"
    if ACTIVE is not None and ACTIVE.poll() is None:
        try:
            os.killpg(ACTIVE.pid, signum)
        except ProcessLookupError:
            pass
        try:
            ACTIVE.wait(timeout=50)
            cleanup = "child stopped"
        except subprocess.TimeoutExpired:
            cleanup = "child stop timeout"
            try:
                subprocess.run(
                    ["bash", "scripts/remote_cancel.sh"], cwd=ROOT,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=45, check=False)
                cleanup += "; remote cancel requested"
            except (OSError, subprocess.TimeoutExpired):
                cleanup += "; remote cancel uncertain"
    if CURRENT_SESSION:
        try:
            write_status(CURRENT_SESSION, "cancelled",
                         f"supervisor received signal {signum}; {cleanup}")
        except OSError:
            pass
    raise SystemExit(128 + signum)


def newest_feedback_path() -> Optional[Path]:
    candidates = sorted(RUNS.glob("02_yosys_ppa--*/feedback.txt"), key=lambda path: path.stat().st_mtime)
    return candidates[-1] if candidates else None


def latest_result_data() -> Dict[str, object]:
    """Read the controller's minimal execution state; never present it directly to Pro."""
    candidates = sorted(RUNS.glob("agent-02_yosys_ppa--*/result.json"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        return {}
    try:
        value = json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    fields = (
        "kind", "mode", "managed_inputs_changed", "expanded_module", "change_mode", "rtl_changed_paths",
        "functional_exit", "functional_quick_exit",
        "functional_medium_exit", "mandatory_full_exit", "preflight_exit", "ppa_exit", "measurement_scope",
        "measurement_status", "measurement_timeout_seconds", "measurement_plan_mode", "measurement_partitions",
        "measurement_fingerprint", "measurement_budget_source", "measurement_plan_source", "agent_failure_reason",
        "functional_counting_requested_seq_len", "functional_counting_baseline_cycles",
        "functional_counting_cycles", "functional_counting_marginal_cycles_per_token",
        "verification_status", "campaign_id", "module", "operation",
        "campaign_contract_id", "campaign_operation_id", "work_package", "reason",
        "sta_scope_gate",
        "sta_closure_audit", "sta_closure_audit_before",
    )
    return {field: value.get(field) for field in fields if field in value}


def latest_functional_regression_passed() -> bool:
    """Require the newest recorded functional gate to be a real pass.

    A baseline result uses ``functional_exit`` while an iteration uses
    ``functional_quick_exit``.  Optional medium/full gates are checked when
    present; infrastructure exits are not treated as functional passes.
    """
    result = latest_result_data()
    quick = result.get("functional_quick_exit")
    if quick is None:
        quick = result.get("functional_exit")
    if quick != 0:
        return False
    return all(result.get(field) in (None, 0)
               for field in ("functional_medium_exit", "mandatory_full_exit"))


def registry_mainline_pending(state: Mapping[str, object]) -> Optional[Dict[str, object]]:
    """Resolve the authoritative pending mainline from the Registry hot view."""
    hot = state.get("module_registry_hot_view")
    hot = hot if isinstance(hot, Mapping) else {}
    transaction = hot.get("pending_transaction")
    if not isinstance(transaction, Mapping):
        return None
    module = str(transaction.get("module_id") or transaction.get("module") or "")
    if not module:
        return None
    return {
        "transaction_id": (transaction.get("transaction_id")
                           or f"blackbox_expansion:{transaction.get('partition') or EXPANSION_PARTITION}:{module}"),
        "kind": transaction.get("kind") or "blackbox_expansion",
        "module": module,
        "partition": str(transaction.get("partition") or EXPANSION_PARTITION),
        "phase": transaction.get("phase") or "verification_pending",
    }


def latest_sta_closure_audit() -> Dict[str, object]:
    """Return the newest audit from the sole portable closure classifier."""
    return controller_closure_audit()


def closure_policy_context(state: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    """Translate project controller state into the portable closure policy API."""
    current = dict(state or {})
    result = latest_result_data()
    raw_timeout_seconds = result.get("measurement_timeout_seconds")
    timeout_seconds = (raw_timeout_seconds
                       if isinstance(raw_timeout_seconds, int)
                       and not isinstance(raw_timeout_seconds, bool)
                       and raw_timeout_seconds > 0 else None)
    exhausted = measurement_descriptor(
        semantic_fingerprint=result.get("measurement_fingerprint"),
        scope_kind=result.get("measurement_plan_mode"),
        targets=result.get("measurement_partitions"),
        operation_kind="measurement",
        intent="exhausted_measurement",
        budget_seconds=result.get("measurement_timeout_seconds"),
    )
    pending = registry_mainline_pending(current) or {}
    pending_partition = str(pending.get("partition") or "").strip()
    proposed = measurement_descriptor(
        semantic_fingerprint=(content_fingerprint(ROOT, TASK02_MEASUREMENT_INPUTS)
                              if pending_partition else ""),
        scope_kind="targeted" if pending_partition else "",
        targets=[pending_partition] if pending_partition else [],
        operation_kind="measurement",
        intent="verify_pending_transaction",
        budget_seconds=pending.get("timeout_seconds"),
    )
    return {
        "measurement_budget_seconds": timeout_seconds,
        "max_measurement_budget_seconds": MAX_CLOSURE_MEASUREMENT_BUDGET_SECONDS,
        "exhausted_measurement": exhausted,
        "proposed_measurement": proposed,
    }


def controller_closure_audit(state: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    """Run the portable closure gate; no project-local classifier is allowed."""
    paths = closure_governance_paths()
    audit = closure_governance_result(
        Path(str(paths["skill_root"])),
        feedback_path=Path(str(paths["feedback"])) if paths.get("feedback") else None,
        artifact_path=Path(str(paths["artifact"])) if paths.get("artifact") else None,
        probe_path=Path(str(paths["probe"])) if paths.get("probe") else None,
        netlist_path=Path(str(paths["netlist"])) if paths.get("netlist") else None,
        top=str(paths["top"]) if paths.get("top") else None,
        required_bodies=paths.get("required_bodies", []),
        policy_context=closure_policy_context(state) if state is not None else None,
        timeout=CLOSURE_GATE_TIMEOUT_SECONDS,
    )
    audit["closure_governance_phase"] = str(audit.get("phase", "not_attempted"))
    audit["closure_governance"] = {
        "skill_root": paths["skill_root"],
        "gate": paths["gate"],
        "gate_api_version": audit.get("gate_api_version"),
        "phase": audit["closure_governance_phase"],
    }
    audit["audit_source"] = "rtl-closure-governance"
    return audit


def latest_queryable_sta_run_id() -> str:
    """Expose one canonical immutable source so Pro never guesses an agent run id."""
    return latest_measured_sta_run() or "unavailable"


def resolve_sta_evidence_run_id(requested: str) -> Optional[str]:
    """Resolve the safe alias without allowing a model-controlled filesystem path."""
    if requested == "latest_measured_sta":
        return latest_measured_sta_run()
    return requested if PPA_RUN_ID_RE.fullmatch(requested) else None


def needs_no_path_probe(audit: Dict[str, object]) -> bool:
    """One no-path result without structural facts is not actionable evidence."""
    return audit.get("diagnostic_required") is True


def digest_tree(relative: Path) -> str:
    digest = hashlib.sha256()
    source = ROOT / relative
    paths = [source] if source.is_file() else sorted(path for path in source.rglob("*") if path.is_file()) if source.exists() else []
    for path in paths:
        if "__pycache__" in path.parts or path.name == ".DS_Store":
            continue
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def evidence_snapshot(state: Optional[Dict[str, object]] = None,
                      external_evidence: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    source_paths = (Path("rtl"), Path("harness"), Path("scripts"), Path("tasks"), Path("harness.toml"),
                    Path("questionAndsolution.md"))
    external = external_evidence or {"manifest": {}}
    value: Dict[str, object] = {
        "sta_closure_audit": ((state or {}).get("latest_closure_governance_audit")
                              or controller_closure_audit(state)),
        "pending_verification": load_pending_verification(),
        "latest_sta_evidence": (state or {}).get("latest_sta_evidence") or None,
        "latest_run_evidence": (state or {}).get("latest_run_evidence") or None,
        "latest_stage_evidence": compact_stage_evidence_record(
            (state or {}).get("latest_stage_evidence")),
        "latest_queryable_sta_run_id": latest_queryable_sta_run_id(),
        "external_evidence": external.get("manifest", {}),
        "stage_evidence_request_fingerprint": (state or {}).get(
            "stage_evidence_request_fingerprint", ""),
        "source": {path.as_posix(): digest_tree(path) for path in source_paths},
    }
    value["fingerprint"] = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return value


def experiment_signature(result: Dict[str, object]) -> str:
    fields = {key: result.get(key) for key in (
        "measurement_fingerprint", "measurement_timeout_seconds", "measurement_plan_mode", "measurement_partitions",
        "measurement_scope", "ppa_exit",
    )}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def record_stage_trend(state: Dict[str, object], *, cycle: int, batch: int, flash_rc: int,
                       after_sta_audit: Dict[str, object], new_results: list[Dict[str, object]]) -> None:
    """Retain at most three structured observations for optional trend review."""
    latest = new_results[-1] if new_results else {}
    raw_records = state.get("recent_stage_trend")
    records = [item for item in raw_records if isinstance(item, dict)] if isinstance(raw_records, list) else []
    records.append({
        "cycle": cycle,
        "batch": batch,
        "new_results": len(new_results),
        "flash_exit": flash_rc,
        "phase": after_sta_audit.get("phase", "not_attempted"),
        "sta_link_result": after_sta_audit.get("sta_link_result"),
        "sta_timing_measured": after_sta_audit.get("sta_timing_measured"),
        "measurement_fingerprint": latest.get("measurement_fingerprint"),
        "measurement_status": latest.get("measurement_status", state.get("last_measurement_status")),
        "measurement_scope": latest.get("measurement_scope"),
        "ppa_exit": latest.get("ppa_exit"),
        "expanded_module": latest.get("expanded_module"),
    })
    state["recent_stage_trend"] = records[-STAGE_TREND_MAX_ITEMS:]


def record_project_execution(
        state: Dict[str, object], *, cycle: int, process_exit: int,
        new_results: list[Dict[str, object]], attempts: int,
        before_sta_audit: Mapping[str, object],
        project_adapter,
        apply_pending_flags: bool = False,
        account_as_flash: bool = True) -> Dict[str, object]:
    """Apply controller-owned accounting without interpreting domain phases."""
    after_sta_audit = controller_closure_audit(state)
    state["latest_closure_governance_audit"] = after_sta_audit
    normalized_outcome = project_adapter.normalize_measurement_outcome(
        results=new_results, process_exit=process_exit,
        closure_audit=after_sta_audit)
    measurement_incomplete = normalized_outcome.get("measurement_incomplete") is True
    if apply_pending_flags and bool(state.get("pending_sta_smoke")):
        old_phase = str(before_sta_audit.get("phase", "not_attempted"))
        new_phase = str(after_sta_audit.get("phase", "not_attempted"))
        state["sta_unchanged_smokes"] = (
            int(state.get("sta_unchanged_smokes", 0)) + 1
            if new_phase == old_phase else 0)
        state["last_sta_phase"] = new_phase
        state["pending_sta_smoke"] = False
        if new_results:
            clear_pending_verification()
            state["pending_verification"] = None
    if apply_pending_flags and bool(state.get("pending_no_path_probe")) and new_results:
        state["pending_no_path_probe"] = False
    state["last_measurement_status"] = str(
        normalized_outcome.get("status") or "not_attempted")
    state["ppa_budget_seconds"] = int(state["ppa_budget_seconds"]) + sum(
        int(result.get("measurement_timeout_seconds") or 0)
        for result in new_results if result.get("ppa_exit") is not None)
    if not account_as_flash:
        return {
            "after_sta_audit": after_sta_audit,
            "normalized_outcome": normalized_outcome,
            "measurement_incomplete": measurement_incomplete,
            "batch_number": int(state["flash_batches"]),
            "outcome": "",
            "repeat_count": 0,
        }
    batch_number = int(state["flash_batches"]) + 1
    state["flash_batches"] = batch_number
    state["flash_attempts"] = int(state["flash_attempts"]) + attempts
    outcome_value = latest_result_data()
    outcome = hashlib.sha256(json.dumps(
        outcome_value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    state["stagnant_batches"] = (
        int(state["stagnant_batches"]) + 1
        if outcome == state["last_outcome"] else 0)
    state["last_outcome"] = outcome
    record_stage_trend(
        state, cycle=cycle, batch=batch_number, flash_rc=process_exit,
        after_sta_audit=after_sta_audit, new_results=new_results)
    signature = experiment_signature(outcome_value)
    experiments = state["experiments"]
    assert isinstance(experiments, dict)
    previous = experiments.get(signature, {})
    repeat_count = (int(previous.get("count", 0)) + 1
                    if previous.get("outcome") == outcome else 1)
    experiments[signature] = {"count": repeat_count, "outcome": outcome}
    return {
        "after_sta_audit": after_sta_audit,
        "normalized_outcome": normalized_outcome,
        "measurement_incomplete": measurement_incomplete,
        "batch_number": batch_number,
        "outcome": outcome,
        "repeat_count": repeat_count,
    }


def elapsed_minutes(started_at: str) -> float:
    try:
        return max(0.0, (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(started_at)).total_seconds() / 60.0)
    except ValueError:
        return 0.0


def copy_scope(destination: Path, paths: Iterable[Path]) -> None:
    for relative in paths:
        source = ROOT / relative
        target = destination / relative
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def hash_scope(paths: Iterable[Path]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for relative in paths:
        source = ROOT / relative
        if source.is_file():
            result[relative.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
        else:
            result[relative.as_posix()] = "<missing>"
    return result


def snapshot_guard(destination: Path) -> None:
    """Snapshot all editable sources so an out-of-scope Pro write is reversible."""
    for relative in GUARDED_ITEMS:
        source, target = ROOT / relative, destination / relative
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def hash_guard() -> Dict[str, str]:
    result: Dict[str, str] = {}
    for relative in GUARDED_ITEMS:
        source = ROOT / relative
        if source.is_file():
            result[relative.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
        elif source.is_dir():
            for path in sorted(source.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts and path.name != ".DS_Store":
                    result[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def restore_guard(snapshot: Path) -> None:
    for relative in GUARDED_ITEMS:
        source, target = snapshot / relative, ROOT / relative
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def restore_scope(snapshot: Path, paths: Iterable[Path]) -> None:
    for relative in paths:
        source, target = snapshot / relative, ROOT / relative
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif target.exists():
            target.unlink()


def execute_pro_action(session_root: Path, decision: Dict[str, object], pro_model: str) -> bool:
    action = str(decision["action"])
    allowed = ACTION_SCOPES[action]
    session_root.mkdir(parents=True, exist_ok=True)
    before = session_root / f"before_{action}"
    guard = session_root / f"guard_before_{action}"
    copy_scope(before, allowed)
    snapshot_guard(guard)
    before_hash = hash_scope(allowed)
    guard_hash = hash_guard()
    prompt = f"""你是 Task 02 的 Pro 实施者。不得运行命令。只实施已批准的上层动作 `{action}`。
允许修改且只能修改：{', '.join(path.as_posix() for path in allowed)}。
当前 STA 闭合审计（由可配置的 rtl-closure-governance gate 产生；若非终态，修改必须预期让 phase 迁移；完整日志仍在 runs）：
{json.dumps(controller_closure_audit(), ensure_ascii=False, indent=2)}
动作依据（已限长，完整战略输出在 session 的 pro_decision 日志中）：
{bounded_text(str(decision.get('rationale', '')), PRO_ACTION_RATIONALE_CHARS)}
保持现有任务边界、回归与 PPA 事实口径；不可修改 RTL 或服务器运行状态。若动作是
record_reusable_knowledge，必须向 questionAndsolution.md 追加一条可跨项目复用的规则，包含证据、
适用条件与预防方式；不得复制逐轮流水账或未证实猜测。完成后用中文简述改动和风险。"""
    rc = run_logged(["omp", "-p", "--cwd", str(ROOT), "--model", pro_model, prompt],
                    session_root / "pro_action.log", record_command=False)
    after_hash = hash_scope(allowed)
    changed = [path for path in after_hash if before_hash[path] != after_hash[path]]
    after_guard = hash_guard()
    guard_changes = (set(guard_hash) ^ set(after_guard)) | {
        path for path in set(guard_hash) & set(after_guard) if guard_hash[path] != after_guard[path]
    }
    unexpected = sorted(guard_changes - {path.as_posix() for path in allowed})
    (session_root / "pro_action.json").write_text(
        json.dumps({"action": action, "exit": rc, "changed": changed, "unexpected": unexpected}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if unexpected:
        restore_guard(guard)
        return False
    if rc != 0:
        restore_scope(before, allowed)
        return False
    return True


def request_stop(session: str) -> int:
    root = RUNS / f"pro_supervisor--{session}"
    if not root.is_dir():
        raise SystemExit(f"unknown supervisor session: {session}")
    (root / "stop.request").write_text(f"requested_at={timestamp()}\n", encoding="utf-8")
    print(f"stop requested for {session}; the active Flash batch will finish before stopping")
    return 0


def active_local_controllers() -> str:
    """Do not compete with the original project or another supervisor session."""
    result = subprocess.run(
        ["pgrep", "-af",
         r"python3 .*harness/(ppa_agent_loop|agent_loop|task02_optimization_executor)\.py"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def result_paths() -> list[Path]:
    return sorted(RUNS.glob("agent-02_yosys_ppa--*/result.json"), key=lambda path: path.stat().st_mtime)


def results_since(before: set[Path]) -> list[Dict[str, object]]:
    values = []
    for path in result_paths():
        if path in before:
            continue
        try:
            values.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return values


def execute_project_plan_command(
        plan: Mapping[str, object], *, directive_path: Path,
        controller_log: Path) -> Dict[str, object]:
    """Run one already-authorized project command and collect raw artifacts.

    This is deliberately policy-free: it does not inspect Optimization phases,
    select actions, or mutate Skill state.
    """
    command = plan.get("command")
    if not isinstance(command, list) or not command:
        raise ProjectAdapterError("project execution plan has no command")
    directive_path.write_text(
        str(plan.get("directive") or "只执行当前已授权请求；禁止扩大修改范围。\n"),
        encoding="utf-8")
    active = active_local_controllers()
    if active:
        return {
            "boundary": {
                "kind": "concurrent_controller",
                "detail": active.replace("\n", " | "),
            }
        }
    before_results = set(result_paths())
    launch_error = None
    try:
        process_exit = run_logged([str(item) for item in command], controller_log)
    except (OSError, subprocess.SubprocessError) as error:
        process_exit = INFRA_EXIT
        launch_error = (
            f"could not launch project controller: {type(error).__name__}: {error}")
        with controller_log.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[supervisor launch error] {launch_error}\n")
    return {
        "process_exit": process_exit,
        "results": results_since(before_results),
        "controller_launch_error": launch_error,
        "controller_log": controller_log.as_posix(),
    }


def save_state(session_root: Path, state: Dict[str, object]) -> None:
    (session_root / "session_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pro-model", help="OMP selector for the strategic Pro model")
    parser.add_argument("--flash-model", help="OMP selector for the Flash execution model")
    parser.add_argument("--evidence-model",
                        help="OMP selector for the current-stage Evidence Model; defaults to --flash-model")
    parser.add_argument("--evidence-workflow", type=Path, default=default_evidence_workflow_path(),
                        help="RTLskill stage-evidence-workflow directory (also configurable via "
                             f"{STAGE_EVIDENCE_WORKFLOW_ENV})")
    parser.add_argument("--evidence-stage", default="sta_health",
                        help="stage contract passed to the Evidence Model (default: sta_health)")
    parser.add_argument("--evidence-contract", type=Path, default=DEFAULT_EVIDENCE_CONTRACT,
                        help="project-owned JSON contract for the selected Evidence stage")
    parser.add_argument("--evidence-max-artifact-bytes", type=int, default=STAGE_EVIDENCE_MAX_ARTIFACT_BYTES,
                        help="maximum bytes included from each current-run evidence artifact")
    parser.add_argument("--evidence-prompt-max-chars", type=int, default=STAGE_EVIDENCE_PROMPT_MAX_CHARS,
                        help="maximum Evidence Model prompt characters")
    parser.add_argument("--evidence-max-facts-per-skill", type=int,
                        default=STAGE_EVIDENCE_MAX_FACTS_PER_SKILL,
                        help="maximum current-stage facts exposed from each Skill")
    parser.add_argument("--evidence-max-total-facts", type=int,
                        default=STAGE_EVIDENCE_MAX_TOTAL_FACTS,
                        help="maximum Skill facts exposed to one Evidence Model call")
    parser.add_argument("--closure-governance-skill", type=Path, default=default_closure_governance_path(),
                        help="RTLskill rtl-closure-governance directory (also configurable via "
                             f"{CLOSURE_GOVERNANCE_ENV})")
    parser.add_argument("--closure-feedback", type=Path,
                        help="deterministic STA feedback.txt; defaults to the newest Task-02 feedback")
    parser.add_argument("--closure-artifact", type=Path,
                        help="optional closure artifact JSON collected on the execution host")
    parser.add_argument("--closure-probe", type=Path,
                        help="optional tool-probe JSON produced by the closure Skill")
    parser.add_argument("--closure-netlist", type=Path,
                        help="optional local closure netlist for structural inventory")
    parser.add_argument("--closure-top",
                        help="configured top name, required when --closure-netlist is supplied")
    parser.add_argument("--closure-required-body", action="append", default=[], metavar="MODULE",
                        help="required non-blackbox body for closure inventory; repeat as needed")
    parser.add_argument("--sta-scope-skill", type=Path,
                        default=default_skill_path("rtl-sta-scope", STA_SCOPE_ENV),
                        help=f"RTLskill rtl-sta-scope directory (also configurable via {STA_SCOPE_ENV})")
    parser.add_argument("--optimization-triage-skill", type=Path,
                        default=default_skill_path("rtl-optimization-triage", OPTIMIZATION_TRIAGE_ENV),
                        help=("RTLskill rtl-optimization-triage directory (also configurable via "
                              f"{OPTIMIZATION_TRIAGE_ENV})"))
    parser.add_argument("--module-registry-skill", type=Path,
                        default=default_skill_path(
                            "rtl-module-evidence-registry", MODULE_EVIDENCE_REGISTRY_ENV),
                        help=("RTLskill rtl-module-evidence-registry directory (also configurable via "
                              f"{MODULE_EVIDENCE_REGISTRY_ENV})"))
    parser.add_argument("--max-rtl-repair-attempts", type=int, default=2,
                        help="repair attempts for one optimization candidate before automatic rollback")
    parser.add_argument("--max-optimization-measurement-retries", type=int, default=2,
                        help="inconclusive measurements permitted after the initial optimization measurement")
    parser.add_argument("--max-optimization-edit-attempts", type=int, default=2,
                        help="single-file edit attempts before the optimization candidate is deferred")
    parser.add_argument("--max-optimization-steps", type=int, default=12,
                        help="maximum internal edit/verify/retry/rollback steps in one optimization campaign")
    parser.add_argument("--optimization-minimum-value-score", type=float, default=0.5,
                        help="deterministic score threshold for autonomous stage-1 optimization")
    parser.add_argument("--optimization-minimum-improvement-ratio", type=float, default=0.01,
                        help="minimum comparable area or delay improvement required for acceptance")
    parser.add_argument("--optimization-maximum-regression-ratio", type=float, default=0.01,
                        help="maximum allowed regression in the other comparable PPA metric")
    parser.add_argument("--max-cycles", type=int, default=24, help="maximum Pro decisions in this session")
    parser.add_argument("--max-stagnant-batches", type=int, default=2,
                        help="stop after this many identical semantic Flash outcomes")
    parser.add_argument("--max-identical-measurements", type=int, default=2,
                        help="stop after this many identical measurement-config/outcome pairs")
    parser.add_argument("--max-flash-attempts-total", type=int, default=72,
                        help="total Flash controller attempts allowed in one session")
    parser.add_argument("--max-ppa-budget-seconds", type=int, default=86400,
                        help="sum of controller-requested PPA budgets allowed in one session")
    parser.add_argument("--max-wall-clock-minutes", type=int, default=10080,
                        help="wall-clock cap; checked only at batch boundaries")
    parser.add_argument("--request-stop", metavar="SESSION", help="write a deferred stop request and exit")
    parser.add_argument("--declare-pending-harness-change", action="store_true",
                        help="record an externally implemented harness change that must receive one smoke before Pro may edit again")
    parser.add_argument("--pending-change-path", action="append", default=[], metavar="PATH",
                        help="allowed harness path changed externally; repeat for multiple paths")
    parser.add_argument("--pending-change-summary", default="", metavar="TEXT",
                        help="concise verified change summary shown to Pro and Flash")
    parser.add_argument("--pending-change-expected-phase", default="sta_timing_measured|sta_no_timing_path", metavar="PHASE",
                        help="expected closure phase after the one forced smoke")
    parser.add_argument("--status", action="store_true", help="show the current supervisor status and exit")
    args = parser.parse_args()
    if args.request_stop:
        return request_stop(args.request_stop)
    if args.declare_pending_harness_change:
        try:
            value = save_pending_verification(args.pending_change_path, args.pending_change_summary,
                                              args.pending_change_expected_phase)
        except ValueError as error:
            parser.error(str(error))
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    if args.status:
        print(tail(STATUS, 4000))
        return 0
    if not args.pro_model or not args.flash_model:
        parser.error("--pro-model and --flash-model are required to start a session")
    try:
        project_adapter = load_project_adapter(ROOT / "harness.toml", ROOT)
    except ProjectAdapterError as error:
        parser.error(str(error))
    guidance_runner = ProGuidanceRunner(ROOT)
    evidence_model = resolve_evidence_model(args.evidence_model, args.flash_model)
    workflow_root = args.evidence_workflow.expanduser()
    if not workflow_root.is_absolute():
        workflow_root = ROOT / workflow_root
    workflow_root = workflow_root.resolve()
    evidence_script = workflow_root / "scripts" / "evidence_workflow.py"
    if not evidence_script.is_file():
        parser.error(f"stage-evidence-workflow script not found: {evidence_script}")
    evidence_contract = args.evidence_contract.expanduser()
    if not evidence_contract.is_absolute():
        evidence_contract = ROOT / evidence_contract
    evidence_contract = evidence_contract.resolve()
    if not evidence_contract.is_file():
        parser.error(f"Evidence stage contract not found: {evidence_contract}")
    try:
        evidence_contract_value = json.loads(evidence_contract.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        parser.error(f"Evidence stage contract is unreadable: {error}")
    if not isinstance(evidence_contract_value, dict):
        parser.error("Evidence stage contract must contain one JSON object")
    if evidence_contract_value.get("stage") != args.evidence_stage:
        parser.error("Evidence stage contract stage must match --evidence-stage")
    closure_root = args.closure_governance_skill.expanduser()
    if not closure_root.is_absolute():
        closure_root = ROOT / closure_root
    closure_root = closure_root.resolve()
    closure_gate = closure_root / "closure_gate.py"
    if not closure_gate.is_file():
        parser.error(f"rtl-closure-governance closure_gate.py not found: {closure_gate}")
    sta_scope_root = args.sta_scope_skill.expanduser()
    if not sta_scope_root.is_absolute():
        sta_scope_root = ROOT / sta_scope_root
    sta_scope_root = sta_scope_root.resolve()
    if not (sta_scope_root / "scope_gate.py").is_file():
        parser.error(f"rtl-sta-scope scope_gate.py not found: {sta_scope_root / 'scope_gate.py'}")
    optimization_root = args.optimization_triage_skill.expanduser()
    if not optimization_root.is_absolute():
        optimization_root = ROOT / optimization_root
    optimization_root = optimization_root.resolve()
    if not (optimization_root / "optimization_gate.py").is_file():
        parser.error(f"rtl-optimization-triage gate not found: {optimization_root / 'optimization_gate.py'}")
    module_registry_root = args.module_registry_skill.expanduser()
    if not module_registry_root.is_absolute():
        module_registry_root = ROOT / module_registry_root
    module_registry_root = module_registry_root.resolve()
    if not (module_registry_root / "registry_gate.py").is_file():
        parser.error(f"rtl-module-evidence-registry gate not found: {module_registry_root / 'registry_gate.py'}")
    global STA_SCOPE_ROOT, OPTIMIZATION_TRIAGE_ROOT
    STA_SCOPE_ROOT, OPTIMIZATION_TRIAGE_ROOT = sta_scope_root, optimization_root

    def resolve_optional_path(value: Optional[Path]) -> Optional[Path]:
        if value is None:
            return None
        resolved = value.expanduser()
        return (ROOT / resolved).resolve() if not resolved.is_absolute() else resolved.resolve()

    closure_feedback = resolve_optional_path(args.closure_feedback)
    closure_artifact = resolve_optional_path(args.closure_artifact)
    closure_probe = resolve_optional_path(args.closure_probe)
    closure_netlist = resolve_optional_path(args.closure_netlist)
    for label, path in (("--closure-feedback", closure_feedback), ("--closure-artifact", closure_artifact),
                        ("--closure-probe", closure_probe), ("--closure-netlist", closure_netlist)):
        if path is not None and not path.is_file():
            parser.error(f"{label} must name an existing regular file: {path}")
    if closure_netlist is not None and not args.closure_top:
        parser.error("--closure-netlist requires --closure-top")
    configure_closure_governance(
        skill_root=closure_root, feedback_path=closure_feedback, artifact_path=closure_artifact,
        probe_path=closure_probe, netlist_path=closure_netlist, top=args.closure_top,
        required_bodies=args.closure_required_body,
    )
    if (args.evidence_max_artifact_bytes < 1024 or args.evidence_prompt_max_chars < 4096
            or args.evidence_max_facts_per_skill < 1 or args.evidence_max_total_facts < 1):
        parser.error("Evidence Model input limits are too small")
    limits = (args.max_cycles, args.max_stagnant_batches, args.max_identical_measurements,
              args.max_flash_attempts_total, args.max_ppa_budget_seconds, args.max_wall_clock_minutes,
              args.max_rtl_repair_attempts,
              args.max_optimization_measurement_retries, args.max_optimization_edit_attempts,
              args.max_optimization_steps)
    if any(value < 1 for value in limits):
        parser.error("all governance limits and cadences must be positive")
    if args.optimization_minimum_value_score < 0:
        parser.error("--optimization-minimum-value-score must be non-negative")
    if not (0 <= args.optimization_minimum_improvement_ratio <= 1):
        parser.error("--optimization-minimum-improvement-ratio must be between 0 and 1")
    if not (0 <= args.optimization_maximum_regression_ratio <= 1):
        parser.error("--optimization-maximum-regression-ratio must be between 0 and 1")

    active = active_local_controllers()
    if active:
        print("refusing to start: another local harness controller is active:\n" + active, file=sys.stderr)
        return 3

    signal.signal(signal.SIGINT, stop_child)
    signal.signal(signal.SIGTERM, stop_child)
    session = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    global CURRENT_SESSION
    CURRENT_SESSION = session
    session_root = RUNS / f"pro_supervisor--{session}"
    session_root.mkdir(parents=True)
    initial_sta_audit = controller_closure_audit()
    (session_root / "closure_gate_initial.json").write_text(
        json.dumps(initial_sta_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        initial_registry = reconcile_project_registry(
            skill_root=module_registry_root, session_id=session, cycle_id=0,
            phase="session_start", controller_state={})
    except (SkillGateError, RuntimeError, OSError, ValueError) as error:
        write_status(session, "infra_uncertain", f"RTL registry/optimization governance unavailable: {error}")
        return INFRA_UNCERTAIN_EXIT
    state: Dict[str, object] = {
        "session": session,
        "started_at": timestamp(),
        "guidance_cycles": 0,
        "scheduler_steps": 0,
        "flash_batches": 0,
        "flash_attempts": 0,
        "ppa_budget_seconds": 0,
        "last_measurement_status": "not_attempted",
        "last_continue_evidence": "",
        "last_outcome": "",
        "stagnant_batches": 0,
        "pending_sta_smoke": bool(load_pending_verification()),
        "pending_no_path_probe": needs_no_path_probe(initial_sta_audit),
        "pending_verification": load_pending_verification() or None,
        "latest_sta_evidence": None,
        "latest_run_evidence": None,
        "latest_stage_evidence": None,
        "evidence_contract": evidence_contract.as_posix(),
        "closure_governance": closure_governance_paths(),
        "sta_scope_skill": sta_scope_root.as_posix(),
        "optimization_triage_skill": optimization_root.as_posix(),
        "module_registry_skill": module_registry_root.as_posix(),
        "module_registry_hot_view": initial_registry.get("hot_view"),
        "optimization_model": args.flash_model,
        "latest_closure_governance_audit": initial_sta_audit,
        "stage_evidence_request_fingerprint": "",
        "evidence_model": evidence_model,
        "evidence_stage": args.evidence_stage,
        "evidence_workflow": workflow_root.as_posix(),
        "sta_unchanged_smokes": 0,
        "last_sta_phase": "not_attempted",
        "recent_stage_trend": [],
        "experiments": {},
        "limits": {
            "max_stagnant_batches": args.max_stagnant_batches,
            "max_identical_measurements": args.max_identical_measurements,
            "max_flash_attempts_total": args.max_flash_attempts_total,
            "max_ppa_budget_seconds": args.max_ppa_budget_seconds,
            "max_wall_clock_minutes": args.max_wall_clock_minutes,
            "max_rtl_repair_attempts": args.max_rtl_repair_attempts,
            "max_optimization_measurement_retries": args.max_optimization_measurement_retries,
            "max_optimization_edit_attempts": args.max_optimization_edit_attempts,
            "max_optimization_steps": args.max_optimization_steps,
            "optimization_minimum_value_score": args.optimization_minimum_value_score,
            "optimization_minimum_improvement_ratio": args.optimization_minimum_improvement_ratio,
            "optimization_maximum_regression_ratio": args.optimization_maximum_regression_ratio,
        },
    }
    save_state(session_root, state)
    write_status(session, "planning", "Pro is evaluating the next Flash batch")

    maximum_scheduler_steps = args.max_cycles * (args.max_optimization_steps + 2)
    optimization_requires_completion = False
    while int(state.get("scheduler_steps", 0)) < maximum_scheduler_steps:
        if (int(state.get("guidance_cycles", 0)) >= args.max_cycles
                and not optimization_requires_completion):
            break
        cycle = int(state.get("scheduler_steps", 0)) + 1
        state["scheduler_steps"] = cycle
        stop_file = session_root / "stop.request"
        # This is deliberately before evidence/model invocation: a stop request
        # must not consume another Pro call or allow a new repair action.
        if stop_file.exists():
            write_status(session, "stopped", f"cycle={cycle}; user stop honored at batch boundary before Pro invocation")
            save_state(session_root, state)
            return 0
        if elapsed_minutes(str(state["started_at"])) >= args.max_wall_clock_minutes:
            write_status(session, "resource_limit", "wall-clock limit reached at batch boundary")
            save_state(session_root, state)
            return 0
        try:
            current_registry = reconcile_project_registry(
                skill_root=module_registry_root, session_id=session, cycle_id=cycle,
                phase="pre_decision", controller_state=state)
            state["module_registry_hot_view"] = current_registry.get("hot_view")
            requested_mainline = state.get("registry_mainline_event")
            current_pending = current_registry.get("pending_transaction")
            if (isinstance(requested_mainline, Mapping)
                    and isinstance(current_pending, Mapping)
                    and requested_mainline.get("module_id") == current_pending.get("module_id")):
                state["registry_mainline_event"] = None
        except (SkillGateError, ProjectAdapterError, RuntimeError, OSError, ValueError) as error:
            save_state(session_root, state)
            write_status(session, "infra_uncertain", f"module registry pre-decision gate unavailable: {error}")
            return INFRA_UNCERTAIN_EXIT
        # Refresh live closure facts before the portable optimization Skill
        # derives its insertion gate.  Otherwise a newly arrived result could
        # be summarized with the previous cycle's safety context.
        current_cycle_closure_audit = controller_closure_audit(state)
        state["latest_closure_governance_audit"] = current_cycle_closure_audit
        try:
            optimization_input = project_adapter.prepare_optimization_input(
                registry=current_registry,
                closure_audit=current_cycle_closure_audit,
                functional_baseline_clean=latest_functional_regression_passed(),
                pending_harness_verification=isinstance(
                    state.get("pending_verification"), Mapping),
                minimum_value_score=args.optimization_minimum_value_score)
            internal_optimization_records: list[dict[str, object]] = []
            optimization_project_batches = 0

            def execute_optimization_project_plan(
                    plan: Mapping[str, object]) -> Mapping[str, object]:
                nonlocal current_registry, current_cycle_closure_audit
                nonlocal optimization_project_batches
                if stop_file.exists():
                    return {"boundary": {"kind": "stop_requested"}}
                optimization_project_batches += 1
                directive_path = Path(str(plan.get("directive_path") or ""))
                if not directive_path.is_absolute():
                    raise ProjectAdapterError(
                        "Optimization project plan has no absolute directive path")
                raw = execute_project_plan_command(
                    plan, directive_path=directive_path,
                    controller_log=(session_root /
                                    f"optimization_executor_{cycle}_{optimization_project_batches}.log"))
                if isinstance(raw.get("boundary"), Mapping):
                    return raw
                process_exit = int(raw.get("process_exit") or 0)
                raw_results = raw.get("results")
                new_results = ([dict(item) for item in raw_results
                                if isinstance(item, Mapping)]
                               if isinstance(raw_results, list) else [])
                execution_record = record_project_execution(
                    state, cycle=cycle, process_exit=process_exit,
                    new_results=new_results, attempts=1,
                    before_sta_audit=current_cycle_closure_audit,
                    project_adapter=project_adapter,
                    account_as_flash=False)
                after_audit = execution_record["after_sta_audit"]
                assert isinstance(after_audit, dict)
                current_cycle_closure_audit = after_audit
                measurement_incomplete = bool(
                    execution_record["measurement_incomplete"])
                batch_number = int(execution_record["batch_number"])
                repeat_count = int(execution_record["repeat_count"])
                current_registry = reconcile_project_registry(
                    skill_root=module_registry_root, session_id=session,
                    cycle_id=cycle, phase="optimization_post_execution",
                    controller_state=state)
                state["module_registry_hot_view"] = current_registry.get("hot_view")
                observation = project_adapter.normalize_optimization_plan(
                    plan=plan, results=new_results, process_exit=process_exit,
                    measurement_incomplete=measurement_incomplete,
                    registry=current_registry,
                    minimum_value_score=args.optimization_minimum_value_score,
                    closure_audit=after_audit)
                record = {
                    **dict(raw), "status": observation.get("status"),
                    "observation": observation, "batch": batch_number,
                    "repeat_count": repeat_count,
                }
                (session_root /
                 f"optimization_project_result_{cycle}_{optimization_project_batches}.json").write_text(
                    json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
                append_pro_progress(
                    PRO_REPORT, session, cycle,
                    "Optimization Skill 项目验证阶段",
                    details={"process_exit": process_exit,
                             "new_results": len(new_results),
                             "observation": observation,
                             "controller_launch_error": raw.get(
                                 "controller_launch_error")})
                save_state(session_root, state)
                return record

            def dispatch_optimization_request(
                    request: Mapping[str, object]) -> Mapping[str, object]:
                request_index = len(internal_optimization_records) + 1
                result = optimization_execute_request(
                    optimization_root, project_adapter, request,
                    execute_project_plan=execute_optimization_project_plan,
                    state_path=OPTIMIZATION_STATE,
                    checkpoint_root=ROOT / "status/task02_optimization_checkpoints",
                    mainline_ref=optimization_input.get("mainline_ref"),
                    integration_target=registry_mainline(current_registry).get("target"),
                    directive_path=(session_root /
                                    f"optimization_directive_{cycle}_{request_index}.md"),
                    optimization_executor=OPTIMIZATION_EXECUTOR,
                    sta_scope_skill=sta_scope_root,
                    model=args.flash_model)
                internal_optimization_records.append(dict(result))
                return result

            campaign = optimization_run_campaign(
                optimization_root, {
                    **optimization_input,
                    "campaign_id_prefix": session,
                    "limits": {
                        "max_repair_attempts": args.max_rtl_repair_attempts,
                        "max_measurement_retries": args.max_optimization_measurement_retries,
                        "max_edit_attempts": args.max_optimization_edit_attempts,
                        "max_internal_steps": args.max_optimization_steps,
                    },
                    "policy": {
                        "minimum_improvement_ratio": args.optimization_minimum_improvement_ratio,
                        "maximum_regression_ratio": args.optimization_maximum_regression_ratio,
                    },
                }, dispatch_optimization_request,
                dispatchable_executor_refs={"optimization.edit", "optimization.rollback",
                                            "project.verify_optimization",
                                            "capability.resolve_sta_scope"},
                max_dispatches=args.max_optimization_steps,
                state_path=OPTIMIZATION_STATE,
                observation_path=OPTIMIZATION_OBSERVATION,
                resolve_handoff=lambda handoff: project_adapter.resolve_optimization_handoff(
                    handoff, current_registry))
            optimization_projection = campaign.get("evidence_projection")
            if not isinstance(optimization_projection, Mapping):
                raise SkillGateError(
                    "Optimization Skill campaign returned no evidence projection")
            optimization_projection = dict(optimization_projection)
            optimization_handoff = campaign.get("external_handoff")
            optimization_handoff = (dict(optimization_handoff)
                                    if isinstance(optimization_handoff, Mapping) else None)
            optimization_requires_completion = campaign.get("active_candidate") is True
            if campaign.get("boundary_reason") == "handoff_hold":
                resolution = ((optimization_handoff or {}).get("resolution")
                              if isinstance(optimization_handoff, Mapping) else {})
                resolution = resolution if isinstance(resolution, Mapping) else {}
                raise SkillGateError(
                    "optimization handoff compatibility is on hold: "
                    + ",".join(str(item) for item in
                               resolution.get("reason_codes", [])))
            execution_boundary = campaign.get("execution_boundary")
            execution_boundary = (dict(execution_boundary)
                                  if isinstance(execution_boundary, Mapping) else None)
            if internal_optimization_records:
                (session_root / f"optimization_execution_{cycle}.json").write_text(
                    json.dumps(internal_optimization_records, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
                for runner_result in internal_optimization_records:
                    append_pro_progress(
                        PRO_REPORT, session, cycle, "Optimization Skill 内部执行阶段",
                        details={key: runner_result.get(key) for key in (
                            "status", "reason", "operation_id", "module", "rtl_path",
                            "rtl_fingerprint", "diff_ref", "rollback_proof")})
                save_state(session_root, state)
                if any(item.get("status") == "infra_uncertain"
                       for item in internal_optimization_records
                       if isinstance(item, Mapping)):
                    write_status(session, "infra_uncertain",
                                 "optimization edit model infrastructure is uncertain")
                    return INFRA_UNCERTAIN_EXIT
            if execution_boundary is not None:
                reason = str(execution_boundary.get("kind") or "execution_boundary")
                save_state(session_root, state)
                if reason == "stop_requested":
                    write_status(session, "stopped",
                                 "user stop honored inside Optimization campaign")
                    return 0
                if reason == "resource_limit":
                    write_status(session, "resource_limit",
                                 str(execution_boundary.get("detail") or reason))
                    return 0
                if reason == "concurrent_controller":
                    write_status(session, "blocked",
                                 "another local harness controller became active")
                    return 3
                raise SkillGateError(f"unknown Optimization execution boundary: {reason}")
        except (SkillGateError, ProjectAdapterError, OSError, ValueError) as error:
            save_state(session_root, state)
            write_status(session, "infra_uncertain",
                         f"Optimization Skill step unavailable: {error}")
            return INFRA_UNCERTAIN_EXIT
        try:
            stage_execution = project_adapter.materialize_stage_evidence_execution(
                state=state, closure_audit=current_cycle_closure_audit,
                optimization_projection=optimization_projection,
                session_root=session_root, cycle=cycle, workflow_root=workflow_root,
                evidence_model=evidence_model, stage=args.evidence_stage,
                contract_path=evidence_contract,
                max_artifact_bytes=args.evidence_max_artifact_bytes,
                prompt_max_chars=args.evidence_prompt_max_chars,
                max_facts_per_skill=args.evidence_max_facts_per_skill,
                max_total_facts=args.evidence_max_total_facts)
        except (ProjectAdapterError, OSError, TypeError, ValueError) as error:
            save_state(session_root, state)
            write_status(session, "infra_uncertain",
                         f"Evidence handoff materialization failed: {error}")
            return INFRA_UNCERTAIN_EXIT
        external_manifest = stage_execution.get("manifest")
        external_evidence = {
            "manifest": dict(external_manifest)
            if isinstance(external_manifest, Mapping) else {}}
        cache_source_run_id = str(stage_execution.get("cache_source_run_id") or "unavailable")
        if cache_source_run_id != "unavailable":
            for cache_key in ("latest_sta_evidence", "latest_run_evidence"):
                cached = state.get(cache_key)
                if isinstance(cached, dict) \
                        and cached.get("source_run_id") != cache_source_run_id:
                    state[cache_key] = None
        stage_record = state.get("latest_stage_evidence")
        cached_request_fingerprint = str(
            state.get("stage_evidence_request_fingerprint", ""))
        request_fingerprint = str(stage_execution.get("request_fingerprint") or "")
        # The runner has already completed all local deterministic stages.
        # Evidence Model work begins only at a strategic or ordinary boundary.
        if bool(stage_execution.get("has_evidence")):
            if not (isinstance(stage_record, dict)
                    and cached_request_fingerprint == request_fingerprint):
                write_status(session, "evidence_review",
                             f"cycle={cycle}; stage={args.evidence_stage}; Evidence Model handoff")
                command = stage_execution.get("command")
                log_path = stage_execution.get("log_path")
                if not isinstance(command, list) or not all(
                        isinstance(item, str) for item in command) \
                        or not isinstance(log_path, Path):
                    write_status(session, "infra_uncertain",
                                 "Evidence adapter returned an invalid execution request")
                    return INFRA_UNCERTAIN_EXIT
                try:
                    workflow_rc = run_logged(command, log_path)
                except (OSError, subprocess.SubprocessError) as error:
                    log_path.write_text(
                        f"{type(error).__name__}: {error}\n", encoding="utf-8")
                    workflow_rc = INFRA_UNCERTAIN_EXIT
                stage_record = project_adapter.normalize_stage_evidence_execution(
                    stage_execution, workflow_rc)
                state["latest_stage_evidence"] = stage_record
                state["stage_evidence_request_fingerprint"] = request_fingerprint
                (session_root / f"stage_evidence_{cycle}.json").write_text(
                    json.dumps(stage_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                append_pro_progress(
                    PRO_REPORT, session, cycle, "当前阶段 Evidence Model 证据整合",
                    details=(compact_stage_evidence_record(stage_record)
                             or {"review_state": "unavailable"}))
            if not isinstance(stage_record, dict) or stage_record.get("hard_gate") != "pass":
                review = stage_record.get("review", {}) if isinstance(stage_record, dict) else {}
                review_state = review.get("review_state", "INFRA_UNCERTAIN") if isinstance(review, dict) else "INFRA_UNCERTAIN"
                save_state(session_root, state)
                if review_state == "INFRA_UNCERTAIN":
                    write_status(session, "infra_uncertain",
                                 f"Evidence Model/stage workflow unresolved: {review_state}")
                    return INFRA_UNCERTAIN_EXIT
                write_status(session, "blocked",
                             f"Evidence Model/stage workflow hard gate hold: {review_state}")
                return 2
        save_state(session_root, state)
        evidence = evidence_snapshot(state, external_evidence=external_evidence)
        (session_root / f"evidence_{cycle}.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        try:
            decision_packet = project_adapter.prepare_cycle(
                state=state,
                stage_record=state.get("latest_stage_evidence"),
                evidence_fingerprint=str(evidence["fingerprint"]),
                stop_requested=stop_file.exists(),
                functional_baseline_clean=latest_functional_regression_passed(),
                optimization_handoff=optimization_handoff,
            )
            (session_root / f"decision_packet_{cycle}.json").write_text(
                json.dumps(decision_packet, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except (ProjectAdapterError, RuntimeError) as error:
            append_pro_progress(PRO_REPORT, session, cycle, "Pro 输入包构建失败",
                                details={"error": str(error)})
            save_state(session_root, state)
            write_status(session, "infra_uncertain", f"Pro input packet unavailable: {error}")
            return INFRA_UNCERTAIN_EXIT
        pro_log = session_root / f"pro_decision_{cycle}.log"
        write_status(session, "planning", f"cycle={cycle}; Pro decision")

        def record_guidance_invocation() -> None:
            state["guidance_cycles"] = int(state.get("guidance_cycles", 0)) + 1
            save_state(session_root, state)

        try:
            guidance = guidance_runner.run(
                decision_packet, model=args.pro_model, log_path=pro_log,
                execute=run_logged, before_invoke=record_guidance_invocation)
        except ProGuidanceInputError as error:
            append_pro_progress(PRO_REPORT, session, cycle, "Pro 输入包构建失败",
                                details={"error": str(error)})
            save_state(session_root, state)
            write_status(session, "infra_uncertain", f"Pro input packet unavailable: {error}")
            return INFRA_UNCERTAIN_EXIT
        except ProModelInvocationError as error:
            append_pro_progress(
                PRO_REPORT, session, cycle, "Pro 决策失败", raw_output=error.raw_output,
                details={"omp_exit": error.exit_code})
            write_status(
                session, "blocked",
                f"Pro exited {error.exit_code}; Flash was not started")
            return error.exit_code or 1
        except ProOutputError as error:
            append_pro_progress(
                PRO_REPORT, session, cycle, "Pro 决策格式无效", raw_output=error.raw_output,
                                details={"error": str(error)})
            write_status(session, "blocked", f"invalid Pro decision: {error}")
            return 2
        except ProDecisionRejected as error:
            decision = error.decision
            append_pro_progress(PRO_REPORT, session, cycle, "Pro 战略决策",
                                raw_output=error.raw_output, details=decision)
            (session_root / f"decision_{cycle}.json").write_text(
                json.dumps(decision, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            retries = int(state.get("decision_packet_retries", 0)) + 1
            state["decision_packet_retries"] = retries
            append_pro_progress(
                PRO_REPORT, session, cycle, "Pro 动作不符合不可变 DecisionPacket",
                details={"error": str(error), "decision": decision,
                         "packet_identity": decision_packet.get("identity"),
                         "allowed_actions": decision_packet.get("allowed_actions"),
                         "retry": retries, "max_retries": 2})
            save_state(session_root, state)
            if retries <= 2:
                write_status(session, "planning",
                             f"cycle={cycle}; DecisionPacket rejected Pro action; retry={retries}/2")
                continue
            write_status(session, "blocked", "Pro repeatedly violated immutable DecisionPacket")
            return 2
        except ProGuidanceError as error:
            append_pro_progress(
                PRO_REPORT, session, cycle, "Pro 指导轮基础设施状态不确定",
                raw_output=error.raw_output, details={"error": str(error)})
            save_state(session_root, state)
            write_status(session, "infra_uncertain", f"Pro guidance unavailable: {error}")
            return INFRA_UNCERTAIN_EXIT
        decision = guidance.decision
        pro_output = guidance.raw_output
        append_pro_progress(PRO_REPORT, session, cycle, "Pro 战略决策",
                            raw_output=pro_output, details=decision)
        (session_root / f"decision_{cycle}.json").write_text(
            json.dumps(decision, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        state["decision_packet_retries"] = 0
        current_sta_audit = controller_closure_audit(state)
        state["latest_closure_governance_audit"] = current_sta_audit
        (session_root / f"closure_gate_before_{cycle}.json").write_text(
            json.dumps(current_sta_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        action = str(decision["action"])
        if action == "stop_for_debug" or stop_file.exists():
            write_status(session, "stopped", f"cycle={cycle}; Pro stopped at batch boundary")
            return 0
        if action == RUN_EVIDENCE_ACTION:
            request = decision["evidence_request"]
            assert isinstance(request, dict)
            resolved_run_id = resolve_sta_evidence_run_id(str(request["run_id"]))
            if resolved_run_id is None:
                write_status(session, "blocked", "requested STA evidence source is not a valid Task-02 PPA run")
                save_state(session_root, state)
                return 2
            request_key = {key: request[key] for key in ("run_id", "artifact", "selector", "max_lines")}
            request_key["resolved_run_id"] = resolved_run_id
            previous = state.get("latest_run_evidence")
            if isinstance(previous, dict) and previous.get("request") == request_key:
                write_status(session, "no_new_evidence", "same bounded run-evidence request already completed")
                save_state(session_root, state)
                return 0
            write_status(session, "reading_run_evidence",
                         f"cycle={cycle}; immutable Task-02 PPA sta.log only")
            evidence_packet = query_sta_timing_paths(resolved_run_id, str(request["selector"]),
                                                      int(request["max_lines"]))
            evidence_packet["request"] = request_key
            (session_root / f"run_evidence_{cycle}.json").write_text(
                json.dumps(evidence_packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            append_pro_progress(PRO_REPORT, session, cycle, "Pro 请求的有界 run 原始证据", details=evidence_packet)
            if evidence_packet.get("status") != "ok":
                write_status(session, "blocked", f"run evidence unavailable: {evidence_packet.get('reason', '')}")
                save_state(session_root, state)
                return 1
            state["latest_run_evidence"] = evidence_packet
            save_state(session_root, state)
            write_status(session, "planning", f"cycle={cycle}; run evidence recorded; Pro will reassess")
            continue
        if action == READ_ONLY_EVIDENCE_ACTION:
            previous = state.get("latest_sta_evidence")
            write_status(session, "reading_existing_evidence", f"cycle={cycle}; immutable STA log only")
            evidence_packet = inspect_latest_measured_sta()
            (session_root / f"sta_evidence_{cycle}.json").write_text(
                json.dumps(evidence_packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            append_pro_progress(PRO_REPORT, session, cycle, "只读既有 STA 路径证据", details=evidence_packet)
            if evidence_packet.get("status") != "ok":
                write_status(session, "blocked", f"existing STA evidence unavailable: {evidence_packet.get('reason', '')}")
                save_state(session_root, state)
                return 1
            if isinstance(previous, dict) and previous.get("source_run_id") == evidence_packet.get("source_run_id"):
                write_status(session, "no_new_evidence", "existing STA evidence already inspected for this source run")
                save_state(session_root, state)
                return 0
            state["latest_sta_evidence"] = evidence_packet
            save_state(session_root, state)
            write_status(session, "planning", f"cycle={cycle}; existing STA evidence recorded; Pro will reassess")
            continue
        if action == EXPAND_BLACKBOX_ACTION:
            module = str(decision.get("module", ""))
            action_root = session_root / f"action_{cycle}"
            action_root.mkdir(parents=True, exist_ok=True)
            config_path = ROOT / "harness/ppa.toml"
            before_config = config_path.read_bytes()
            pending_mainline = registry_mainline_pending(state)
            if isinstance(pending_mainline, Mapping):
                write_status(session, "blocked",
                             "Registry already owns an unfinished mainline transaction")
                save_state(session_root, state)
                return 2
            try:
                expansion_request = project_adapter.materialize_request(
                    decision, decision_packet,
                    pending_verification=state.get("pending_verification"),
                )
                expansion_execution = project_adapter.materialize_project_execution(
                    expansion_request, session_root=session_root, cycle=cycle)
                expansion_command = expansion_execution.get("command")
                if not isinstance(expansion_command, list) or not expansion_command:
                    raise ValueError("project adapter returned no expansion command")
                expansion_rc = run_logged(
                    [str(item) for item in expansion_command],
                    action_root / "project_executor.log")
                expansion_result = project_adapter.normalize_project_execution(
                    expansion_execution, expansion_rc)
                (session_root / f"execution_result_{cycle}.json").write_text(
                    json.dumps(expansion_result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
                if expansion_result.get("status") != "success":
                    raise ValueError(str(expansion_result.get("detail") or
                                         expansion_result.get("status")))
                expansion_value = expansion_result.get("details")
                if not isinstance(expansion_value, dict):
                    raise ValueError("project adapter returned no expansion details")
                expansion = expansion_value
            except (OSError, ValueError) as error:
                write_status(session, "blocked", f"blackbox expansion rejected: {error}")
                save_state(session_root, state)
                return 2
            (action_root / "before_ppa.toml").write_bytes(before_config)
            (action_root / "pro_action.log").write_text(
                "controller 已按 Pro 决策从 top_level_sta 黑盒名单移除一个模块；未调用模型实施者，避免扩大变更范围。\n"
                f"module={module}\n", encoding="utf-8")
            action_details = {"action": EXPAND_BLACKBOX_ACTION, "exit": 0,
                              "changed": ["harness/ppa.toml"], "unexpected": [], **expansion}
            state["registry_mainline_event"] = {
                "operation": "begin", "module_id": module,
                "partition": EXPANSION_PARTITION,
                "transaction_id": f"blackbox_expansion:{EXPANSION_PARTITION}:{module}",
                "operation_id": str(expansion_request.get("operation_id") or ""),
            }
            (action_root / "pro_action.json").write_text(
                json.dumps(action_details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            append_pro_progress(PRO_REPORT, session, cycle, "Pro 受限动作：expand_blackbox",
                                raw_output=tail(action_root / "pro_action.log"), details=action_details)
            save_state(session_root, state)
            write_status(session, "module_expansion", f"cycle={cycle}; module={module}; awaiting targeted verification")
            continue
        if action in ACTION_SCOPES:
            write_status(session, "pro_action", f"cycle={cycle}; action={action}")
            action_root = session_root / f"action_{cycle}"
            action_ok = execute_pro_action(action_root, decision, args.pro_model)
            try:
                action_details = json.loads((action_root / "pro_action.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                action_details = {"action": action, "result": "action record unavailable"}
            append_pro_progress(PRO_REPORT, session, cycle, f"Pro 受限动作：{action}",
                                raw_output=tail(action_root / "pro_action.log"), details=action_details)
            if not action_ok:
                write_status(session, "blocked", f"Pro action {action} failed; scoped edits restored")
                return 1
            if action == "repair_harness":
                changed = action_details.get("changed", [])
                if isinstance(changed, list) and changed:
                    summary = "Pro repair_harness 已修改；下一批必须先验证本次改动。"
                    state["pending_verification"] = save_pending_verification(
                        [str(path) for path in changed], summary, "sta_timing_measured|sta_no_timing_path")
                    state["pending_sta_smoke"] = True
            save_state(session_root, state)
            continue

        pending_expansion = registry_mainline_pending(state)
        if (action in {"continue", INCREASE_BUDGET_ACTION}
                and isinstance(pending_expansion, Mapping)):
            requested_module = str(decision.get("module") or "")
            if requested_module and requested_module != str(pending_expansion.get("module") or ""):
                write_status(session, "blocked", "mainline resume module does not match Registry")
                save_state(session_root, state)
                return 2

        directive = session_root / f"execution_note_{cycle}.md"
        campaign_contract_path: Optional[Path] = None
        optimization_plan = project_adapter.materialize_external_optimization_plan(
            decision=decision, decision_packet=decision_packet,
            optimization_handoff=optimization_handoff,
            directive_path=directive,
            optimization_executor=OPTIMIZATION_EXECUTOR)
        if optimization_plan is not None:
            execution_directive = str(
                optimization_plan.get("directive") or
                "只执行当前 Skill 请求；禁止额外编辑。\n")
            attempts = 0
            request_record = dict(optimization_plan)
            request_name = f"optimization_external_sta_request_{cycle}.json"
        else:
            executor_request = project_adapter.materialize_request(
                decision, decision_packet,
                pending_verification=state.get("pending_verification"))
            campaign_contract = executor_request.get("campaign_contract")
            if not isinstance(campaign_contract, Mapping):
                raise ProjectAdapterError("Flash request has no immutable campaign contract")
            attempts = int(campaign_contract.get("max_steps") or 0)
            campaign_contract_path = session_root / f"flash_campaign_contract_{cycle}.json"
            campaign_contract_path.write_text(
                json.dumps(campaign_contract, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            execution_directive = (
                "Flash 只执行已物化的不可变 campaign contract；不得读取此说明作为第二策略入口。\n")
            request_record = dict(executor_request)
            request_name = f"executor_request_{cycle}.json"
        (session_root / request_name).write_text(
            json.dumps(request_record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        directive.write_text(execution_directive, encoding="utf-8")
        plan_execution = (optimization_plan.get("execution")
                          if isinstance(optimization_plan, Mapping) else {})
        plan_execution = plan_execution if isinstance(plan_execution, Mapping) else {}
        batch_intent = hashlib.sha256(json.dumps({
            "evidence": evidence["fingerprint"],
            "action": action,
            "module": decision.get("module"),
            "campaign_contract_id": (
                campaign_contract.get("contract_id")
                if optimization_plan is None else None),
            "optimization_operation_id": plan_execution.get("operation_id"),
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if batch_intent == state["last_continue_evidence"]:
            write_status(session, "no_new_evidence", "identical batch intent rejected: evidence and action did not change")
            save_state(session_root, state)
            return 0
        if int(state["flash_attempts"]) + attempts > args.max_flash_attempts_total:
            write_status(session, "resource_limit", "total Flash-attempt budget would be exceeded")
            save_state(session_root, state)
            return 0
        state["last_continue_evidence"] = batch_intent
        batch_number = int(state["flash_batches"]) + 1
        pending_expansion = registry_mainline_pending(state)
        optimization_external_sta_run = optimization_plan is not None
        if optimization_plan is not None:
            command = list(optimization_plan["command"])
        else:
            assert campaign_contract_path is not None
            command = [sys.executable, str(PPA_CONTROLLER), "--skip-baseline",
                       "--model", args.flash_model,
                       "--campaign-contract", str(campaign_contract_path),
                       "--closure-governance-skill", closure_root.as_posix()]
        if isinstance(pending_expansion, dict) and not optimization_external_sta_run:
            command += ["--forced-blackbox-expansion-module", str(pending_expansion.get("module", ""))]
        execution_label = ("optimization_external_sta"
                           if optimization_external_sta_run else "flash")
        write_status(
            session, f"{execution_label}_running",
            f"cycle={cycle}; attempts={attempts}")
        controller_log = session_root / f"{execution_label}_controller_{cycle}.log"
        raw_execution = execute_project_plan_command(
            {"command": command, "directive": execution_directive},
            directive_path=directive, controller_log=controller_log)
        boundary = raw_execution.get("boundary")
        if isinstance(boundary, Mapping):
            write_status(session, "blocked",
                         f"another local harness controller became active before {execution_label}: "
                         + str(boundary.get("detail") or "unknown controller"))
            append_pro_progress(
                PRO_REPORT, session, cycle,
                f"controller 拒绝并发 {execution_label}",
                details={"boundary": dict(boundary), "command_not_started": command})
            save_state(session_root, state)
            return 3
        flash_rc = int(raw_execution.get("process_exit") or 0)
        raw_results = raw_execution.get("results")
        new_results = ([dict(item) for item in raw_results if isinstance(item, Mapping)]
                       if isinstance(raw_results, list) else [])
        controller_launch_error = raw_execution.get("controller_launch_error")
        execution_record = record_project_execution(
            state, cycle=cycle, process_exit=flash_rc,
            new_results=new_results, attempts=attempts,
            before_sta_audit=current_sta_audit, project_adapter=project_adapter,
            apply_pending_flags=not optimization_external_sta_run,
            account_as_flash=not optimization_external_sta_run)
        after_sta_audit = execution_record["after_sta_audit"]
        assert isinstance(after_sta_audit, dict)
        normalized_execution_outcome = execution_record.get("normalized_outcome")
        normalized_execution_outcome = (
            dict(normalized_execution_outcome)
            if isinstance(normalized_execution_outcome, Mapping) else {})
        measurement_incomplete = bool(execution_record["measurement_incomplete"])
        batch_number = int(execution_record["batch_number"])
        outcome = str(execution_record["outcome"])
        repeat_count = int(execution_record["repeat_count"])
        (session_root / f"closure_gate_after_{cycle}.json").write_text(
            json.dumps(after_sta_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            post_registry = reconcile_project_registry(
                skill_root=module_registry_root, session_id=session, cycle_id=cycle,
                phase="post_execution", controller_state=state)
            state["module_registry_hot_view"] = post_registry.get("hot_view")
            if optimization_plan is not None:
                observation = project_adapter.normalize_optimization_plan(
                    plan=optimization_plan, results=new_results,
                    process_exit=flash_rc,
                    measurement_incomplete=measurement_incomplete,
                    registry=post_registry,
                    minimum_value_score=args.optimization_minimum_value_score,
                    closure_audit=after_sta_audit)
                save_optimization_observation(observation)
                measurement_incomplete = (measurement_incomplete
                                          or observation.get("status") == "measurement_incomplete")
                (session_root / f"optimization_observation_{cycle}.json").write_text(
                    json.dumps({"observation": observation,
                                "consumer": "rtl-optimization-triage campaign runner"},
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (SkillGateError, ProjectAdapterError, RuntimeError, OSError, ValueError) as error:
            save_state(session_root, state)
            write_status(session, "infra_uncertain", f"module registry post-execution gate unavailable: {error}")
            return INFRA_UNCERTAIN_EXIT
        save_state(session_root, state)
        (session_root / f"{execution_label}_result_{cycle}.json").write_text(
            json.dumps({"cycle": cycle, "exit": flash_rc, "finished_at": timestamp(),
                        "new_results": len(new_results), "outcome": outcome, "sta_closure_audit": after_sta_audit,
                        "measurement_status": state.get("last_measurement_status", "not_attempted"),
                        "measurement_incomplete": measurement_incomplete,
                        "repeat_count": repeat_count}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        append_pro_progress(
            PRO_REPORT, session, cycle,
            ("Optimization 外部 STA 执行结束"
             if optimization_external_sta_run else "Flash 批次结束"),
            details={"decision": decision, "flash_exit": flash_rc, "new_results": len(new_results),
                     "latest_result": latest_result_data(),
                     "controller_launch_error": controller_launch_error},
        )
        if stop_file.exists():
            write_status(session, "stopped", f"cycle={cycle}; deferred user stop honored after Flash batch")
            return 0
        if optimization_external_sta_run:
            if flash_rc in (INFRA_EXIT, INFRA_UNCERTAIN_EXIT):
                write_status(session, "infra_uncertain",
                             f"optimization verification transport exit={flash_rc}; candidate preserved")
                return flash_rc
            if int(state["ppa_budget_seconds"]) >= args.max_ppa_budget_seconds:
                write_status(session, "resource_limit",
                             f"PPA budget={state['ppa_budget_seconds']}s during optimization campaign")
                return 0
            write_status(
                session, "optimization",
                f"cycle={cycle}; external verification observation queued for Optimization Skill")
            # The next edit/retry/rollback is another Skill step.  It does not
            # spend a Pro guidance round or invoke the Evidence Model.
            continue
        if int(state.get("sta_unchanged_smokes", 0)) >= 2:
            write_status(session, "sta_closure_no_progress",
                         f"STA closure phase unchanged after {state['sta_unchanged_smokes']} targeted smokes")
            return 0
        if measurement_incomplete:
            save_state(session_root, state)
            append_pro_progress(
                PRO_REPORT, session, cycle, "PPA 测量未完成，进入下一指导周期",
                details={
                    "flash_exit": flash_rc,
                    "measurement_status": normalized_execution_outcome.get(
                        "status", "measurement_incomplete"),
                    "measurement_reason": normalized_execution_outcome.get("reason"),
                    "new_results": len(new_results),
                    "next_step": "下一调度步按当前结构化状态决定；仅战略边界调用 Evidence Model/Pro",
                    "active_mainline_transaction": registry_mainline_pending(state),
                },
            )
            write_status(
                session, "measurement_incomplete",
                f"cycle={cycle}; reason={normalized_execution_outcome.get('reason') or 'measurement_incomplete'}; "
                "next Pro guidance cycle will reassess",
            )
            # Keep the outer Pro cycle alive after the normalized observation;
            # normal cycle/resource/wall limits still apply at later boundaries.
            if int(state["ppa_budget_seconds"]) >= args.max_ppa_budget_seconds:
                write_status(session, "resource_limit",
                             f"PPA budget={state['ppa_budget_seconds']}s after incomplete measurement")
                return 0
            continue
        if flash_rc != 0:
            stage = ("infra_failed" if flash_rc == INFRA_EXIT else
                     ("infra_uncertain" if flash_rc == INFRA_UNCERTAIN_EXIT else "blocked"))
            detail = (controller_launch_error if controller_launch_error else
                      f"Flash controller exited {flash_rc}; Pro will not hide the failure")
            write_status(session, stage, detail)
            return flash_rc
        if int(state["stagnant_batches"]) >= args.max_stagnant_batches:
            write_status(session, "no_progress", f"identical semantic outcomes={state['stagnant_batches']}")
            return 0
        if repeat_count >= args.max_identical_measurements:
            write_status(session, "repeated_measurement", f"identical experiment/outcome repeats={repeat_count}")
            return 0
        if int(state["ppa_budget_seconds"]) >= args.max_ppa_budget_seconds:
            write_status(session, "resource_limit", f"PPA budget={state['ppa_budget_seconds']}s")
            return 0
    if int(state.get("scheduler_steps", 0)) >= maximum_scheduler_steps:
        write_status(session, "resource_limit",
                     f"reached scheduler hard limit={maximum_scheduler_steps}; stopped for review")
    else:
        write_status(session, "cycle_limit",
                     f"reached Pro guidance max_cycles={args.max_cycles}; stopped for review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
