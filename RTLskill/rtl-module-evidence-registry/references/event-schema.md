# Event schema

Events are append-only JSON Lines records. Required fields are `event_id`,
`event_kind`, `project_id`, `session_id`, `cycle_id`, `phase`, `observed_at`,
and `reconciliation_id`. Module events also include `module_id`, `before`, and
`after` where applicable.

The generic reconciler emits one `cycle_reconciled` event per unique
reconciliation and may emit `module_observed`, `representation_changed`,
`verification_observed`, `pending_transaction_changed`, and
`design_context_changed` events. Project
adapters must deduplicate by `event_id` before appending.
