# Controller API

Run `python3 registry_gate.py reconcile` with the observation packet on stdin.
Persist the returned registry atomically, then append deduplicated
`emitted_events` and `new_measurements`.

Before a model decision and after execution, reconcile again with a unique
`reconciliation_id`. Call `hot-view` with the expected session, cycle, phase,
fingerprints, and evidence watermark. A result other than `hard_gate=pass`
must stop the workflow as evidence/state uncertainty.

Before removing one blackbox, call `can-expand` with the persisted registry,
module, and partition. Only `allowed=true` authorizes that exact configuration
change. The controller remains responsible for the write and subsequent
functional/synthesis/STA verification.

Before a scope decision that needs hierarchy or coupling facts, call
`module-context` with the persisted registry and module. Only
`hard_gate=pass` is current context. The project must build or refresh the
design-context artifact outside this Skill whenever source fingerprints change.
