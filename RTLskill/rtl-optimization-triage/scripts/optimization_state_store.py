#!/usr/bin/env python3
"""Canonical persistence for rtl-optimization-triage durable state.

The governance file contains exactly one bare Optimization state object.  The
optional observation file is a one-way project-to-Skill message and is removed
only after the corresponding state transition has been persisted.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path


class OptimizationStateStoreError(ValueError):
    """A durable Optimization object is unreadable or has the wrong shape."""


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OptimizationStateStoreError(
            f"Optimization JSON is unreadable: {path}: {error}") from error
    if not isinstance(value, dict):
        raise OptimizationStateStoreError(
            f"Optimization JSON must contain one object: {path}")
    return value


def _atomic_write(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(dict(value), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_governance(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    value = _read_object(path)
    if "governance" in value or "pending_observation" in value:
        raise OptimizationStateStoreError(
            "legacy controller envelope is unsupported; governance must be the bare object")
    return value


def save_governance(path: Path, value: Mapping[str, object]) -> None:
    _atomic_write(path, value)


def load_observation(path: Path) -> dict[str, object] | None:
    return _read_object(path) if path.is_file() else None


def clear_observation(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
