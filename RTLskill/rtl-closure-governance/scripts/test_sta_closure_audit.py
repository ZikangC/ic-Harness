#!/usr/bin/env python3
"""Minimal regression tests for portable closure artifact classification."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
COLLECT = ROOT / "sta_closure_collect.py"
CLASSIFY = ROOT / "sta_closure_audit.py"
sys.path.insert(0, str(ROOT.parent))
from closure_gate import run as run_gate
from tool_probe import run_probe_config


def run(*args, stdin=""):
    return subprocess.run(args, input=stdin, text=True, check=True, capture_output=True).stdout


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    empty = root / "empty.v"
    empty.write_text("/* Generated netlist */\n", encoding="utf-8")
    artifact = json.loads(run("python3", str(COLLECT), "--netlist", str(empty), "--top", "chip_top"))
    assert artifact["sta_closure_module_count"] == 0

    mapped = root / "mapped.v"
    mapped.write_text("module \\chip_top (input clk); endmodule\nmodule child (); endmodule\n", encoding="utf-8")
    artifact_path = root / "artifact.json"
    run("python3", str(COLLECT), "--netlist", str(mapped), "--top", "chip_top", "--required-body", "child", "--output", str(artifact_path))
    audit = json.loads(run("python3", str(CLASSIFY), "--artifact", str(artifact_path), stdin="sta_status=partial\nsta_link_result=ok\nsta_timing_measured=yes\n"))
    assert audit["phase"] == "sta_timing_measured"

    compact = root / "compact.v"
    compact.write_text("module chip_top (); endmodule; module child (); endmodule\n", encoding="utf-8")
    artifact = json.loads(run("python3", str(COLLECT), "--netlist", str(compact), "--top", "chip_top"))
    assert artifact["sta_closure_module_count"] == 2 and artifact["sta_closure_top_found"] == 1

    empty_path = root / "empty.json"
    run("python3", str(COLLECT), "--netlist", str(empty), "--top", "chip_top", "--output", str(empty_path))
    audit = json.loads(run("python3", str(CLASSIFY), "--artifact", str(empty_path), stdin="sta_status=link_error\nsta_closure_top_found=0\n"))
    assert audit["phase"] == "closure_export_empty"

    audit = run_gate(netlist_path=empty, top="chip_top")
    assert audit["phase"] == "closure_export_empty" and audit["gate_api_version"] == 6
    audit = run_gate(netlist_path=mapped, top="chip_top")
    assert audit["phase"] == "not_attempted"

    timeout_feedback = root / "timeout_feedback.txt"
    timeout_feedback.write_text(
        "sta_closure_bytes=0\nsta_closure_module_count=0\nsta_status=not_attempted\n"
        "sta_link_result=未测得\nsta_timing_measured=no\n"
        "measurement_status=incomplete_timeout\n"
        "sta_error=closure partition top_level_sta exited 124; timeout/truncation\n",
        encoding="utf-8",
    )
    retry_audit = run_gate(feedback_path=timeout_feedback)
    assert retry_audit["phase"] == "closure_measurement_incomplete"
    assert not retry_audit["closure_repair_required"]
    assert retry_audit["measurement_rerun_admissible"] is None
    assert retry_audit["closure_gate_required"]
    assert not retry_audit["measurement_resource_blocked"]
    assert retry_audit["repair_domain"] == "measurement_budget"

    admissible = run_gate(feedback_path=timeout_feedback, policy_context={
        "measurement_budget_seconds": 600,
        "max_measurement_budget_seconds": 7200,
    })
    assert admissible["phase"] == "closure_measurement_incomplete"
    assert admissible["measurement_rerun_admissible"] is True

    exhausted = run_gate(feedback_path=timeout_feedback, policy_context={
        "measurement_budget_seconds": 7200,
        "max_measurement_budget_seconds": 7200,
    })
    assert exhausted["phase"] == "closure_resource_blocked"
    assert exhausted["measurement_incomplete"]
    assert not exhausted["measurement_rerun_admissible"]
    assert exhausted["strategy_change_required"]
    assert exhausted["measurement_resource_blocked"]
    assert exhausted["closure_gate_required"]
    assert exhausted["repair_domain"] == "measurement_strategy"
    same = run_gate(feedback_path=timeout_feedback, policy_context={
        "measurement_budget_seconds": 600,
        "max_measurement_budget_seconds": 7200,
        "exhausted_measurement": {
            "semantic_fingerprint": "design-a", "scope_kind": "targeted",
            "targets": ["top"], "budget_seconds": 600,
        },
        "proposed_measurement": {
            "semantic_fingerprint": "design-a", "scope_kind": "targeted",
            "targets": ["top"], "budget_seconds": 600,
            "intent": "verify_pending_transaction",
        },
    })
    assert same["measurement_policy"]["measurement_relation"] == "same_measurement"
    assert same["measurement_policy"]["proposed_measurement_allowed"] is False
    assert (same["measurement_policy"]["exhausted_measurement"]["measurement_key"]
            == same["measurement_policy"]["proposed_measurement"]["measurement_key"])

    increased = run_gate(feedback_path=timeout_feedback, policy_context={
        "measurement_budget_seconds": 600,
        "max_measurement_budget_seconds": 7200,
        "exhausted_measurement": {
            "semantic_fingerprint": "design-a", "scope_kind": "targeted",
            "targets": ["top"], "budget_seconds": 600,
        },
        "proposed_measurement": {
            "semantic_fingerprint": "design-a", "scope_kind": "targeted",
            "targets": ["top"], "budget_seconds": 1800,
        },
    })
    assert increased["measurement_policy"]["proposed_measurement_allowed"] is True
    assert increased["measurement_policy"]["same_measurement_rerun_admissible"] is True

    above_max = run_gate(feedback_path=timeout_feedback, policy_context={
        "measurement_budget_seconds": 600,
        "max_measurement_budget_seconds": 7200,
        "exhausted_measurement": {
            "semantic_fingerprint": "design-a", "scope_kind": "targeted",
            "targets": ["top"], "budget_seconds": 600,
        },
        "proposed_measurement": {
            "semantic_fingerprint": "design-a", "scope_kind": "targeted",
            "targets": ["top"], "budget_seconds": 14400,
        },
    })
    assert above_max["measurement_policy"]["proposed_measurement_allowed"] is False

    changed = run_gate(feedback_path=timeout_feedback, policy_context={
        "measurement_budget_seconds": 7200,
        "max_measurement_budget_seconds": 7200,
        "exhausted_measurement": {
            "semantic_fingerprint": "design-a", "scope_kind": "full", "targets": [],
        },
        "proposed_measurement": {
            "semantic_fingerprint": "design-a", "scope_kind": "targeted",
            "targets": ["block-a"], "intent": "verify_pending_transaction",
        },
    })
    assert changed["measurement_policy"]["measurement_relation"] == "changed_scope"
    assert changed["measurement_policy"]["proposed_measurement_allowed"] is True

    # Optional budget facts may be absent without making the Skill unusable.
    optional_budget = run_gate(feedback_path=timeout_feedback, policy_context={
        "max_budget_exhausted": True,
    })
    assert optional_budget["phase"] == "closure_measurement_incomplete"
    assert optional_budget["max_budget_exhausted"] is False
    assert optional_budget["measurement_budget_seconds"] is None
    assert optional_budget["max_measurement_budget_seconds"] is None
    assert optional_budget["measurement_rerun_admissible"] is None

    syntax_feedback = root / "syntax_feedback.txt"
    syntax_feedback.write_text(
        "sta_status=read_error\nsta_error=[ERROR STA-0171] candidate.v line 99, syntax error\n"
        "sta_closure_bytes=123\nsta_closure_module_count=2\nsta_closure_top_found=1\n"
        "sta_read_skip_count=1\nsta_link_result=未测得\nsta_timing_measured=no\n",
        encoding="utf-8",
    )
    syntax = run_gate(feedback_path=syntax_feedback)
    assert syntax["phase"] == "sta_parse_error"
    assert syntax["structural_repair_required"]

    blackbox_feedback = root / "blackbox_feedback.txt"
    blackbox_feedback.write_text(
        "sta_status=read_error\nsta_error=STA-0171 unknown module compute_leaf\n"
        "sta_closure_bytes=123\nsta_closure_module_count=2\nsta_closure_top_found=1\n"
        "sta_read_skip_count=1\nsta_link_result=未测得\nsta_timing_measured=no\n",
        encoding="utf-8",
    )
    blackbox = run_gate(feedback_path=blackbox_feedback)
    assert blackbox["phase"] == "undeclared_blackbox"

    no_path_feedback = root / "no_path_feedback.txt"
    no_path_feedback.write_text(
        "sta_status=ok_no_path\nsta_closure_bytes=123\nsta_closure_module_count=2\n"
        "sta_closure_top_found=1\nsta_read_skip_count=0\nsta_link_result=ok\n"
        "sta_timing_measured=no\nsta_diag_status=ok\nsta_diag_clock_port_count=1\n"
        "sta_diag_clock_count=1\nsta_diag_register_count=0\nsta_diag_data_input_count=5\n",
        encoding="utf-8",
    )
    no_path = run_gate(feedback_path=no_path_feedback)
    assert no_path["phase"] == "sta_no_timing_path"
    assert no_path["no_timing_path_cause"] == "no_registers_in_linked_netlist"
    assert no_path["sta_diag_data_input_count"] == 5
    assert not no_path["diagnostic_required"]
    assert no_path["closure_gate_required"]

    missing_diag_feedback = root / "missing_diag_feedback.txt"
    missing_diag_feedback.write_text(
        "sta_status=ok_no_path\nsta_closure_bytes=123\nsta_closure_module_count=2\n"
        "sta_closure_top_found=1\nsta_read_skip_count=0\nsta_link_result=ok\n"
        "sta_timing_measured=no\n",
        encoding="utf-8",
    )
    missing_diag = run_gate(feedback_path=missing_diag_feedback)
    assert missing_diag["diagnostic_required"]
    assert missing_diag["no_timing_path_cause"] == "diagnostic_missing"

    probe = run_probe_config({"probes": [{
        "name": "minimal_link", "argv": [sys.executable, "-c", "print('LINK_OK')"],
        "success_markers": ["LINK_OK"],
    }]})
    assert probe["status"] == "passed"
    probe_path = root / "probe.json"
    probe_path.write_text(json.dumps(probe), encoding="utf-8")
    audit = run_gate(netlist_path=mapped, top="chip_top", probe_path=probe_path)
    assert audit["phase"] == "not_attempted"

    failed_probe = run_probe_config({"probes": [{
        "name": "broken_link", "argv": [sys.executable, "-c", "raise SystemExit(7)"],
        "success_markers": ["LINK_OK"],
    }]})
    assert failed_probe["status"] == "failed"

    missing_marker_probe = run_probe_config({"probes": [{
        "name": "missing_marker", "argv": [sys.executable, "-c", "print('command exists')"],
        "success_markers": ["LINK_OK"],
    }]})
    assert missing_marker_probe["status"] == "failed"
    failed_path = root / "failed_probe.json"
    failed_path.write_text(json.dumps(failed_probe), encoding="utf-8")
    audit = run_gate(netlist_path=mapped, top="chip_top", probe_path=failed_path)
    assert audit["phase"] == "tool_probe_failed" and audit["repair_domain"] == "harness_or_environment"

print("sta closure audit tests: PASS")
