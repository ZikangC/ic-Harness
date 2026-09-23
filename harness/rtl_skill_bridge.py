#!/usr/bin/env python3
"""Small project adapter for generic RTLskill controller gates.

The skills own reusable policy.  This module owns only subprocess transport
and conservative conversion of this project's unified RTL diff into facts.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Mapping, Optional


ROOT = Path(__file__).resolve().parents[1]
STA_SCOPE_ENV = "RTLSKILL_STA_SCOPE"
OPTIMIZATION_TRIAGE_ENV = "RTLSKILL_OPTIMIZATION_TRIAGE"
MODULE_EVIDENCE_REGISTRY_ENV = "RTLSKILL_MODULE_EVIDENCE_REGISTRY"
CLOSURE_GOVERNANCE_ENV = "RTLSKILL_CLOSURE_GOVERNANCE"
DEFAULT_RTLSKILL_ROOT = ROOT.parent / "RTLskill"
TASK02_MEASUREMENT_INPUTS = (
    Path("rtl"), Path("harness/ppa_measure.ys"), Path("harness/ppa_preflight.ys"),
    Path("harness/ppa.toml"), Path("harness/ppa_leaves_blackbox.v"),
    Path("scripts/remote_yosys_round.sh"),
)


class SkillGateError(RuntimeError):
    """The external policy gate was absent, malformed, or unavailable."""


def content_fingerprint(root: Path, relative_paths: Iterable[Path]) -> str:
    """Hash semantic measurement inputs; runtime budget is intentionally absent."""
    digest = hashlib.sha256()
    for relative in relative_paths:
        source = root / relative
        paths = sorted(source.rglob("*") if source.is_dir() else [source])
        for path in paths:
            if not path.is_file():
                continue
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def measurement_descriptor(*, semantic_fingerprint: object, scope_kind: object,
                           targets: object, operation_kind: str = "measurement",
                           intent: str = "unspecified",
                           budget_seconds: object = None) -> dict[str, object]:
    """Build the portable descriptor consumed by rtl-closure-governance."""
    target_list = ([str(item) for item in targets] if isinstance(targets, list) else [])
    return {
        "semantic_fingerprint": str(semantic_fingerprint or ""),
        "scope_kind": str(scope_kind or ""),
        "targets": target_list,
        "operation_kind": operation_kind,
        "intent": intent,
        "budget_seconds": budget_seconds,
    }


def default_skill_path(name: str, env_name: str) -> Path:
    configured = os.environ.get(env_name)
    return Path(configured).expanduser() if configured else DEFAULT_RTLSKILL_ROOT / name


def resolve_gate(skill_root: Path, filename: str) -> Path:
    root = skill_root.expanduser()
    if not root.is_absolute():
        root = ROOT / root
    gate = root.resolve() / filename
    if not gate.is_file():
        raise SkillGateError(f"RTLskill gate not found: {gate}")
    return gate


def run_json_command(command: list[str], *, input_text: Optional[str] = None,
                     timeout: int = 120) -> dict[str, object]:
    """Run one deterministic Skill gate and require one JSON object."""
    run_options: dict[str, object] = {
        "cwd": ROOT,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "timeout": timeout,
        "check": False,
    }
    if input_text is None:
        run_options["stdin"] = subprocess.DEVNULL
    else:
        run_options["input"] = input_text
    try:
        completed = subprocess.run(command, **run_options)
    except (OSError, subprocess.SubprocessError) as error:
        raise SkillGateError(f"RTLskill gate unavailable: {type(error).__name__}: {error}") from error
    if completed.returncode != 0:
        raise SkillGateError(f"RTLskill gate exit={completed.returncode}: {completed.stdout[-2000:]}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise SkillGateError(f"RTLskill gate returned invalid JSON: {completed.stdout[-2000:]}") from error
    if not isinstance(value, dict):
        raise SkillGateError("RTLskill gate result must be a JSON object")
    return value


def run_json_gate(command: list[str], packet: Mapping[str, object], timeout: int = 120) -> dict[str, object]:
    return run_json_command(
        command, input_text=json.dumps(packet, ensure_ascii=False), timeout=timeout)


def closure_governance_audit(
    skill_root: Path,
    *,
    feedback_path: Optional[Path] = None,
    artifact_path: Optional[Path] = None,
    probe_path: Optional[Path] = None,
    netlist_path: Optional[Path] = None,
    top: Optional[str] = None,
    required_bodies: Iterable[str] = (),
    policy_context: Optional[Mapping[str, object]] = None,
    timeout: int = 120,
) -> dict[str, object]:
    """Call the canonical rtl-closure-governance gate through its stable CLI."""
    gate = resolve_gate(skill_root, "closure_gate.py")
    if netlist_path is not None and not str(top or "").strip():
        raise SkillGateError("closure netlist requires an explicit top")
    command = [sys.executable, str(gate)]
    for option, path in (
        ("feedback", feedback_path), ("artifact", artifact_path),
        ("probe", probe_path), ("netlist", netlist_path),
    ):
        if path is not None:
            command.extend([f"--{option}", str(path.expanduser().resolve())])
    if top:
        command.extend(["--top", str(top)])
    for module in required_bodies:
        name = str(module).strip()
        if name:
            command.extend(["--required-body", name])
    if policy_context is not None:
        command.extend(["--policy-json", json.dumps(dict(policy_context), ensure_ascii=False)])
    audit = run_json_command(command, timeout=timeout)
    api_version = audit.get("gate_api_version")
    if not isinstance(api_version, int) or api_version < 6:
        raise SkillGateError(
            f"rtl-closure-governance gate_api_version must be >=6, got {api_version!r}")
    required = {
        "phase", "closure_repair_required", "structural_repair_required",
        "diagnostic_required", "closure_gate_required", "measurement_incomplete",
        "measurement_rerun_admissible", "measurement_resource_blocked",
        "strategy_change_required", "timing_ready",
    }
    missing = sorted(required - set(audit))
    if missing:
        raise SkillGateError(
            "rtl-closure-governance result missing required fields: " + ", ".join(missing))
    return audit


def closure_governance_result(
    skill_root: Path,
    *,
    feedback_path: Optional[Path] = None,
    artifact_path: Optional[Path] = None,
    probe_path: Optional[Path] = None,
    netlist_path: Optional[Path] = None,
    top: Optional[str] = None,
    required_bodies: Iterable[str] = (),
    policy_context: Optional[Mapping[str, object]] = None,
    timeout: int = 120,
) -> dict[str, object]:
    """Return one ready/unavailable adapter result without local reclassification."""
    try:
        audit = closure_governance_audit(
            skill_root,
            feedback_path=feedback_path,
            artifact_path=artifact_path,
            probe_path=probe_path,
            netlist_path=netlist_path,
            top=top,
            required_bodies=required_bodies,
            policy_context=policy_context,
            timeout=timeout,
        )
    except (SkillGateError, OSError, ValueError) as error:
        return {
            "audit_version": None,
            "phase": "not_attempted",
            "closure_repair_required": False,
            "structural_repair_required": False,
            "diagnostic_required": False,
            "closure_gate_required": True,
            "measurement_incomplete": False,
            "measurement_rerun_admissible": None,
            "measurement_resource_blocked": False,
            "strategy_change_required": False,
            "timing_ready": False,
            "repair_domain": "infrastructure",
            "closure_governance_status": "unavailable",
            "closure_governance_error": f"{type(error).__name__}: {error}",
            "audit_source": "rtl-closure-governance",
        }
    return {
        **audit,
        "closure_governance_status": "ready",
        "audit_source": "rtl-closure-governance",
    }


def sta_scope_decision(skill_root: Path, packet: Mapping[str, object]) -> dict[str, object]:
    gate = resolve_gate(skill_root, "scope_gate.py")
    return run_json_gate([sys.executable, str(gate)], packet)


def optimization_run_campaign(skill_root: Path, packet: Mapping[str, object],
                              dispatch, *, dispatchable_executor_refs: set[str],
                              max_dispatches: int, state_path: Path,
                              observation_path: Path,
                              resolve_handoff=None) -> dict[str, object]:
    """Enter the portable Skill campaign runner with project callbacks."""
    runner = resolve_gate(skill_root, "scripts/optimization_campaign_runner.py")
    scripts = str(runner.parent)
    inserted = scripts not in sys.path
    if inserted:
        sys.path.insert(0, scripts)
    try:
        spec = importlib.util.spec_from_file_location(
            f"rtlskill_optimization_campaign_runner_{hashlib.sha256(str(runner).encode()).hexdigest()[:12]}",
            runner)
        if spec is None or spec.loader is None:
            raise SkillGateError(f"could not load Optimization campaign runner: {runner}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.run_campaign(
            packet, dispatch,
            state_path=state_path,
            observation_path=observation_path,
            dispatchable_executor_refs=set(dispatchable_executor_refs),
            max_dispatches=max_dispatches,
            resolve_handoff=resolve_handoff)
    except (OSError, RuntimeError, ValueError) as error:
        raise SkillGateError(
            f"Optimization campaign runner unavailable: {type(error).__name__}: {error}") from error
    finally:
        if inserted:
            sys.path.remove(scripts)
    if not isinstance(result, dict):
        raise SkillGateError("Optimization campaign runner returned no JSON object")
    return result


def optimization_execute_request(
        skill_root: Path, project_adapter, request: Mapping[str, object], *,
        execute_project_plan=None, **context: object) -> dict[str, object]:
    """Execute one campaign request through exactly one transport path.

    The project adapter only binds paths and commands.  Skill-local edit and
    rollback requests are handled by the Skill runner; project verification is
    delegated to the caller's generic process-execution port.
    """
    materialized = project_adapter.materialize_optimization_execution(
        request, **context)
    runner_request = materialized.get("runner_request")
    if isinstance(runner_request, Mapping):
        executor_ref = str(request.get("executor_ref") or "")
        timeout = {"optimization.edit": 1800,
                   "optimization.rollback": 120}.get(executor_ref)
        if timeout is None:
            raise SkillGateError(
                f"unsupported Skill-local Optimization executor: {executor_ref}")
        runner = resolve_gate(skill_root, "scripts/optimization_runner.py")
        return run_json_gate(
            [sys.executable, str(runner)], runner_request, timeout=timeout)
    command = materialized.get("command")
    if isinstance(command, list) and command:
        if not callable(execute_project_plan):
            raise SkillGateError("Optimization project execution port is unavailable")
        plan = {
            "kind": "local",
            "execution": dict(request),
            "command": [str(item) for item in command],
            "directive": materialized.get("directive"),
            "directive_path": str(context.get("directive_path") or ""),
        }
        result = execute_project_plan(plan)
        if not isinstance(result, Mapping):
            raise SkillGateError("Optimization project execution returned no mapping")
        return dict(result)
    raise SkillGateError("Optimization adapter returned no executable transport")


def module_registry_gate(skill_root: Path, command: str,
                         packet: Mapping[str, object]) -> dict[str, object]:
    gate = resolve_gate(skill_root, "registry_gate.py")
    return run_json_gate([sys.executable, str(gate), command], packet)


def module_registry_reconcile(skill_root: Path, packet: Mapping[str, object]) -> dict[str, object]:
    return module_registry_gate(skill_root, "reconcile", packet)


def module_registry_hot_view(skill_root: Path, registry: Mapping[str, object],
                             expected: Mapping[str, object]) -> dict[str, object]:
    return module_registry_gate(
        skill_root, "hot-view", {"registry": dict(registry), "expected": dict(expected)})


def module_registry_can_expand(skill_root: Path, registry: Mapping[str, object],
                               module: str, partition: str) -> dict[str, object]:
    return module_registry_gate(
        skill_root, "can-expand",
        {"registry": dict(registry), "module": module, "partition": partition})


def module_registry_value_evidence(skill_root: Path, registry: Mapping[str, object],
                                   module: str) -> dict[str, object]:
    return module_registry_gate(
        skill_root, "value-evidence", {"registry": dict(registry), "module": module})


def module_registry_module_context(skill_root: Path, registry: Mapping[str, object],
                                   module: str) -> dict[str, object]:
    return module_registry_gate(
        skill_root, "module-context", {"registry": dict(registry), "module": module})


def stage_evidence_envelope(*, skill: str, identity: Mapping[str, object],
                            hard_gate: Mapping[str, object],
                            summary_input: Mapping[str, object],
                            execution_contract: Optional[Mapping[str, object]] = None) -> dict[str, object]:
    """Project adapter boundary for the portable Evidence Skill envelope."""
    value = {
        "schema_version": 1,
        "skill": skill,
        "scope": "current_stage",
        "history_included": False,
        "identity": dict(identity),
        "hard_gate": dict(hard_gate),
        "summary_input": dict(summary_input),
    }
    if execution_contract is not None:
        value["execution_contract"] = dict(execution_contract)
    return value


def _compact_stage_value(value: object, *, depth: int = 0) -> object:
    """Bound a Skill result for progress reporting without changing semantics."""
    if isinstance(value, str):
        return value if len(value) <= 400 else value[:399] + "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth > 4:
        return "…"
    if isinstance(value, list):
        items = [_compact_stage_value(item, depth=depth + 1) for item in value[:12]]
        if len(value) > 12:
            items.append(f"… {len(value) - 12} items omitted")
        return items
    if isinstance(value, Mapping):
        items = list(value.items())
        result = {str(key): _compact_stage_value(item, depth=depth + 1)
                  for key, item in items[:24]}
        if len(items) > 24:
            result["…"] = f"{len(items) - 24} items omitted"
        return result
    return str(value)


def compact_stage_evidence_record(record: object) -> dict[str, object] | None:
    """Project the workflow's final strategic view for bounded controller logs.

    This is a transport projection only. It does not revalidate the packet or
    reinterpret Evidence workflow states.
    """
    if not isinstance(record, Mapping):
        return None
    packet = record.get("packet")
    review = record.get("review")
    if not isinstance(packet, Mapping) or not isinstance(review, Mapping):
        return None
    artifacts = packet.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, list) else []
    input_roles = [str(item.get("role")) for item in artifacts
                   if isinstance(item, Mapping) and item.get("role")]
    integrity_passed = review.get("integrity_state") == "pass"
    raw_errors = review.get("errors", [])
    raw_warnings = review.get("warnings", [])
    return {
        "workflow": record.get("workflow", "stage-evidence-workflow"),
        "stage": record.get("stage", packet.get("stage")),
        "run_id": record.get("run_id", packet.get("run_id")),
        "scope": packet.get("scope", "current_stage"),
        "history_included": packet.get("history_included", False),
        "history_mode": packet.get("history_mode", "none"),
        "input_roles": input_roles,
        "request_fingerprint": record.get("request_fingerprint", ""),
        "packet_fingerprint": review.get(
            "packet_fingerprint", packet.get("packet_fingerprint", "")),
        "input_status": packet.get("input_status"),
        "deterministic_state": packet.get("deterministic_state"),
        "missing": [] if integrity_passed else _compact_stage_value(packet.get("missing", [])),
        "conflicts": [] if integrity_passed else _compact_stage_value(packet.get("conflicts", [])),
        "review_state": review.get("review_state"),
        "integrity_state": review.get("integrity_state"),
        "summary_state": review.get("summary_state"),
        "hard_gate": review.get("hard_gate", record.get("hard_gate", "hold")),
        "attempt": review.get("attempt"),
        "model_status": review.get("model_status"),
        "validated_evidence": _compact_stage_value(review.get("strategic_view", {})),
        "adapter_report": _compact_stage_value(review.get("adapter_report", {})),
        "error_count": len(raw_errors) if isinstance(raw_errors, list) else 0,
        "warning_count": len(raw_warnings) if isinstance(raw_warnings, list) else 0,
        "errors": [] if integrity_passed else _compact_stage_value(raw_errors),
        "warnings": [] if integrity_passed else _compact_stage_value(raw_warnings),
        "next_action": review.get("next_action"),
    }


def compact_module_registry_hot_view(value: object) -> dict[str, object]:
    """Project-specific registry projection; the controller does not parse it."""
    if not isinstance(value, dict):
        return {"hard_gate": "hold", "reason": "module_registry_hot_view_missing"}
    pending = value.get("pending_transaction")
    pending_module = ((pending or {}).get("module_id") or (pending or {}).get("module")) \
        if isinstance(pending, dict) else None
    summary = value.get("module_summary")
    selected: dict[str, object] = {}
    if isinstance(summary, dict) and isinstance(pending_module, str) and pending_module in summary:
        raw = summary[pending_module]
        if isinstance(raw, dict):
            measurement = raw.get("latest_measurement")
            selected[pending_module] = {key: raw.get(key) for key in (
                "representation", "integration_state", "expansion_verified_once",
                "latest_verification",
            )}
            if isinstance(measurement, dict):
                selected[pending_module]["latest_measurement"] = {key: measurement.get(key) for key in (
                    "method", "scope", "source_run", "area_um2", "delay_ps",
                    "path_class", "current_design_match", "fmax_valid",
                )}
    current = value.get("current_blackboxes")
    current_names = list(current) if isinstance(current, list) else []
    available = value.get("body_available_blackboxes")
    available_names = list(available) if isinstance(available, list) else list(current_names)
    expandable = value.get("expandable_blackboxes")
    compact = {key: value.get(key) for key in (
        "registry_status", "hard_gate", "project_id", "session_id", "cycle_id", "phase",
        "generation", "primary_partition", "verification_pending_modules",
        "pending_transaction", "mainline", "design_context", "conflicts",
    )}
    compact.update({
        "current_blackbox_count": len(current_names),
        "expandable_blackboxes": list(expandable) if isinstance(expandable, list) else [],
        "unavailable_blackboxes": sorted(set(current_names) - set(available_names)),
        "relevant_module_summary": selected,
    })
    return compact


def _changed_lines(unified_diff: str) -> list[str]:
    return [line[1:] for line in unified_diff.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]


def conservative_rtl_change_packet(
    unified_diff: str,
    rtl_paths: list[str],
    *,
    change_mode: str,
    remaining_blackboxes: list[str],
    affected_modules: Optional[list[str]] = None,
    design_context: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    """Produce fail-closed scope facts from an actual controller diff.

    Static text checks may escalate to top-level STA.  They never claim that a
    dependency cone is safe.  Local STA is released only for a single module
    with an unchanged declaration/clock/parameter boundary and clearly
    sequential changed statements.
    """
    changed = _changed_lines(unified_diff)
    text = "\n".join(changed)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//.*", "", text)
    declaration = bool(re.search(r"\b(module|input|output|inout)\b", text))
    clock_reset = bool(re.search(r"\b(posedge|negedge|clk|clock|reset|rst_n?|reset_n)\b", text, re.I))
    parameter = bool(re.search(r"\b(parameter|localparam|defparam)\b", text))
    shared_state = bool(re.search(r"\b(memory|mem\s*\[|ram|sram|fifo|cache|arbiter)\b", text, re.I))
    combinational = bool(re.search(r"\b(always_comb|always\s*@\s*\*|assign|function|task)\b", text))
    sequential = bool(re.search(r"\b(always_ff|posedge|negedge)\b|\b[A-Za-z_][A-Za-z0-9_$.[\]]*\s*<=", text))
    local_proven = (len(rtl_paths) == 1 and sequential and not any(
        (declaration, clock_reset, parameter, shared_state, combinational)))
    facts = []
    module_names = affected_modules if affected_modules and len(affected_modules) == len(rtl_paths) else [
        Path(path).stem for path in rtl_paths]
    for path, module_name in zip(rtl_paths, module_names):
        facts.append({
            "module": module_name,
            "path": path,
            "interface_changed": declaration,
            "clock_reset_changed": clock_reset,
            "parameter_changed": parameter,
            "shared_state_changed": shared_state,
            "combinational_boundary_changed": combinational,
            "boundary_registered": local_proven,
            "impact_known": local_proven,
        })
    return {
        "change_mode": change_mode,
        "rtl_changes": facts,
        "affected_modules": module_names,
        "remaining_blackboxes": list(remaining_blackboxes),
        "top_module_changed": False,
        "filelist_changed": False,
        "constraints_changed": False,
        "blackbox_policy_changed": False,
        "measurement_adapter_changed": False,
        "design_context": dict(design_context or {}),
        "adapter_policy": "conservative_fail_closed_v1",
    }


def result_is_infrastructure(result: Mapping[str, object]) -> bool:
    fields = ("functional_exit", "functional_quick_exit", "functional_medium_exit",
              "mandatory_full_exit", "preflight_exit", "ppa_exit")
    return any(result.get(field) in (75, 76) for field in fields)


def functional_result(result: Mapping[str, object]) -> Optional[bool]:
    quick = result.get("functional_quick_exit", result.get("functional_exit"))
    if quick is None or quick in (75, 76):
        return None
    gates = [quick, result.get("functional_medium_exit"), result.get("mandatory_full_exit")]
    return all(value in (None, 0) for value in gates)


def partition_value_evidence(feedback_paths: Iterable[Path], modules: Iterable[str]) -> dict[str, dict[str, object]]:
    """Extract newest real per-partition ABC facts for selected modules.

    This adapter understands the Task-02 feedback grammar.  It returns raw
    measurements with provenance; normalization and policy stay outside it.
    """
    wanted = set(modules)
    evidence: dict[str, dict[str, object]] = {module: {} for module in wanted}
    abc = re.compile(
        r"^partition=(\S+)\s+module=.*?\bArea\s*=\s*([0-9.]+).*?\bDelay\s*=\s*([0-9.]+)\s+ps\b")
    path_class = re.compile(
        r"^partition=(\S+)\s+module=.*?\bdelay=([0-9.]+)\s+seq_pct=([0-9.]+)\s+class=(\S+)")
    for path in feedback_paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        source = path.parent.name
        for line in lines:
            match = abc.match(line)
            if match and match.group(1) in wanted:
                item = evidence[match.group(1)]
                if "area_um2" not in item:
                    item.update({"area_um2": float(match.group(2)),
                                 "delay_ps": float(match.group(3)),
                                 "ppa_source_run": source})
                continue
            match = path_class.match(line)
            if match and match.group(1) in wanted:
                item = evidence[match.group(1)]
                if "path_class" not in item:
                    item.update({"classified_delay_ps": float(match.group(2)),
                                 "sequential_percent": float(match.group(3)),
                                 "path_class": match.group(4),
                                 "classification_source_run": source})
        if all(evidence[module].get("area_um2") is not None for module in wanted):
            break
    return evidence


def rtl_instantiation_counts(rtl_root: Path, modules: Iterable[str]) -> dict[str, int]:
    """Count source-level instantiation statements as a replication proxy."""
    names = list(dict.fromkeys(str(module) for module in modules if str(module)))
    patterns = {
        module: re.compile(rf"^\s*{re.escape(module)}(?![A-Za-z0-9_$])\s*(?:#\s*\(|[A-Za-z_][A-Za-z0-9_$]*\b)", re.MULTILINE)
        for module in names
    }
    counts = {module: 0 for module in names}
    for path in sorted(rtl_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".v", ".sv"):
            continue
        try:
            text_value = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text_value = re.sub(r"//.*", "", text_value)
        for module, pattern in patterns.items():
            counts[module] += len(pattern.findall(text_value))
    return counts
