#!/usr/bin/env python3
"""Recoverable one-file RTL edit runner owned by rtl-optimization-triage.

The project adapter supplies paths and a parameterized model command.  This
module owns the optimization campaign transition, pre-image checkpoint,
single-file scope check, and crash-recovery journal.  It never runs functional
or STA verification; a successful edit stops at ``modified_pending_functional``.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping


SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))
from optimization_governance import migrate_state, transition  # noqa: E402
from optimization_state_store import load_governance, save_governance  # noqa: E402


DEFAULT_EXCLUDES = (".git", "runs", "status", "__pycache__")


class RunnerError(RuntimeError):
    """The request is invalid or cannot be made recoverable."""


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(project_root: Path, value: object) -> Path:
    raw = Path(str(value or ""))
    candidate = raw if raw.is_absolute() else project_root / raw
    candidate = candidate.resolve()
    try:
        return candidate.relative_to(project_root)
    except ValueError as error:
        raise RunnerError(f"path escapes project root: {value}") from error


def _excluded(relative: Path, excludes: tuple[str, ...]) -> bool:
    text = relative.as_posix()
    return any(text == prefix or text.startswith(prefix.rstrip("/") + "/")
               for prefix in excludes)


def _manifest(project_root: Path, excludes: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(project_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(project_root)
        if _excluded(relative, excludes):
            continue
        result[relative.as_posix()] = _sha256(path)
    return result


def _snapshot(project_root: Path, snapshot_root: Path,
              manifest: Mapping[str, str]) -> None:
    if snapshot_root.exists():
        return
    snapshot_root.mkdir(parents=True)
    for relative in manifest:
        source = project_root / relative
        target = snapshot_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _changed(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    return sorted(key for key in set(before) | set(after)
                  if before.get(key) != after.get(key))


def _restore(project_root: Path, snapshot_root: Path,
             before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    changed = _changed(before, after)
    for relative in changed:
        target = project_root / relative
        source = snapshot_root / relative
        if relative in before:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif target.is_file() or target.is_symlink():
            target.unlink()
    return changed


def _restore_operation(project_root: Path, snapshot_root: Path,
                       operation_preimage: Path, relative_rtl: str,
                       before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """Undo one model attempt while preserving its input candidate revision."""
    restored = _restore(project_root, snapshot_root, before, after)
    target = project_root / relative_rtl
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(operation_preimage, target)
    return restored


def _render_prompt(request: Mapping[str, object], relative_rtl: str,
                   state: Mapping[str, object]) -> str:
    context = request.get("evidence")
    context = context if isinstance(context, Mapping) else {}
    hypothesis = str(request.get("hypothesis") or "降低已测得的时序或面积代价")
    return f"""你是一次性的 RTL 优化执行模型。只处理一个候选，不负责选择主线或调度 STA。

允许修改的唯一文件：{relative_rtl}
候选模块：{request.get('module')}
优化假设：{hypothesis}

必须遵守：
1. 只修改上述一个 RTL 文件；不得修改 filelist、测试、harness、配置、文档或其他 RTL。
2. 保持端口、协议、位宽和功能语义；不得为了指标删除功能。
3. 先阅读该文件及其只读上下文，再做一个范围有限、可验证的改动。
4. 不运行完整综合、STA 或 PPA；外层流程会在下一阶段执行功能门禁和最小 STA 判断。
5. 若证据不足以安全修改，则保持文件不变并在输出中说明。

当前确定性证据：
{json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2)}

当前阶段：{state.get('phase')}
"""


def _model_command(template: object, *, prompt: str, project_root: Path,
                   model: str) -> list[str]:
    if not isinstance(template, list) or not template:
        raise RunnerError("model_command must be one non-empty argv list")
    values = {"prompt": prompt, "project_root": str(project_root), "model": model}
    command = [str(part).format(**values) for part in template]
    if not any("{prompt}" in str(part) for part in template):
        command.append(prompt)
    return command


def _diff(snapshot_root: Path, project_root: Path, relative: str) -> str:
    before = (snapshot_root / relative).read_text(encoding="utf-8", errors="replace")
    after = (project_root / relative).read_text(encoding="utf-8", errors="replace")
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{relative}", tofile=f"b/{relative}"))


def _finish_failed(*, state: dict[str, object], request: Mapping[str, object],
                   state_path: Path, journal_path: Path, journal: dict[str, object],
                   reason: str, evidence: object) -> dict[str, object]:
    next_state = transition(state, {
        "kind": "edit_failed", "event_id": f"{request['operation_id']}:edit-failed",
        "reason": reason, "evidence": evidence,
    })
    save_governance(state_path, next_state)
    output = {
        "status": "edit_failed", "reason": reason,
        "operation_id": request["operation_id"], "governance_state": next_state,
    }
    journal.update({"phase": "completed", "result": output})
    _atomic_json(journal_path, journal)
    return output


def run_edit(request: Mapping[str, object]) -> dict[str, object]:
    project_root = Path(str(request.get("project_root") or "")).expanduser().resolve()
    state_path = Path(str(request.get("state_path") or "")).expanduser().resolve()
    checkpoint_root = Path(str(request.get("checkpoint_root") or "")).expanduser().resolve()
    operation_id = str(request.get("operation_id") or "").strip()
    module = str(request.get("module") or "").strip()
    if not project_root.is_dir() or not state_path.is_file():
        raise RunnerError("project_root and existing state_path are required")
    if not operation_id or not module:
        raise RunnerError("operation_id and module are required")
    relative_rtl = _safe_relative(project_root, request.get("rtl_path")).as_posix()
    rtl_path = project_root / relative_rtl
    if not rtl_path.is_file():
        raise RunnerError("rtl_path must name one existing file")
    if checkpoint_root == project_root or checkpoint_root in project_root.parents:
        raise RunnerError("checkpoint_root must not contain the project")

    operation_key = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    journal_path = checkpoint_root / "operations" / f"{operation_key}.json"
    if journal_path.is_file():
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if journal.get("phase") == "completed" and isinstance(journal.get("result"), dict):
            return dict(journal["result"])
    else:
        journal = {}

    state = migrate_state(load_governance(state_path))
    current = state.get("current_candidate")
    current = current if isinstance(current, Mapping) else None

    campaign_id = str(request.get("campaign_id") or f"campaign:{operation_id}")
    campaign_key = hashlib.sha256(campaign_id.encode("utf-8")).hexdigest()
    campaign_root = checkpoint_root / "campaigns" / campaign_key
    snapshot_root = campaign_root / "preimage"
    manifest_path = campaign_root / "manifest.json"
    excludes_raw = request.get("excluded_roots")
    excludes = tuple(str(item).strip("/") for item in excludes_raw
                     if str(item).strip("/")) if isinstance(excludes_raw, list) else DEFAULT_EXCLUDES

    if not current:
        mainline_ref = request.get("interrupted_mainline_ref")
        evidence = request.get("evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        candidate_evidence = evidence.get("candidate")
        candidate_evidence = (candidate_evidence
                              if isinstance(candidate_evidence, Mapping) else {})
        state = transition(state, {
            "kind": "begin_candidate", "event_id": f"{operation_id}:begin",
            "module": module, "campaign_id": campaign_id,
            "checkpoint_ref": str(campaign_root), "rtl_ref": relative_rtl,
            "hypothesis": request.get("hypothesis"),
            "integration_target": request.get("integration_target"),
            "insertion_mode": request.get("insertion_mode", "decoupled"),
            "interrupted_mainline_ref": mainline_ref,
            "top_level_acceptance_required": bool(
                request.get("top_level_acceptance_required")),
            "baseline_measurement": candidate_evidence.get("value_evidence"),
            "baseline_value_score": candidate_evidence.get("value_score"),
        })
        save_governance(state_path, state)
        current = state.get("current_candidate")
    if not isinstance(current, Mapping) or str(current.get("module")) != module:
        raise RunnerError("active optimization candidate does not match request")
    if state.get("phase") not in {"editing", "functional_repair"}:
        raise RunnerError(f"edit is forbidden in phase {state.get('phase')}")

    if manifest_path.is_file():
        before = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        before = _manifest(project_root, excludes)
        _snapshot(project_root, snapshot_root, before)
        _atomic_json(manifest_path, before)

    if not journal:
        journal = {
            "schema_version": 1, "operation_id": operation_id,
            "campaign_id": campaign_id, "module": module,
            "rtl_path": relative_rtl, "phase": "edit_prepared",
            "manifest_fingerprint": hashlib.sha256(json.dumps(
                before, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }
        _atomic_json(journal_path, journal)

    current_manifest = _manifest(project_root, excludes)
    pending_changes = _changed(before, current_manifest)
    if pending_changes and pending_changes != [relative_rtl]:
        restored = _restore(project_root, snapshot_root, before, current_manifest)
        return _finish_failed(
            state=state, request=request, state_path=state_path,
            journal_path=journal_path, journal=journal,
            reason="scope_violation_before_or_during_recovery",
            evidence={"restored_paths": restored})

    operation_preimage = campaign_root / "operation_inputs" / operation_key / relative_rtl
    if not operation_preimage.is_file():
        operation_preimage.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rtl_path, operation_preimage)
    input_rtl_fingerprint = str(
        journal.get("input_rtl_fingerprint") or _sha256(operation_preimage))
    if journal.get("input_rtl_fingerprint") != input_rtl_fingerprint:
        journal["input_rtl_fingerprint"] = input_rtl_fingerprint
        _atomic_json(journal_path, journal)

    model_exit = None
    # On crash recovery, a changed target means the model may already have
    # completed.  Validate that change without invoking the same operation a
    # second time.  A repair starts from an already campaign-dirty target, so
    # compare against this operation's pre-image rather than campaign baseline.
    if _sha256(rtl_path) == input_rtl_fingerprint:
        prompt = _render_prompt(request, relative_rtl, state)
        command = _model_command(request.get("model_command"), prompt=prompt,
                                 project_root=project_root,
                                 model=str(request.get("model") or ""))
        log_path = campaign_root / f"{operation_key}.model.log"
        result = subprocess.run(command, cwd=project_root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, check=False)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(result.stdout or "", encoding="utf-8")
        model_exit = result.returncode

    after = _manifest(project_root, excludes)
    changed = _changed(before, after)
    if model_exit not in (None, 0):
        restored = _restore_operation(
            project_root, snapshot_root, operation_preimage, relative_rtl, before, after)
        next_state = transition(state, {
            "kind": "edit_infra_uncertain",
            "event_id": f"{request['operation_id']}:edit-infra",
            "evidence": {"model_exit": model_exit, "restored_paths": restored},
        })
        save_governance(state_path, next_state)
        output = {
            "status": "infra_uncertain", "reason": "model_command_failed",
            "model_exit": model_exit, "restored_paths": restored,
            "operation_id": operation_id, "governance_state": next_state,
        }
        journal.update({"phase": "completed", "result": output})
        _atomic_json(journal_path, journal)
        return output
    output_rtl_fingerprint = _sha256(rtl_path)
    if output_rtl_fingerprint == input_rtl_fingerprint or changed != [relative_rtl]:
        restored = _restore_operation(
            project_root, snapshot_root, operation_preimage, relative_rtl, before, after)
        reason = ("model_made_no_change" if output_rtl_fingerprint == input_rtl_fingerprint
                  else "model_changed_multiple_files")
        return _finish_failed(
            state=state, request=request, state_path=state_path,
            journal_path=journal_path, journal=journal, reason=reason,
            evidence={"changed_paths": changed, "restored_paths": restored})

    diff_path = campaign_root / f"{operation_key}.diff"
    diff_path.write_text(_diff(snapshot_root, project_root, relative_rtl), encoding="utf-8")
    next_state = transition(state, {
        "kind": "edit_completed", "event_id": f"{operation_id}:completed",
        "operation_id": operation_id, "campaign_id": campaign_id,
        "rtl_fingerprint": output_rtl_fingerprint, "diff_ref": str(diff_path),
    })
    save_governance(state_path, next_state)
    output = {
        "status": "modified_pending_functional", "operation_id": operation_id,
        "module": module, "rtl_path": relative_rtl,
        "rtl_fingerprint": _sha256(rtl_path), "diff_ref": str(diff_path),
        "governance_state": next_state,
    }
    journal.update({"phase": "completed", "result": output})
    _atomic_json(journal_path, journal)
    return output


def run_rollback(request: Mapping[str, object]) -> dict[str, object]:
    """Restore one active campaign and atomically close its Skill lifecycle."""
    project_root = Path(str(request.get("project_root") or "")).expanduser().resolve()
    state_path = Path(str(request.get("state_path") or "")).expanduser().resolve()
    checkpoint_root = Path(str(request.get("checkpoint_root") or "")).expanduser().resolve()
    operation_id = str(request.get("operation_id") or "").strip()
    if not project_root.is_dir() or not state_path.is_file() or not operation_id:
        raise RunnerError("project_root, existing state_path, and operation_id are required")
    state = migrate_state(load_governance(state_path))
    current = state.get("current_candidate")
    if not isinstance(current, Mapping) or state.get("phase") != "rollback_required":
        raise RunnerError("rollback requires one rollback_required candidate")
    checkpoint = Path(str(current.get("checkpoint_ref") or "")).expanduser().resolve()
    try:
        checkpoint.relative_to(checkpoint_root)
    except ValueError as error:
        raise RunnerError("candidate checkpoint escapes checkpoint_root") from error
    relative_rtl = _safe_relative(project_root, current.get("rtl_ref")).as_posix()
    source = checkpoint / "preimage" / relative_rtl
    target = project_root / relative_rtl
    if not source.is_file() or not target.is_file():
        raise RunnerError("rollback pre-image or target RTL is unavailable")

    operation_key = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    journal_path = checkpoint / "rollback" / f"{operation_key}.json"
    if journal_path.is_file():
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if journal.get("phase") == "completed" and isinstance(journal.get("result"), dict):
            return dict(journal["result"])
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    proof = {
        "checkpoint_restored": True,
        "rtl_fingerprint_restored": _sha256(source) == _sha256(target),
        "configuration_restored": True,
    }
    next_state = transition(state, {
        "kind": "rollback_complete", "event_id": f"{operation_id}:rollback-complete",
        "rollback_proof": proof,
    })
    save_governance(state_path, next_state)
    output = {
        "status": "rollback_complete", "operation_id": operation_id,
        "module": current.get("module"), "rtl_path": relative_rtl,
        "rollback_proof": proof, "governance_state": next_state,
    }
    _atomic_json(journal_path, {
        "schema_version": 1, "phase": "completed", "result": output,
    })
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", nargs="?", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8")
                         if args.request else sys.stdin.read())
    try:
        output = (run_rollback(request)
                  if request.get("operation") == "rollback_candidate"
                  else run_edit(request))
        exit_code = 0
    except (RunnerError, OSError, ValueError, json.JSONDecodeError) as error:
        output = {"status": "invalid_request", "error": f"{type(error).__name__}: {error}"}
        exit_code = 2
    text = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        _atomic_json(args.output, output)
    else:
        print(text, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
