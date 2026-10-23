# Current-stage evidence schema

The workflow is scoped to one focused stage and one explicit run handoff. A
contract is a JSON object with:

```json
{
  "stage": "sta_health",
  "objective": "decide whether the linked top-level design has measured timing",
  "required_fields": [
    "sta_link_result",
    "sta_timing_measured",
    "sta_status",
    "sta_timing_path_count"
  ],
  "allowed_sources": ["result", "feedback", "sta_log_excerpt"]
}
```

The Skill does not embed project-specific contracts. A harness stores and
versions its contract beside its controller configuration, then passes it to
`prepare --contract`. Typical projects may define stages such as
`functional_gate`, `yosys_preflight`, `sta_health`, or `ppa_measurement`, but
their required fields are project protocol rather than Skill policy.

The script creates a packet with this shape:

```json
{
  "schema_version": 3,
  "workflow": "stage-evidence-workflow",
  "stage": "sta_health",
  "run_id": "one-explicit-run",
  "packet_fingerprint": "sha256-of-immutable-current-packet",
  "input_status": "ready_for_model",
  "deterministic_state": "ready_for_model",
  "required_fields": ["sta_link_result"],
  "deterministic_facts": {
    "sta_link_result": {
      "value": "ok",
      "sources": ["result", "feedback"],
      "raw_values": ["ok"]
    }
  },
  "missing": [],
  "conflicts": [],
  "artifacts": [],
  "history_included": false,
  "history_mode": "none",
  "scope": "current_stage",
  "fact_catalog": {
    "schema_version": 2,
    "scope": "current_packet",
    "facts": {},
    "source_fact_ids": {},
    "required_fact_ids": [],
    "fact_count": 0
  }
}
```

Callers may add explicitly scoped artifacts with roles such as
`progress_delta`, `sta_closure_audit`, `controller_state`, `stage_context`, or
the optional `trend_summary`.
These are current-stage context, not task rules. A full cumulative
`progress.md`, previous Pro output, or a historical experiment table must not
be supplied under those roles. `trend_summary` is the only role that may carry
explicitly selected comparable-run history, and it must be compact and
controller-generated.

Structured Skill outputs use a role beginning with `skill_` and this envelope:

```json
{
  "schema_version": 1,
  "skill": "rtl-module-evidence-registry",
  "scope": "current_stage",
  "history_included": false,
  "identity": {"session_id": "s1", "cycle_id": 3, "generation": 8},
  "hard_gate": {"status": "pass", "pending_module": "compute"},
  "summary_input": {"integration_state": "measurement_incomplete"}
}
```

The workflow derives a bounded Fact Catalog from parsed current-run artifacts
and every supplied Skill envelope. The model returns topic-oriented groups and
does not copy Skill identity, generation, values, paths, or types:

```json
{
  "packet_fingerprint": "sha256-of-immutable-current-packet",
  "evidence_groups": [{
    "topic": "current blackbox expansion",
    "fact_refs": ["f_0123456789abcdef"],
    "summary": "compute remains pending verification"
  }]
}
```

The model does not create a path or copy a value. The validator resolves every
fact from the exact catalog identified by `packet_fingerprint`. Unknown refs,
missing required refs/Skill coverage, malformed topic groups, and prohibited
policy fields are summary-quality errors and receive one bounded,
error-aware retry. Mutation of the prepared packet is an integrity error.

The Evidence Model must return schema version 3 JSON with
`packet_fingerprint`, `stage`, `run_id`, `status` (`consistent`, `conflict`,
`incomplete`, or `unavailable`), `evidence_groups`, and a short `summary`.

The validator adds `integrity_state`, `summary_state`, compatibility
`review_state`, complete `validated_evidence`, a bounded `strategic_view`,
`summary_source`, and `hard_gate`.
`hard_gate` follows source integrity, not model formatting. When integrity
passes but a second model review still fails, the validator selects bounded
catalog facts deterministically and emits `DEGRADED_SUMMARY` with
`summary_source=deterministic_fallback`. Source
missing/conflict/unreadable/mutated conditions remain hard holds. The Evidence
Model's prose is explanatory; the controller remains authoritative for
downstream actions.

See [Fact Catalog schema](fact-catalog-schema.md) and
[Mismatch taxonomy](mismatch-taxonomy.md) for the v3 interfaces.
