# Optimization state schema

The durable state is Skill-owned and follows this shape. The campaign runner
is its only project-facing reader and writer; controllers and adapters pass the
configured path but must not parse, wrap, mirror, or rewrite this object:

```json
{
  "schema_version": 9,
  "phase": "idle|editing|functional_repair|modified_pending_functional|sta_required|waiting_external_verification|rollback_required|infra_pending|measurement_pending|resume_pending",
  "interrupted_mainline_ref": {
    "transaction_id": "stable Registry mainline identity",
    "kind": "blackbox_expansion",
    "module_id": "stage_b",
    "partition": "top_level_sta",
    "registry_generation": 7,
    "identity_fingerprint": "stable identity fingerprint"
  },
  "current_candidate": {
    "module": "stage_a",
    "hypothesis": "one falsifiable optimization",
    "campaign_id": "stable optimization campaign",
    "checkpoint_ref": "Skill runner checkpoint",
    "rtl_ref": "rtl/stage_a.sv",
    "edit_attempts": 0,
    "repair_attempts": 0,
    "measurement_retries": 0,
    "execution_steps": 0,
    "integration_target": "stage_b",
    "insertion_mode": "fused_same_unit|decoupled",
    "resume_token": {"registry_generation": 7},
    "top_level_acceptance_required": true,
    "rtl_fingerprint": "sha256 of preserved candidate",
    "functional_evidence": {"quick": 0, "medium": 0, "full": 0, "rtl_fingerprint": "sha256"},
    "preflight_evidence": {"preflight_exit": 0, "rtl_fingerprint": "sha256"},
    "verification_obligation": null,
    "measurement_partitions": ["stage_a"],
    "measurement_timeout_seconds": 600,
    "baseline_measurement": {
      "comparability_key": "yosys_abc:Nangate45:typical:stage_a:partition_proxy",
      "area_um2": 1200.0,
      "delay_ps": 9000.0
    },
    "baseline_value_score": 2.1,
    "verification_obligations": [
      {"purpose": "optimization_effect", "target_module": "stage_a", "status": "awaiting_sta_scope"},
      {"purpose": "integration_acceptance", "target_module": "stage_b", "status": "pending_mainline"}
    ],
    "pending_terminal_outcome": null
  },
  "acceptance_debt": [],
  "accepted": [],
  "rejected": [],
  "deferred": [],
  "max_repair_attempts": 2,
  "max_measurement_retries": 2,
  "max_edit_attempts": 2,
  "max_internal_steps": 12,
  "resume_request": null,
  "processed_event_ids": [],
  "processed_observation_ids": [],
  "execution_contract": {}
}
```

The state stores identifiers and compact evidence, not full logs or RTL.

Candidate evidence must not conflate prior proof with current integration
policy. Persist `expansion_verified_once`, `integration_state`, and
`top_level_acceptance_pending` independently when the project stores candidate
inventory. A resource-control blackbox can preserve local optimization
eligibility while requiring later top-level acceptance.

Events:

| Event | Required result |
|---|---|
| `begin_candidate` | Require a validated `interrupted_mainline_ref`; enter `editing` with a real checkpoint |
| `edit_completed` | Require the same campaign and a new RTL fingerprint; enter `modified_pending_functional` |
| `edit_failed` | Consume a bounded edit attempt; at the limit require rollback/defer |
| `functional_failed` | Enter `functional_repair` or `rollback_required` |
| `functional_passed` | Enter `sta_required` |
| `external_verification_required` | Preserve the candidate, enter `waiting_external_verification`, and emit a fingerprint-bound top-level STA obligation |
| `external_verification_unavailable` | Defer the optional candidate and require verified rollback |
| `external_measurement_incomplete` | Keep the global obligation pending within the bounded measurement retry budget |
| `combined_sta_passed` | Require a deterministic combined guard; accept without module causality attribution and retain complete-top debt after partial coverage |
| `infra_uncertain` | Preserve candidate in `infra_pending` without incrementing attempts |
| `measurement_incomplete` | Preserve candidate; increment only when `retry_attempt=true`; require rollback at the limit |
| `sta_passed` | Require deterministic comparable PPA improvement proof, then record local/top acceptance and enter `resume_pending` |
| `sta_failed` | Require rollback |
| `rollback_complete` | Require verified checkpoint/RTL/config restoration, archive rejected/deferred candidate, enter `resume_pending` |
| `defer_candidate` | Mark deferred intent and require rollback; it does not leave candidate RTL in mainline |
| `optimization_not_improved` | Preserve evaluation evidence and require rollback/defer |
| `mainline_resumed` | Require Registry to confirm the referenced mainline reached a deterministic terminal state, then return to `idle` |

`execution_contract` is derived state. Do not edit it independently. Rebuild it
through `migrate` or `transition` after loading durable state. `migrate` upgrades
older records conservatively: a missing RTL fingerprint remains unknown, so a
no-edit verification must rerun functional gates instead of reusing old proof.

The durable state never copies a mutable live `mainline` object. Module
Registry owns it; Optimization stores only `interrupted_mainline_ref`.
At triage time the adapter supplies transient Registry facts: `mainline`,
`pending_mainline`, `current_blackboxes`, and the Closure Skill's
`measurement_policy`. The campaign runner injects the current
`optimization_phase` from Skill-owned state. The Optimization Skill derives the transient
mainline execution profile; neither the raw facts nor that derived profile are
copied into durable state. The derived contract records the profile and Closure
Skill verdict needed for the next action.

The state file contains the bare governance object above. A controller envelope
such as `{ "governance": ..., "pending_observation": ... }` is invalid and is
rejected rather than migrated. A pending external-STA observation, when needed
for crash recovery, uses a separate raw JSON envelope and is never embedded in
the governance file.

`sta_passed` is not authorized by tool exit status alone. Its evidence must
contain `value_evaluation.verdict=improved` and `improved=true`, produced by
the deterministic `evaluate-measurement` gate from the saved baseline and the
post-run measurement.

`step` is the preferred lifecycle API. It accepts at most one normalized
observation, applies the transition, enforces `max_internal_steps`, and emits
at most one portable `requested_execution`. A timeout, truncation, or missing
required artifact is `measurement_incomplete`; a complete measurement with a
different `comparability_key` is `optimization_not_improved` and requires
rollback rather than another measurement retry.
