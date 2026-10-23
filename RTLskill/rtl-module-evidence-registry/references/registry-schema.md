# Registry schema

`reconcile` accepts a JSON object with `project_id`, `session_id`, `cycle_id`,
`phase`, `observed_at`, `reconciliation_id`, `observation`, optional
`previous_registry`, `verification_evidence`, `measurements`,
`pending_transaction`, and `mainline`. Mainline fields
are structured observations or events supplied by a project adapter. They may
come from an optional external transaction system, but after reconciliation
the versioned Registry projection is authoritative for Registry consumers.

`observation` contains `config_fingerprint`, `filelist_fingerprint`,
`rtl_fingerprint`, `evidence_watermark`, `design_context`, and `modules`.
`design_context` is a project-built artifact with source fingerprints, graph
fingerprint, generation, coverage, and reference. The registry validates and
maintains it; it never constructs the graph. Every module item has a
stable `module_id`, optional `variant_key` and `rtl_path`, body/filelist facts,
`relationships`, `timing_context`, `semantic_intent`, a matching
`design_context_fingerprint`, and a `partitions` map. A partition representation is one of `blackbox`,
`rtl_body`, `not_in_scope`, or `unknown`.

The output uses Registry `schema_version=3` and contains generation and freshness metadata,
`registry_status`, conflicts, fingerprints, modules, pending transaction,
mainline, validated design context, `hot_view`, `emitted_events`, and
`new_measurements`.

Optimization campaign state is not a Registry input or projection. The
Optimization Skill remains its sole owner; the Registry stores only module,
verification, measurement, design-context, and integration-mainline facts.
An existing v1/v2 snapshot may be supplied only as `previous_registry` for
reconciliation; the rebuilt output is always v3 and drops the former
Optimization mirror.

Current observation always supersedes an old representation. Historical proof
is merged by stable evidence id and never inferred from representation alone.
Verification evidence status may be `verified`, `measurement_incomplete`,
`deterministic_failed`, `infra_uncertain`, or `unclassified`. The last value is
the portable fallback when a project does not emit an optional normalized
verification observation; it preserves pending work and never triggers
project-specific exit-code reconstruction inside the Registry.

The reconciler derives `verification_pending_modules`. A nonempty list without
a Registry `pending_transaction` projection, or together with a mainline whose
status is `select_next`, is internally inconsistent. The output records
`verification_pending_without_transaction` or
`mainline_skips_pending_verification` and sets the hot-view hard gate to
`hold`. Project adapters should recover the pending transaction from immutable
structured verification evidence before reconciliation; the generic Skill
does not guess it from progress prose.

`module-context` returns one bounded module projection only when the registry,
design context, and module graph fingerprint are current. It returns a hold
rather than silently serving stale relationships.
