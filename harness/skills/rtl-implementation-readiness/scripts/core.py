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
    keys = ("api_version", "top", "sources", "include_dirs", "allowed_paths", "files")
    if ws.get("api_version") == 3:
        keys += ("region_roots", "build_files", "frozen_assets")
    return digest({k: ws[k] for k in keys})


def _ordered_paths(value, field):
    if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
        raise ReadinessError(f"{field} must be an ordered list of relative paths")
    paths = [safe_relative(x) for x in value]
    if len(paths) != len(set(paths)):
        raise ReadinessError(f"{field} contains duplicate paths")
    return paths


def variant_build(ws, obj=None):
    """The effective build is part of each regional candidate's identity."""
    baseline = {k: ws[k] for k in ("top", "sources", "include_dirs")}
    raw = obj.get("build") if isinstance(obj, dict) else None
    if ws.get("api_version") != 3 or raw is None:
        return {"top": baseline["top"], "sources": list(baseline["sources"]),
                "include_dirs": list(baseline["include_dirs"])}
    if not isinstance(raw, dict) or set(raw) - {"top", "sources", "include_dirs"}:
        raise ReadinessError("Variant build must contain only top, sources and include_dirs")
    if raw.get("top", ws["top"]) != ws["top"]:
        raise ReadinessError("Regional restructuring must preserve the frozen top")
    if "sources" not in raw or "include_dirs" not in raw:
        raise ReadinessError("Variant build requires sources and include_dirs")
    sources = _ordered_paths(raw["sources"], "build.sources")
    if not sources:
        raise ReadinessError("Variant build requires at least one source")
    return {"top": ws["top"], "sources": sources,
            "include_dirs": _ordered_paths(raw["include_dirs"], "build.include_dirs")}


def variant_identity(ws, files, build=None):
    identity = {"workspace_id": ws["workspace_id"], "files": files}
    if ws.get("api_version") == 3:
        identity["build"] = variant_build(ws, {"build": build})
    return digest(identity)


def load_workspace(workspace):
    ws = read_json(Path(workspace) / "workspace.json")
    try:
        valid = (ws.get("api_version") in (2, 3) and context_identity(ws) == ws.get("workspace_id")
                 and ws.get("baseline_identity") == variant_identity(ws, ws.get("files")))
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise ReadinessError("Workspace context changed or unsupported workspace version")
    if ws["api_version"] == 3:
        if (set(ws["allowed_paths"]) & set(ws["frozen_assets"])
                or set(ws["build_files"]) & (set(ws["allowed_paths"]) | set(ws["frozen_assets"]))):
            raise ReadinessError("Regional edit scope overlaps frozen assets or RTL build inputs")
    return ws


def variant_tree(workspace, name):
    return Path(workspace) / "variants" / label(name) / "tree"


def load_variant(workspace, name, verify=True):
    ws = load_workspace(workspace)
    obj = read_json(Path(workspace) / "variants" / label(name) / "variant.json")
    if obj.get("name") != name or obj.get("identity") != variant_identity(ws, obj.get("files"), obj.get("build")):
        raise ReadinessError(f"Invalid variant identity: {name}")
    if obj.get("based_on") != ws["baseline_identity"]:
        raise ReadinessError(f"Variant belongs to another baseline: {name}")
    if ws["api_version"] == 3:
        if name != "baseline":
            load_variant(workspace, "baseline", verify=verify)
        _validate_regional_variant(ws, obj)
        tree = variant_tree(workspace, name)
        if verify and file_manifest(tree) != obj["files"]:
            raise ReadinessError(f"Sealed variant was modified: {name}")
        # Dependency/build checks are required even for callers that skip byte hashing.
        validate_variant_build(ws, tree, obj["build"], obj["files"],
                               changed_paths=obj["operations"]["add"] + obj["operations"]["modify"])
        return obj
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


RTL_SUFFIXES = frozenset((".v", ".sv", ".vh", ".svh"))


def _is_rtl(rel):
    return Path(rel).suffix.lower() in RTL_SUFFIXES


def _in_region(rel, roots):
    return any(Path(rel).is_relative_to(Path(root)) for root in roots)


def _operations(baseline, files):
    return {"add": sorted(set(files) - set(baseline)),
            "modify": sorted(rel for rel in set(files) & set(baseline) if files[rel] != baseline[rel]),
            "delete": sorted(set(baseline) - set(files))}


def _validate_regional_variant(ws, obj):
    if obj.get("api_version") != 3 or not isinstance(obj.get("files"), dict):
        raise ReadinessError("Unsupported regional variant metadata")
    for rel in obj["files"]:
        if safe_relative(rel) != rel:
            raise ReadinessError(f"Noncanonical candidate path: {rel}")
    operations = _operations(ws["files"], obj["files"])
    changes = sorted(set().union(*operations.values()))
    if obj.get("operations") != operations or obj.get("changes") != changes:
        raise ReadinessError("Variant operation manifest does not match complete candidate inputs")
    frozen = set(ws["frozen_assets"])
    allowed = set(ws["allowed_paths"])
    build_files = set(ws["build_files"])
    if any(not _is_rtl(rel) or not _in_region(rel, ws["region_roots"]) for rel in operations["add"]):
        raise ReadinessError("New files must be RTL beneath an explicitly owned region root")
    if set(operations["modify"]) - (allowed | build_files):
        raise ReadinessError("Modified files exceed declared RTL/build edit scope")
    if set(operations["delete"]) - allowed:
        raise ReadinessError("Only explicitly editable existing RTL may be deleted")
    if frozen & set(changes):
        raise ReadinessError("Frozen assets cannot be changed or deleted")
    build = variant_build(ws, obj)
    if obj.get("build") != build:
        raise ReadinessError("Variant build metadata is not canonical")
    if obj.get("name") == "baseline" and (changes or build != variant_build(ws)
                                           or obj.get("identity") != ws["baseline_identity"]):
        raise ReadinessError("Regional baseline is immutable")


def _plain_filelist(tree, paths):
    sources = []
    for rel in paths:
        path = under(tree, rel)
        if not path.is_file():
            raise ReadinessError(f"Missing declared build file: {rel}")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if (line.startswith(("-", "+", "@")) or re.search(r'\s|[\"\'$`{}]', line)
                    or line.startswith("//")):
                raise ReadinessError(f"Unsupported filelist syntax in {rel}:{number}; use plain root-relative source paths")
            sources.append(safe_relative(line))
    return sources


def validate_variant_build(ws, tree, build, files=None, *, changed_paths=()):
    """Check the exact ordered build and lexical source/include/data closure.

    This is a build-input check, not elaboration or functional verification.
    Plain filelists intentionally do not interpret tool flags or nested includes.
    """
    tree = Path(tree).resolve()
    build = variant_build(ws, {"build": build})
    files = file_manifest(tree) if files is None else files
    if not set(build["sources"]).issubset(files):
        raise ReadinessError("Effective build references missing source files")
    for rel in build["include_dirs"]:
        if not under(tree, rel).is_dir():
            raise ReadinessError(f"Missing candidate include directory: {rel}")
    if ws.get("build_files") and _plain_filelist(tree, ws["build_files"]) != build["sources"]:
        raise ReadinessError("Declared filelists must exactly match the ordered effective build.sources")
    dependencies, rtl_paths = _dependencies(tree, build["sources"], build["include_dirs"])
    if not dependencies.issubset(files):
        raise ReadinessError("Variant references inputs outside its sealed input closure")
    # Existing RTL headers can have nonstandard extensions; allowed_paths were
    # derived from the lexical RTL closure, not from a filename suffix guess.
    unused = sorted(set(changed_paths) - set(ws.get("build_files", ())) - rtl_paths)
    if unused:
        raise ReadinessError(f"Added or modified RTL is omitted from the effective build dependency closure: {unused}")
    return build


def init_workspace(project_root, workspace, sources, top, include_dirs=(), assets=(), allowed_paths=None,
                   *, region_roots=(), build_files=()):
    root, workspace = Path(project_root).resolve(), Path(workspace).resolve()
    if workspace.exists():
        raise ReadinessError(f"Workspace already exists (use its status, do not initialize again): {workspace}")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top):
        raise ReadinessError("Top must be an explicit simple Verilog module name")
    regional = bool(region_roots or build_files)
    sources = (_ordered_paths(sources, "sources") if regional
               else list(dict.fromkeys(safe_relative(x) for x in sources)))
    includes = (_ordered_paths(include_dirs, "include_dirs") if regional
                else list(dict.fromkeys(safe_relative(x) for x in include_dirs)))
    if not sources:
        raise ReadinessError("Provide the ordered synthesis source list; discovery is not a build manifest")
    for d in includes:
        if not under(root, d).is_dir():
            raise ReadinessError(f"Missing include directory: {d}")
    closure, rtl_paths = _dependencies(root, sources, includes)
    frozen_assets = set()
    for raw in assets:
        p = under(root, raw)
        if p.is_dir():
            frozen_assets.update(x.relative_to(root).as_posix() for x in p.rglob("*") if x.is_file() or x.is_symlink())
        elif p.is_file():
            frozen_assets.add(safe_relative(raw))
        else:
            raise ReadinessError(f"Missing asset: {raw}")
    closure.update(frozen_assets)
    allowed = sorted((rtl_paths - frozen_assets if regional else rtl_paths) if allowed_paths is None
                     else {safe_relative(x) for x in allowed_paths})
    if not set(allowed).issubset(rtl_paths):
        raise ReadinessError("Editable paths must be declared RTL inputs or included RTL headers; assets stay frozen")
    if regional:
        regions = _ordered_paths(region_roots, "region_roots")
        manifests = _ordered_paths(build_files, "build_files")
        for rel in regions:
            path = under(root, rel)
            if path.exists() and not path.is_dir():
                raise ReadinessError(f"Region root must name a directory: {rel}")
        if set(allowed) & frozen_assets or set(manifests) & (rtl_paths | frozen_assets):
            raise ReadinessError("Build files and editable RTL must not overlap frozen assets")
        for rel in manifests:
            if not under(root, rel).is_file():
                raise ReadinessError(f"Missing declared build file: {rel}")
        closure.update(manifests)
    files = file_manifest(root, sorted(closure))
    ws = {"api_version": 2, "created_utc": utc_now(), "origin": str(root), "top": top,
          "sources": sources, "include_dirs": includes, "allowed_paths": allowed, "files": files}
    if regional:
        ws.update(api_version=3, region_roots=regions, build_files=manifests,
                  frozen_assets=sorted(frozen_assets))
        validate_variant_build(ws, root, variant_build(ws), files)
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
        if regional:
            for rel in includes:
                under(tree, rel).mkdir(parents=True, exist_ok=True)
        if file_manifest(tree) != files:
            raise ReadinessError("Inputs changed during snapshot; no workspace published")
        save_json(staging / "workspace.json", ws)
        baseline = {"api_version": ws["api_version"], "name": "baseline", "identity": ws["baseline_identity"], "based_on": ws["baseline_identity"], "files": files, "changes": []}
        if regional:
            baseline.update(build=variant_build(ws), operations={"add": [], "modify": [], "delete": []})
        save_json(staging / "variants/baseline/variant.json", baseline)
        staging.rename(workspace)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return ws


def create_variant(workspace, name, overlay_dir, *, delete_paths=(), build=None):
    label(name)
    if name == "baseline":
        raise ReadinessError("Cannot replace baseline")
    ws = load_workspace(workspace)
    base = load_variant(workspace, "baseline")
    overlay = Path(overlay_dir).resolve()
    if not overlay.is_dir():
        raise ReadinessError("Overlay directory does not exist")
    edits = file_manifest(overlay)
    if ws["api_version"] == 3:
        return _create_regional_variant(workspace, name, overlay, edits, ws, base,
                                        delete_paths=delete_paths, build=build)
    if delete_paths or build is not None:
        raise ReadinessError("File additions/deletions or candidate builds require a regional API 3 workspace")
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


def _create_regional_variant(workspace, name, overlay, edits, ws, base, *, delete_paths, build):
    deletes = _ordered_paths(delete_paths, "delete_paths")
    if set(deletes) & set(edits):
        raise ReadinessError("An overlay path cannot also be deleted")
    if set(deletes) - set(base["files"]):
        raise ReadinessError("Cannot delete a path absent from the frozen baseline")
    effective = variant_build(ws, {"build": build})
    dest = Path(workspace) / "variants" / name
    if dest.exists():
        raise ReadinessError(f"Variant exists: {name}; use a new name")
    staging = Path(tempfile.mkdtemp(prefix=".variant-", dir=dest.parent))
    try:
        tree = staging / "tree"
        shutil.copytree(variant_tree(workspace, "baseline"), tree)
        for rel in deletes:
            under(tree, rel).unlink()
        for rel in edits:
            path = under(tree, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(under(overlay, rel), path)
        files = file_manifest(tree)
        operations = _operations(ws["files"], files)
        changes = sorted(set().union(*operations.values()))
        if not changes and effective == variant_build(ws):
            raise ReadinessError("Overlay has no input or build change")
        obj = {"api_version": 3, "name": name, "identity": variant_identity(ws, files, effective),
               "based_on": ws["baseline_identity"], "files": files, "changes": changes,
               "build": effective, "operations": operations}
        _validate_regional_variant(ws, obj)
        validate_variant_build(ws, tree, effective, files,
                               changed_paths=operations["add"] + operations["modify"])
        save_json(staging / "variant.json", obj)
        staging.rename(dest)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return obj
