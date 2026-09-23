#!/usr/bin/env python3
"""Task-02 project boundary for data normalization and command materialization.

This module may know ppa.toml, Task-02 result vocabulary, paths, and commands.
It translates portable requests and normalizes observations but does not own
Registry, Closure, Optimization lifecycle policy, or process supervision.
"""
from __future__ import annotations

import difflib
import re
import json
import hashlib
import sys
from pathlib import Path
from typing import Mapping

from decision_packet_adapter import build_decision_packet, materialize_executor_request
from decision_packet_adapter import DecisionPacketError
from module_registry_adapter import (
    optimization_packet_from_registry, registry_mainline, registry_mainline_ref)
from project_adapter_api import ProjectAdapterError
from design_context_adapter import design_context_slice
from optimization_obligation_adapter import (
    match_optimization_sta_obligation, optimization_obligation_decision_actions)
from rtl_skill_bridge import (compact_module_registry_hot_view,
                              conservative_rtl_change_packet,
                              stage_evidence_envelope)


class Task02ProjectAdapter:
    api_version = 12
    expansion_partition = "top_level_sta"
    evidence_model_argv_template = (
        "omp", "-p", "--cwd", "{project_root}",
        "--model", "{model}", "{prompt}",
    )

    def __init__(self, *, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    @property
    def ppa_config(self) -> Path:
        return self.project_root / "harness/ppa.toml"

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    @staticmethod
    def _file_identity(path: Path) -> dict[str, object]:
        content = path.read_bytes()
        return {
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def _newest_evidence_path(self, pattern: str) -> Path | None:
        candidates = list((self.project_root / "runs").glob(pattern))
        if not candidates:
            return None
        try:
            return max(candidates,
                       key=lambda path: (path.stat().st_mtime_ns, path.as_posix()))
        except OSError:
            return None

    def _evidence_descriptor(self, path: Path | None, kind: str) -> dict[str, object] | None:
        if path is None:
            return None
        try:
            identity = self._file_identity(path)
            display_path = path.relative_to(self.project_root).as_posix()
        except OSError:
            return {"kind": kind, "path": path.as_posix(), "status": "unreadable"}
        except ValueError:
            display_path = path.as_posix()
        return {
            "kind": kind,
            "path": display_path,
            "run_id": path.parent.name,
            **identity,
        }

    def _evidence_manifest(self) -> dict[str, object]:
        manifest: dict[str, object] = {
            "version": 1,
            "result": self._evidence_descriptor(
                self._newest_evidence_path("agent-02_yosys_ppa--*/result.json"),
                "controller_result"),
            "feedback": self._evidence_descriptor(
                self._newest_evidence_path("02_yosys_ppa--*/feedback.txt"),
                "ppa_feedback"),
        }
        manifest["has_evidence"] = bool(manifest["result"] or manifest["feedback"])
        manifest["fingerprint"] = hashlib.sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return manifest

    @staticmethod
    def _compact_run_evidence(value: object) -> dict[str, object] | None:
        if not isinstance(value, Mapping) or value.get("status") != "ok":
            return None
        blocks = value.get("path_blocks")
        summaries: list[dict[str, object]] = []
        if isinstance(blocks, list):
            for block in blocks[:8]:
                if isinstance(block, Mapping) and isinstance(block.get("summary"), Mapping):
                    summaries.append(dict(block["summary"]))
        return {
            "scope": "current_stage",
            "history_included": False,
            "status": "ok",
            "source_run_id": value.get("source_run_id"),
            "selector": value.get("selector"),
            "available_path_count": value.get("available_path_count"),
            "returned_path_count": value.get("returned_path_count", len(summaries)),
            "truncated": value.get("truncated", False),
            "path_summaries": summaries,
        }

    @staticmethod
    def _descriptor_path(project_root: Path, descriptor: object) -> Path | None:
        if not isinstance(descriptor, Mapping):
            return None
        raw_path = descriptor.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        path = Path(raw_path).expanduser()
        return path if path.is_absolute() else project_root / path

    def materialize_stage_evidence_execution(
            self, *, state: Mapping[str, object], closure_audit: Mapping[str, object],
            optimization_projection: Mapping[str, object],
            session_root: Path, cycle: int, workflow_root: Path,
            evidence_model: str, stage: str, contract_path: Path,
            max_artifact_bytes: int, prompt_max_chars: int,
            max_facts_per_skill: int, max_total_facts: int) -> dict[str, object]:
        """Materialize one Task-02 handoff to the existing Evidence Skill runner."""
        manifest = self._evidence_manifest()
        artifacts: dict[str, Path] = {}
        run_id = "unknown"
        for role in ("result", "feedback"):
            descriptor = manifest.get(role)
            path = self._descriptor_path(self.project_root, descriptor)
            if path is not None:
                artifacts[role] = path
            if run_id == "unknown" and isinstance(descriptor, Mapping) \
                    and isinstance(descriptor.get("run_id"), str):
                run_id = str(descriptor["run_id"])

        session_root.mkdir(parents=True, exist_ok=True)
        registry = state.get("module_registry_hot_view")
        registry = registry if isinstance(registry, Mapping) else {}
        source_cycle_id = registry.get("cycle_id")
        closure_path = session_root / f"skill_closure_governance_{cycle}.json"
        self._write_json(closure_path, stage_evidence_envelope(
            skill="rtl-closure-governance",
            identity={"session_id": state.get("session"), "cycle_id": source_cycle_id,
                      "phase": closure_audit.get("phase")},
            hard_gate={key: closure_audit.get(key) for key in (
                "closure_governance_status", "closure_repair_required",
                "measurement_incomplete", "measurement_rerun_admissible",
                "max_budget_exhausted",
                "strategy_change_required", "repair_domain", "error_signature")},
            summary_input={key: value for key, value in closure_audit.items()
                           if key not in {"raw", "diagnostic_lines"}},
        ))
        artifacts["skill_closure_governance"] = closure_path

        registry_path = session_root / f"skill_module_registry_{cycle}.json"
        self._write_json(registry_path, stage_evidence_envelope(
            skill="rtl-module-evidence-registry",
            identity={key: registry.get(key) for key in (
                "project_id", "session_id", "cycle_id", "generation")},
            hard_gate={
                "status": registry.get("hard_gate", "hold"),
                "pending_transaction": registry.get("pending_transaction"),
                "expandable_blackboxes": registry.get("expandable_blackboxes", []),
            },
            summary_input=compact_module_registry_hot_view(registry),
        ))
        artifacts["skill_module_registry"] = registry_path

        optimization_value = (dict(optimization_projection)
                              if isinstance(optimization_projection, Mapping) else {})
        optimization_hard_gate = optimization_value.get("hard_gate")
        optimization_hard_gate = (dict(optimization_hard_gate)
                                  if isinstance(optimization_hard_gate, Mapping) else {})
        optimization_summary = optimization_value.get("summary_input")
        optimization_summary = (dict(optimization_summary)
                                if isinstance(optimization_summary, Mapping) else {})
        optimization_path = session_root / f"skill_optimization_triage_{cycle}.json"
        execution_contract = optimization_value.get("execution_contract")
        self._write_json(optimization_path, stage_evidence_envelope(
            skill="rtl-optimization-triage",
            identity={"session_id": state.get("session"), "cycle_id": source_cycle_id,
                      "registry_generation": registry.get("generation")},
            hard_gate=optimization_hard_gate,
            summary_input=optimization_summary,
            execution_contract=(execution_contract
                                if isinstance(execution_contract, Mapping) else None),
        ))
        artifacts["skill_optimization_triage"] = optimization_path

        compact_run = self._compact_run_evidence(state.get("latest_run_evidence"))
        if compact_run is not None:
            run_evidence_path = session_root / f"run_evidence_{cycle}.json"
            self._write_json(run_evidence_path, compact_run)
            artifacts["run_evidence"] = run_evidence_path

        result_path = artifacts.get("result")
        result_value: object = {}
        if result_path is not None:
            try:
                result_value = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                result_value = {}
        sta_scope = (result_value.get("sta_scope_gate")
                     if isinstance(result_value, Mapping) else None)
        if isinstance(sta_scope, Mapping):
            run_ids = [str(item.get("run_id")) for item in (
                manifest.get("result"), manifest.get("feedback"))
                       if isinstance(item, Mapping) and item.get("run_id")]
            sta_scope_path = session_root / f"skill_sta_scope_{cycle}.json"
            self._write_json(sta_scope_path, stage_evidence_envelope(
                skill="rtl-sta-scope",
                identity={"session_id": state.get("session"), "cycle_id": source_cycle_id,
                          "result_run_ids": run_ids},
                hard_gate={"scope": sta_scope.get("scope"),
                           "reason_codes": sta_scope.get("reason_codes")},
                summary_input=sta_scope,
            ))
            artifacts["skill_sta_scope"] = sta_scope_path

        artifact_identity = {
            role: self._file_identity(path) for role, path in sorted(artifacts.items())
        }
        request_payload = {
            "stage": stage,
            "run_id": run_id,
            "manifest": manifest,
            "contract": self._file_identity(contract_path),
            "artifacts": artifact_identity,
            "model": evidence_model,
            "model_command": list(self.evidence_model_argv_template),
            "limits": {
                "max_artifact_bytes": max_artifact_bytes,
                "prompt_max_chars": prompt_max_chars,
                "max_facts_per_skill": max_facts_per_skill,
                "max_total_facts": max_total_facts,
            },
        }
        request_fingerprint = hashlib.sha256(json.dumps(
            request_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        output_path = session_root / f"stage_evidence_workflow_{cycle}.json"
        log_path = session_root / f"evidence_workflow_{cycle}.log"
        script = workflow_root / "scripts/evidence_workflow.py"
        command = [
            sys.executable, str(script), "run", "--stage", stage,
            "--run-id", run_id, "--output", str(output_path),
            "--work-dir", str(session_root), "--run-label", str(cycle),
            "--project-root", str(self.project_root), "--model", evidence_model,
            "--model-command-json", json.dumps(request_payload["model_command"]),
            "--contract", str(contract_path),
            "--max-artifact-bytes", str(max_artifact_bytes),
            "--prompt-max-chars", str(prompt_max_chars),
            "--max-facts-per-skill", str(max_facts_per_skill),
            "--max-total-facts", str(max_total_facts),
        ]
        for role, path in artifacts.items():
            command.extend(("--artifact", f"{role}={path}"))
        feedback = manifest.get("feedback")
        result = manifest.get("result")
        cache_source_run_id = str(
            (feedback.get("run_id") if isinstance(feedback, Mapping) else None)
            or (result.get("run_id") if isinstance(result, Mapping) else None)
            or "unavailable")
        return {
            "has_evidence": bool(manifest.get("has_evidence")),
            "manifest": manifest,
            "stage": stage,
            "run_id": run_id,
            "artifacts": artifacts,
            "request_fingerprint": request_fingerprint,
            "cache_source_run_id": cache_source_run_id,
            "command": command,
            "output_path": output_path,
            "log_path": log_path,
        }

    @staticmethod
    def normalize_stage_evidence_execution(
            execution: Mapping[str, object], exit_code: int) -> dict[str, object]:
        """Normalize transport/output without reimplementing Evidence validation."""
        stage = str(execution.get("stage") or "")
        run_id = str(execution.get("run_id") or "unknown")
        output_path = Path(str(execution.get("output_path") or ""))
        log_path = Path(str(execution.get("log_path") or ""))
        try:
            record = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return {
                "workflow": "stage-evidence-workflow", "stage": stage,
                "run_id": run_id, "request_fingerprint": execution.get("request_fingerprint", ""),
                "hard_gate": "hold",
                "review": {
                    "review_state": "INFRA_UNCERTAIN", "integrity_state": "infra_uncertain",
                    "hard_gate": "hold", "attempt": 0,
                    "errors": [{"kind": "evidence_workflow_failed", "exit": exit_code,
                                "detail": str(error), "log": log_path.name}],
                },
            }
        if not isinstance(record, dict):
            return {
                "workflow": "stage-evidence-workflow", "stage": stage,
                "run_id": run_id, "request_fingerprint": execution.get("request_fingerprint", ""),
                "hard_gate": "hold",
                "review": {
                    "review_state": "INFRA_UNCERTAIN", "integrity_state": "infra_uncertain",
                    "hard_gate": "hold", "attempt": 0,
                    "errors": [{"kind": "evidence_workflow_output_invalid"}],
                },
            }
        record["request_fingerprint"] = execution.get("request_fingerprint", "")
        packet = record.get("packet")
        review = record.get("review")
        identity_valid = (
            record.get("workflow") == "stage-evidence-workflow"
            and record.get("stage") == stage and record.get("run_id") == run_id
            and isinstance(packet, Mapping) and packet.get("stage") == stage
            and packet.get("run_id") == run_id and isinstance(review, Mapping)
            and review.get("stage") == stage and review.get("run_id") == run_id
            and review.get("packet_fingerprint") == packet.get("packet_fingerprint"))
        if exit_code == 0 and identity_valid:
            return record
        normalized_review = dict(review) if isinstance(review, Mapping) else {}
        normalized_review.update({
            "review_state": "INFRA_UNCERTAIN" if exit_code else "BLOCKED_EVIDENCE",
            "integrity_state": "infra_uncertain" if exit_code else "hold",
            "hard_gate": "hold",
        })
        normalized_review.setdefault("errors", []).append({
            "kind": "evidence_workflow_exit" if exit_code
                    else "evidence_workflow_identity_mismatch",
            "exit": exit_code,
            "log": log_path.name,
        })
        record["review"] = normalized_review
        record["hard_gate"] = "hold"
        return record

    def current_blackboxes(self) -> list[str]:
        in_partition = False
        current_name = None
        for line in self.ppa_config.read_text(encoding="utf-8").splitlines():
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
            if current_name == self.expansion_partition:
                match = re.match(r'\s*blackbox\s*=\s*"([^"]*)"\s*$', line)
                if match:
                    return [item for item in match.group(1).split() if item]
        return []

    def partition_timeouts(self) -> dict[str, int]:
        """Read exact project partition names and their declared tool windows."""
        values: dict[str, int] = {}
        current = None
        for line in self.ppa_config.read_text(encoding="utf-8").splitlines():
            if line.strip() == "[[partitions]]":
                current = None
                continue
            name_match = re.match(
                r'\s*name\s*=\s*"([A-Za-z0-9_.-]+)"\s*$', line)
            if name_match:
                current = name_match.group(1)
                values.setdefault(current, 0)
                continue
            timeout_match = re.match(r"\s*timeout_s\s*=\s*(\d+)\s*$", line)
            if current and timeout_match:
                values[current] = int(timeout_match.group(1))
        return values

    def optimization_sta_scope_packet(
            self, execution: Mapping[str, object]) -> dict[str, object]:
        """Map one candidate identity to the portable STA-scope input schema."""
        module = str(execution.get("module") or "")
        rtl_ref = execution.get("rtl_ref") or self.rtl_file_for_module(module)
        checkpoint_ref = str(execution.get("checkpoint_ref") or "")
        checkpoint = Path(checkpoint_ref)
        if not rtl_ref or not checkpoint_ref:
            raise ProjectAdapterError("optimization STA scope identity is incomplete")
        relative = Path(str(rtl_ref))
        current = (self.project_root / relative).resolve()
        baseline = (checkpoint / "preimage" / relative).resolve()
        try:
            current.relative_to(self.project_root)
            baseline.relative_to(checkpoint.resolve())
        except ValueError as error:
            raise ProjectAdapterError(
                "optimization STA scope paths escape their roots") from error
        if not current.is_file() or not baseline.is_file():
            raise ProjectAdapterError("optimization STA scope RTL or checkpoint is missing")
        before = baseline.read_text(
            encoding="utf-8", errors="replace").splitlines(keepends=True)
        after = current.read_text(
            encoding="utf-8", errors="replace").splitlines(keepends=True)
        changes = "".join(difflib.unified_diff(
            before, after, fromfile=f"baseline/{relative.as_posix()}",
            tofile=f"current/{relative.as_posix()}"))
        context = design_context_slice(
            self.project_root, rtl_paths=[relative.as_posix()],
            module_hints=[module])
        return conservative_rtl_change_packet(
            changes, [relative.as_posix()], change_mode="local",
            remaining_blackboxes=self.current_blackboxes(),
            affected_modules=[module], design_context=context)

    def prepare_cycle(self, *, state: Mapping[str, object], stage_record: object,
                      evidence_fingerprint: str, stop_requested: bool,
                      functional_baseline_clean: bool,
                      optimization_handoff: Mapping[str, object] | None) -> dict[str, object]:
        registry = state.get("module_registry_hot_view")
        registry = registry if isinstance(registry, Mapping) else {}
        eligible = registry.get("expandable_blackboxes")
        eligible_names = ({str(item) for item in eligible}
                          if isinstance(eligible, list) else set())
        expandable = [name for name in self.current_blackboxes()
                      if name in eligible_names]
        try:
            handoff = (optimization_handoff
                       if isinstance(optimization_handoff, Mapping) else {})
            obligation_match = handoff.get("resolution")
            obligation_match = (obligation_match
                                if isinstance(obligation_match, Mapping) else {})
            packet = build_decision_packet(
                state=state,
                stage_record=stage_record,
                evidence_fingerprint=evidence_fingerprint,
                stop_requested=stop_requested,
                functional_baseline_clean=functional_baseline_clean,
                expandable_blackboxes=expandable,
                project_actions=optimization_obligation_decision_actions(
                    obligation_match),
            )
            return packet
        except DecisionPacketError as error:
            raise ProjectAdapterError(str(error)) from error

    def materialize_request(self, decision: Mapping[str, object],
                            packet: Mapping[str, object], **context: object) -> dict[str, object]:
        return materialize_executor_request(decision, packet, **context)

    def materialize_project_execution(
            self, request: Mapping[str, object], *, session_root: Path,
            cycle: int) -> dict[str, object]:
        """Map one portable project request to the Task-02 one-shot executor."""
        operation_id = str(request.get("operation_id") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", operation_id):
            raise ProjectAdapterError("project execution requires one SHA-256 operation_id")
        if request.get("executor_ref") != "project.expand_blackbox":
            raise ProjectAdapterError("unsupported Task-02 project executor reference")
        request_path = session_root / f"project_execution_request_{cycle}.json"
        output_path = session_root / f"project_execution_result_{cycle}.json"
        self._write_json(request_path, dict(request))
        return {
            "operation_id": operation_id,
            "executor_ref": request.get("executor_ref"),
            "command": [
                sys.executable,
                str(self.project_root / "harness/task02_project_executor.py"),
                "--project-root", str(self.project_root),
                "--request", str(request_path),
                "--output", str(output_path),
            ],
            "request_path": request_path,
            "output_path": output_path,
        }

    @staticmethod
    def normalize_project_execution(
            execution: Mapping[str, object], process_exit: int) -> dict[str, object]:
        """Normalize one executor transport result without deciding next state."""
        operation_id = str(execution.get("operation_id") or "")
        executor_ref = str(execution.get("executor_ref") or "")
        output_path = Path(str(execution.get("output_path") or ""))
        try:
            value = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return {
                "operation_id": operation_id, "executor_ref": executor_ref,
                "status": "infra_uncertain", "exit_code": process_exit,
                "artifacts": [], "evidence_refs": [],
                "detail": f"project executor output unavailable: {error}",
            }
        if (not isinstance(value, dict)
                or value.get("operation_id") != operation_id
                or value.get("executor_ref") != executor_ref):
            return {
                "operation_id": operation_id, "executor_ref": executor_ref,
                "status": "infra_uncertain", "exit_code": process_exit,
                "artifacts": [], "evidence_refs": [],
                "detail": "project executor result identity mismatch",
            }
        if process_exit != 0:
            return {
                **value, "status": "infra_uncertain", "exit_code": process_exit,
                "detail": str(value.get("detail") or
                              "project executor process exited unsuccessfully"),
            }
        return value

    @staticmethod
    def normalize_measurement_outcome(
            *, results: list[Mapping[str, object]], process_exit: int | None,
            closure_audit: Mapping[str, object], preflight_only: bool = False,
            require_timing: bool = False) -> dict[str, object]:
        """Normalize Task-02 execution facts without choosing a next action.

        Closure phase semantics come only from ``closure_audit``.  This method
        composes those semantics with project transport and functional/preflight
        gates so controllers do not maintain another timeout classifier.
        """
        values = [dict(item) for item in results if isinstance(item, Mapping)]
        audit = dict(closure_audit) if isinstance(closure_audit, Mapping) else {}
        exit_fields = (
            "planner_exit", "executor_exit", "functional_recovery_exit",
            "functional_exit", "functional_quick_exit", "functional_medium_exit",
            "mandatory_medium_exit", "mandatory_full_exit", "preflight_exit",
            "ppa_exit",
        )
        exit_codes = [item.get(field) for item in values for field in exit_fields]
        if process_exit is not None:
            exit_codes.append(process_exit)
        if (any(item.get("kind") == "infrastructure_uncertain" for item in values)
                or 76 in exit_codes):
            return {"status": "infra_uncertain", "measurement_incomplete": False,
                    "reason": "project_transport_uncertain"}
        if 75 in exit_codes:
            return {"status": "infra_failed", "measurement_incomplete": False,
                    "reason": "project_transport_failed"}

        functional_fields = (
            "functional_exit", "functional_quick_exit", "functional_medium_exit",
            "mandatory_medium_exit", "mandatory_full_exit",
        )
        for item in values:
            quick = item.get("functional_quick_exit")
            if quick is None:
                quick = item.get("functional_exit")
            functional_codes = [quick, *(item.get(field) for field in functional_fields[2:])]
            if any(code not in (None, 0) for code in functional_codes):
                return {"status": "functional_failed", "measurement_incomplete": False,
                        "reason": "deterministic_functional_failure"}
            if item.get("preflight_exit") not in (None, 0):
                return {"status": "preflight_failed", "measurement_incomplete": False,
                        "reason": "deterministic_preflight_failure"}
            if any(item.get(field) not in (None, 0)
                   for field in ("planner_exit", "executor_exit")):
                return {"status": "execution_failed", "measurement_incomplete": False,
                        "reason": "planner_or_executor_failure"}

        if audit.get("closure_governance_status") == "unavailable":
            return {"status": "infra_uncertain", "measurement_incomplete": False,
                    "reason": "closure_governance_unavailable"}

        ppa_codes = [item.get("ppa_exit") for item in values
                     if item.get("ppa_exit") is not None]
        if preflight_only and values:
            return {"status": "measurement_not_run", "measurement_incomplete": False,
                    "reason": "preflight_only_no_measurement"}
        if (require_timing and ppa_codes
                and (audit.get("sta_link_result") != "ok"
                     or audit.get("sta_timing_measured") is not True)):
            return {"status": "verification_evidence_missing", "measurement_incomplete": False,
                    "reason": "required_timing_evidence_missing"}
        if ppa_codes and audit.get("measurement_incomplete") is True:
            return {"status": "measurement_incomplete", "measurement_incomplete": True,
                    "reason": str(audit.get("phase") or "closure_measurement_incomplete")}
        if any(code == 0 for code in ppa_codes):
            return {"status": "complete", "measurement_incomplete": False,
                    "reason": "measurement_completed"}
        if ppa_codes:
            return {"status": "measurement_failed", "measurement_incomplete": False,
                    "reason": "measurement_process_failed_without_incomplete_closure_audit"}
        if process_exit not in (None, 0):
            return {"status": "execution_failed", "measurement_incomplete": False,
                    "reason": "controller_process_failed"}
        return {"status": "measurement_not_run", "measurement_incomplete": False,
                "reason": "no_current_measurement"}

    def rtl_file_for_module(self, module: str) -> Path | None:
        """Resolve one module declaration to exactly one project-relative RTL file."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module):
            return None
        declaration = re.compile(rf"^\s*module\s+{re.escape(module)}\b", re.MULTILINE)
        matches: list[Path] = []
        for path in sorted((self.project_root / "rtl").rglob("*")):
            if not path.is_file() or path.suffix.lower() not in (".v", ".sv"):
                continue
            try:
                if declaration.search(path.read_text(encoding="utf-8", errors="replace")):
                    matches.append(path.relative_to(self.project_root))
            except OSError:
                continue
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _optimization_runtime_facts(
            *, registry: Mapping[str, object],
            closure_audit: Mapping[str, object],
            functional_baseline_clean: bool,
            pending_harness_verification: bool) -> dict[str, object]:
        """Normalize live project facts without deriving Optimization actions."""
        pending = registry.get("pending_transaction")
        pending = pending if isinstance(pending, Mapping) else {}
        blackboxes = registry.get("current_blackboxes")
        blackboxes = blackboxes if isinstance(blackboxes, list) else []
        measurement_policy = closure_audit.get("measurement_policy")
        measurement_policy = (measurement_policy
                              if isinstance(measurement_policy, Mapping) else {})
        return {
            "registry_integrity": (
                "pass" if registry.get("hard_gate") == "pass" else "hold"),
            "registry_conflict": bool(registry.get("conflicts")),
            "closure_integrity": (
                "hold" if closure_audit.get("closure_governance_status") == "unavailable"
                else "pass"),
            "structural_harness_repair_pending": bool(
                closure_audit.get("structural_repair_required") is True
                and "harness" in str(closure_audit.get("repair_domain", ""))),
            "pending_harness_verification": bool(pending_harness_verification),
            "functional_baseline_clean": bool(functional_baseline_clean),
            "current_blackboxes": [str(item) for item in blackboxes],
            "pending_mainline": ({
                "kind": pending.get("kind"),
                "module": pending.get("module_id") or pending.get("module"),
                "partition": pending.get("partition"),
                "transaction_id": pending.get("transaction_id"),
            } if pending else {}),
            "measurement_policy": dict(measurement_policy),
        }

    def prepare_optimization_input(
            self, *, registry: Mapping[str, object],
            closure_audit: Mapping[str, object], functional_baseline_clean: bool,
            pending_harness_verification: bool,
            minimum_value_score: float) -> dict[str, object]:
        """Project current facts into the portable campaign runner input."""
        triage_input = optimization_packet_from_registry(
            registry, mainline=registry_mainline(registry),
            minimum_value_score=minimum_value_score)
        triage_input["runtime_context"] = self._optimization_runtime_facts(
            registry=registry, closure_audit=closure_audit,
            functional_baseline_clean=functional_baseline_clean,
            pending_harness_verification=pending_harness_verification)
        return {
            "triage_input": triage_input,
            "mainline_ref": registry_mainline_ref(registry),
            "registry_snapshot": {
                "hard_gate": registry.get("hard_gate"),
                "conflicts": registry.get("conflicts", []),
                "generation": registry.get("generation"),
                "pending_transaction": registry.get("pending_transaction"),
            },
        }

    def resolve_optimization_handoff(
            self, handoff: Mapping[str, object],
            registry: Mapping[str, object]) -> dict[str, object]:
        """Resolve a portable STA obligation against current Task-02 work."""
        obligation = handoff.get("verification_obligation")
        if not isinstance(obligation, Mapping):
            raise ProjectAdapterError("optimization handoff has no STA obligation")
        return self.match_optimization_obligation(obligation, registry)

    def materialize_optimization_execution(
            self, execution: Mapping[str, object], **context: object) -> dict[str, object]:
        """Translate a portable Optimization Skill request into Task-02 transport.

        This adapter selects paths and command-line flags only.  It does not
        choose a candidate, advance the lifecycle, or interpret success.
        """
        executor_ref = str(execution.get("executor_ref") or "")
        operation_id = str(execution.get("operation_id") or "")
        campaign_id = str(execution.get("campaign_id") or "")
        module = str(execution.get("module") or "")
        if not operation_id or not campaign_id or not module:
            raise ProjectAdapterError("optimization execution identity is incomplete")
        state_path = Path(str(context.get("state_path") or ""))
        checkpoint_root = Path(str(context.get("checkpoint_root") or ""))
        if executor_ref in {"optimization.edit", "optimization.rollback"}:
            rtl_ref = execution.get("rtl_ref") or self.rtl_file_for_module(module)
            if rtl_ref is None:
                raise ProjectAdapterError(
                    f"optimization module {module!r} has no unique RTL source")
            request: dict[str, object] = {
                "project_root": self.project_root.as_posix(),
                "state_path": state_path.as_posix(),
                "checkpoint_root": checkpoint_root.as_posix(),
                "operation_id": operation_id,
                "campaign_id": campaign_id,
                "module": module,
                "rtl_path": Path(str(rtl_ref)).as_posix(),
            }
            if executor_ref == "optimization.rollback":
                request["operation"] = "rollback_candidate"
            else:
                candidate = execution.get("candidate")
                candidate = candidate if isinstance(candidate, Mapping) else {}
                insertion = execution.get("insertion_plan")
                insertion = insertion if isinstance(insertion, Mapping) else {}
                request.update({
                    "interrupted_mainline_ref": dict(
                        execution.get("mainline_ref")
                        if isinstance(execution.get("mainline_ref"), Mapping)
                        else context.get("mainline_ref")
                        if isinstance(context.get("mainline_ref"), Mapping) else {}),
                    "integration_target": insertion.get("integration_target")
                    or context.get("integration_target"),
                    "insertion_mode": insertion.get("insertion_mode") or "decoupled",
                    "top_level_acceptance_required": bool(
                        insertion.get("top_level_acceptance_required")),
                    "hypothesis": context.get("hypothesis")
                    or "将高价值候选从较差实现优化到可接受实现",
                    "model": context.get("model"),
                    "model_command": [
                        "omp", "-p", "--cwd", "{project_root}",
                        "--model", "{model}", "{prompt}"],
                    "evidence": {"candidate": dict(candidate)},
                })
            return {"executor_ref": executor_ref, "runner_request": request}
        if executor_ref == "capability.resolve_sta_scope":
            return {
                "executor_ref": executor_ref,
                "capability_request": {
                    "capability_ref": "rtl.sta_scope",
                    "packet": self.optimization_sta_scope_packet(execution),
                },
            }
        if executor_ref != "project.verify_optimization":
            raise ProjectAdapterError(
                f"unsupported optimization executor_ref: {executor_ref}")
        operation = str(execution.get("operation") or "")
        if operation not in {"functional_preflight", "measure_candidate",
                             "consume_external_sta_obligation"}:
            raise ProjectAdapterError(
                f"unsupported optimization verification operation: {operation}")
        optimization_executor = Path(str(context.get("optimization_executor") or ""))
        checkpoint_ref = str(execution.get("checkpoint_ref") or "")
        checkpoint = Path(checkpoint_ref)
        rtl_ref = execution.get("rtl_ref") or self.rtl_file_for_module(module)
        if not optimization_executor.is_file() or not checkpoint_ref or rtl_ref is None:
            raise ProjectAdapterError("optimization verification paths are incomplete")
        command = [
            sys.executable, optimization_executor.as_posix(),
            "--operation", ("measure_candidate"
                            if operation == "consume_external_sta_obligation"
                            else operation),
            "--module", module,
            "--campaign-id", campaign_id,
            "--rtl-ref", Path(str(rtl_ref)).as_posix(),
            "--checkpoint", checkpoint.as_posix(),
        ]
        fingerprint = str(execution.get("expected_rtl_fingerprint") or "")
        if not fingerprint:
            raise ProjectAdapterError(
                "optimization verification requires the candidate RTL fingerprint")
        command.extend(["--expected-rtl-sha256", fingerprint])
        functional_proof = execution.get("functional_evidence")
        functional_proof = functional_proof if isinstance(functional_proof, Mapping) else {}
        functional_clean = all(functional_proof.get(key) == 0 for key in (
            "functional_quick_exit", "functional_medium_exit", "mandatory_full_exit"))
        if (fingerprint and functional_clean
                and functional_proof.get("rtl_fingerprint") == fingerprint
                and functional_proof.get("verification_context_fingerprint")):
            command.extend(["--functional-proof-rtl-sha256", fingerprint])
            command.extend(["--functional-proof-context-sha256",
                            str(functional_proof["verification_context_fingerprint"])])
        preflight_proof = execution.get("preflight_evidence")
        preflight_proof = preflight_proof if isinstance(preflight_proof, Mapping) else {}
        if (fingerprint and preflight_proof.get("preflight_exit") == 0
                and preflight_proof.get("rtl_fingerprint") == fingerprint
                and preflight_proof.get("verification_context_fingerprint")):
            command.extend(["--preflight-proof-rtl-sha256", fingerprint])
            command.extend(["--preflight-proof-context-sha256",
                            str(preflight_proof["verification_context_fingerprint"])])
        partitions = execution.get("measurement_partitions")
        selected_partitions = (
            [str(item) for item in partitions if isinstance(item, str) and item]
            if isinstance(partitions, list) else [])
        if operation == "consume_external_sta_obligation":
            selected_partitions = [self.expansion_partition]
        for partition in selected_partitions:
                if isinstance(partition, str) and partition:
                    command.extend(["--measurement-partition", partition])
        timeout = execution.get("measurement_timeout_seconds")
        if (operation == "consume_external_sta_obligation"
                and not isinstance(timeout, int)):
            timeout = max(180, self.partition_timeouts().get(
                self.expansion_partition, 0))
        if isinstance(timeout, int) and timeout > 0:
            command.extend(["--measurement-timeout-seconds", str(timeout)])
        return {
            "executor_ref": executor_ref,
            "directive": (
                "只执行当前 Optimization Skill 请求的无状态项目验证；禁止编辑 RTL。\n"),
            "command": command,
        }

    def match_optimization_obligation(
            self, obligation: Mapping[str, object], registry: Mapping[str, object], *,
            current_rtl_fingerprint: str | None = None) -> dict[str, object]:
        """Apply only Task-02 transaction compatibility and availability rules."""
        if current_rtl_fingerprint is None:
            rtl_ref = self.rtl_file_for_module(str(obligation.get("target_module") or ""))
            rtl_path = (self.project_root / rtl_ref).resolve() if rtl_ref else None
            if rtl_path is not None and rtl_path.is_file():
                current_rtl_fingerprint = hashlib.sha256(rtl_path.read_bytes()).hexdigest()
        return match_optimization_sta_obligation(
            obligation, registry,
            current_rtl_fingerprint=current_rtl_fingerprint)

    def materialize_external_optimization_sta(
            self, execution: Mapping[str, object], **context: object) -> dict[str, object]:
        """Map one yielded global STA duty to the stateless project executor."""
        materialized = self.materialize_optimization_execution(execution, **context)
        command = materialized.get("command")
        if not isinstance(command, list):
            raise ProjectAdapterError("external optimization STA command is missing")
        materialized["directive"] = (
            "消费已登记的顶层 STA 验证义务；禁止 planner、executor 和任何 RTL/harness 修改。\n")
        return materialized

    def materialize_external_optimization_plan(
            self, *, decision: Mapping[str, object] | None,
            decision_packet: Mapping[str, object] | None,
            optimization_handoff: Mapping[str, object] | None,
            directive_path: Path,
            optimization_executor: Path) -> dict[str, object] | None:
        """Materialize only a Pro-approved shared/global STA handoff."""
        selected = decision if isinstance(decision, Mapping) else {}
        packet = decision_packet if isinstance(decision_packet, Mapping) else {}
        action = str(selected.get("action") or "")
        actions = packet.get("allowed_actions")
        actions = actions if isinstance(actions, Mapping) else {}
        spec = actions.get(action)
        spec = spec if isinstance(spec, Mapping) else {}
        handoff = (optimization_handoff
                   if isinstance(optimization_handoff, Mapping) else {})
        match = handoff.get("resolution")
        match = match if isinstance(match, Mapping) else {}
        execution = handoff.get("requested_execution")
        execution = execution if isinstance(execution, Mapping) else {}
        if (spec.get("executor_ref") != "project.optimization_external_sta"
                or match.get("status") not in {
                    "merge_with_mainline", "standalone_ready"}
                or execution.get("executor_ref") != "project.verify_optimization"):
            return None
        materialized = self.materialize_external_optimization_sta(
            execution, directive_path=directive_path,
            optimization_executor=optimization_executor)
        command = materialized.get("command")
        if not isinstance(command, list) or not command:
            raise ProjectAdapterError("project adapter returned no optimization command")
        return {
            "kind": "external",
            "execution": dict(execution),
            "command": [str(item) for item in command],
            "directive": materialized.get("directive"),
            "obligation_match": dict(match),
        }

    def normalize_optimization_capability(
            self, execution: Mapping[str, object], *, decision: object = None,
            unavailable_reason: str | None = None) -> dict[str, object]:
        """Map a generic STA-scope decision to Task-02 measurement availability."""
        module = str(execution.get("module") or "")
        identity = {
            "operation_id": execution.get("operation_id"),
            "campaign_id": execution.get("campaign_id"),
            "module": module,
        }
        rtl_ref = execution.get("rtl_ref") or self.rtl_file_for_module(module)
        rtl_path = (self.project_root / Path(str(rtl_ref))).resolve() if rtl_ref else None
        rtl_fingerprint = None
        if rtl_path is not None and rtl_path.is_file():
            try:
                rtl_path.relative_to(self.project_root)
                rtl_fingerprint = hashlib.sha256(rtl_path.read_bytes()).hexdigest()
            except (OSError, ValueError):
                rtl_fingerprint = None
        expected = str(execution.get("expected_rtl_fingerprint") or "")
        if expected and rtl_fingerprint != expected:
            status = "infra_uncertain"
            observation = {
                **identity, "status": status, "rtl_fingerprint": rtl_fingerprint,
                "reason": "candidate_identity_changed_before_sta_scope",
            }
        elif unavailable_reason is not None:
            status = "sta_scope_unavailable"
            observation = {
                **identity, "status": status, "rtl_fingerprint": rtl_fingerprint,
                "reason": unavailable_reason,
            }
        else:
            gate = dict(decision) if isinstance(decision, Mapping) else {}
            scope = str(gate.get("scope") or "")
            timeouts = self.partition_timeouts()
            partitions: list[str] = []
            if scope == "local_module" and module in timeouts:
                partitions = [module]
            elif scope == "affected_cone":
                affected = gate.get("affected_modules")
                affected = affected if isinstance(affected, list) else []
                partitions = [str(item) for item in affected if str(item) in timeouts]
            external = scope in {"top_level_partial", "top_level_complete"}
            if not external and not partitions:
                raw_reasons = gate.get("reason_codes")
                raw_reasons = raw_reasons if isinstance(raw_reasons, list) else []
                gate = {
                    **gate,
                    "requested_scope": scope or None,
                    "scope": "top_level_partial",
                    "reason_codes": [
                        *[str(item) for item in raw_reasons
                          if isinstance(item, str)],
                        "project_local_measurement_partition_unavailable",
                    ],
                }
                external = True
            status = ("external_verification_required"
                      if external else "sta_scope_resolved")
            timeout = max(
                [180, *[timeouts.get(name, 0) for name in partitions]])
            observation = {
                **identity, "status": status, "rtl_fingerprint": rtl_fingerprint,
                "sta_scope_gate": gate,
                "measurement_partitions": partitions,
                "measurement_timeout_seconds": timeout,
            }
        observation["observation_id"] = hashlib.sha256(json.dumps(
            observation, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        return observation

    def normalize_external_optimization_observation(
            self, execution: Mapping[str, object], obligation_match: Mapping[str, object],
            **context: object) -> dict[str, object]:
        """Normalize a shared global run without deciding its STA verdict."""
        raw_results = context.get("results")
        results = [item for item in raw_results if isinstance(item, Mapping)] \
            if isinstance(raw_results, list) else []
        result = dict(results[-1]) if results else {}
        process_exit = context.get("process_exit")
        campaign_id = str(execution.get("campaign_id") or "")
        module = str(execution.get("module") or "")
        identity = {
            "operation_id": execution.get("operation_id"),
            "campaign_id": campaign_id, "module": module,
            "result_run": result.get("run_id") or result.get("time"),
            "obligation_id": obligation_match.get("obligation_id"),
        }
        expected = str(execution.get("expected_rtl_fingerprint") or "")
        rtl_ref = execution.get("rtl_ref") or self.rtl_file_for_module(module)
        rtl_path = (self.project_root / Path(str(rtl_ref))).resolve() if rtl_ref else None
        current = ""
        if rtl_path is not None and rtl_path.is_file():
            try:
                rtl_path.relative_to(self.project_root)
                current = hashlib.sha256(rtl_path.read_bytes()).hexdigest()
            except (ValueError, OSError):
                current = ""
        audit = context.get("closure_audit") or result.get("sta_closure_audit")
        audit = audit if isinstance(audit, Mapping) else {}
        ppa_log = result.get("ppa_log")
        ppa_path = (self.project_root / str(ppa_log)).resolve() if ppa_log else None
        ppa_text = ""
        if ppa_path is not None and ppa_path.is_file():
            try:
                ppa_path.relative_to(self.project_root)
                ppa_text = ppa_path.read_text(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                ppa_text = ""
        target_match = re.findall(
            r'(?:sta_credible_meets_10ns=|"sta_credible_meets_10ns"\s*:\s*")'
            r'(yes|no|true|false)', ppa_text, flags=re.IGNORECASE)
        timing_target_met = bool(
            target_match and target_match[-1].lower() in {"yes", "true"})
        required_scope = str(obligation_match.get("required_scope") or "")
        measured_scope = str(result.get("measurement_scope") or "")
        reported_incomplete = context.get("measurement_incomplete") is True
        if process_exit in {75, 76} or result.get("kind") == "infrastructure_uncertain":
            status = "infra_uncertain"
        else:
            status = "external_measurement_observed"
        return {
            **identity,
            "observation_id": hashlib.sha256(json.dumps(
                {**identity, "status": status}, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest(),
            "status": status,
            "rtl_fingerprint": current or None,
            "external_measurement": {
                "reported_incomplete": reported_incomplete,
                "expected_rtl_fingerprint": expected or None,
                "current_rtl_fingerprint": current or None,
                "ppa_exit": result.get("ppa_exit"),
                "measurement_scope": measured_scope,
                "required_scope": required_scope,
                "timing_target_met": timing_target_met,
                "sta_scope_gate": result.get("sta_scope_gate"),
                "sta_closure_audit": dict(audit),
                "result_kind": result.get("kind"),
            },
        }

    def normalize_optimization_observation(
            self, execution: Mapping[str, object], **context: object) -> dict[str, object]:
        """Normalize Task-02 result vocabulary into the portable Skill protocol."""
        module = str(execution.get("module") or "")
        raw_results = context.get("results")
        results = [item for item in raw_results if isinstance(item, Mapping)] \
            if isinstance(raw_results, list) else []
        result = dict(results[-1]) if results else {}
        process_exit = context.get("process_exit")
        operation = str(execution.get("operation") or "")
        verification_status = str(result.get("verification_status") or "")
        if process_exit in (75, 76) or result.get("kind") == "infrastructure_uncertain":
            status = "infra_uncertain"
        elif verification_status == "functional_failed":
            status = "functional_failed"
        elif verification_status == "functional_passed":
            status = "functional_passed"
        elif (operation == "measure_candidate"
              and context.get("measurement_incomplete") is True):
            status = "measurement_incomplete"
        elif verification_status == "measurement_executed" \
                and result.get("ppa_exit") == 0:
            measurement = context.get("measurement")
            measurement = measurement if isinstance(measurement, Mapping) else {}
            ppa_log = result.get("ppa_log")
            ppa_artifact = (self.project_root / str(ppa_log)).resolve() if ppa_log else None
            artifact_available = False
            if ppa_artifact is not None and ppa_artifact.is_file():
                try:
                    ppa_artifact.relative_to(self.project_root)
                    artifact_available = True
                except ValueError:
                    artifact_available = False
            status = "measurement_observed"
        elif verification_status == "no_change":
            status = "no_change"
        elif verification_status == "verification_failed":
            status = "verification_failed"
        elif verification_status in {
                "candidate_identity_mismatch", "verification_contract_invalid"}:
            status = "infra_uncertain"
        else:
            status = "infra_uncertain"
        identity = {
            "operation_id": execution.get("operation_id"),
            "campaign_id": execution.get("campaign_id"),
            "module": execution.get("module"),
            "status": status,
            "result_run": result.get("run_id") or result.get("time"),
        }
        rtl_ref = execution.get("rtl_ref") or self.rtl_file_for_module(
            str(execution.get("module") or ""))
        rtl_source = (self.project_root / Path(str(rtl_ref))).resolve() if rtl_ref else None
        rtl_fingerprint = None
        if rtl_source is not None and rtl_source.is_file():
            try:
                rtl_source.relative_to(self.project_root)
                rtl_fingerprint = hashlib.sha256(rtl_source.read_bytes()).hexdigest()
            except (ValueError, OSError):
                rtl_fingerprint = None
        observation: dict[str, object] = {
            **identity,
            "observation_id": hashlib.sha256(json.dumps(
                identity, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest(),
            "rtl_fingerprint": rtl_fingerprint,
            "functional_evidence": (dict(execution.get("functional_evidence"))
                                    if operation == "measure_candidate"
                                    and isinstance(execution.get("functional_evidence"), Mapping)
                                    else {
                "functional_quick_exit": result.get("functional_quick_exit"),
                "functional_medium_exit": result.get("functional_medium_exit"),
                "mandatory_full_exit": result.get("mandatory_full_exit"),
                "reused": result.get("functional_evidence_reused") is True,
                "source": result.get("functional_evidence_source"),
                "rtl_fingerprint": rtl_fingerprint,
                "verification_context_fingerprint": result.get(
                    "verification_context_fingerprint"),
            }),
            "preflight_evidence": (dict(execution.get("preflight_evidence"))
                                   if operation == "measure_candidate"
                                   and isinstance(execution.get("preflight_evidence"), Mapping)
                                   else {
                "preflight_exit": result.get("preflight_exit"),
                "reused": result.get("preflight_evidence_reused") is True,
                "source": result.get("preflight_evidence_source"),
                "rtl_fingerprint": rtl_fingerprint,
                "verification_context_fingerprint": result.get(
                    "verification_context_fingerprint"),
            }),
            "sta_scope_gate": (execution.get("sta_scope_gate")
                               or result.get("sta_scope_gate")),
            "measurement_partitions": (result.get("measurement_partitions")
                                       or execution.get("measurement_partitions", [])),
            "measurement_timeout_seconds": result.get("measurement_timeout_seconds"),
            "measurement_fingerprint": result.get("measurement_fingerprint"),
        }
        if status == "measurement_observed":
            observation["measurement"] = dict(context.get("measurement") or {})
            observation["measurement_artifact_available"] = artifact_available
            observation["ppa_exit"] = result.get("ppa_exit")
            scope_gate = execution.get("sta_scope_gate") or result.get("sta_scope_gate")
            scope_gate = scope_gate if isinstance(scope_gate, Mapping) else {}
            covered = [module]
            if scope_gate.get("scope") in {"top_level_partial", "top_level_complete"}:
                target = str(execution.get("integration_target") or "")
                if target:
                    covered.append(target)
            observation["sta_evidence"] = {
                "result_kind": result.get("kind"),
                "measurement_scope": result.get("measurement_scope"),
                "sta_scope_gate": dict(scope_gate),
                "covered_modules": sorted(set(covered)),
                "sta_closure_audit": context.get("closure_audit"),
            }
        return observation

    def normalize_optimization_plan(
            self, *, plan: Mapping[str, object],
            results: list[Mapping[str, object]], process_exit: int,
            measurement_incomplete: bool, registry: Mapping[str, object],
            minimum_value_score: float,
            closure_audit: Mapping[str, object] | None = None) -> dict[str, object]:
        """Translate one completed Task-02 process into a portable observation."""
        execution = plan.get("execution")
        execution = execution if isinstance(execution, Mapping) else {}
        module = str(execution.get("module") or "")
        if plan.get("kind") == "external":
            match = plan.get("obligation_match")
            match = match if isinstance(match, Mapping) else {}
            observation = self.normalize_external_optimization_observation(
                execution, match, results=results, process_exit=process_exit,
                measurement_incomplete=measurement_incomplete,
                closure_audit=closure_audit)
        else:
            packet = optimization_packet_from_registry(
                registry, mainline=registry_mainline(registry),
                minimum_value_score=minimum_value_score)
            candidates = packet.get("candidates")
            candidates = candidates if isinstance(candidates, list) else []
            candidate = next((item for item in candidates
                              if isinstance(item, Mapping)
                              and item.get("module") == module), {})
            return self.normalize_optimization_observation(
                execution, results=results, process_exit=process_exit,
                measurement_incomplete=measurement_incomplete,
                closure_audit=closure_audit,
                measurement=(candidate.get("value_evidence")
                             if isinstance(candidate, Mapping) else None))
        return observation
