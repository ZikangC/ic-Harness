"""Read-only, source-bound structural evidence; never runs an EDA command.

Only the Yosys CHECK transcript is interpreted. A clean result is scoped to that
check, not functional correctness, synthesis completion, or whole-design readiness.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

from core import (ReadinessError, _dependencies, digest, load_variant, load_workspace,
                  read_json, sha, under, utc_now, variant_build, variant_tree)


_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CHECK = re.compile(r"\bExecuting CHECK pass\b")
_FOUND = re.compile(r"^Found (?:and reported )?(\d+) problems?\.$")
_ASSERT = re.compile(r"^ERROR: Found (\d+) problems? in ['`]check -assert['`]\.$")
_LIMITS = [
    "Only the recorded source-bound checker, top, stage, models and configuration were assessed.",
    "Clean scoped evidence is not proof of whole-design synthesis readiness, functional correctness, mapped area, STA or acceptance.",
    "Unknown includes missing, unfinished, unsupported or contradictory evidence; observed findings remain visible.",
    "An absent candidate diagnostic does not establish that a baseline issue was fixed.",
    "created_utc is this assessment/reuse time, not proof that a check occurred before an experiment; available original timestamps are recorded separately.",
]


def _time(path):
    return datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc).isoformat()


def _result_times(result):
    return {key: result[key] for key in ("started_utc", "ended_utc", "completed_utc", "created_utc",
                                       "started_at", "ended_at", "started", "ended")
            if key in result and isinstance(result[key], (str, int, float))}


def _absolute_file(raw, label):
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise ReadinessError(f"{label} needs an absolute file path")
    path = Path(raw)
    if path.is_symlink() or not path.is_file():
        raise ReadinessError(f"{label} needs an existing regular file: {path}")
    return path.resolve()


def _hash_record(raw, label):
    if not isinstance(raw, dict) or set(raw) != {"path", "sha256"} or not _HASH.fullmatch(str(raw.get("sha256", ""))):
        raise ReadinessError(f"{label} needs exactly path and sha256")
    path = _absolute_file(raw["path"], label)
    if sha(path) != raw["sha256"]:
        raise ReadinessError(f"{label} bytes changed: {path}")
    return {"path": str(path), "sha256": raw["sha256"]}


def _has_assert(script):
    # A comment or an incidental string naming the command is not execution.
    # Recognize ordinary Yosys .ys command lines and semicolon-separated commands;
    # deliberately do not evaluate Tcl, shell wrappers or command substitutions.
    with Path(script).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.split("#", 1)[0]
            if re.search(r"(?:^|;)\s*check\s+-assert(?:\s|;|$)", line):
                return True
    return False


def _category(message):
    low = message.lower()
    if "has no driver" in low:
        return "undriven"
    if "multiple conflicting drivers" in low:
        return "multiple_drivers"
    if "logic loop" in low:
        return "logic_loop"
    return "unclassified_check_diagnostic"


def _parse_yosys(log):
    """Stream the log; retain CHECK diagnostics rather than the full transcript.

    The final *started* CHECK must complete. A later truncated check cannot borrow
    an earlier zero count. Full diagnostic text (including continuation lines and
    module context) forms the signature; count alone never identifies a problem.
    """
    last, observed, errors, blocks, active_diag = None, {}, [], 0, None

    def flush():
        nonlocal active_diag
        if active_diag is None:
            return
        text = "\n".join(active_diag.pop("text_lines"))
        finding = {**active_diag, "diagnostic": text, "category": _category(text)}
        finding["signature"] = digest({key: finding[key] for key in ("module", "diagnostic", "category")})
        key = finding["signature"]
        if key in last["findings"]:
            last["findings"][key]["raw_line_ranges"].append([finding["line_start"], finding["line_end"]])
        else:
            finding["raw_line_ranges"] = [[finding.pop("line_start"), finding.pop("line_end")]]
            last["findings"][key] = finding
        observed.setdefault(key, last["findings"][key].copy())
        active_diag = None

    module = None
    with Path(log).open(encoding="utf-8", errors="replace") as handle:
        for number, raw in enumerate(handle, 1):
            line = raw.rstrip("\r\n")
            text = line.strip()
            if _CHECK.search(line):
                flush()
                blocks += 1
                last = {"start_line": number, "end_line": None, "reported_problem_count": None,
                        "assert_failure_count": None, "complete": False, "findings": {},
                        "diagnostic_occurrences": 0, "unparsed_lines": []}
                module = None
                continue
            assertion = _ASSERT.fullmatch(text)
            if text.startswith("ERROR:"):
                flush()
                errors.append({"line": number, "diagnostic": text,
                               "check_assert": bool(assertion)})
                if assertion and last is not None:
                    last["assert_failure_count"] = int(assertion.group(1))
                continue
            if last is None:
                continue
            if text.startswith("Checking module "):
                flush()
                module = text[len("Checking module "):].removesuffix("...")
                continue
            found = _FOUND.fullmatch(text)
            if found:
                flush()
                last.update(end_line=number, reported_problem_count=int(found.group(1)), complete=True)
                continue
            if not last["complete"] and text.startswith("Warning:"):
                flush()
                last["diagnostic_occurrences"] += 1
                active_diag = {"module": module, "text_lines": [text],
                               "line_start": number, "line_end": number}
                continue
            if active_diag is not None:
                if raw[:1].isspace() and text:
                    active_diag["text_lines"].append(text)
                    active_diag["line_end"] = number
                else:
                    flush()
                    if text and not last["complete"]:
                        last["unparsed_lines"].append({"line": number, "diagnostic": text})
            elif text and not last["complete"]:
                last["unparsed_lines"].append({"line": number, "diagnostic": text})
        flush()
    if last:
        last["findings"] = list(last["findings"].values())
    return {"checks_started": blocks, "final_check": last,
            "observed_findings": list(observed.values()), "errors": errors}


def _interpret(log, script, result, supported):
    parsed = _parse_yosys(log) if supported else {"checks_started": 0, "final_check": None,
                                                "observed_findings": [], "errors": []}
    limits, state = [], "unknown"
    check = parsed["final_check"]
    asserted = _has_assert(script) if script else False
    status = result.get("status")
    code = result.get("exit_code")
    valid_code = isinstance(code, int) and not isinstance(code, bool)
    if not supported:
        limits.append("This tool/stage has no supported automatic structural interpretation.")
    elif not asserted:
        limits.append("The pinned script does not contain a recognized executable check -assert command.")
    elif status in ("timeout", "incomplete", "running", "submitted") or not valid_code:
        limits.append("The run did not supply a usable terminal exit result.")
    elif status not in (None, "completed", "failed"):
        limits.append("The result status is not a supported terminal state.")
    elif not check or not check["complete"]:
        limits.append("The final started CHECK pass did not supply a complete problem summary.")
    elif any(not error["check_assert"] for error in parsed["errors"]):
        limits.append("The log also contains a non-CHECK failure; it cannot establish a final structural result.")
    else:
        count = check["reported_problem_count"]
        fatal = check["assert_failure_count"]
        inconsistent = (fatal is not None and fatal != count) or (count == 0 and check["diagnostic_occurrences"] > 0)
        if inconsistent:
            limits.append("The final CHECK diagnostics, summary or assert-failure count contradict one another.")
        elif check["unparsed_lines"]:
            limits.append("The final CHECK contains unparsed transcript lines; no clean scoped conclusion is available.")
        elif count > 0:
            if code == 0:
                limits.append("A positive CHECK count contradicts successful execution of check -assert.")
            else:
                state = "issues"
        elif code == 0 and not parsed["errors"]:
            state = "clean_scoped"
        else:
            limits.append("The zero-count CHECK is not accompanied by successful execution.")
    # Complete diagnostic accounting is needed for comparison/attribution. An
    # observed problem is still useful when Yosys emitted a format we cannot parse.
    comparable = bool(state in ("clean_scoped", "issues") and check
                      and check["reported_problem_count"] == check["diagnostic_occurrences"]
                      and all(f["category"] != "unclassified_check_diagnostic" for f in check["findings"]))
    if state == "issues" and not comparable:
        limits.append("Problem count or diagnostic format is not fully parsed; issue attribution is unavailable.")
    return {"state": state, "findings": (check["findings"] if check and check["complete"] else parsed["observed_findings"]),
            "observed_findings": parsed["observed_findings"], "check": check,
            "checks_started": parsed["checks_started"], "errors": parsed["errors"],
            "script_has_check_assert": asserted, "comparable": comparable, "limits": limits}


def _job(workspace, name, expected_identity):
    from comparison import receipt
    request, result = receipt(workspace, name, require_success=False)
    if request["variant_identity"] != expected_identity:
        raise ReadinessError(f"Structural receipt belongs to another sealed variant: {name}")
    job = under(Path(request["workspace_root"]), Path("jobs") / name)
    config = request["config"]
    supported = config.get("kind") == "yosys" and config.get("stage") in ("proc", "generic")
    script = job / "experiment.ys"
    artifacts = {"log": {"path": str(job / "tool.log"), "sha256": sha(job / "tool.log")},
                 "result": {"path": str(job / "result.json"), "sha256": sha(job / "result.json")},
                 "request": {"path": str(job / "request.json"), "sha256": sha(job / "request.json")}}
    if supported:
        if "experiment.ys" not in result.get("artifact_hashes", {}) or not script.is_file():
            raise ReadinessError(f"Structural receipt lacks its pinned actual Yosys script: {name}")
        artifacts["script"] = {"path": str(script), "sha256": sha(script)}
    checker = {"scope": "sealed_build", "pass": config.get("stage"),
               "macros": config.get("defines", []), "models": {},
               "config": {"kind": config.get("kind"), "parameters": config.get("parameters", {})}}
    report = {"origin": "job", "job_id": name, "source_identity": expected_identity,
              "build": {key: request[key] for key in ("top", "sources", "include_dirs")},
              "tool": request["tool"], "checker": checker,
              "checker_key": digest({"tool": request["tool"], "checker": checker, "top": request["top"]}),
              "artifacts": artifacts, "evidence_times": {"result": _result_times(result),
                  "log_mtime_utc": _time(job / "tool.log"), "result_mtime_utc": _time(job / "result.json")},
              "result": {key: result.get(key) for key in ("status", "exit_code")},
              **_interpret(job / "tool.log", script if supported else None, result, supported)}
    # Do not let concurrent historical-artifact edits pass a single hash snapshot.
    if any(sha(item["path"]) != item["sha256"] for item in artifacts.values()):
        raise ReadinessError(f"Structural receipt changed during assessment: {name}")
    return report


def _external(workspace, variant, raw):
    path = _absolute_file(str(raw), "Structural evidence descriptor")
    descriptor_sha = sha(path)
    descriptor = read_json(path)
    if descriptor.get("kind") != "readiness_structure_evidence_v1":
        raise ReadinessError("Unsupported structural evidence descriptor kind")
    ws, selected = load_workspace(workspace), load_variant(workspace, variant)
    build = variant_build(ws, selected)
    if descriptor.get("build") != build:
        raise ReadinessError("Structural evidence build differs from the sealed ordered build")
    root_raw = descriptor.get("source_root")
    if not isinstance(root_raw, str) or not Path(root_raw).is_absolute() or not Path(root_raw).is_dir() or Path(root_raw).is_symlink():
        raise ReadinessError("Structural evidence needs an absolute, regular source_root directory")
    root = Path(root_raw).resolve()
    listed = descriptor.get("source_files")
    if not isinstance(listed, dict) or not listed:
        raise ReadinessError("Structural evidence requires source_files relative path/SHA pairs")
    required, _ = _dependencies(variant_tree(workspace, variant), build["sources"], build["include_dirs"])
    if not required.issubset(listed):
        raise ReadinessError("Structural evidence does not cover the sealed compiled dependency closure")
    for rel, expected in listed.items():
        if not _HASH.fullmatch(str(expected)) or sha(under(root, rel)) != expected:
            raise ReadinessError(f"Structural evidence source bytes changed: {rel}")
        if rel in required and selected["files"][rel]["sha256"] != expected:
            raise ReadinessError(f"Structural evidence source differs from the sealed variant: {rel}")
    tool = _hash_record(descriptor.get("tool"), "Structural evidence tool")
    checker = descriptor.get("checker")
    if (not isinstance(checker, dict) or not {"scope", "pass", "macros", "models", "config"}.issubset(checker)
            or not isinstance(checker["scope"], str) or not checker["scope"]
            or not isinstance(checker["pass"], str) or not checker["pass"]
            or any(not isinstance(checker[key], dict) for key in ("macros", "models", "config"))):
        raise ReadinessError("Structural checker needs scope, pass, macros, models and config identities")
    raw_artifacts = descriptor.get("artifacts")
    if not isinstance(raw_artifacts, dict) or set(raw_artifacts) != {"script", "log", "result"}:
        raise ReadinessError("Structural evidence needs exactly script/log/result artifact pins")
    artifacts = {key: _hash_record(raw_artifacts[key], "Structural " + key) for key in raw_artifacts}
    result = read_json(artifacts["result"]["path"])
    supported = descriptor.get("tool_kind") == "yosys"
    report = {"origin": "external", "descriptor": {"path": str(path), "sha256": descriptor_sha},
              "source_identity": selected["identity"], "source_root": str(root), "source_files": listed,
              "build": build, "tool": tool, "checker": checker,
              "checker_key": digest({"tool": tool, "checker": checker, "top": build["top"]}),
              "applicability": "Caller-declared checker/model/config applicability; file hashes alone do not prove the declaration.",
              "artifacts": artifacts, "result": {key: result.get(key) for key in ("status", "exit_code")},
              "evidence_times": {"result": _result_times(result), "log_mtime_utc": _time(artifacts["log"]["path"]),
                                 "result_mtime_utc": _time(artifacts["result"]["path"])},
              **_interpret(artifacts["log"]["path"], artifacts["script"]["path"], result, supported)}
    if sha(path) != descriptor_sha or any(sha(item["path"]) != item["sha256"] for item in artifacts.values()):
        raise ReadinessError("Structural evidence changed during assessment")
    return report


def _side_state(records):
    if not records:
        return "unknown"
    if any(record["state"] == "issues" for record in records):
        return "issues"
    return "clean_scoped" if all(record["state"] == "clean_scoped" for record in records) else "unknown"


def _attribution(baseline, candidate):
    findings = []
    for side, records in (("baseline", baseline), ("candidate", candidate)):
        for index, record in enumerate(records):
            for finding in record["findings"]:
                relation, matches = "unknown", []
                if side == "candidate" and record["comparable"]:
                    peers = [peer for peer in baseline if peer["checker_key"] == record["checker_key"]]
                    signature = finding["signature"]
                    for peer_index, peer in enumerate(baseline):
                        if peer in peers and peer["comparable"] and any(f["signature"] == signature for f in peer["findings"]):
                            matches.append(peer_index)
                    if matches:
                        relation = "baseline_existing"
                    elif peers and all(peer["comparable"] and peer["state"] == "clean_scoped" for peer in peers):
                        relation = "candidate_new"
                findings.append({"side": side, "evidence_index": index, "signature": finding["signature"],
                                 "category": finding["category"], "diagnostic": finding["diagnostic"],
                                 "module": finding["module"], "attribution": relation,
                                 "matching_baseline_evidence": matches})
    return findings


def assess(workspace, variant, baseline_jobs=(), candidate_jobs=(), baseline_evidence=(), candidate_evidence=()):
    """Inspect existing evidence without changing files, running tools or scheduling jobs."""
    workspace = Path(workspace).resolve()
    ws, baseline = load_workspace(workspace), load_variant(workspace, "baseline")
    selected = load_variant(workspace, variant)
    inputs = {"baseline_jobs": list(baseline_jobs), "candidate_jobs": list(candidate_jobs),
              "baseline_evidence": [str(Path(p).absolute()) for p in baseline_evidence],
              "candidate_evidence": [str(Path(p).absolute()) for p in candidate_evidence]}
    if variant == "baseline" and (inputs["candidate_jobs"] or inputs["candidate_evidence"]):
        raise ReadinessError("Baseline-only assessment cannot take candidate evidence")
    for names in (inputs["baseline_jobs"], inputs["candidate_jobs"], inputs["baseline_evidence"], inputs["candidate_evidence"]):
        if len(names) != len(set(names)):
            raise ReadinessError("Repeated structural evidence is one observation, not multiple checks")
    b = [_job(workspace, name, baseline["identity"]) for name in inputs["baseline_jobs"]]
    b += [_external(workspace, "baseline", path) for path in inputs["baseline_evidence"]]
    c = [_job(workspace, name, selected["identity"]) for name in inputs["candidate_jobs"]]
    c += [_external(workspace, variant, path) for path in inputs["candidate_evidence"]]
    attribution = _attribution(b, c)
    unresolved = {entry["signature"] for entry in attribution}
    report = {"api_version": 1, "kind": "readiness_structural_health_v1", "created_utc": utc_now(),
              "workspace_id": ws["workspace_id"], "baseline_identity": baseline["identity"],
              "variant": variant, "variant_identity": selected["identity"], "inputs": inputs,
              "baseline": b, "candidate": c, "attribution": attribution,
              "summary": {"baseline_state": _side_state(b), "candidate_state": _side_state(c),
                          "candidate_new_count": len({entry["signature"] for entry in attribution if entry["attribution"] == "candidate_new"}),
                          "unresolved_count": len(unresolved), "whole_design_readiness": "not_established"},
              "limits": list(_LIMITS)}
    report["identity"] = digest(report)
    return report


def verify_report(workspace, variant, report):
    """Re-read identical inputs and compare stable evidence; never trust edited claims."""
    if not isinstance(report, dict) or digest({key: value for key, value in report.items() if key != "identity"}) != report.get("identity"):
        raise ReadinessError("Structural health report identity changed")
    if report.get("kind") != "readiness_structural_health_v1" or report.get("variant") != variant:
        raise ReadinessError("Structural health report targets another variant or format")
    inputs = report.get("inputs")
    expected = {"baseline_jobs", "candidate_jobs", "baseline_evidence", "candidate_evidence"}
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ReadinessError("Structural health input specifications changed")
    fresh = assess(workspace, variant, **inputs)
    stable = lambda value: {key: item for key, item in value.items() if key not in ("identity", "created_utc")}
    if stable(fresh) != stable(report):
        raise ReadinessError("Structural health report is stale or its evidence changed")
    return report
