#!/usr/bin/env python3
"""Deterministic single-mainline transaction ledger."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Mapping


SCHEMA_VERSION = 1
TERMINAL = {"completed", "failed", "cancelled"}
STATES = {"proposed", "ready", "active", "suspended", "verification_pending", *TERMINAL}


def _fingerprint(value: Mapping[str, object]) -> str:
    payload = {key: value.get(key) for key in (
        "transaction_id", "role", "kind", "subject_ref", "parent_transaction_id", "owner_skill")}
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _evidence(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} requires deterministic_evidence_ref")
    result = copy.deepcopy(dict(value))
    if not str(result.get("source_role") or "").strip() or not str(result.get("source_id") or "").strip():
        raise ValueError(f"{name} evidence requires source_role and source_id")
    return result


def _reference(state: Mapping[str, object], transaction: Mapping[str, object]) -> dict[str, object]:
    return {
        "transaction_id": transaction.get("transaction_id"),
        "ledger_generation": state.get("generation"),
        "transaction_fingerprint": transaction.get("fingerprint"),
    }


def _strategic_transaction(transaction: object) -> object:
    if not isinstance(transaction, Mapping):
        return None
    return {key: copy.deepcopy(transaction.get(key)) for key in (
        "transaction_id", "role", "kind", "state", "subject_ref", "parent_transaction_id")}


def hot_view(ledger: Mapping[str, object]) -> dict[str, object]:
    transactions = ledger.get("transactions")
    transactions = transactions if isinstance(transactions, Mapping) else {}
    mainline_id = ledger.get("mainline_transaction_id")
    active_id = ledger.get("active_execution_transaction_id")
    mainline = transactions.get(mainline_id) if isinstance(mainline_id, str) else None
    active = transactions.get(active_id) if isinstance(active_id, str) else None
    ready = sorted(
        str(tx_id) for tx_id, transaction in transactions.items()
        if isinstance(transaction, Mapping) and transaction.get("state") == "ready")
    conflicts = list(ledger.get("conflicts") or [])
    status = "hold" if conflicts else str(ledger.get("ledger_status") or "current")
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": ledger.get("project_id"),
        "generation": ledger.get("generation"),
        "ledger_status": status,
        "hard_gate": "hold" if status != "current" else "pass",
        "mainline_transaction_ref": (_reference(ledger, mainline)
                                     if isinstance(mainline, Mapping) else None),
        "active_execution_transaction_ref": (_reference(ledger, active)
                                              if isinstance(active, Mapping) else None),
        "ready_transaction_ids": ready,
        "strategic_view": {
            "active_transaction": _strategic_transaction(active),
            "mainline_transaction": _strategic_transaction(mainline),
            "ready_transaction_ids": ready,
            "reason_codes": conflicts,
        },
    }


def _finish(state: dict[str, object]) -> dict[str, object]:
    state["hot_view"] = hot_view(state)
    return state


def initial_state(project_id: str) -> dict[str, object]:
    if not project_id.strip():
        raise ValueError("project_id is required")
    return _finish({
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "generation": 0,
        "ledger_status": "current",
        "transactions": {},
        "mainline_transaction_id": None,
        "active_execution_transaction_id": None,
        "processed_event_ids": [],
        "conflicts": [],
        "emitted_events": [],
    })


def _transaction(event: Mapping[str, object], role: str, parent: object = None) -> dict[str, object]:
    tx_id = str(event.get("transaction_id") or "").strip()
    kind = str(event.get("transaction_kind") or "").strip()
    subject = event.get("subject_ref")
    if not tx_id or not kind or not isinstance(subject, Mapping):
        raise ValueError("transaction_id, transaction_kind and subject_ref are required")
    transaction = {
        "transaction_id": tx_id,
        "role": role,
        "kind": kind,
        "state": "active",
        "subject_ref": copy.deepcopy(dict(subject)),
        "parent_transaction_id": parent,
        "owner_skill": str(event.get("owner_skill") or "project_adapter"),
        "created_evidence_ref": _evidence(event.get("deterministic_evidence_ref"), "transaction creation"),
        "terminal_evidence_ref": None,
    }
    transaction["fingerprint"] = _fingerprint(transaction)
    return transaction


def transition(ledger: Mapping[str, object], event: Mapping[str, object]) -> dict[str, object]:
    state = copy.deepcopy(dict(ledger))
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported transaction ledger schema")
    event_id = str(event.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("event_id is required")
    processed = list(state.get("processed_event_ids") or [])
    if event_id in processed:
        return _finish(state)
    expected = event.get("expected_generation")
    if not isinstance(expected, int) or expected != state.get("generation"):
        raise ValueError("transaction ledger generation mismatch")
    transactions = state.get("transactions")
    if not isinstance(transactions, dict):
        raise ValueError("transactions must be an object")
    kind = str(event.get("kind") or "")
    tx_id = str(event.get("transaction_id") or "")

    if kind == "create_mainline":
        current_id = state.get("mainline_transaction_id")
        current = transactions.get(current_id) if isinstance(current_id, str) else None
        if isinstance(current, Mapping) and current.get("state") not in TERMINAL:
            raise ValueError("one nonterminal mainline already exists")
        if tx_id in transactions:
            raise ValueError("transaction_id must not reuse historical identity")
        transaction = _transaction(event, "mainline")
        transaction["state"] = str(event.get("initial_state") or "active")
        if transaction["state"] not in {"ready", "active"}:
            raise ValueError("mainline initial_state must be ready or active")
        transactions[tx_id] = transaction
        state["mainline_transaction_id"] = tx_id
        state["active_execution_transaction_id"] = tx_id if transaction["state"] == "active" else None
    elif kind == "activate_mainline":
        transaction = transactions.get(tx_id)
        if not isinstance(transaction, dict) or transaction.get("role") != "mainline":
            raise ValueError("mainline transaction not found")
        if transaction.get("state") != "ready":
            raise ValueError("activate_mainline requires ready state")
        if state.get("active_execution_transaction_id") not in (None, tx_id):
            raise ValueError("another execution transaction is active")
        transaction["state"] = "active"
        state["active_execution_transaction_id"] = tx_id
    elif kind == "mainline_verification_pending":
        transaction = transactions.get(tx_id)
        if not isinstance(transaction, dict) or transaction.get("role") != "mainline":
            raise ValueError("mainline transaction not found")
        if transaction.get("state") not in {"active", "verification_pending"}:
            raise ValueError("mainline verification requires active state")
        transaction["state"] = "verification_pending"
        state["active_execution_transaction_id"] = tx_id
    elif kind == "begin_child":
        parent_id = str(event.get("parent_transaction_id") or "")
        parent_tx = transactions.get(parent_id)
        if (parent_id != state.get("mainline_transaction_id")
                or not isinstance(parent_tx, dict)
                or parent_tx.get("state") not in {"active", "verification_pending", "ready"}):
            raise ValueError("begin_child requires the current nonterminal mainline")
        for transaction in transactions.values():
            if (isinstance(transaction, Mapping)
                    and transaction.get("role") == "optimization_child"
                    and transaction.get("state") not in TERMINAL):
                raise ValueError("one nonterminal optimization child already exists")
        child = _transaction(event, "optimization_child", parent_id)
        transactions[tx_id] = child
        parent_tx["state"] = "suspended"
        state["active_execution_transaction_id"] = tx_id
    elif kind == "child_verification_pending":
        transaction = transactions.get(tx_id)
        if not isinstance(transaction, dict) or transaction.get("role") != "optimization_child":
            raise ValueError("optimization child not found")
        if transaction.get("state") not in {"active", "verification_pending"}:
            raise ValueError("child verification requires active state")
        transaction["state"] = "verification_pending"
        state["active_execution_transaction_id"] = tx_id
    elif kind == "finish_child":
        transaction = transactions.get(tx_id)
        if not isinstance(transaction, dict) or transaction.get("role") != "optimization_child":
            raise ValueError("optimization child not found")
        outcome = str(event.get("outcome") or "")
        if outcome not in {"completed", "failed", "cancelled"}:
            raise ValueError("finish_child outcome is invalid")
        transaction["terminal_evidence_ref"] = _evidence(
            event.get("deterministic_evidence_ref"), "finish_child")
        transaction["state"] = outcome
        parent = transactions.get(transaction.get("parent_transaction_id"))
        if not isinstance(parent, dict) or parent.get("state") != "suspended":
            raise ValueError("optimization child parent is not suspended")
        parent["state"] = "ready"
        state["active_execution_transaction_id"] = None
    elif kind in {"mainline_completed", "mainline_failed", "cancel_mainline"}:
        transaction = transactions.get(tx_id)
        if not isinstance(transaction, dict) or transaction.get("role") != "mainline":
            raise ValueError("mainline transaction not found")
        if transaction.get("state") not in {"active", "verification_pending", "ready"}:
            raise ValueError("mainline terminal event requires a live mainline")
        terminal_state = {"mainline_completed": "completed", "mainline_failed": "failed",
                          "cancel_mainline": "cancelled"}[kind]
        transaction["terminal_evidence_ref"] = _evidence(
            event.get("deterministic_evidence_ref"), kind)
        transaction["state"] = terminal_state
        state["active_execution_transaction_id"] = None
    else:
        raise ValueError(f"unknown transaction event: {kind}")

    state["generation"] = int(state.get("generation", 0)) + 1
    processed.append(event_id)
    state["processed_event_ids"] = processed[-128:]
    emitted = list(state.get("emitted_events") or [])
    emitted.append({
        "event_id": event_id, "event_kind": kind,
        "transaction_id": tx_id, "generation": state["generation"],
    })
    state["emitted_events"] = emitted[-128:]
    return _finish(state)


def validate_ref(ledger: Mapping[str, object], reference: Mapping[str, object]) -> dict[str, object]:
    transactions = ledger.get("transactions")
    transactions = transactions if isinstance(transactions, Mapping) else {}
    tx_id = str(reference.get("transaction_id") or "")
    transaction = transactions.get(tx_id)
    reasons: list[str] = []
    if ledger.get("ledger_status") != "current" or ledger.get("conflicts"):
        reasons.append("ledger_not_current")
    if not isinstance(transaction, Mapping):
        reasons.append("transaction_not_found")
    else:
        reference_generation = reference.get("ledger_generation")
        current_generation = ledger.get("generation")
        if (not isinstance(reference_generation, int)
                or not isinstance(current_generation, int)
                or reference_generation > current_generation):
            reasons.append("ledger_generation_invalid")
        if reference.get("transaction_fingerprint") != transaction.get("fingerprint"):
            reasons.append("transaction_fingerprint_mismatch")
    return {
        "hard_gate": "hold" if reasons else "pass",
        "reason_codes": reasons,
        "transaction": copy.deepcopy(dict(transaction)) if isinstance(transaction, Mapping) else None,
        "transaction_ref": (_reference(ledger, transaction)
                            if isinstance(transaction, Mapping) else None),
    }
