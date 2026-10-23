#!/usr/bin/env python3
"""Behavior tests for the lightweight transaction ledger."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.transaction_ledger import initial_state, transition, validate_ref


def event(ledger, event_id, kind, **fields):
    return transition(ledger, {"event_id": event_id, "expected_generation": ledger["generation"],
                               "kind": kind, **fields})


ledger = initial_state("demo")
ledger = event(
    ledger, "e1", "create_mainline", transaction_id="tx-main",
    transaction_kind="blackbox_expansion",
    subject_ref={"registry_module_id": "q4", "partition": "top_level_sta"},
    deterministic_evidence_ref={"source_role": "module_registry", "source_id": "generation:7"})
main_ref = ledger["hot_view"]["mainline_transaction_ref"]
assert validate_ref(ledger, main_ref)["hard_gate"] == "pass"

ledger = event(
    ledger, "e2", "begin_child", transaction_id="tx-opt",
    transaction_kind="rtl_optimization", parent_transaction_id="tx-main",
    subject_ref={"registry_module_id": "q4"},
    deterministic_evidence_ref={"source_role": "optimization_skill", "source_id": "candidate:1"})
assert ledger["transactions"]["tx-main"]["state"] == "suspended"
assert ledger["hot_view"]["strategic_view"]["active_transaction"]["transaction_id"] == "tx-opt"
assert validate_ref(ledger, main_ref)["hard_gate"] == "pass"

ledger = event(
    ledger, "e3", "finish_child", transaction_id="tx-opt", outcome="failed",
    deterministic_evidence_ref={"source_role": "controller", "source_id": "rollback-proof:1"})
assert ledger["transactions"]["tx-main"]["state"] == "ready"
assert ledger["hot_view"]["ready_transaction_ids"] == ["tx-main"]

ledger = event(ledger, "e4", "activate_mainline", transaction_id="tx-main")
ledger = event(ledger, "e5", "mainline_verification_pending", transaction_id="tx-main")
try:
    event(ledger, "e6", "mainline_completed", transaction_id="tx-main")
    raise AssertionError("completion without deterministic evidence must fail")
except ValueError:
    pass
ledger = event(
    ledger, "e6", "mainline_completed", transaction_id="tx-main",
    deterministic_evidence_ref={"source_role": "module_registry", "source_id": "verification:ok"})
assert ledger["transactions"]["tx-main"]["state"] == "completed"
assert ledger["hot_view"]["active_execution_transaction_ref"] is None
try:
    event(
        ledger, "e7", "create_mainline", transaction_id="tx-main",
        transaction_kind="blackbox_expansion",
        subject_ref={"registry_module_id": "q4", "partition": "top_level_sta"},
        deterministic_evidence_ref={
            "source_role": "module_registry", "source_id": "generation:8"})
    raise AssertionError("historical transaction identity was reused")
except ValueError:
    pass

assert transition(ledger, {
    "event_id": "e6", "expected_generation": 0,
    "kind": "mainline_completed", "transaction_id": "tx-main",
})["generation"] == ledger["generation"]

print("rtl transaction ledger tests: PASS")
