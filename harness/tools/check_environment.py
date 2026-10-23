#!/usr/bin/env python3
"""Read-only SSH, container, EDA and optional model connectivity checks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import tomllib


def invoke(argv: list[str], timeout: int, stdin: str | None = None) -> dict:
    try:
        p = subprocess.run(argv, input=stdin, text=True, capture_output=True,
                           timeout=timeout, check=False)
        return {"exit_code": p.returncode, "stdout": p.stdout[-12000:],
                "stderr": p.stderr[-4000:], "ok": p.returncode == 0}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error": type(error).__name__}


def load_configuration(path: Path) -> tuple[dict, dict, Path, str]:
    env = tomllib.loads(path.read_text())
    models = tomllib.loads((path.parent / "models.toml").read_text())
    root = (path.parent / env["project"]["root"]).resolve()
    name = env["remote"]["project_name"] or root.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name in {".", ".."}:
        raise ValueError("project_name must be a safe directory name")
    for key in ("host_workspace_root", "container_workspace_root"):
        p = PurePosixPath(env["remote"][key])
        if not p.is_absolute() or ".." in p.parts:
            raise ValueError(f"invalid {key}")
    return env, models, root, name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1]
                        / "config/environment.toml")
    parser.add_argument("--models", action="store_true", help="send one tiny request per distinct model")
    parser.add_argument("--resolve-only", action="store_true", help="validate config without network calls")
    args = parser.parse_args()
    env, models, root, name = load_configuration(args.config.resolve())
    remote, eda = env["remote"], env["eda"]
    result = {"project_root": str(root), "remote_project":
              str(PurePosixPath(remote["host_workspace_root"]) / name),
              "container_project": str(PurePosixPath(remote["container_workspace_root"]) / name),
              "container": remote["container"], "models": models["roles"], "checks": {}}
    if not args.resolve_only:
        ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", f"ConnectTimeout={remote['connect_timeout_seconds']}",
               "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1",
               remote["ssh_host"]]
        timeout = int(remote["command_timeout_seconds"])
        q = shlex.quote
        container = q(remote["container"])
        command = "set -e; hostname; docker inspect --format " + q(
            "state={{.State.Status}} image={{.Config.Image}} mounts={{json .Mounts}}")
        command += f" {container}; docker exec {container} sh -lc " + q(
            "set -e; " + "; ".join([
                f"{q(eda['python'])} --version", f"{q(eda['verilator'])} --version",
                f"{q(eda['yosys'])} -V", f"{q(eda['openroad'])} -version",
                f"test -r {q(eda['liberty'])}", "echo LIBERTY_FOUND"]))
        result["checks"]["remote_eda"] = invoke(ssh + [command], timeout)
        sta_script = ('if {[llength [info commands report_checks]] == 0} {exit 2}\n'
                      'puts "HARNESS_STA_OK"\nexit\n')
        result["checks"]["sta"] = invoke(ssh + [f"docker exec -i {container} "
            f"{q(eda['openroad'])} -no_init"], timeout, sta_script)
        result["checks"]["sta"]["ok"] = (result["checks"]["sta"]["ok"] and
            "HARNESS_STA_OK" in result["checks"]["sta"].get("stdout", ""))
        executable = shutil.which(models["runtime"]["executable"])
        result["checks"]["omp_cli"] = {"ok": executable is not None, "path": executable}
        if args.models and executable:
            for model in sorted(set(models["roles"].values())):
                probe = invoke([executable, "-p", "--cwd", str(root), "--model", model,
                    "--no-tools", "--no-extensions", "--no-skills", "--no-rules",
                    "--no-session", "--no-title", "--no-lsp", "--max-time", "45",
                    "--system-prompt", "Return exactly the requested marker.",
                    "Return exactly HARNESS_MODEL_OK"], int(models["runtime"]["request_timeout_seconds"]))
                probe["ok"] = probe["ok"] and "HARNESS_MODEL_OK" in probe.get("stdout", "")
                result["checks"][f"model:{model}"] = probe
    result["ok"] = all(c["ok"] for c in result["checks"].values())
    result["scope"] = "config_only" if args.resolve_only else (
        "environment_and_models" if args.models else "environment_only_models_not_probed")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
