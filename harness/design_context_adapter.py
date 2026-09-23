#!/usr/bin/env python3
"""Deterministically build and slice current RTL design context.

This is a project adapter, not registry policy.  It parses the current
filelist/RTL into a conservative structural graph, caches that graph by source
fingerprint, and returns bounded module neighborhoods to deterministic Skills.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


SCHEMA_VERSION = 1
DEFAULT_GRAPH = Path("status/task02_design_context.json")
RTL_SUFFIXES = {".v", ".sv"}


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _aggregate(paths: Iterable[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        if not path.is_file():
            continue
        try:
            label = path.relative_to(root).as_posix()
        except ValueError:
            label = path.as_posix()
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def filelist_paths(project_root: Path) -> tuple[Path, list[Path]]:
    filelist = project_root / "rtl/filelist.f"
    paths: list[Path] = []
    for raw in filelist.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("+", "-")):
            continue
        path = Path(line)
        paths.append(path if path.is_absolute() else project_root / path)
    return filelist, paths


def source_fingerprints(project_root: Path) -> dict[str, str]:
    filelist, rtl_paths = filelist_paths(project_root)
    config = project_root / "harness/ppa.toml"
    return {
        "config_fingerprint": _sha_file(config),
        "filelist_fingerprint": _aggregate([filelist, *rtl_paths], project_root),
        "rtl_fingerprint": _aggregate(rtl_paths, project_root),
    }


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", text)


def _module_blocks(project_root: Path, rtl_paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    pattern = re.compile(
        r"\bmodule\s+(?:automatic\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\b"
        r"(?P<header>.*?);(?P<body>.*?)\bendmodule\b",
        re.DOTALL,
    )
    modules: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for path in rtl_paths:
        if not path.is_file() or path.suffix.lower() not in RTL_SUFFIXES:
            continue
        source = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for match in pattern.finditer(source):
            name = match.group("name")
            try:
                label = path.relative_to(project_root).as_posix()
            except ValueError:
                label = path.as_posix()
            if name in modules:
                duplicates.add(name)
                continue
            modules[name] = {
                "module_id": name,
                "source_files": [label],
                "header": match.group("header"),
                "body": match.group("body"),
            }
    for name in duplicates:
        modules[name]["duplicate_declaration"] = True
    return modules


def _instance_names(body: str, known_modules: Iterable[str], self_name: str) -> list[str]:
    found: list[str] = []
    for child in known_modules:
        if child == self_name:
            continue
        pattern = re.compile(
            rf"\b{re.escape(child)}\b\s*(?:#\s*\(.*?\)\s*)?"
            r"[A-Za-z_][A-Za-z0-9_$]*\s*\(",
            re.DOTALL,
        )
        if pattern.search(body):
            found.append(child)
    return sorted(found)


def _timing_context(header: str, body: str) -> dict[str, Any]:
    edges = re.findall(r"\b(?:posedge|negedge)\s+([A-Za-z_][A-Za-z0-9_$]*)", body)
    clocks = sorted({name for name in edges if not re.search(r"rst|reset", name, re.I)})
    resets = sorted({name for name in edges if re.search(r"rst|reset", name, re.I)})
    has_sequential = bool(re.search(r"\balways_ff\b|\b(?:posedge|negedge)\b", body))
    has_combinational = bool(re.search(r"\balways_comb\b|\balways\s*@\s*\*|\bassign\b", body))
    return {
        "clock_domains": clocks,
        "reset_domains": resets,
        "has_sequential_logic": has_sequential,
        "has_combinational_logic": has_combinational,
        # Exact port-to-register proof requires an elaborated timing graph.
        # Keep it explicitly unknown rather than deriving safety from syntax.
        "registered_inputs": "unknown",
        "registered_outputs": "unknown",
        "parameterized": bool(re.search(r"\bparameter\b", header)),
        "unknowns": ["registered_input_boundary", "registered_output_boundary"],
    }


def build_design_context(project_root: Path, fingerprints: Mapping[str, str],
                         previous_generation: int = 0) -> dict[str, Any]:
    _, rtl_paths = filelist_paths(project_root)
    raw = _module_blocks(project_root, rtl_paths)
    names = sorted(raw)
    children = {
        name: _instance_names(str(item["body"]), names, name)
        for name, item in raw.items()
    }
    parents: dict[str, list[str]] = {name: [] for name in names}
    for parent, child_names in children.items():
        for child in child_names:
            parents[child].append(parent)
    modules: dict[str, Any] = {}
    for name in names:
        item = raw[name]
        header = str(item["header"])
        body = str(item["body"])
        packages = sorted(set(
            re.findall(r"\bimport\s+([A-Za-z_][A-Za-z0-9_$]*)::", header + "\n" + body)
            + re.findall(r"\b([A-Za-z_][A-Za-z0-9_$]*)::", header + "\n" + body)
        ))
        relationships = {
            "instantiates": children[name],
            "instantiated_by": sorted(parents[name]),
            "direct_consumers": sorted(parents[name]),
            "package_dependencies": packages,
        }
        timing = _timing_context(header, body)
        modules[name] = {
            "module_id": name,
            "source_files": list(item["source_files"]),
            "ports_signature": _sha_bytes(" ".join(header.split()).encode("utf-8")),
            "relationships": relationships,
            "timing_context": timing,
            "semantic_intent": {
                "summary": None,
                "source": "unavailable",
                "confidence": "none",
            },
            "duplicate_declaration": item.get("duplicate_declaration") is True,
        }
    graph_payload = {
        "source_fingerprints": dict(fingerprints),
        "modules": modules,
    }
    graph_fingerprint = _sha_bytes(json.dumps(
        graph_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8"))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "current",
        "hard_gate": "pass",
        "source": "project_design_context_adapter",
        "generation": max(1, previous_generation + 1),
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_fingerprints": dict(fingerprints),
        "graph_fingerprint": graph_fingerprint,
        "coverage": {"module_count": len(modules), "complete_module_scan": True},
        "modules": modules,
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def ensure_design_context(project_root: Path,
                          artifact_path: Optional[Path] = None) -> dict[str, Any]:
    artifact = artifact_path or project_root / DEFAULT_GRAPH
    fingerprints = source_fingerprints(project_root)
    previous: dict[str, Any] = {}
    try:
        loaded = json.loads(artifact.read_text(encoding="utf-8"))
        previous = loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        previous = {}
    if (previous.get("status") == "current"
            and previous.get("source_fingerprints") == fingerprints
            and isinstance(previous.get("graph_fingerprint"), str)):
        return previous
    value = build_design_context(
        project_root, fingerprints, int(previous.get("generation", 0) or 0))
    value["graph_ref"] = artifact.relative_to(project_root).as_posix()
    _atomic_json(artifact, value)
    return value


def design_context_slice(project_root: Path, *, rtl_paths: Iterable[str] = (),
                         module_hints: Iterable[str] = (),
                         artifact_path: Optional[Path] = None) -> dict[str, Any]:
    graph = ensure_design_context(project_root, artifact_path)
    wanted = {str(item) for item in module_hints if str(item)}
    normalized_paths = {Path(item).as_posix() for item in rtl_paths if str(item)}
    modules = graph.get("modules") if isinstance(graph.get("modules"), dict) else {}
    for module, raw in modules.items():
        item = raw if isinstance(raw, dict) else {}
        sources = {Path(str(path)).as_posix() for path in item.get("source_files", [])}
        if normalized_paths & sources:
            wanted.add(str(module))
    selected = {
        module: {
            "source_files": list((modules[module] or {}).get("source_files", [])),
            "relationships": dict((modules[module] or {}).get("relationships", {})),
            "timing_context": dict((modules[module] or {}).get("timing_context", {})),
            "semantic_intent": dict((modules[module] or {}).get("semantic_intent", {})),
        }
        for module in sorted(wanted) if isinstance(modules.get(module), dict)
    }
    missing = sorted(wanted - set(selected))
    return {
        "status": "current" if not missing else "conflict",
        "hard_gate": "pass" if not missing else "hold",
        "graph_fingerprint": graph.get("graph_fingerprint"),
        "graph_ref": graph.get("graph_ref"),
        "generation": graph.get("generation"),
        "source_fingerprints": dict(graph.get("source_fingerprints", {})),
        "module_contexts": selected,
        "missing_modules": missing,
        "reason_codes": (["design_context_module_missing"] if missing else []),
    }
