# Transaction ledger schema

The durable state contains `schema_version`, `project_id`, `generation`,
`ledger_status`, `transactions`, `mainline_transaction_id`,
`active_execution_transaction_id`, `processed_event_ids`, `conflicts`, and a
derived `hot_view`.

Every transaction contains:

```json
{
  "transaction_id": "stable project identity",
  "role": "mainline|optimization_child",
  "kind": "opaque adapter-owned kind",
  "state": "proposed|ready|active|suspended|verification_pending|completed|failed|cancelled",
  "subject_ref": {"registry_module_id": "module", "partition": "top_level_sta"},
  "parent_transaction_id": null,
  "owner_skill": "source role",
  "created_evidence_ref": {"source_role": "...", "source_id": "..."},
  "terminal_evidence_ref": null,
  "fingerprint": "sha256"
}
```

`transaction_ref` is `{transaction_id, ledger_generation,
transaction_fingerprint}`. Consumers must validate it against the current
ledger before use.

`transaction_id` is immutable and may not be reused after a transaction reaches
a terminal state. A retry of the same subject is a new attempt with a new ID;
otherwise the retry would overwrite historical identity and terminal evidence.

The hot view contains only current references, ready IDs, conflicts, and a
small strategic projection. Full terminal history does not enter model input.
