# Controller API

CLI reads JSON from stdin:

```text
python3 transaction_gate.py initial
python3 transaction_gate.py transition
python3 transaction_gate.py validate-ref
python3 transaction_gate.py hot-view
```

Every mutating event supplies `expected_generation` and a stable `event_id`.
An exact replay is idempotent even after the ledger generation advances.
Persist successful output atomically before performing an activated action.

Supported events are `create_mainline`, `activate_mainline`,
`mainline_verification_pending`, `begin_child`, `child_verification_pending`,
`finish_child`, `mainline_completed`, `mainline_failed`, and
`cancel_mainline`. Terminal events require `deterministic_evidence_ref` with a
nonempty `source_role` and `source_id`.

The controller validates every stored reference with `validate-ref`. A hold is
state uncertainty, not permission to reconstruct a target from prompt or
progress text.

A durable transaction reference may carry an older ledger generation while
the same transaction fingerprint remains valid. A future generation or a
fingerprint mismatch is rejected. Validation returns the refreshed canonical
reference for the current generation.
