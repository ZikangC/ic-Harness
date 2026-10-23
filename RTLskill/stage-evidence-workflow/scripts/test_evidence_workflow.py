#!/usr/bin/env python3
"""Local tests for the current-stage evidence consolidation workflow."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evidence_workflow import (_parse_artifacts, prepare, render_prompt,
                               render_retry_prompt, run_workflow,
                               validate_model_output)  # noqa: E402
from fact_catalog import deterministic_fallback_facts  # noqa: E402


TEST_CONTRACTS = {
    "sta_health": {
        "stage": "sta_health",
        "objective": "determine whether the current linked design has measured STA evidence",
        "required_fields": [
            "sta_link_result", "sta_timing_measured", "sta_status",
            "sta_timing_path_count", "sta_closure_bytes", "sta_closure_module_count",
        ],
    },
    "functional_gate": {
        "stage": "functional_gate",
        "objective": "determine whether the current functional gate completed successfully",
        "required_fields": ["functional_quick_exit", "functional_exit"],
        "any_of": [["functional_quick_exit", "functional_exit"]],
    },
}


def prepare_stage(*, stage, **kwargs):
    return prepare(stage=stage, contract=TEST_CONTRACTS[stage], **kwargs)


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    result = root / "result.json"
    result.write_text(json.dumps({
        "functional_quick_exit": 0,
        "sta_closure_audit": {
            "sta_link_result": "ok",
            "sta_timing_measured": True,
            "sta_status": "partial",
            "sta_timing_path_count": 16,
            "sta_closure_bytes": 176250085,
            "sta_closure_module_count": 9,
        },
    }), encoding="utf-8")
    feedback = root / "feedback.txt"
    feedback.write_text("sta_status=partial\nsta_link_result=ok\nsta_timing_measured=yes\n"
                        "sta_timing_path_count=16\nsta_closure_bytes=176250085\n"
                        "sta_closure_module_count=9\n" + "noise=ignored\n" * 1000, encoding="utf-8")
    progress = root / "progress_delta.json"
    progress.write_text(json.dumps({
        "scope": "current_stage", "history_included": False,
        "last_action": "targeted_sta", "new_observation": "measured paths present",
    }), encoding="utf-8")
    closure = root / "closure.json"
    closure.write_text(json.dumps({
        "phase": "sta_timing_measured", "closure_repair_required": False,
        "sta_link_result": "ok", "sta_timing_measured": True,
    }), encoding="utf-8")
    packet = prepare_stage(stage="sta_health", run_id="run-a",
                           artifacts={"result": result, "feedback": feedback,
                                      "progress_delta": progress, "sta_closure_audit": closure})
    assert packet["input_status"] == "ready_for_model"
    assert packet["deterministic_facts"]["sta_status"]["value"] == "partial"
    assert packet["deterministic_facts"]["sta_link_result"]["value"] == "ok"
    prompt = render_prompt(packet)
    assert "History included: false" in prompt
    assert "sta_health" in prompt and "progress.md" not in prompt
    assert "progress_delta" in prompt and "task-wide policy" in prompt
    assert packet["scope"] == "current_stage"
    assert packet["context_roles"] == ["progress_delta", "sta_closure_audit"]
    assert packet["history_mode"] == "none" and packet["history_included"] is False
    assert packet["allowed_source_roles"] == ["feedback", "progress_delta", "result", "sta_closure_audit"]
    assert packet["packet_fingerprint"] in prompt
    assert "evidence_groups" in prompt and "observations" not in prompt
    generic_packet = prepare(stage="project_specific_stage", run_id="run-a",
                             artifacts={"result": result})
    assert generic_packet["required_fields"] == []
    try:
        prepare(stage="sta_health", run_id="run-a", artifacts={"result": result},
                contract={"stage": "different_stage", "required_fields": []})
    except ValueError as error:
        assert "does not match requested stage" in str(error)
    else:
        raise AssertionError("mismatched project contract stage was accepted")
    try:
        _parse_artifacts(["progress=progress.md"])
    except ValueError as error:
        assert "current-stage scoped role" in str(error)
    else:
        raise AssertionError("cumulative progress role was accepted")
    trend = root / "trend.json"
    trend.write_text(json.dumps({
        "scope": "controller_trend", "history_mode": "trend",
        "records": [{"phase": "sta_no_timing_path"}, {"phase": "sta_no_timing_path"}],
    }), encoding="utf-8")
    trend_packet = prepare_stage(stage="sta_health", run_id="run-a",
                                 artifacts={"result": result, "trend_summary": trend})
    trend_prompt = render_prompt(trend_packet)
    assert trend_packet["history_mode"] == "trend"
    assert trend_packet["history_included"] is True
    assert trend_packet["scope"] == "current_stage_with_trend"
    assert "History mode: trend" in trend_prompt

    registry = root / "skill_registry.json"
    registry_envelope = {
        "schema_version": 1,
        "skill": "rtl-module-evidence-registry",
        "scope": "current_stage",
        "history_included": False,
        "identity": {"session_id": "s1", "cycle_id": 2, "generation": 7},
        "hard_gate": {"status": "pass", "pending_module": "compute"},
        "summary_input": {
            "pending_module": "compute", "integration_state": "measurement_incomplete",
        },
    }
    registry.write_text(json.dumps(registry_envelope), encoding="utf-8")
    bus_packet = prepare_stage(stage="sta_health", run_id="run-a",
                               artifacts={"result": result, "skill_module_registry": registry})
    assert bus_packet["skill_outputs"]["skill_module_registry"]["identity"]["generation"] == 7
    assert "skill_module_registry" in bus_packet["context_roles"]
    bus_prompt = render_prompt(bus_packet)
    assert "Bounded current-stage Fact Catalog" in bus_prompt
    assert "fact_refs" in bus_prompt and "key_facts" not in bus_prompt
    registry_fact_ids = bus_packet["fact_catalog"]["skill_fact_ids"]["skill_module_registry"]
    assert registry_fact_ids
    assert bus_packet["fact_catalog"]["fact_count"] <= 60

    optimization = root / "skill_optimization.json"
    optimization_envelope = {
        "schema_version": 1,
        "skill": "rtl-optimization-triage",
        "scope": "current_stage",
        "history_included": False,
        "identity": {"session_id": "s1", "cycle_id": 2, "registry_generation": 7},
        "hard_gate": {"phase": "measurement_pending"},
        "summary_input": {"candidate": "compute", "measurement_retries_remaining": 2},
        "execution_contract": {
            "schema_version": 1,
            "contract_kind": "active_optimization",
            "enforcement": "hard",
            "current_phase": "measurement_pending",
            "applies_to_actions": ["*"],
            "allowed_actions": ["retry_measurement", "defer_optimization"],
            "forbidden_actions": ["optimize_rtl"],
            "action_parameters": {"retry_measurement": {"module": {"enum": ["compute"]}}},
            "execution_profiles": {"retry_measurement": {"execution_mode": "verify_existing_candidate"}},
            "hard_constraints": {"no_edit_during_measurement": True},
            "recommended_actions": [{"action": "retry_measurement", "reason": "bounded_retry"}],
            "strategic_questions": ["retry or defer"],
            "retry_budget": {"measurement_retries_remaining": 2},
            "resume_context": {"kind": "blackbox_expansion", "target": "compute"},
        },
    }
    optimization.write_text(json.dumps(optimization_envelope), encoding="utf-8")
    contract_packet = prepare_stage(
        stage="sta_health", run_id="run-a",
        artifacts={"result": result, "skill_optimization_triage": optimization})
    assert all(
        not str(fact.get("source_pointer", "")).startswith("/execution_contract")
        for fact in contract_packet["fact_catalog"]["facts"].values()
    )

    run_evidence = root / "run_evidence.json"
    run_evidence.write_text(json.dumps({
        "scope": "current_stage", "history_included": False,
        "source_run_id": "run-a", "selector": "worst_slack",
        "path_summaries": [{"slack_ns": -1.0, "startpoint": "a", "endpoint": "b"}],
    }), encoding="utf-8")
    run_packet = prepare_stage(stage="sta_health", run_id="run-a",
                               artifacts={"result": result, "run_evidence": run_evidence})
    assert "run_evidence" in run_packet["context_roles"]
    assert "cell-by-cell raw report" not in render_prompt(run_packet)

    def valid_model(current_packet, extra_refs=()):
        refs = list(current_packet["fact_catalog"]["required_fact_ids"])
        for role, role_refs in current_packet["fact_catalog"]["skill_fact_ids"].items():
            if role_refs:
                refs.append(role_refs[0])
        refs.extend(extra_refs)
        refs = list(dict.fromkeys(refs))
        return {
            "schema_version": 3,
            "packet_fingerprint": current_packet["packet_fingerprint"],
            "stage": current_packet["stage"],
            "run_id": current_packet["run_id"],
            "status": "consistent",
            "evidence_groups": [{
                "topic": "current STA evidence",
                "fact_refs": refs,
                "summary": "linked partial STA with measured paths",
            }],
            "summary": "linked partial STA with measured paths",
        }

    contract_review = validate_model_output(contract_packet, valid_model(contract_packet))
    validated_contract = contract_review["validated_execution_contracts"]["skill_optimization_triage"]
    assert validated_contract["contract"]["allowed_actions"] == ["retry_measurement", "defer_optimization"]
    assert validated_contract["source_identity"]["registry_generation"] == 7

    malformed = json.loads(json.dumps(optimization_envelope))
    malformed["execution_contract"]["allowed_actions"] = ["retry_measurement", "optimize_rtl"]
    malformed["execution_contract"]["forbidden_actions"] = ["optimize_rtl"]
    optimization.write_text(json.dumps(malformed), encoding="utf-8")
    malformed_packet = prepare_stage(
        stage="sta_health", run_id="run-a",
        artifacts={"result": result, "skill_optimization_triage": optimization})
    assert malformed_packet["input_status"] == "conflict"
    assert malformed_packet["skill_output_errors"][0]["kind"] == "skill_execution_contract_invalid"
    optimization.write_text(json.dumps(optimization_envelope), encoding="utf-8")

    model = valid_model(packet)
    review = validate_model_output(packet, model)
    assert review["review_state"] == "CONSISTENT" and review["hard_gate"] == "pass"
    assert review["validated_evidence"] and review["strategic_view"]["evidence_groups"]

    bus_model = valid_model(bus_packet)
    bus_review = validate_model_output(bus_packet, bus_model)
    assert bus_review["review_state"] == "CONSISTENT"

    expected_contract = dict(bus_packet["contract"])
    expected_contract["expected_skill_identities"] = {
        "skill_module_registry": {"generation": 8},
    }
    stale_source_packet = prepare(
        stage="sta_health", run_id="run-a",
        artifacts={"result": result, "skill_module_registry": registry},
        contract=expected_contract,
    )
    assert stale_source_packet["input_status"] == "conflict"
    stale_review = validate_model_output(stale_source_packet, valid_model(stale_source_packet))
    assert stale_review["integrity_state"] == "hold" and stale_review["hard_gate"] == "hold"

    mutated_packet = json.loads(json.dumps(bus_packet))
    one_fact = next(iter(mutated_packet["fact_catalog"]["facts"].values()))
    one_fact["value"] = "mutated-after-prepare"
    mutated_review = validate_model_output(mutated_packet, bus_model)
    assert mutated_review["integrity_state"] == "hold"
    assert mutated_review["integrity_errors"]["packet_errors"][0]["kind"] == "packet_fingerprint_invalid"

    wrong_fingerprint = json.loads(json.dumps(bus_model))
    wrong_fingerprint["packet_fingerprint"] = "stale-packet"
    fingerprint_review = validate_model_output(bus_packet, wrong_fingerprint)
    assert fingerprint_review["review_state"] == "MODEL_REVIEW_REQUIRED"
    assert fingerprint_review["integrity_state"] == "pass"
    assert fingerprint_review["retry_context"]["packet_fingerprint"] == bus_packet["packet_fingerprint"]
    retry_prompt = render_retry_prompt(bus_packet, fingerprint_review)
    assert "MODEL_PACKET_FINGERPRINT_MISMATCH" in retry_prompt
    assert bus_packet["packet_fingerprint"] in retry_prompt

    wrong_ref = json.loads(json.dumps(bus_model))
    wrong_ref["evidence_groups"][0]["fact_refs"].append("f_not_current")
    held_ref = validate_model_output(bus_packet, wrong_ref)
    assert held_ref["review_state"] == "MODEL_REVIEW_REQUIRED"
    assert any(error["kind"] == "unknown_fact_ref" for error in held_ref["errors"])
    fallback_ref = validate_model_output(bus_packet, wrong_ref, attempt=1)
    assert fallback_ref["review_state"] == "DEGRADED_SUMMARY"
    assert fallback_ref["integrity_state"] == "pass" and fallback_ref["hard_gate"] == "pass"
    assert fallback_ref["summary_source"] == "deterministic_fallback"
    assert fallback_ref["validated_evidence"]

    unavailable = validate_model_output(bus_packet, {}, attempt=1)
    assert unavailable["summary_state"] == "unavailable"
    assert unavailable["hard_gate"] == "pass"
    assert unavailable["summary_source"] == "deterministic_fallback"

    fair_fallback = deterministic_fallback_facts({"facts": {
        f"f_{role}_{index}": {
            "fact_id": f"f_{role}_{index}", "source_role": role,
            "source_pointer": f"/hard_gate/{index}", "priority": "gate", "value": index,
        }
        for role in ("skill_a", "skill_b", "skill_c") for index in range(8)
    }})
    assert len(fair_fallback) == 12
    assert {fact["source_role"] for fact in fair_fallback} == {"skill_a", "skill_b", "skill_c"}

    duplicate = json.loads(json.dumps(model))
    duplicate["evidence_groups"][0]["fact_refs"].append(
        duplicate["evidence_groups"][0]["fact_refs"][0])
    duplicate_review = validate_model_output(packet, duplicate)
    assert duplicate_review["review_state"] == "CONSISTENT"
    assert duplicate_review["warnings"][0]["kind"] == "duplicate_fact_ref_ignored"

    policy_output = json.loads(json.dumps(model))
    policy_output["action"] = "expand_blackbox"
    policy_review = validate_model_output(packet, policy_output)
    assert policy_review["review_state"] == "MODEL_REVIEW_REQUIRED"
    assert policy_review["hard_gate"] == "pass"
    policy_fallback = validate_model_output(packet, policy_output, attempt=1)
    assert policy_fallback["summary_source"] == "deterministic_fallback"

    conflicting_feedback = root / "conflicting.txt"
    conflicting_feedback.write_text(feedback.read_text(encoding="utf-8").replace(
        "sta_status=partial", "sta_status=not_attempted", 1), encoding="utf-8")
    conflicting = prepare_stage(stage="sta_health", run_id="run-a",
                                artifacts={"result": result, "feedback": conflicting_feedback})
    assert conflicting["input_status"] == "conflict"
    held = validate_model_output(conflicting, model)
    assert held["review_state"] == "RECHECK_REQUIRED" and held["hard_gate"] == "hold"
    stopped = validate_model_output(conflicting, model, attempt=1)
    assert stopped["review_state"] == "BLOCKED_EVIDENCE"
    assert stopped["integrity_state"] == "hold" and stopped["hard_gate"] == "hold"

    # Arrays retain their real JSON Pointer positions. Cross-packet safety is
    # supplied by packet_fingerprint rather than an invented by_module path.
    nested_registry = root / "skill_nested_registry.json"
    nested_envelope = json.loads(json.dumps(registry_envelope))
    nested_envelope["summary_input"]["eligible_candidates"] = [
        {"module": "beta", "value_score": 2.0},
        {"module": "alpha", "value_score": 3.0},
    ]
    nested_envelope["summary_input"]["extra"] = {f"k{index}": index for index in range(20)}
    nested_registry.write_text(json.dumps(nested_envelope), encoding="utf-8")
    nested_packet = prepare_stage(stage="sta_health", run_id="run-a",
                                  artifacts={"result": result, "skill_module_registry": nested_registry})
    nested_facts = nested_packet["fact_catalog"]["facts"]
    pointers = {fact["source_pointer"] for fact in nested_facts.values()}
    assert "/summary_input/eligible_candidates/0/module" in pointers
    assert "/summary_input/eligible_candidates/1/value_score" in pointers
    assert not any("by_module" in pointer or "[0]" in pointer or ".0" in pointer
                   for pointer in pointers)
    nested_envelope["summary_input"]["eligible_candidates"].reverse()
    nested_registry.write_text(json.dumps(nested_envelope), encoding="utf-8")
    reordered_packet = prepare_stage(stage="sta_health", run_id="run-a",
                                     artifacts={"result": result, "skill_module_registry": nested_registry})
    assert nested_packet["packet_fingerprint"] != reordered_packet["packet_fingerprint"]
    over_limit_model = valid_model(nested_packet)
    over_limit_model["evidence_groups"][0]["fact_refs"] = list(
        nested_packet["fact_catalog"]["facts"])[0:13]
    over_limit_review = validate_model_output(nested_packet, over_limit_model)
    assert any(error["kind"] == "selected_fact_limit_exceeded"
               for error in over_limit_review["errors"])

    truncated = prepare_stage(stage="sta_health", run_id="run-a", artifacts={"feedback": feedback},
                               max_artifact_bytes=1024)
    assert truncated["input_status"] == "incomplete"

    quick_only = root / "quick.json"
    quick_only.write_text('{"functional_quick_exit": 0}\n', encoding="utf-8")
    functional = prepare_stage(stage="functional_gate", run_id="run-a",
                               artifacts={"result": quick_only})
    assert functional["input_status"] == "ready_for_model"
    assert functional["missing"] == []

    fake_model = root / "fake_evidence_model.py"
    fake_model.write_text(r'''#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path

prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
mode = sys.argv[2]
counter = Path(sys.argv[3])
attempt = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
counter.write_text(str(attempt + 1), encoding="utf-8")
if mode == "fail":
    raise SystemExit(7)
def field(name):
    match = re.search(rf"^{re.escape(name)}: (.+)$", prompt, re.MULTILINE)
    return match.group(1).strip() if match else ""
required = re.search(r'"required_fact_ids":\s*(\[[^\]]*\])', prompt)
refs = json.loads(required.group(1)) if required else []
if mode == "retry" and attempt == 0:
    refs = ["f_unknown"]
output = {
    "schema_version": 3,
    "packet_fingerprint": field("Packet fingerprint"),
    "stage": field("Stage"),
    "run_id": field("Run id"),
    "status": "consistent",
    "evidence_groups": [{"topic": "current", "fact_refs": refs,
                         "summary": "current packet only"}],
    "summary": "current packet only",
}
print(json.dumps(output))
''', encoding="utf-8")

    for mode, expected_attempts, expected_source in (
            ("success", 1, "model"),
            ("retry", 2, "model_retry"),
            ("fail", 2, "deterministic_fallback")):
        counter = root / f"counter_{mode}.txt"
        workflow = run_workflow(
            stage="sta_health", run_id=f"run-{mode}", artifacts={"result": result},
            contract=TEST_CONTRACTS["sta_health"], project_root=root,
            work_dir=root / f"workflow_{mode}", run_label=mode,
            model_command=[sys.executable, fake_model.as_posix(),
                           "{prompt_file}", mode, counter.as_posix()])
        assert workflow["hard_gate"] == "pass"
        assert len(workflow["model_attempts"]) == expected_attempts
        assert workflow["review"]["summary_source"] == expected_source
        if mode == "retry":
            retry_prompt = (root / "workflow_retry" /
                            "evidence_model_prompt_retry_attempt1.md").read_text(encoding="utf-8")
            assert "previous_validation_errors" in retry_prompt
            assert "UNKNOWN_FACT_REF" in retry_prompt

    source_hold = run_workflow(
        stage="sta_health", run_id="run-source-hold",
        artifacts={"result": result, "feedback": conflicting_feedback},
        contract=TEST_CONTRACTS["sta_health"], project_root=root,
        work_dir=root / "workflow_source_hold", run_label="source_hold",
        model_command=[sys.executable, fake_model.as_posix(), "{prompt_file}",
                       "success", (root / "counter_source_hold.txt").as_posix()])
    assert source_hold["hard_gate"] == "hold"
    assert source_hold["review"]["integrity_state"] == "hold"
    assert source_hold["model_attempts"] == []

print("stage evidence workflow tests: PASS")
