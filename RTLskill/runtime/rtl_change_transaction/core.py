#!/usr/bin/env python3
"""Policy-free, recoverable file transactions for RTL change workflows."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path


class TransactionError(ValueError):
    """The requested transaction violates identity or workspace invariants."""


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(dict(value), handle, ensure_ascii=False, indent=2,
                      sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TransactionError(f"unreadable transaction object: {path}: {error}") from error
    if not isinstance(value, dict):
        raise TransactionError(f"transaction object must be a JSON object: {path}")
    return value


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_relative(raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise TransactionError(f"path must be project-relative without '..': {raw}")
    normalized = Path(*[part for part in path.parts if part not in {"", "."}])
    if not normalized.parts:
        raise TransactionError(f"empty managed path: {raw}")
    return normalized


def _normalized_paths(values: Sequence[str], label: str) -> list[str]:
    if not values:
        raise TransactionError(f"{label} must not be empty")
    result = sorted({_safe_relative(value).as_posix() for value in values})
    if len(result) != len(values):
        raise TransactionError(f"{label} contains duplicates")
    return result


def _resolve_under(root: Path, relative: str) -> Path:
    root = root.resolve()
    result = (root / _safe_relative(relative)).resolve()
    try:
        result.relative_to(root)
    except ValueError as error:
        raise TransactionError(f"path escapes project root: {relative}") from error
    return result


def _entry(project_root: Path, relative: str) -> dict[str, object]:
    path = _resolve_under(project_root, relative)
    if not path.exists():
        return {"path": relative, "exists": False, "sha256": None, "size": 0}
    if not path.is_file() or path.is_symlink():
        raise TransactionError(f"managed path must be a regular non-symlink file: {relative}")
    content = path.read_bytes()
    return {
        "path": relative,
        "exists": True,
        "sha256": _sha256_bytes(content),
        "size": len(content),
        "mode": path.stat().st_mode & 0o777,
    }


def _manifest(project_root: Path, managed_paths: Sequence[str]) -> dict[str, object]:
    entries = [_entry(project_root, relative) for relative in managed_paths]
    fingerprint = _sha256_bytes(json.dumps(
        entries, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8"))
    return {"version": 1, "entries": entries, "fingerprint": fingerprint}


def acquire_lease(lease_path: Path, *, owner: str) -> dict[str, object]:
    """Acquire an exclusive project mutation lease without stale takeover."""
    lease_path = lease_path.resolve()
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    value: dict[str, object] = {
        "version": 1,
        "owner": owner,
        "token": token,
        "pid": os.getpid(),
        "created_unix": time.time(),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(lease_path, flags, 0o600)
    except FileExistsError as error:
        try:
            existing = _read_object(lease_path)
        except TransactionError:
            existing = {"status": "unreadable"}
        raise TransactionError(f"workspace lease already exists: {existing}") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return value


def release_lease(lease_path: Path, *, token: str) -> None:
    value = _read_object(lease_path)
    if value.get("token") != token:
        raise TransactionError("workspace lease token mismatch")
    lease_path.unlink()


def begin_transaction(
        *, project_root: Path, transaction_root: Path, operation_id: str,
        managed_paths: Sequence[str], allowed_paths: Sequence[str],
        max_changed_files: int = 3) -> dict[str, object]:
    """Capture an immutable preimage for one exact operation."""
    project_root = project_root.resolve()
    transaction_root = transaction_root.resolve()
    managed = _normalized_paths(managed_paths, "managed_paths")
    allowed = _normalized_paths(allowed_paths, "allowed_paths")
    if not set(allowed).issubset(managed):
        raise TransactionError("allowed_paths must be a subset of managed_paths")
    if max_changed_files < 1 or len(allowed) > max_changed_files:
        raise TransactionError("allowed_paths exceeds max_changed_files")
    identity = {
        "operation_id": operation_id,
        "project_root": str(project_root),
        "managed_paths": managed,
        "allowed_paths": allowed,
        "max_changed_files": max_changed_files,
    }
    request_fingerprint = _sha256_bytes(json.dumps(
        identity, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    state_path = transaction_root / "transaction.json"
    if state_path.exists():
        current = _read_object(state_path)
        if current.get("request_fingerprint") != request_fingerprint:
            raise TransactionError("operation_id already exists with another request")
        return current
    if transaction_root.exists() and any(transaction_root.iterdir()):
        raise TransactionError("transaction root is nonempty without transaction state")
    transaction_root.mkdir(parents=True, exist_ok=True)
    before = _manifest(project_root, managed)
    preimage = transaction_root / "preimage"
    for entry in before["entries"]:
        if not isinstance(entry, Mapping) or not entry.get("exists"):
            continue
        relative = str(entry["path"])
        source = _resolve_under(project_root, relative)
        destination = _resolve_under(preimage, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    value: dict[str, object] = {
        "version": 1,
        **identity,
        "request_fingerprint": request_fingerprint,
        "phase": "BASELINE_CAPTURED",
        "before_manifest": before,
        "changed_paths": [],
        "created_unix": time.time(),
        "journal": [{"event": "baseline_captured", "unix": time.time()}],
    }
    _atomic_json(state_path, value)
    return value


def inspect_transaction(transaction_root: Path) -> dict[str, object]:
    transaction_root = transaction_root.resolve()
    state_path = transaction_root / "transaction.json"
    state = _read_object(state_path)
    project_root = Path(str(state["project_root"]))
    managed = [str(item) for item in state.get("managed_paths", [])]
    allowed = set(str(item) for item in state.get("allowed_paths", []))
    before_entries = {
        str(entry["path"]): entry for entry in
        state.get("before_manifest", {}).get("entries", [])
        if isinstance(entry, Mapping) and entry.get("path")
    }
    after = _manifest(project_root, managed)
    after_entries = {
        str(entry["path"]): entry for entry in after["entries"]
        if isinstance(entry, Mapping) and entry.get("path")
    }
    changed = sorted(path for path in managed
                     if before_entries.get(path) != after_entries.get(path))
    unauthorized = sorted(set(changed) - allowed)
    max_changed = int(state.get("max_changed_files", 0))
    diff_parts: list[str] = []
    preimage = transaction_root / "preimage"
    for relative in changed:
        before_path = _resolve_under(preimage, relative)
        after_path = _resolve_under(project_root, relative)
        before_lines = (before_path.read_text(
            encoding="utf-8", errors="replace").splitlines(keepends=True)
            if before_path.is_file() else [])
        after_lines = (after_path.read_text(
            encoding="utf-8", errors="replace").splitlines(keepends=True)
            if after_path.is_file() else [])
        diff_parts.extend(difflib.unified_diff(
            before_lines, after_lines,
            fromfile=f"baseline/{relative}", tofile=f"current/{relative}"))
    result = {
        **state,
        "after_manifest": after,
        "changed_paths": changed,
        "unauthorized_paths": unauthorized,
        "within_contract": not unauthorized and len(changed) <= max_changed,
        "diff": "".join(diff_parts),
    }
    return result


def _append_event(state: dict[str, object], event: str, **facts: object) -> None:
    journal = list(state.get("journal", []))
    journal.append({"event": event, "unix": time.time(), **facts})
    state["journal"] = journal


def rollback_transaction(transaction_root: Path) -> dict[str, object]:
    """Restore all managed paths and prove the original manifest fingerprint."""
    transaction_root = transaction_root.resolve()
    state_path = transaction_root / "transaction.json"
    state = _read_object(state_path)
    project_root = Path(str(state["project_root"]))
    preimage = transaction_root / "preimage"
    before = state.get("before_manifest")
    if not isinstance(before, Mapping):
        raise TransactionError("before manifest is missing")
    for entry in before.get("entries", []):
        if not isinstance(entry, Mapping):
            raise TransactionError("invalid before manifest entry")
        relative = str(entry.get("path") or "")
        target = _resolve_under(project_root, relative)
        if entry.get("exists"):
            source = _resolve_under(preimage, relative)
            if not source.is_file():
                raise TransactionError(f"preimage missing: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif target.exists():
            if not target.is_file() or target.is_symlink():
                raise TransactionError(f"refuse to remove non-file path: {relative}")
            target.unlink()
    restored = _manifest(project_root, [str(item) for item in state["managed_paths"]])
    proof = restored.get("fingerprint") == before.get("fingerprint")
    if not proof:
        raise TransactionError("rollback manifest does not match baseline")
    state["phase"] = "ROLLED_BACK"
    state["rollback_proof"] = {
        "verified": True,
        "fingerprint": restored.get("fingerprint"),
    }
    _append_event(state, "rollback_complete", verified=True)
    _atomic_json(state_path, state)
    return state


def commit_transaction(transaction_root: Path) -> dict[str, object]:
    transaction_root = transaction_root.resolve()
    state_path = transaction_root / "transaction.json"
    inspected = inspect_transaction(transaction_root)
    if not inspected.get("within_contract"):
        raise TransactionError("cannot commit changes outside the contract")
    if not inspected.get("changed_paths"):
        raise TransactionError("cannot commit a transaction with no changes")
    state = _read_object(state_path)
    state["phase"] = "COMMITTED"
    state["changed_paths"] = inspected["changed_paths"]
    state["after_manifest"] = inspected["after_manifest"]
    _append_event(state, "commit_complete",
                  changed_paths=inspected["changed_paths"])
    _atomic_json(state_path, state)
    return state


def run_argv(*, argv: Sequence[str], cwd: Path, log_path: Path,
             timeout_seconds: int) -> dict[str, object]:
    """Run exact argv with bounded time and terminate its process group on timeout."""
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise TransactionError("argv must be a nonempty sequence of strings")
    if timeout_seconds < 1:
        raise TransactionError("timeout_seconds must be positive")
    cwd = cwd.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write("argv=" + json.dumps(list(argv), ensure_ascii=False) + "\n")
        handle.flush()
        process = subprocess.Popen(
            list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                exit_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                exit_code = process.wait()
    return {
        "argv": list(argv),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 6),
        "log_path": str(log_path),
    }
