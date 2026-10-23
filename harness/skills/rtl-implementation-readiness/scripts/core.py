"""Input closure and immutable experiment variants. Python standard library only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from datetime import datetime, timezone


class ReadinessError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    try:
        with Path(path).open(encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, ValueError) as e:
        raise ReadinessError(f"Cannot read {path}: {e}") from e
    if not isinstance(value, dict):
        raise ReadinessError(f"Expected JSON object: {path}")
    return value


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def safe_relative(raw):
    raw = str(raw)
    p = Path(raw)
    if not raw or p.is_absolute() or ".." in p.parts or "\\" in raw or "\x00" in raw:
        raise ReadinessError(f"Unsafe relative path: {raw!r}")
    return p.as_posix()


def under(root, raw):
    root = Path(root).resolve()
    path = root / safe_relative(raw)
    if not path.resolve().is_relative_to(root):
        raise ReadinessError(f"Path escapes tree: {raw}")
    # Symlink targets can change after validation; snapshots contain regular files only.
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink():
            raise ReadinessError(f"Symlink not allowed in snapshot: {item}")
    return path


def label(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name) or name in (".", ".."):
        raise ReadinessError(f"Invalid name: {name!r}")
    return name


def file_manifest(tree, paths=None):
    tree = Path(tree)
    paths = paths if paths is not None else sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file() or p.is_symlink())
    result = {}
    for rel in paths:
        p = under(tree, rel)
        if not p.is_file():
            raise ReadinessError(f"Missing input: {p}")
        result[rel] = {"sha256": sha(p), "size": p.stat().st_size}
    return result


def context_identity(ws):
    return digest({k: ws[k] for k in ("api_version", "top", "sources", "include_dirs", "allowed_paths", "files")})


def variant_identity(ws, files):
    return digest({"workspace_id": ws["workspace_id"], "files": files})


def load_workspace(workspace):
    ws = read_json(Path(workspace) / "workspace.json")
    if (ws.get("api_version") != 2 or context_identity(ws) != ws.get("workspace_id")
            or ws.get("baseline_identity") != variant_identity(ws, ws.get("files"))):
        raise ReadinessError("Workspace context changed or unsupported workspace version")
    return ws


def variant_tree(workspace, name):
    return Path(workspace) / "variants" / label(name) / "tree"


def load_variant(workspace, name, verify=True):
    ws = load_workspace(workspace)
    obj = read_json(Path(workspace) / "variants" / label(name) / "variant.json")
    if obj.get("name") != name or obj.get("identity") != variant_identity(ws, obj.get("files")):
        raise ReadinessError(f"Invalid variant identity: {name}")
    if obj.get("based_on") != ws["baseline_identity"]:
        raise ReadinessError(f"Variant belongs to another baseline: {name}")
    if set(obj["files"]) != set(ws["files"]):
        raise ReadinessError(f"Variant input closure changed: {name}")
    changes = sorted(rel for rel in obj["files"] if obj["files"][rel] != ws["files"][rel])
    if (obj.get("changes") != changes or not set(changes).issubset(ws["allowed_paths"])
            or (name == "baseline" and (changes or obj["identity"] != ws["baseline_identity"]))):
        raise ReadinessError(f"Variant changes do not match the frozen baseline/edit scope: {name}")
    if verify and file_manifest(variant_tree(workspace, name)) != obj["files"]:
        raise ReadinessError(f"Sealed variant was modified: {name}")
    return obj


def _strip_comments(text):
    return re.sub(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*',
                  lambda m: m.group() if m.group().startswith('"') else "\n" * m.group().count("\n"), text, flags=re.S)


def _dependencies(root, sources, includes):
    closure, rtl_paths, todo = set(sources), set(sources), list(sources)
    # Conservative lexical closure includes inactive branches too; callers must supply
    # generated inputs and resolve ambiguous/macro dependencies before execution.
    while todo:
        rel = todo.pop()
        path = under(root, rel)
        if not path.is_file():
            raise ReadinessError(f"Missing RTL input: {rel}")
        text = _strip_comments(path.read_text(encoding="utf-8"))
        for line in re.findall(r"^\s*`include\s+([^\r\n]+)", text, re.M):
            m = re.match(r'"([^"\n]+)"', line.strip())
            if not m:
                raise ReadinessError(f"Nonliteral include in {rel}; resolve its synthesis closure explicitly")
            inc = m.group(1)
            if Path(inc).is_absolute() or "\\" in inc:
                raise ReadinessError(f"Include must stay within the project: {inc}")
            candidates = [Path(rel).parent / inc, Path(inc), *(Path(d) / inc for d in includes)]
            normalized = []
            for p in candidates:
                try:
                    normalized.append(Path(os.path.normpath(root / p)).relative_to(root).as_posix())
                except ValueError:
                    continue
            found = next((p for p in normalized if under(root, p).is_file()), None)
            if found is None:
                raise ReadinessError(f"Unresolved include {inc} in {rel}")
            rtl_paths.add(found)
            if found not in closure:
                closure.add(found)
                todo.append(found)
        for token in re.finditer(r'"(?:\\.|[^"\\])*"|\$readmem[hb]\s*\(\s*([^,\n]+)', text):
            call = token.group(1)
            if call is None:
                continue
            m = re.fullmatch(r'"([^"\n]+)"\s*', call)
            if not m:
                raise ReadinessError(f"Dynamic readmem filename in {rel}; use a dedicated explicit probe scope")
            mem = safe_relative(m.group(1))
            if not under(root, mem).is_file():
                raise ReadinessError(f"Missing readmem data (relative to project root): {mem}")
            closure.add(mem)
    return closure, rtl_paths


def init_workspace(project_root, workspace, sources, top, include_dirs=(), assets=(), allowed_paths=None):
    root, workspace = Path(project_root).resolve(), Path(workspace).resolve()
    if workspace.exists():
        raise ReadinessError(f"Workspace already exists (use its status, do not initialize again): {workspace}")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top):
        raise ReadinessError("Top must be an explicit simple Verilog module name")
    sources = list(dict.fromkeys(safe_relative(x) for x in sources))
    includes = list(dict.fromkeys(safe_relative(x) for x in include_dirs))
    if not sources:
        raise ReadinessError("Provide the ordered synthesis source list; discovery is not a build manifest")
    for d in includes:
        if not under(root, d).is_dir():
            raise ReadinessError(f"Missing include directory: {d}")
    closure, rtl_paths = _dependencies(root, sources, includes)
    for raw in assets:
        p = under(root, raw)
        if p.is_dir():
            closure.update(x.relative_to(root).as_posix() for x in p.rglob("*") if x.is_file() or x.is_symlink())
        elif p.is_file():
            closure.add(safe_relative(raw))
        else:
            raise ReadinessError(f"Missing asset: {raw}")
    allowed = sorted(rtl_paths if allowed_paths is None else {safe_relative(x) for x in allowed_paths})
    if not set(allowed).issubset(rtl_paths):
        raise ReadinessError("Editable paths must be declared RTL inputs or included RTL headers; assets stay frozen")
    files = file_manifest(root, sorted(closure))
    ws = {"api_version": 2, "created_utc": utc_now(), "origin": str(root), "top": top,
          "sources": sources, "include_dirs": includes, "allowed_paths": allowed, "files": files}
    ws["workspace_id"] = context_identity(ws)
    ws["baseline_identity"] = variant_identity(ws, files)
    # Stage beside destination, publish only a complete snapshot.
    workspace.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=workspace.parent))
    try:
        tree = staging / "variants/baseline/tree"
        for rel in files:
            dst = under(tree, rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(under(root, rel), dst)
        if file_manifest(tree) != files:
            raise ReadinessError("Inputs changed during snapshot; no workspace published")
        save_json(staging / "workspace.json", ws)
        save_json(staging / "variants/baseline/variant.json", {"api_version": 2, "name": "baseline", "identity": ws["baseline_identity"], "based_on": ws["baseline_identity"], "files": files, "changes": []})
        staging.rename(workspace)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return ws


def create_variant(workspace, name, overlay_dir):
    label(name)
    if name == "baseline":
        raise ReadinessError("Cannot replace baseline")
    ws = load_workspace(workspace)
    base = load_variant(workspace, "baseline")
    overlay = Path(overlay_dir).resolve()
    if not overlay.is_dir():
        raise ReadinessError("Overlay directory does not exist")
    edits = file_manifest(overlay)
    if not edits or not set(edits).issubset(ws["allowed_paths"]):
        raise ReadinessError("Overlay must contain only allowed RTL paths and at least one file")
    dest = Path(workspace) / "variants" / name
    if dest.exists():
        raise ReadinessError(f"Variant exists: {name}; use a new name")
    staging = Path(tempfile.mkdtemp(prefix=".variant-", dir=dest.parent))
    try:
        tree = staging / "tree"
        shutil.copytree(variant_tree(workspace, "baseline"), tree)
        for rel in edits:
            shutil.copyfile(under(overlay, rel), under(tree, rel))
        files = file_manifest(tree)
        changes = [rel for rel in files if files[rel] != base["files"][rel]]
        if not changes:
            raise ReadinessError("Overlay has no RTL change")
        dependencies, _ = _dependencies(tree, ws["sources"], ws["include_dirs"])
        if not dependencies.issubset(files):
            raise ReadinessError("Variant references inputs outside the frozen closure")
        obj = {"api_version": 2, "name": name, "identity": variant_identity(ws, files), "based_on": ws["baseline_identity"], "files": files, "changes": changes}
        save_json(staging / "variant.json", obj)
        staging.rename(dest)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return obj
