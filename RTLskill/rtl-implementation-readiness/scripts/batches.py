"""Finite sequential experiments with an immutable claim; never a resume engine."""
from __future__ import annotations

import math
from pathlib import Path

from comparison import finalize, receipt, verify_finalization
from core import (ReadinessError, digest, file_manifest, label, load_workspace,
                  read_json, safe_relative, save_json, sha, under)
from experiments import TERMINAL, _artifact_hashes, _job_dir, _lock, _request, submit


def _owned_plan(workspace, plan):
    logical = Path(workspace).absolute()
    workspace = logical.resolve()
    path = Path(plan).absolute()
    relative = None
    for root in (logical, workspace):
        try:
            relative = path.relative_to(root)
            break
        except ValueError:
            pass
    if relative is None or not relative.parts:
        raise ReadinessError("Batch plan must be a regular file within its workspace")
    path = under(workspace, relative)
    if not path.is_file() or path.is_symlink():
        raise ReadinessError("Batch plan must be a regular non-symlink file")
    return workspace, path


def _ids(value, field, *, required=False):
    if not isinstance(value, list) or (required and not value):
        raise ReadinessError(f"{field} must be a nonempty job ID array" if required else f"{field} must be a job ID array")
    for item in value:
        _job_dir(Path("."), item)
    if len(set(value)) != len(value):
        raise ReadinessError(f"{field} contains duplicate job IDs")
    return value


def _finalize_options(workspace, raw):
    if not isinstance(raw, dict) or set(raw) - {
        "variant", "baseline_jobs", "candidate_jobs", "verification_jobs", "out",
        "metric", "min_ratio", "min_repeats",
    }:
        raise ReadinessError("Invalid batch finalization options")
    if not isinstance(raw.get("variant"), str):
        raise ReadinessError("Batch finalization needs a variant name")
    variant = label(raw["variant"])
    if variant == "baseline":
        raise ReadinessError("Batch finalization needs a changed variant")
    baseline = _ids(raw.get("baseline_jobs"), "baseline_jobs", required=True)
    candidate = _ids(raw.get("candidate_jobs"), "candidate_jobs", required=True)
    if set(baseline) & set(candidate):
        raise ReadinessError("Baseline and candidate observations must have distinct IDs")
    verification = _ids(raw.get("verification_jobs", []), "verification_jobs")
    if not isinstance(raw.get("out"), str) or raw["out"] in {"", "."}:
        raise ReadinessError("Batch finalization needs a relative output directory")
    out = safe_relative(raw["out"])
    parts = Path(out).parts
    if not parts or parts[0] in {"jobs", "variants", "batches", "workspace.json", ".finalize.lock"}:
        raise ReadinessError("Batch finalization must avoid reserved workspace paths")
    under(workspace, out)
    metric = raw.get("metric", "elapsed_seconds")
    ratio, repeats = raw.get("min_ratio", 1.2), raw.get("min_repeats", 3)
    if (metric not in {"elapsed_seconds", "peak_rss_bytes"}
            or isinstance(ratio, bool) or not isinstance(ratio, (int, float))
            or not math.isfinite(ratio) or ratio <= 1
            or isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1):
        raise ReadinessError("Invalid comparison metric, ratio or repeat count")
    return {"variant": variant, "baseline_jobs": baseline, "candidate_jobs": candidate,
            "verification_jobs": verification, "out": out, "metric": metric,
            "min_ratio": ratio, "min_repeats": repeats}


def _prepare(workspace, plan, batch_id, tool_seconds):
    workspace, plan = _owned_plan(workspace, plan)
    label(batch_id)
    if isinstance(tool_seconds, bool) or not isinstance(tool_seconds, int) or not 0 < tool_seconds <= 86400:
        raise ReadinessError("tool_seconds must be an integer in 1..86400")
    ws = load_workspace(workspace)
    raw = read_json(plan)
    if set(raw) - {"jobs", "finalize"}:
        raise ReadinessError("Unknown batch plan fields")
    jobs = raw.get("jobs")
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= 64:
        raise ReadinessError("Batch jobs must contain 1..64 finite experiments")
    frozen, names = [], set()
    for item in jobs:
        if not isinstance(item, dict) or set(item) - {"job_id", "variant", "config", "reuse"}:
            raise ReadinessError("Invalid batch job")
        name = item.get("job_id")
        _job_dir(workspace, name)
        if name in names:
            raise ReadinessError("Batch job IDs must be distinct")
        names.add(name)
        reuse = item.get("reuse", False)
        if not isinstance(reuse, bool):
            raise ReadinessError("reuse must be an explicit boolean")
        if not isinstance(item.get("variant"), str):
            raise ReadinessError("Batch job needs a variant name")
        request = _request(workspace, item["variant"], item.get("config"))
        frozen.append({"job_id": name, "reuse": reuse, "request": request})
    admitted = sum(item["request"]["config"]["timeout_seconds"] for item in frozen if not item["reuse"])
    if admitted > tool_seconds:
        raise ReadinessError("Configured new-job timeouts exceed the batch tool-seconds budget")
    finalization = _finalize_options(workspace, raw["finalize"]) if "finalize" in raw else None
    value = {"kind": "readiness_batch_v1", "workspace": str(workspace), "plan": str(plan),
             "plan_sha256": sha(plan), "batch_id": batch_id, "tool_seconds": tool_seconds,
             "workspace_id": ws["workspace_id"], "baseline_identity": ws["baseline_identity"],
             "jobs": frozen, "finalize": finalization}
    value["batch_identity"] = digest(value)
    return workspace, value


def _directory(workspace, batch_id):
    return under(workspace, Path("batches") / label(batch_id))


def _terminal(workspace, job_id, expected=None):
    """Bind even failed receipts without querying processes or repairing status."""
    job = _job_dir(workspace, job_id)
    request, result = read_json(job / "request.json"), read_json(job / "result.json")
    unsigned = {key: value for key, value in request.items() if key not in {"job_id", "request_identity"}}
    if (request.get("job_id") != job_id or request.get("workspace_root") != str(workspace)
            or digest(unsigned) != request.get("request_identity")
            or (expected is not None and request != expected | {"job_id": job_id})):
        raise ReadinessError(f"Batch job request changed or conflicts: {job_id}")
    current = _request(workspace, request["variant"], request["config"])
    if current != {key: value for key, value in request.items() if key != "job_id"}:
        raise ReadinessError(f"Batch job source, tool or configuration changed: {job_id}")
    for key in ("request_identity", "workspace_id", "variant_identity", "baseline_identity", "measurement_key"):
        if result.get(key) != request.get(key):
            raise ReadinessError(f"Batch result binding changed for {job_id}: {key}")
    if result.get("status") not in TERMINAL:
        raise ReadinessError(f"Batch job has no terminal result: {job_id}")
    if _artifact_hashes(job) != result.get("artifact_hashes"):
        raise ReadinessError(f"Batch job artifacts changed: {job_id}")
    trees = {name: file_manifest(job / "work" / name) if (job / "work" / name).exists() else None
             for name in ("tree", "baseline_tree")}
    if result["status"] == "completed":
        receipt(workspace, job_id)
    elif result.get("source_integrity_passed"):
        if trees["tree"] != request["variant_files"] or (
                request.get("baseline_identity") and trees["baseline_tree"] != request["baseline_files"]):
            raise ReadinessError(f"Failed experiment source copy changed: {job_id}")
    return {"job_id": job_id, "status": result["status"], "reason": result.get("reason"),
            "request_identity": request["request_identity"], "elapsed_seconds": result.get("elapsed_seconds"),
            "evidence_identity": digest({"result": result, "trees": trees})}


def _observed(workspace, item):
    job = _job_dir(workspace, item["job_id"])
    if (job / "result.json").is_file():
        return _terminal(workspace, item["job_id"], item["request"])
    if job.exists():
        request = read_json(job / "request.json")
        if request != item["request"] | {"job_id": item["job_id"]}:
            raise ReadinessError("Existing unfinished batch job has conflicting request")
        return {"job_id": item["job_id"], "status": "pending", "request_identity": request["request_identity"]}
    return {"job_id": item["job_id"], "status": "not_started"}


def _finalization(workspace, options):
    destination = under(workspace, options["out"])
    finalize(workspace, options["variant"], options["baseline_jobs"], options["candidate_jobs"],
             options["verification_jobs"], destination=destination, metric=options["metric"],
             min_ratio=options["min_ratio"], min_repeats=options["min_repeats"])
    return verify_finalization(workspace, destination)


def verify_batch(workspace, *, plan, batch_id, tool_seconds):
    """Pure observation of this exact claim and fresh validation of its evidence."""
    workspace, expected = _prepare(workspace, plan, batch_id, tool_seconds)
    directory = _directory(workspace, batch_id)
    request = read_json(directory / "request.json")
    if request != expected:
        raise ReadinessError("Existing batch belongs to a different invocation or changed plan")
    observed = [_observed(workspace, item) for item in request["jobs"]]
    base = {key: request[key] for key in ("workspace", "plan", "batch_id", "tool_seconds", "batch_identity")}
    if not (directory / "result.json").is_file():
        return base | {"status": "incomplete", "reason": "claimed_batch_not_finished_never_resume",
                       "jobs": observed, "finalization": None}
    result = read_json(directory / "result.json")
    if (result.get("identity") != digest({key: value for key, value in result.items() if key != "identity"})
            or any(result.get(key) != value for key, value in base.items())
            or result.get("jobs") != observed):
        raise ReadinessError("Batch result or terminal evidence changed")
    if result.get("finalization") is not None:
        options = request["finalize"]
        if not options or any(job["status"] in {"pending", "not_started"} for job in observed):
            raise ReadinessError("Partial batch cannot certify a finalization")
        checked = verify_finalization(workspace, under(workspace, options["out"]))
        report = read_json(Path(checked["comparison"]))
        if (checked != result["finalization"]
                or any(report.get(key) != value for key, value in options.items() if key != "out")):
            raise ReadinessError("Batch finalization differs from its frozen plan")
    return result


def batch(workspace, *, plan, batch_id, tool_seconds):
    workspace, request = _prepare(workspace, plan, batch_id, tool_seconds)
    directory = _directory(workspace, batch_id)
    if directory.exists():
        return verify_batch(workspace, plan=plan, batch_id=batch_id, tool_seconds=tool_seconds)
    # Nonblocking claim: a second caller never waits for or resumes this work.
    with _lock(under(workspace, "batches/.claim.lock")):
        if directory.exists():
            return verify_batch(workspace, plan=plan, batch_id=batch_id, tool_seconds=tool_seconds)
        for item in request["jobs"]:
            exists = _job_dir(workspace, item["job_id"]).exists()
            if item["reuse"]:
                if not exists or _terminal(workspace, item["job_id"], item["request"])["status"] != "completed":
                    raise ReadinessError("Explicit reuse needs a successful exact existing job")
            elif exists:
                raise ReadinessError("Existing job ID requires explicit reuse:true; never resume or replace it")
        directory.mkdir()
        save_json(directory / "request.json", request)
        elapsed = 0.0
        reason, status, finalization, error = "all_planned_jobs_completed", "completed", None, None
        for item in request["jobs"]:
            if item["reuse"]:
                continue
            config = item["request"]["config"]
            if config["timeout_seconds"] > tool_seconds - elapsed:
                reason, status = "remaining_tool_budget_cannot_admit_next_timeout", "incomplete"
                break
            try:
                submit(workspace, variant=item["request"]["variant"], config=config,
                       job_id=item["job_id"], background=False, require_new=True)
                outcome = _terminal(workspace, item["job_id"], item["request"])
                elapsed += outcome["elapsed_seconds"] or 0.0
                if outcome["status"] != "completed":
                    reason, status = "experiment_failed_stopped_remaining_jobs", "failed"
                    break
            except (ReadinessError, OSError, KeyError) as problem:
                reason, status, error = "experiment_submission_or_validation_failed", "incomplete", str(problem)
                break
        outcomes = [_observed(workspace, item) for item in request["jobs"]]
        all_terminal = all(item["status"] in TERMINAL for item in outcomes)
        if request["finalize"] and all_terminal and error is None:
            try:
                finalization = _finalization(workspace, request["finalize"])
            except (ReadinessError, OSError, KeyError) as problem:
                reason, status, error = "finalization_unavailable", "incomplete", str(problem)
        result = {key: request[key] for key in ("workspace", "plan", "batch_id", "tool_seconds", "batch_identity")}
        result.update(status=status, reason=reason, jobs=outcomes,
                      tool_elapsed_seconds=round(elapsed, 6), finalization=finalization)
        if error:
            result["error"] = error
        result["identity"] = digest(result)
        save_json(directory / "result.json", result)
    return verify_batch(workspace, plan=plan, batch_id=batch_id, tool_seconds=tool_seconds)
