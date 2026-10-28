"""Compare measured, source-bound experiments and export reviewable changes."""
from __future__ import annotations

import difflib
import fcntl
from pathlib import Path
import shutil
import statistics
import tempfile

from core import (ReadinessError, digest, file_manifest, load_variant, load_workspace,
                  read_json, save_json, sha, under, utc_now, variant_build, variant_tree)


def receipt(workspace, job_id, *, require_success=True):
    from experiments import _job_dir
    workspace = Path(workspace).resolve()
    ws = load_workspace(workspace)
    # Comparison consumes terminal receipts only. Observation of an unfinished
    # worker can change status files, so never invoke the live-status path here.
    job = _job_dir(workspace, job_id)
    if (job / "reference.json").exists():
        from receipt_reuse import referenced_receipt
        return referenced_receipt(workspace, job_id)
    req, result = read_json(job / "request.json"), read_json(job / "result.json")
    if req.get("workspace_root") != str(workspace) or req.get("job_id") != job_id:
        raise ReadinessError(f"Job ownership mismatch: {job_id}")
    unsigned = {k: v for k, v in req.items() if k not in ("request_identity", "job_id")}
    if digest(unsigned) != req["request_identity"] or result.get("request_identity") != req["request_identity"]:
        raise ReadinessError(f"Modified or mismatched job request: {job_id}")
    for key in ("workspace_id", "variant_identity", "baseline_identity", "measurement_key"):
        if result.get(key) != req.get(key):
            raise ReadinessError(f"Result binding mismatch for {key}: {job_id}")
    if req["workspace_id"] != ws["workspace_id"] or req["variant_identity"] != load_variant(workspace, req["variant"])["identity"]:
        raise ReadinessError(f"Stale experiment: {job_id}")
    if req.get("baseline_identity") and req["baseline_identity"] != load_variant(workspace, req["baseline"])["identity"]:
        raise ReadinessError(f"Stale paired verification: {job_id}")
    from experiments import _verify_request_builds, _verify_job_builds
    _verify_request_builds(workspace, req)
    _verify_job_builds(req, job)
    if require_success and (result.get("status") != "completed" or result.get("exit_code") != 0 or not result.get("source_integrity_passed")):
        raise ReadinessError(f"Experiment is not successful and intact: {job_id}")
    if not require_success:
        from experiments import TERMINAL
        if result.get("status") not in TERMINAL or not result.get("source_integrity_passed"):
            raise ReadinessError(f"Structural evidence needs an intact terminal receipt: {job_id}")
    artifacts = result.get("artifact_hashes", {})
    if "tool.log" not in artifacts:
        raise ReadinessError(f"Missing artifact receipt: {job_id}")
    for rel, expected in artifacts.items():
        if sha(under(job, rel)) != expected:
            raise ReadinessError(f"Experiment artifact changed: {job_id}/{rel}")
    if not (job / "tool.log").is_file():
        raise ReadinessError(f"Missing raw tool log: {job_id}")
    # Recheck copied sources too: a completed log cannot certify a later edited tree.
    if file_manifest(job / "work/tree") != req["variant_files"]:
        raise ReadinessError(f"Experiment work tree changed: {job_id}")
    if req.get("baseline_identity") and file_manifest(job / "work/baseline_tree") != req["baseline_files"]:
        raise ReadinessError(f"Experiment baseline tree changed: {job_id}")
    if ((req["include_dirs"] or (req.get("baseline_build") or {}).get("include_dirs"))
            and req["config"]["kind"] in ("yosys", "equiv")):
        from experiments import _verify_include_aliases
        aliases = read_json(job / "include-aliases.json")
        if aliases.get("request_identity") != req["request_identity"]:
            raise ReadinessError(f"Include alias receipt mismatch: {job_id}")
        _verify_include_aliases(job, aliases["aliases"])
    return req, result


STRUCTURE_LIMIT = "Structural evidence covers only its recorded checks and scope; whole-design synthesis readiness and acceptance remain unestablished."


def _health_reference(workspace, health):
    if health is None:
        return None
    logical_workspace = Path(workspace).absolute()
    workspace, path = logical_workspace.resolve(), Path(health).absolute()
    relative = next((path.relative_to(root) for root in (logical_workspace, workspace)
                     if path.is_relative_to(root)), None)
    if relative is None or not relative.parts:
        raise ReadinessError("Structural report must stay inside this workspace")
    path = under(workspace, relative)
    return {"path": str(path), "sha256": sha(path)}


def _structural_health(workspace, variant, health):
    reference = _health_reference(workspace, health)
    if reference:
        from health import verify_report
        report = read_json(reference["path"])
        verify_report(workspace, variant, report)
        if sha(reference["path"]) != reference["sha256"]:
            raise ReadinessError("Structural report changed during validation")
        return reference, report
    return None, {"summary": {"baseline_state": "unknown", "candidate_state": "unknown",
                               "candidate_new_count": 0, "unresolved_count": None,
                               "whole_design_readiness": "not_established"},
                  "limits": ["No structural report supplied; absence of evidence does not establish clean RTL."]}


def compare(workspace, variant, baseline_jobs, candidate_jobs, verification_jobs=(), *, metric="elapsed_seconds", min_ratio=1.2, min_repeats=3, health=None):
    ws = load_workspace(workspace)
    candidate = load_variant(workspace, variant)
    if variant == "baseline" or not baseline_jobs or not candidate_jobs:
        raise ReadinessError("Comparison requires a changed variant and baseline/candidate measurements")
    if metric not in ("elapsed_seconds", "peak_rss_bytes") or min_ratio <= 1 or min_repeats < 1:
        raise ReadinessError("Choose wall time or peak RSS, an improvement ratio > 1 and positive repeat count")
    if len(set([*baseline_jobs, *candidate_jobs])) != len(baseline_jobs) + len(candidate_jobs):
        raise ReadinessError("Repeated IDs are one observation, not repeated experiments")
    measures = [receipt(workspace, j) for j in [*baseline_jobs, *candidate_jobs]]
    keys = {req["measurement_key"] for req, _ in measures}
    if len(keys) != 1:
        raise ReadinessError("Measurements differ in scope, tool, configuration or resource budget")
    for i, (req, _) in enumerate(measures):
        expected = ws["baseline_identity"] if i < len(baseline_jobs) else candidate["identity"]
        if req["variant_identity"] != expected:
            raise ReadinessError("Measurement job belongs to a different candidate")
        if req["config"]["kind"] == "equiv" or (req["config"]["kind"] == "command" and req["config"].get("purpose") != "measurement"):
            raise ReadinessError("Verification runtime is not a synthesis cost measurement")
    values = [r.get(metric) for _, r in measures]
    if any(not isinstance(x, (int, float)) or isinstance(x, bool) or x <= 0 for x in values):
        raise ReadinessError(f"Missing positive measured {metric}")
    n = len(baseline_jobs)
    b, c = values[:n], values[n:]
    ratio = statistics.median(b) / statistics.median(c)
    verifications, failures = [], []
    for j in verification_jobs:
        try:
            req, result = receipt(workspace, j)
            if (req["variant_identity"] != candidate["identity"] or req.get("baseline_identity") != ws["baseline_identity"]
                    or not result.get("verification_passed")):
                raise ReadinessError("Verification does not bind and pass this baseline/candidate pair")
            if req["config"]["kind"] not in ("equiv", "command") or (req["config"]["kind"] == "command" and req["config"].get("purpose") not in ("regression", "equivalence")):
                raise ReadinessError("Verification purpose is not functional")
            # Re-read actual proof artifacts: result flags are summaries, not the
            # underlying verification. This catches accidental flag reuse/edits.
            # receipt() validates ownership and any direct reference. Read proof
            # markers and built-in coverage at that verified actual origin.
            job = under(Path(req["workspace_root"]), Path("jobs") / j)
            if req["config"]["kind"] == "command":
                markers = req["config"].get("success_markers", [])
                log = (job / "tool.log").read_text(encoding="utf-8", errors="replace")
                if not markers or any(marker not in log for marker in markers):
                    raise ReadinessError("Functional command has no actual successful marker evidence")
            else:
                from experiments import _equiv_coverage, _has_blackboxes
                if ("Equivalence successfully proven!" not in (job / "tool.log").read_text(encoding="utf-8", errors="replace")
                        or not _equiv_coverage(job, req["top"])["passed"]
                        or _has_blackboxes(job / "gold-design.json") or _has_blackboxes(job / "gate-design.json")):
                    raise ReadinessError("Functional equivalence has no valid actual proof coverage")
            # Same macro and parameter settings for built-in formal and measured Yosys.
            measure_req = measures[0][0]
            if req["config"]["kind"] == "equiv" and measure_req["config"]["kind"] == "yosys":
                for key in ("defines", "parameters"):
                    if req["config"].get(key) != measure_req["config"].get(key):
                        raise ReadinessError("Formal/measurement macro or parameter configuration differs")
            verifications.append({"job_id": j, "kind": req["config"]["kind"], "scope": result.get("proof_scope", req["config"].get("purpose"))})
        except ReadinessError as error:
            failures.append({"job_id": j, "reason": str(error)})
    enough = len(b) >= min_repeats and len(c) >= min_repeats
    # Median improvement alone is too weak when the repeated measurements overlap.
    separated = max(c) < min(b)
    benefit = ratio >= min_ratio and separated
    if failures:
        decision, reason = "keep_baseline", "provided_functional_verification_invalid_or_failed"
    elif not verifications:
        decision, reason = "evidence_incomplete", "functional_verification_missing"
    elif not benefit:
        decision, reason = "keep_baseline", "no_clear_measured_improvement"
    elif not enough:
        decision, reason = "evidence_incomplete", "insufficient_repeated_measurements"
    else:
        decision, reason = "candidate_for_integration", "local_benefit_with_scoped_functional_evidence"
    health_reference, structural = _structural_health(workspace, variant, health)
    if structural["summary"]["candidate_new_count"] and decision == "candidate_for_integration":
        decision, reason = "evidence_incomplete", "candidate_introduced_structural_issues"
    result = {"api_version": 2, "created_utc": utc_now(), "workspace_id": ws["workspace_id"],
              "baseline_identity": ws["baseline_identity"], "variant": variant, "variant_identity": candidate["identity"],
              "decision": decision, "reason": reason, "metric": metric, "baseline_jobs": list(baseline_jobs),
              "candidate_jobs": list(candidate_jobs), "verification_jobs": list(verification_jobs),
              "valid_verification": verifications, "invalid_verification": failures,
              "baseline_values": b, "candidate_values": c, "median_ratio": ratio,
              "min_ratio": min_ratio, "min_repeats": min_repeats, "ranges_separated": separated,
              "health_report": health_reference, "structural_health": structural,
              "limits": ["Only the measured top, inputs, tool, configuration and stage were exercised.",
                         "Local benefit is not proof of full-design speed, mapped area, STA or physical acceptance.",
                         "Inspect resource statistics and timing consequences before integrating a sharing change.",
                         STRUCTURE_LIMIT]}
    if ws["api_version"] == 3:
        result.update(api_version=3, build=variant_build(ws, candidate),
                      baseline_build=variant_build(ws, load_variant(workspace, "baseline")))
    if any(v["kind"] == "command" for v in verifications):
        result["limits"].append("Command verification covers its actual program/stimuli; inspect its adequacy before integration.")
    if any(v["kind"] == "equiv" for v in verifications):
        result["limits"].append("Formal evidence applies to the recorded Yosys equivalence model and configuration.")
    result["identity"] = digest(result)
    return result


def _revalidate(workspace, comparison):
    unsigned = {k: v for k, v in comparison.items() if k != "identity"}
    if digest(unsigned) != comparison.get("identity"):
        raise ReadinessError("Comparison report changed")
    # Re-evaluate the receipts rather than accepting a caller's decision field.
    health_reference = comparison.get("health_report")
    if health_reference and sha(health_reference["path"]) != health_reference["sha256"]:
        raise ReadinessError("Structural report changed after comparison")
    fresh = compare(workspace, comparison["variant"], comparison["baseline_jobs"], comparison["candidate_jobs"],
                    comparison["verification_jobs"], metric=comparison["metric"], min_ratio=comparison["min_ratio"], min_repeats=comparison["min_repeats"],
                    health=health_reference["path"] if health_reference else None)
    if "structural_health" not in comparison:
        # Historical bundles retain their original bytes and decisions. A fresh
        # consumer still receives unknown structural status from verify().
        fresh.pop("structural_health", None)
        fresh.pop("health_report", None)
        if STRUCTURE_LIMIT in fresh.get("limits", []):
            fresh["limits"].remove(STRUCTURE_LIMIT)
    stable = lambda value: {k: v for k, v in value.items() if k not in ("identity", "created_utc")}
    if stable(fresh) != stable(comparison):
        raise ReadinessError("Comparison is stale")
    return comparison


def _git_patch_path(path):
    # Git quotes path bytes with C escapes, including octal UTF-8 bytes. JSON's
    # Unicode/control escapes are not Git path syntax.
    return '"' + "".join('\\"' if byte == 34 else '\\\\' if byte == 92
                         else f"\\{byte:03o}" if byte < 32 or byte >= 127 else chr(byte)
                         for byte in path.encode("utf-8")) + '"'


def _patch(workspace, candidate):
    baseline, changed = variant_tree(workspace, "baseline"), variant_tree(workspace, candidate["name"])
    patch = []
    for rel in candidate["changes"]:
        old, new = under(baseline, rel), under(changed, rel)
        a = old.read_text(encoding="utf-8").splitlines(keepends=True) if old.is_file() else []
        b = new.read_text(encoding="utf-8").splitlines(keepends=True) if new.is_file() else []
        if old.is_file() != new.is_file() and not a and not b:
            # An empty file has no unified-diff hunk; preserve its existence
            # change explicitly. The blank line ends these extended headers
            # before the next historical plain unified diff, if any.
            patch.append(f"diff --git {_git_patch_path('a/' + rel)} {_git_patch_path('b/' + rel)}\n")
            patch.append(("new" if new.is_file() else "deleted") + " file mode 100644\n\n")
            continue
        for line in difflib.unified_diff(a, b, fromfile="a/" + rel if old.is_file() else "/dev/null",
                                        tofile="b/" + rel if new.is_file() else "/dev/null"):
            patch.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(patch)


def _regional_delivery(ws, candidate):
    if ws["api_version"] != 3:
        return {}
    binding = {"build": variant_build(ws, candidate),
               "baseline_build": {key: ws[key] for key in ("top", "sources", "include_dirs")},
               "files": candidate["files"], "baseline_files": ws["files"],
               "operations": candidate["operations"],
               "region_roots": ws["region_roots"], "build_files": ws["build_files"],
               "frozen_assets": ws["frozen_assets"]}
    return {"api_version": 3, **binding, "manifest_identity": digest(binding)}


def export(workspace, comparison, destination):
    comparison = _revalidate(workspace, comparison)
    out = Path(destination).resolve()
    workspace = Path(workspace).resolve()
    if (out.exists() or not out.is_relative_to(workspace)
            or out.relative_to(workspace).parts[0] in ("variants", "jobs", "workspace.json")):
        raise ReadinessError("Export must use a new directory inside this experiment workspace")
    candidate = load_variant(workspace, comparison["variant"])
    ws = load_workspace(workspace)
    changed = variant_tree(workspace, comparison["variant"])
    out.mkdir(parents=True)
    (out / "candidate.patch").write_text(_patch(workspace, candidate), encoding="utf-8")
    save_json(out / "comparison.json", comparison)
    save_json(out / "candidate.json", candidate)
    save_json(out / "delivery.json", {"api_version": 2, "workspace_id": comparison["workspace_id"],
              "baseline_identity": comparison["baseline_identity"], "variant_identity": candidate["identity"],
              "decision": comparison["decision"], "patch_sha256": sha(out / "candidate.patch"),
              "candidate_tree": str(changed), "evidence_root": str(workspace / "jobs"),
              "status": "scoped_candidate" if comparison["decision"] == "candidate_for_integration" else "proposal_only_keep_original",
              "note": "No source was applied. Integrate only after assessing verification scope and full-design consequences.",
              **_regional_delivery(ws, candidate),
              **({"structural_health": comparison["structural_health"], "health_report": comparison.get("health_report")}
                 if "structural_health" in comparison else {})})
    return read_json(out / "delivery.json")


def verify(workspace, comparison_path, delivery_path):
    """Freshly check exported evidence for a host; never apply source changes."""
    workspace = Path(workspace).resolve()
    comparison_path, delivery_path = Path(comparison_path).resolve(), Path(delivery_path).resolve()
    for path in (comparison_path, delivery_path):
        if not path.is_relative_to(workspace) or path == workspace:
            raise ReadinessError("Comparison and delivery must stay inside the workspace")
        under(workspace, path.relative_to(workspace))
    file_manifest(delivery_path)  # also reject links in exported metadata/artifacts
    report = _revalidate(workspace, read_json(comparison_path))
    if read_json(delivery_path / "comparison.json") != report:
        raise ReadinessError("Export comparison differs from the selected comparison")
    ws = load_workspace(workspace)
    load_variant(workspace, "baseline")
    candidate = load_variant(workspace, report["variant"])
    if read_json(delivery_path / "candidate.json") != candidate:
        raise ReadinessError("Export candidate metadata changed")
    if (delivery_path / "candidate.patch").read_text(encoding="utf-8") != _patch(workspace, candidate):
        raise ReadinessError("Export patch differs from the sealed sources")
    delivery = read_json(delivery_path / "delivery.json")
    expected = {"api_version": 2, "workspace_id": ws["workspace_id"],
                "baseline_identity": ws["baseline_identity"], "variant_identity": candidate["identity"],
                "decision": report["decision"], "patch_sha256": sha(delivery_path / "candidate.patch"),
                "candidate_tree": str(variant_tree(workspace, report["variant"])),
                "evidence_root": str(workspace / "jobs"),
                "status": "scoped_candidate" if report["decision"] == "candidate_for_integration" else "proposal_only_keep_original",
                **_regional_delivery(ws, candidate),
                **({"structural_health": report["structural_health"], "health_report": report.get("health_report")}
                   if "structural_health" in report else {})}
    if any(delivery.get(k) != v for k, v in expected.items()):
        raise ReadinessError("Export delivery binding changed")
    return {"decision": report["decision"], "comparison_identity": report["identity"],
            "workspace": str(workspace), "workspace_id": ws["workspace_id"],
            "baseline_identity": ws["baseline_identity"], "variant_identity": candidate["identity"],
            "baseline_files": ws["files"], "files": candidate["files"],
            "allowed_paths": ws["allowed_paths"], "changes": candidate["changes"],
            "candidate_tree": expected["candidate_tree"], "delivery": str(delivery_path),
            "structural_health": report.get("structural_health", _structural_health(workspace, report["variant"], None)[1]),
            "health_report": report.get("health_report"),
            **_regional_delivery(ws, candidate)}


FINALIZATION_FILES = {"comparison.json", "candidate.json", "candidate.patch", "delivery.json", "finalization.json"}


def _finalization_destination(workspace, destination):
    logical_workspace = Path(workspace).absolute()
    workspace = logical_workspace.resolve()
    path = Path(destination).absolute()
    # Preserve checks on aliases below W, while accepting an OS alias of W
    # itself (for example /var versus /private/var on macOS).
    relative = None
    for root in (logical_workspace, workspace):
        try:
            relative = path.relative_to(root)
            break
        except ValueError:
            pass
    if relative is None:
        raise ReadinessError("Finalization must stay inside its experiment workspace") from None
    if not relative.parts or relative.parts[0] in ("variants", "jobs", "batches", "workspace.json", ".finalize.lock"):
        raise ReadinessError("Finalization needs a directory inside the workspace, outside variants/jobs/batches")
    return workspace, under(workspace, relative)


def _finalization_record(workspace, destination, report, *, limits=True):
    value = {"kind": "readiness_finalization_v1", "decision": report["decision"], "reason": report["reason"],
             "workspace": str(workspace), "comparison": str(destination / "comparison.json"),
             "delivery": str(destination), "workspace_id": report["workspace_id"],
             "baseline_identity": report["baseline_identity"], "variant_identity": report["variant_identity"],
             "comparison_identity": report["identity"]}
    if limits and "limits" in report:
        value["limits"] = report["limits"]
    if "structural_health" in report:
        value["structural_health"] = report["structural_health"]["summary"]
        value["health_report"] = report.get("health_report")
    value["identity"] = digest(value)
    return value


def _verify_finalization(workspace, stored, published):
    if (not stored.is_dir() or {p.name for p in stored.iterdir()} != FINALIZATION_FILES
            or any(p.is_symlink() or not p.is_file() for p in stored.iterdir())):
        raise ReadinessError("Finalization bundle is incomplete or contains unexpected files")
    record = read_json(stored / "finalization.json")
    report = read_json(stored / "comparison.json")
    expected = _finalization_record(workspace, published, report, limits="limits" in record)
    if record != expected:
        raise ReadinessError("Finalization record or its source/decision/path binding changed")
    checked = verify(workspace, stored / "comparison.json", stored)
    return checked | {"reason": report["reason"], "comparison": str(published / "comparison.json"),
                      "delivery": str(published), "finalization_identity": record["identity"],
                      "finalization_path": str(published / "finalization.json")}


def verify_finalization(workspace, delivery):
    """Freshly validate a final bundle and terminal evidence; read-only, no EDA."""
    workspace, destination = _finalization_destination(workspace, delivery)
    return _verify_finalization(workspace, destination, destination)


def finalize(workspace, variant, baseline_jobs, candidate_jobs, verification_jobs=(), *, destination,
             metric="elapsed_seconds", min_ratio=1.2, min_repeats=3, health=None):
    """Publish comparison/export/report together, or observe the same valid output."""
    workspace, destination = _finalization_destination(workspace, destination)
    load_workspace(workspace)
    invocation = {"variant": variant, "baseline_jobs": list(baseline_jobs), "candidate_jobs": list(candidate_jobs),
                  "verification_jobs": list(verification_jobs), "metric": metric,
                  "min_ratio": min_ratio, "min_repeats": min_repeats}
    invocation["health_report"] = _health_reference(workspace, health)
    # Serialize publication in this workspace. No target directory is exposed
    # until every output and its actual evidence have passed fresh verification.
    with under(workspace, ".finalize.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if destination.exists():
            verify_finalization(workspace, destination)
            report = read_json(destination / "comparison.json")
            if any(report.get(key) != value for key, value in invocation.items()):
                raise ReadinessError("Existing finalization belongs to a different invocation")
            return read_json(destination / "finalization.json")
        report = compare(workspace, variant, invocation["baseline_jobs"], invocation["candidate_jobs"],
                         invocation["verification_jobs"], metric=metric, min_ratio=min_ratio, min_repeats=min_repeats, health=health)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".finalize-", dir=destination.parent))
        try:
            prepared = temporary / "bundle"
            export(workspace, report, prepared)
            save_json(prepared / "finalization.json", _finalization_record(workspace, destination, report))
            _verify_finalization(workspace, prepared, destination)
            if destination.exists():
                raise ReadinessError("Finalization output appeared during preparation")
            prepared.rename(destination)
            verify_finalization(workspace, destination)
            return read_json(destination / "finalization.json")
        finally:
            shutil.rmtree(temporary)
