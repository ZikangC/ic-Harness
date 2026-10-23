#!/usr/bin/env python3
"""Owned, source-bound, budgeted experiments. No external skill runtime required.

The worker is deliberately a separate, detached process. A lost worker is reported
as incomplete, never restarted by status() or by re-submitting its job identity.
Commands run without a shell and all outputs belong to the job directory.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import fcntl
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
import uuid
from typing import Any

from core import (ReadinessError, digest, load_variant, load_workspace, read_json,
                  safe_relative, save_json, sha, under, utc_now, variant_tree)


TERMINAL = {"completed", "failed", "timeout", "incomplete"}
_JOB_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_LAUNCHED: dict[int, subprocess.Popen] = {}


def _mac_info(pid: int, flavor: int, size: int) -> bytes | None:
    """libproc avoids shell ps (which can be unavailable in a desktop sandbox)."""
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                        ctypes.c_void_p, ctypes.c_int]
        library.proc_pidinfo.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(size)
        count = library.proc_pidinfo(pid, flavor, 0, buffer, size)
        return buffer.raw if count == size else None
    except (OSError, AttributeError):
        return None


def _proc_identity(pid: int) -> dict[str, Any] | None:
    """PID alone is never an identity. Linux uses boot ID and start ticks."""
    try:
        if sys.platform.startswith("linux"):
            raw = Path(f"/proc/{pid}/stat").read_text()
            fields = raw[raw.rfind(")") + 2:].split()
            if fields[0] == "Z":
                return None
            return {"pid": pid, "start": fields[19], "scheme": "linux-proc-start-ticks",
                    "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "pid_namespace": _pid_namespace()}
        if sys.platform == "darwin":
            # proc_bsdinfo layout from the platform SDK sys/proc_info.h (136 bytes).
            import struct
            raw = _mac_info(pid, 3, 136)  # PROC_PIDTBSDINFO
            if raw is None or struct.unpack_from("=I", raw, 4)[0] == 5:  # SZOMB
                return None
            seconds, micros = struct.unpack_from("=QQ", raw, 120)
            return {"pid": pid, "start": f"{seconds}.{micros:06d}",
                    "scheme": "macos-libproc-start-time", "boot": "absolute-epoch-start"}
        process = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=3, check=False)
        fields = process.stdout.split()
        if process.returncode or len(fields) < 6 or fields[-1].startswith("Z"):
            return None
        boot = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=3, check=False)
        return {"pid": pid, "start": " ".join(fields[:-1]),
                "scheme": "ps-start-time", "boot": boot.stdout.strip()}
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def _alive(identity: dict[str, Any] | None) -> bool:
    return bool(identity and _proc_identity(int(identity["pid"])) == identity)


def _pid_namespace() -> str | None:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def _visibility(identity: dict[str, Any] | None) -> str | None:
    """Missing processes differ from processes outside the observer's namespace."""
    if not identity or not sys.platform.startswith("linux"):
        return None
    namespace = _pid_namespace()
    if not namespace or not identity.get("pid_namespace"):
        return "pid_namespace_unavailable"
    if identity["pid_namespace"] != namespace:
        return "different_pid_namespace"
    try:
        Path(f"/proc/{int(identity['pid'])}/stat").read_text()
    except FileNotFoundError:
        # The namespace is the same, so absence really can mean a lost process.
        return None
    except (OSError, ValueError):
        return "process_identity_unreadable"
    try:
        Path("/proc/sys/kernel/random/boot_id").read_text()
    except OSError:
        return "process_identity_unreadable"
    return None


def _job_dir(workspace: Path, job_id: str) -> Path:
    if not isinstance(job_id, str) or not _JOB_NAME.fullmatch(job_id):
        raise ReadinessError("invalid job_id")
    root = workspace.resolve() / "jobs"
    path = root / job_id
    if path.is_symlink() or (root.exists() and root.is_symlink()):
        raise ReadinessError("job paths must not be symlinks")
    return path


@contextlib.contextmanager
def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReadinessError(f"another operation owns {path.name}; retry the same job ID") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _executable(raw: str) -> dict[str, str]:
    resolved = shutil.which(raw)
    if not resolved:
        raise ReadinessError(f"executable unavailable: {raw}")
    path = Path(resolved).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ReadinessError(f"not an executable file: {path}")
    return {"path": str(path), "sha256": sha(path)}


def _positive(raw: Any, name: str, maximum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 < raw <= maximum:
        raise ReadinessError(f"{name} must be an integer in 1..{maximum}")
    return raw


def _config(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], list[dict[str, str]]]:
    if not isinstance(config, dict):
        raise ReadinessError("experiment config must be an object")
    value = json.loads(json.dumps(config))
    kind = value.get("kind")
    if kind not in {"yosys", "equiv", "command"}:
        raise ReadinessError("kind must be yosys, equiv, or command")
    value["timeout_seconds"] = _positive(value.get("timeout_seconds", 30), "timeout_seconds", 86400)
    value["memory_mib"] = _positive(value.get("memory_mib", 2048), "memory_mib", 1048576)
    value["max_concurrent_jobs"] = _positive(value.get("max_concurrent_jobs", 1), "max_concurrent_jobs", 64)
    inputs: list[dict[str, str]] = []
    if kind == "command":
        argv = value.get("argv")
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(item, str) or "\0" in item for item in argv)
                or "{" in argv[0]):
            raise ReadinessError("command requires a nonempty argv string array and a fixed executable")
        tool = _executable(argv[0])
        value["argv"][0] = tool["path"]
        markers = value.get("success_markers", [])
        if not isinstance(markers, list) or any(not isinstance(item, str) or not item for item in markers):
            raise ReadinessError("success_markers must contain nonempty strings")
        value["success_markers"] = markers
        value["purpose"] = str(value.get("purpose") or "custom")
        value["baseline"] = str(value.get("baseline", "baseline"))
        for arg in argv[1:]:
            if "{" not in arg and Path(arg).is_absolute() and Path(arg).is_file():
                path = Path(arg).resolve()
                inputs.append({"path": str(path), "sha256": sha(path)})
    else:
        tool = _executable(str(value.get("tool") or "yosys"))
        value["tool"] = tool["path"]
        if kind == "yosys":
            if value.get("stage") not in {"elaborate", "proc", "generic"}:
                raise ReadinessError("yosys stage must be elaborate, proc, or generic")
        else:
            value["baseline"] = str(value.get("baseline", "baseline"))
            value["seq_depth"] = _positive(value.get("seq_depth", 2), "seq_depth", 1024)
        defines = value.get("defines", [])
        if not isinstance(defines, list) or any(
                not isinstance(item, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:=[A-Za-z0-9_'()+.:-]+)?", item)
                for item in defines):
            raise ReadinessError("unsafe or invalid Verilog defines")
        parameters = value.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ReadinessError("parameters must be an object")
        for key, parameter in parameters.items():
            if (not _IDENTIFIER.fullmatch(key) or isinstance(parameter, bool)
                    or not isinstance(parameter, (str, int))
                    or not re.fullmatch(r"[A-Za-z0-9_'()+.:-]+", str(parameter))):
                raise ReadinessError("unsafe or invalid Verilog parameter")
        value["defines"] = defines
        value["parameters"] = parameters
    # Explicit command dependencies are hashed, not merely named in a JSON claim.
    for raw in value.get("input_files", []):
        path = Path(str(raw)).expanduser().resolve()
        if not path.is_file():
            raise ReadinessError(f"missing verification input: {path}")
        inputs.append({"path": str(path), "sha256": sha(path)})
    return value, tool, sorted({item["path"]: item for item in inputs}.values(), key=lambda item: item["path"])


def _request(workspace: Path, variant: str, config: dict[str, Any]) -> dict[str, Any]:
    ws = load_workspace(workspace)
    selected = load_variant(workspace, variant, verify=True)
    normalized, tool, inputs = _config(config)
    top = ws.get("top")
    if not isinstance(top, str) or not _IDENTIFIER.fullmatch(top):
        raise ReadinessError("Yosys experiments currently require an unescaped top identifier")
    sources = [safe_relative(item) for item in ws.get("sources", [])]
    includes = [safe_relative(item) if item != "." else "." for item in ws.get("include_dirs", [])]
    if not sources:
        raise ReadinessError("workspace has no source list")
    pair = normalized["kind"] == "equiv" or (
        normalized["kind"] == "command"
        and any("{baseline_tree}" in item for item in normalized["argv"]))
    baseline = load_variant(workspace, str(normalized.get("baseline", "baseline")), verify=True) if pair else None
    verification = {"config": normalized, "workspace_id": ws["workspace_id"], "top": top, "sources": sources,
                    "include_dirs": includes, "tool": tool, "input_files": inputs}
    request = {
        "api_version": 2, "workspace_root": str(workspace.resolve()),
        "workspace_id": ws["workspace_id"], "variant": variant,
        "variant_identity": selected["identity"], "variant_files": selected["files"],
        "baseline": baseline["name"] if baseline else None,
        "baseline_identity": baseline["identity"] if baseline else None,
        "baseline_files": baseline["files"] if baseline else None,
        "top": top, "sources": sources, "include_dirs": includes,
        "config": normalized, "tool": tool, "input_files": inputs,
        "verification_identity": digest(verification),
        "measurement_key": digest(verification),
    }
    request["request_identity"] = digest(request)
    return request


def _active_jobs(workspace: Path) -> list[str]:
    active = []
    root = workspace / "jobs"
    if root.exists():
        for path in root.iterdir():
            if path.is_dir() and not path.is_symlink() and (path / "request.json").is_file():
                observed = status(workspace, path.name)
                if observed.get("active_process") or observed.get("status") in {"submitted", "running"}:
                    active.append(path.name)
    return active


def submit(workspace: Path, *, variant: str, config: dict[str, Any],
           job_id: str | None = None, background: bool = True,
           require_new: bool = False) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    request = _request(workspace, variant, config)
    job_id = job_id or ("job-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8])
    path = _job_dir(workspace, job_id)
    with _lock(workspace / "jobs" / ".submit.lock"):
        if path.exists():
            if require_new:
                raise ReadinessError("batch new-job identity was already reserved; explicit reuse is required")
            if not (path / "request.json").is_file():
                raise ReadinessError("job ID was already reserved by an incomplete submission; never reuse it")
            old = read_json(path / "request.json")
            if old.get("request_identity") != request["request_identity"]:
                raise ReadinessError("job ID already binds a different request")
            return status(workspace, job_id)
        active = _active_jobs(workspace)
        if len(active) >= request["config"]["max_concurrent_jobs"]:
            raise ReadinessError(f"workspace concurrency budget occupied: {', '.join(active)}")
        if sys.platform.startswith("linux"):
            try:
                available = next(int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))
                if request["config"]["memory_mib"] * 1048576 > available * 0.8:
                    raise ReadinessError("requested memory exceeds 80% of currently available host memory")
            except (OSError, StopIteration, ValueError):
                pass
        path.mkdir()
        request["job_id"] = job_id
        save_json(path / "request.json", request)
        save_json(path / "status.json", {
            "api_version": 2, "job_id": job_id, "request_identity": request["request_identity"],
            "status": "submitted", "submitted_utc": utc_now(), "submitted_epoch": time.time()})
        if background:
            with (path / "worker.log").open("ab") as output:
                process = subprocess.Popen(
                    [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", str(path)],
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            save_json(path / "launcher.json", {"worker": _proc_identity(process.pid),
                                                "launched_utc": utc_now()})
            # Retain handles while this client lives so its own workers can be
            # reaped; worker lifetime never depends on this in-memory cache.
            _LAUNCHED[process.pid] = process
    if not background:
        worker(path)
    return status(workspace, job_id)


def _incomplete(request: dict[str, Any], state: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "api_version": 2, "job_id": request["job_id"], "request_identity": request["request_identity"],
        "workspace_id": request["workspace_id"], "variant_identity": request["variant_identity"],
        "baseline_identity": request.get("baseline_identity"), "kind": request["config"]["kind"],
        "measurement_key": request["measurement_key"], "status": "incomplete", "reason": reason,
        "exit_code": None, "elapsed_seconds": None, "peak_rss_bytes": None,
        "started_utc": state.get("started_utc"), "completed_utc": utc_now(),
        "log_path": str(Path(request["workspace_root"]) / "jobs" / request["job_id"] / "tool.log"),
        "verification_passed": False,
    }


def status(workspace: Path, job_id: str) -> dict[str, Any]:
    path = _job_dir(Path(workspace), job_id)
    if not (path / "request.json").is_file():
        raise ReadinessError(f"unknown or incompletely submitted job: {job_id}")
    request = read_json(path / "request.json")
    unsigned = {key: value for key, value in request.items() if key not in {"request_identity", "job_id"}}
    if digest(unsigned) != request.get("request_identity"):
        raise ReadinessError("job request contents changed")
    for pid, process in list(_LAUNCHED.items()):
        if process.poll() is not None:
            del _LAUNCHED[pid]
    if request.get("workspace_root") != str(Path(workspace).resolve()) or request.get("job_id") != job_id:
        raise ReadinessError("job ownership mismatch")
    if (path / "result.json").is_file():
        result = read_json(path / "result.json")
        if result.get("request_identity") != request["request_identity"]:
            raise ReadinessError("result/request identity mismatch")
        return {**result, "job_dir": str(path), "active_process": False}
    state = read_json(path / "status.json")
    launcher = read_json(path / "launcher.json") if (path / "launcher.json").is_file() else {}
    identity = state.get("worker") or launcher.get("worker")
    visibility = ("worker_identity_unavailable" if sys.platform.startswith("linux")
                  and state.get("status") == "running" and not identity else None)
    visibility = visibility or _visibility(identity) or _visibility(state.get("process"))
    if visibility:
        # Observation from a fresh container cannot declare a live worker lost.
        # Preserve its reservation and receipts; the outer executor owns liveness.
        return {**state, "job_dir": str(path), "active_process": None,
                "worker_identity_verified": False, "process_visibility": visibility}
    alive = _alive(identity)
    child_alive = _alive(state.get("process"))
    if alive:
        return {**state, "job_dir": str(path), "active_process": True,
                "process_alive": child_alive, "worker_identity_verified": True}
    if not identity and time.time() - float(state.get("submitted_epoch", 0)) < 10:
        return {**state, "job_dir": str(path), "active_process": False,
                "worker_identity_verified": False}
    reason = "worker_lost_child_still_running" if child_alive else "worker_lost_or_never_started"
    result = _incomplete(request, state, reason)
    # An orphan may still consume resources. Never kill or silently resubmit it.
    if not child_alive:
        with _lock(path / ".status.lock"):
            if not (path / "result.json").exists():
                result["artifact_hashes"] = _artifact_hashes(path)
                save_json(path / "result.json", result)
    return {**result, "job_dir": str(path), "active_process": child_alive,
            "process": state.get("process"), "worker": identity,
            "worker_identity_verified": False}


def _quote(path: Path | str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _read_design(request: dict[str, Any], tree: Path) -> list[str]:
    options = ["read_verilog", "-sv"]
    # Yosys preserves quotes in option tokens. Paths with spaces therefore use
    # deterministic, space-free aliases OUTSIDE the frozen source tree. The
    # frontend cwd is work/tree for both stage and pair experiments.
    options += [f"-I../include_paths/{tree.name}/{index}"
                for index, _ in enumerate(request["include_dirs"])]
    options += ["-D" + item for item in request["config"].get("defines", [])]
    options += [_quote(under(tree, item)) for item in request["sources"]]
    commands = [" ".join(options)]
    for key, value in sorted(request["config"].get("parameters", {}).items()):
        commands.append(f"chparam -set {key} {value} {request['top']}")
    commands.append(f"hierarchy -check -top {request['top']}")
    return commands


def _prepare_include_aliases(request: dict[str, Any], job: Path) -> list[dict[str, str]]:
    records = []
    trees = [job / "work" / "tree"]
    if request.get("baseline_identity"):
        trees.append(job / "work" / "baseline_tree")
    for tree in trees:
        for index, relative in enumerate(request["include_dirs"]):
            target = tree if relative == "." else under(tree, relative)
            alias = job / "work" / "include_paths" / tree.name / str(index)
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.symlink_to(os.path.relpath(target, alias.parent), target_is_directory=True)
            records.append({"alias": alias.relative_to(job).as_posix(),
                            "target": target.relative_to(job).as_posix()})
    save_json(job / "include-aliases.json", {"request_identity": request["request_identity"], "aliases": records})
    return records


def _verify_include_aliases(job: Path, records: list[dict[str, str]]) -> None:
    for record in records:
        alias, target = job / record["alias"], job / record["target"]
        if not alias.is_symlink() or alias.resolve() != target.resolve():
            raise ReadinessError("job include alias target changed")


def _yosys_script(request: dict[str, Any], job: Path) -> Path:
    config = request["config"]
    tree = job / "work" / "tree"
    if config["kind"] == "equiv":
        commands = _read_design(request, job / "work" / "baseline_tree")
        commands += ["proc", "memory", "flatten", "opt_clean", "check -assert",
                     f"write_json {_quote(job / 'gold-design.json')}",
                     "design -stash gold", "design -reset"]
        commands += _read_design(request, tree)
        commands += ["proc", "memory", "flatten", "opt_clean", "check -assert",
                     f"write_json {_quote(job / 'gate-design.json')}",
                     "design -stash gate", "design -reset",
                     f"design -copy-from gold -as gold {request['top']}",
                     f"design -copy-from gate -as gate {request['top']}",
                     "equiv_make gold gate equiv", "hierarchy -check -top equiv",
                     f"write_json {_quote(job / 'equiv-design.json')}",
                     f"equiv_simple -seq {config['seq_depth']}", "equiv_status -assert"]
    else:
        commands = _read_design(request, tree)
        if config["stage"] == "proc":
            commands += ["proc", "opt_clean", "check -assert"]
        elif config["stage"] == "generic":
            commands += [f"synth -top {request['top']} -noabc", "check -assert"]
        commands += [f"tee -o {_quote(job / 'statistics.json')} stat -json"]
    path = job / "experiment.ys"
    path.write_text("\n".join(commands) + "\n", encoding="utf-8")
    return path


def _verify_files(tree: Path, files: dict[str, Any]) -> None:
    actual = {str(path.relative_to(tree)) for path in tree.rglob("*") if path.is_file()}
    if actual != set(files):
        raise ReadinessError("source tree file set changed")
    for name, metadata in files.items():
        path = under(tree, name)
        if path.is_symlink() or sha(path) != metadata["sha256"]:
            raise ReadinessError(f"source integrity mismatch: {name}")


def _copy_variant(workspace: Path, name: str, identity: str, files: dict[str, Any], target: Path) -> None:
    current = load_variant(workspace, name, verify=True)
    if current["identity"] != identity or current["files"] != files:
        raise ReadinessError("sealed variant no longer matches submitted identity")
    original = variant_tree(workspace, name)
    target.mkdir(parents=True)
    for relative in files:
        source = under(original, relative)
        destination = under(target, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    _verify_files(target, files)


def _group_rss(pgid: int) -> int:
    if sys.platform.startswith("linux"):
        total = 0
        page_size = os.sysconf("SC_PAGE_SIZE")
        for directory in Path("/proc").iterdir():
            if not directory.name.isdigit():
                continue
            try:
                raw = (directory / "stat").read_text()
                fields = raw[raw.rfind(")") + 2:].split()
                if int(fields[2]) == pgid and fields[0] != "Z":
                    total += int((directory / "statm").read_text().split()[1]) * page_size
            except (OSError, ValueError, IndexError):
                pass
        return total
    if sys.platform == "darwin":
        import struct
        try:
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            library.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                             ctypes.c_void_p, ctypes.c_int]
            library.proc_listpids.restype = ctypes.c_int
            size = library.proc_listpids(2, pgid, None, 0)  # PROC_PGRP_ONLY
            buffer = ctypes.create_string_buffer(max(size + 64, 4096))
            count = library.proc_listpids(2, pgid, buffer, len(buffer))
            total = 0
            for (pid,) in struct.iter_unpack("=i", buffer.raw[:max(0, count) - max(0, count) % 4]):
                raw = _mac_info(pid, 4, 96)  # PROC_PIDTASKINFO
                if raw:
                    total += struct.unpack_from("=Q", raw, 8)[0]
            return total
        except (OSError, AttributeError):
            return 0
    try:
        observed = subprocess.run(["ps", "-axo", "pgid=,rss="], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, timeout=3)
        return sum(int(parts[1]) * 1024 for line in observed.stdout.splitlines()
                   if len(parts := line.split()) == 2 and parts[0] == str(pgid))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


def _kill_group(process: subprocess.Popen, identity: dict[str, Any] | None) -> None:
    # The group was created by this worker. A still-existing leader must match;
    # after its exit the surviving group is still owned by this exact launch.
    current = _proc_identity(process.pid)
    if current is not None and current != identity:
        raise ReadinessError("process-group leader identity changed; refusing to signal")
    if current is None and not _group_rss(process.pid):
        return
    try:
        if current is not None and os.getpgid(process.pid) != process.pid:
            raise ReadinessError("owned child is no longer its recorded process-group leader")
        os.killpg(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and _group_rss(process.pid):
            time.sleep(0.02)
        # Kill descendants still in the owned group even if the leader exited on TERM.
        if _group_rss(process.pid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run(request: dict[str, Any], job: Path, argv: list[str], state: dict[str, Any]) -> dict[str, Any]:
    budget = request["config"]
    memory_bytes = budget["memory_mib"] * 1048576
    def limits() -> None:
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    begin = time.monotonic()
    with (job / "tool.log").open("wb") as log:
        cwd = job / "work" if request["config"]["kind"] == "command" else job / "work" / "tree"
        process = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, close_fds=True, start_new_session=True,
            preexec_fn=limits, env={**os.environ, "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"})
        identity = _proc_identity(process.pid)
        state.update({"process": identity, "argv": argv, "log_path": str(job / "tool.log")})
        save_json(job / "status.json", state)
        peak = 0
        outcome = None
        usage = None
        while True:
            peak = max(peak, _group_rss(process.pid))
            reaped, wait_status, child_usage = os.wait4(process.pid, os.WNOHANG)
            if reaped:
                code = os.waitstatus_to_exitcode(wait_status)
                process.returncode = code
                usage = child_usage
                break
            elapsed = time.monotonic() - begin
            if elapsed > budget["timeout_seconds"]:
                outcome = "timeout"
            elif peak > memory_bytes:
                outcome = "memory_budget_exceeded"
            if outcome:
                _kill_group(process, identity)
                _, wait_status, usage = os.wait4(process.pid, 0)
                code = os.waitstatus_to_exitcode(wait_status)
                process.returncode = code
                break
            time.sleep(0.1)
        if _group_rss(process.pid):
            _kill_group(process, identity)
            outcome = outcome or "background_descendants_after_tool_exit"
    multiplier = 1 if sys.platform == "darwin" else 1024
    peak = max(peak, int(usage.ru_maxrss * multiplier)) if usage else peak
    return {"exit_code": code, "elapsed_seconds": round(time.monotonic() - begin, 6),
            "peak_rss_bytes": peak,
            "status": "timeout" if outcome == "timeout" else "failed" if outcome or code else "completed",
            "reason": outcome or ("tool_exit_nonzero" if code else "tool_completed"),
            "process": identity, "memory_limit_scope": "owned_process_group_rss_and_linux_rlimit_as"
            if sys.platform.startswith("linux") else "owned_process_group_rss"}


def _statistics(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        value, _ = json.JSONDecoder().raw_decode(raw[raw.index("{"):])
        return value if isinstance(value, dict) else None
    except (ValueError, json.JSONDecodeError):
        return None


def _find_markers(path: Path, markers: list[str]) -> list[str]:
    """Search full logs without loading an unbounded tool log into the worker."""
    if not markers:
        return []
    pending = {item.encode("utf-8"): item for item in markers}
    found: set[str] = set()
    overlap = max(map(len, pending)) - 1
    tail = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1048576), b""):
            data = tail + chunk
            for raw, marker in list(pending.items()):
                if raw in data:
                    found.add(marker)
                    del pending[raw]
            if not pending:
                break
            tail = data[-overlap:] if overlap else b""
    return [item for item in markers if item in found]


def _has_blackboxes(path: Path) -> bool:
    if not path.is_file():
        return True
    design = read_json(path)
    for module in design.get("modules", {}).values():
        for attr in ("blackbox", "whitebox"):
            value = module.get("attributes", {}).get(attr)
            if value and str(value).strip("0"):
                return True
    return False


def _equiv_coverage(job: Path, top: str) -> dict[str, Any]:
    """Reject interface mismatch, vacuous proofs, and unaccounted output bits."""
    gold = read_json(job / "gold-design.json").get("modules", {}).get(top, {})
    gate = read_json(job / "gate-design.json").get("modules", {}).get(top, {})
    def signature(module: dict[str, Any]) -> dict[str, Any]:
        return {name: {"direction": value.get("direction"), "width": len(value.get("bits", []))}
                for name, value in module.get("ports", {}).items()}
    interface_equal = bool(gold) and bool(gate) and signature(gold) == signature(gate)
    equiv = read_json(job / "equiv-design.json").get("modules", {}).get("equiv", {})
    cells = [cell for cell in equiv.get("cells", {}).values() if cell.get("type") == "$equiv"]
    covered = {bit for cell in cells for bit in cell.get("connections", {}).get("Y", [])}
    outputs = {name: port.get("bits", []) for name, port in equiv.get("ports", {}).items()
               if port.get("direction") in {"output", "inout"}}
    missing = {name: [bit for bit in bits if bit not in covered]
               for name, bits in outputs.items() if any(bit not in covered for bit in bits)}
    return {"interface_equal": interface_equal, "equiv_cell_count": len(cells),
            "output_bit_count": sum(len(bits) for bits in outputs.values()),
            "uncovered_output_bits": missing,
            "passed": interface_equal and bool(cells) and bool(outputs) and not missing}


def _artifact_hashes(job: Path) -> dict[str, str]:
    omitted = {"status.json", "result.json", "worker.log", "launcher.json", "worker-claim.json"}
    values = {}
    for path in job.rglob("*"):
        relative = path.relative_to(job).as_posix()
        if (not path.is_file() or path.is_symlink() or path.name.startswith(".")
                or relative in omitted or relative == "request.json"
                or relative.startswith(("work/tree/", "work/baseline_tree/"))):
            continue
        values[relative] = sha(path)
    return values


def _tool_version(request: dict[str, Any], job: Path) -> str | None:
    if request["config"]["kind"] == "command":
        return None
    # This read-only query is separately bounded; experiment.ys remains the actual proof.
    try:
        process = subprocess.Popen([request["tool"]["path"], "-V"], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        identity = _proc_identity(process.pid)
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_group(process, identity)
            process.communicate()
            return None
        raw = output.decode("utf-8", errors="replace")
        (job / "version.log").write_text(raw, encoding="utf-8")
        return raw.strip()[:4096] if process.returncode == 0 else None
    except OSError:
        return None


def worker(job_dir: Path) -> int:
    job = Path(job_dir).resolve()
    request = read_json(job / "request.json")
    with _lock(job / ".worker.lock"):
        if (job / "result.json").is_file():
            return 0
        claim = job / "worker-claim.json"
        if claim.exists():
            raise ReadinessError("job worker already claimed; a lost worker cannot be restarted")
        state = read_json(job / "status.json")
        state.update({"status": "running", "started_utc": utc_now(),
                      "worker": _proc_identity(os.getpid())})
        save_json(claim, {"worker": state["worker"], "request_identity": request["request_identity"]})
        save_json(job / "status.json", state)
        result = _incomplete(request, state, "worker_exception")
        begin = time.monotonic()
        copied = False
        aliases: list[dict[str, str]] = []
        try:
            workspace = Path(request["workspace_root"])
            ws = load_workspace(workspace)
            if ws["workspace_id"] != request["workspace_id"]:
                raise ReadinessError("workspace identity changed")
            unsigned = {key: value for key, value in request.items() if key not in {"request_identity", "job_id"}}
            if digest(unsigned) != request["request_identity"]:
                raise ReadinessError("request contents changed")
            for item in [request["tool"], *request["input_files"]]:
                if sha(Path(item["path"])) != item["sha256"]:
                    raise ReadinessError("tool or verification input changed since submission")
            _copy_variant(workspace, request["variant"], request["variant_identity"], request["variant_files"], job / "work" / "tree")
            if request.get("baseline_identity"):
                _copy_variant(workspace, request["baseline"], request["baseline_identity"], request["baseline_files"], job / "work" / "baseline_tree")
            copied = True
            if request["config"]["kind"] == "equiv":
                # Frontends resolve literal readmem paths against process cwd. Until
                # separate per-tree elaboration exists, never accidentally use the
                # candidate's init data while claiming a baseline/candidate proof.
                for folder in (job / "work" / "tree", job / "work" / "baseline_tree"):
                    for relative in request["variant_files"]:
                        if re.search(rb"\$readmem[hb]\b", under(folder, relative).read_bytes()):
                            raise ReadinessError("equiv with readmem is unsupported; use a separately bound pair command")
            if request["config"]["kind"] == "command":
                argv = [item.replace("{tree}", str(job / "work" / "tree")).replace(
                    "{baseline_tree}", str(job / "work" / "baseline_tree")).replace(
                    "{job}", str(job)) for item in request["config"]["argv"]]
            else:
                aliases = _prepare_include_aliases(request, job)
                script = _yosys_script(request, job)
                argv = [request["tool"]["path"], "-s", str(script)]
            cwd = job / "work" if request["config"]["kind"] == "command" else job / "work" / "tree"
            save_json(job / "execution.json", {"argv": argv, "cwd": str(cwd),
                                              "request_identity": request["request_identity"]})
            measured = _run(request, job, argv, state)
            result.update(measured)
            result["verification_identity"] = request["verification_identity"]
            result["tool"] = request["tool"]
            result["yosys_version"] = _tool_version(request, job)
            result["verification_passed"] = False
            kind = request["config"]["kind"]
            if kind == "yosys":
                result["stage"] = request["config"]["stage"]
                result["statistics"] = _statistics(job / "statistics.json")
                if measured["status"] == "completed" and result["statistics"] is None:
                    result.update(status="incomplete", reason="statistics_missing_or_unreadable")
            elif measured["status"] == "completed":
                if kind == "equiv":
                    coverage = _equiv_coverage(job, request["top"])
                    proof = (bool(_find_markers(job / "tool.log", ["Equivalence successfully proven!"]))
                             and coverage["passed"]
                             and not _has_blackboxes(job / "gold-design.json")
                             and not _has_blackboxes(job / "gate-design.json"))
                    result["verification_passed"] = proof
                    result["proof_scope"] = "yosys_equiv_simple"
                    result["equiv_coverage"] = coverage
                    result["seq_depth"] = request["config"]["seq_depth"]
                    if not proof:
                        result.update(status="incomplete", reason="equivalence_proof_missing_or_blackbox_present")
                else:
                    markers = request["config"]["success_markers"]
                    matched = _find_markers(job / "tool.log", markers)
                    result["matched_success_markers"] = matched
                    result["verification_passed"] = (bool(markers) and len(matched) == len(markers)
                                                     and bool(request.get("baseline_identity"))
                                                     and request["config"].get("purpose") in {"regression", "equivalence"})
                    if markers and len(matched) != len(markers):
                        result.update(status="failed", reason="success_marker_missing")
            if copied:
                _verify_include_aliases(job, aliases)
                _verify_files(job / "work" / "tree", request["variant_files"])
                checked = load_variant(workspace, request["variant"], verify=True)
                if checked["identity"] != request["variant_identity"]:
                    raise ReadinessError("sealed source identity changed during experiment")
                if request.get("baseline_identity"):
                    _verify_files(job / "work" / "baseline_tree", request["baseline_files"])
                    checked = load_variant(workspace, request["baseline"], verify=True)
                    if checked["identity"] != request["baseline_identity"]:
                        raise ReadinessError("sealed baseline identity changed during experiment")
            for item in [request["tool"], *request["input_files"]]:
                if sha(Path(item["path"])) != item["sha256"]:
                    raise ReadinessError("tool or verification input changed during experiment")
            result["source_integrity_passed"] = True
        except Exception as error:
            result.update(status="incomplete", reason="worker_exception", error=f"{type(error).__name__}: {error}",
                          verification_passed=False, source_integrity_passed=False)
        result["worker_elapsed_seconds"] = round(time.monotonic() - begin, 6)
        result["completed_utc"] = utc_now()
        result["artifact_hashes"] = _artifact_hashes(job)
        save_json(job / "result.json", result)
        save_json(job / "status.json", result)
        return 0 if result["status"] == "completed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    args = parser.parse_args()
    return worker(args.worker)


if __name__ == "__main__":
    raise SystemExit(main())
