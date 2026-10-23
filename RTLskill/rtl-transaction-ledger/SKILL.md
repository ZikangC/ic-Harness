---
name: rtl-transaction-ledger
description: "Maintain one durable RTL/EDA mainline transaction and at most one temporary child transaction using stable references, bounded lifecycle states, and evidence-backed completion. Use when workflow intent must survive optimization, retries, restarts, or model handoffs without copying mutable target state across components."
---

# RTL Transaction Ledger

Use this Skill as the single owner of workflow transaction identity and
lifecycle. It records what mainline transaction is active or suspended; it does
not decide which module is valuable, choose STA scope, classify closure, or map
an action to project commands.

Read [the schema](references/schema.md) before producing transaction events and
[the controller API](references/controller-api.md) before integration. Use
`transaction_gate.py` as the stable deterministic interface.

## Invariants

- Permit at most one nonterminal `mainline` transaction and at most one
  nonterminal `optimization_child`.
- A child must reference the current mainline. Beginning it suspends the
  parent; finishing it makes the same parent ready to resume.
- Other components store `transaction_ref`, never an independently mutable
  copy of target, phase, or pending state. Display caches are non-authoritative.
- A transaction reference includes its ID, ledger generation, and transaction
  fingerprint. An older observed generation remains valid when the transaction
  fingerprint still matches; a future generation, missing transaction, or
  fingerprint mismatch fails closed. Validation returns a refreshed reference.
- Completion and failure require deterministic evidence references. Model
  prose may select an allowed action but cannot close a transaction.
- Infrastructure uncertainty preserves the transaction and does not prove
  success or failure.
- The ledger stores compact references and lifecycle facts, not logs, RTL,
  checkpoints, execution profiles, or project commands.
- State transitions are generation-checked and event-idempotent. Persist the
  returned state atomically before executing a newly activated transaction.

## Boundary

Skill or project adapters propose transactions and execution contracts. The
strategic model chooses among validated ready IDs. The controller executes the
chosen contract and reports deterministic evidence events. This ledger only
validates lifecycle and ownership; it must not become a scheduler with
kind-specific policy branches.
