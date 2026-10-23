# Fact Catalog schema

The Fact Catalog is an internal, bounded interface between current-stage Skill
envelopes and the Evidence Model. It is not a historical database and must not
be copied wholesale into the strategic agent prompt.

`scripts/fact_catalog.py` walks parsed current-run structures plus each Skill
envelope's `hard_gate`, `summary_input`, and optional `execution_contract`. It emits scalar leaves, empty
containers, and bounded scalar lists. Arrays retain their real JSON Pointer
positions (`/eligible_candidates/0/module`); the catalog does not invent an
entity identity layer such as `by_module`.

```json
{
  "schema_version": 2,
  "scope": "current_packet",
  "facts": {
    "f_0123456789abcdef": {
      "fact_id": "f_0123456789abcdef",
      "source_role": "skill_module_registry",
      "skill": "rtl-module-evidence-registry",
      "source_pointer": "/hard_gate/pending_transaction/module_id",
      "name": "module_id",
      "semantic_name": "module_registry.hard_gate.pending_transaction.module_id",
      "value": "compute",
      "value_type": "string",
      "priority": "gate",
      "required": false
    }
  },
  "source_fact_ids": {
    "skill_module_registry": ["f_0123456789abcdef"]
  },
  "required_fact_ids": [],
  "fact_count": 1,
  "limits": {
    "max_facts_per_source": 16,
    "max_total_facts": 60
  }
}
```

`fact_id` is derived deterministically from `source_role` and the real JSON
Pointer so one packet is reproducible. Its authority is nevertheless scoped to
the packet: every model output must echo `packet_fingerprint`, and a ref is
accepted only against that exact immutable packet. Cross-packet equality of a
`fact_id` is not a valid reference.

Facts use four priorities: `gate`, `mainline`, `decision`, and `diagnostic`.
Required contract facts and hard gates are pinned first; remaining budget is
allocated fairly across current sources. Raw forensic logs do not enter the
catalog. Only parsed structures and bounded diagnostic excerpts reach the
Evidence Model.

The model receives a smaller projection containing `fact_id`, source, name,
value, type, priority, and required status. It returns topic groups containing
only `fact_refs` and prose. The validator resolves current values, identities,
and provenance from the catalog; the model never authorizes an action or
supplies a replacement value.

An `execution_contract` never enters the Fact Catalog or Evidence Model prompt.
The validator separately checks the complete source-authored contract and emits
it under `validated_execution_contracts` only when source integrity passes. A
project adapter may expose a compact `allowed_actions` projection beside the
validated strategic evidence, but the model never supplies or changes that
projection. The strategic model never receives the full Catalog and the
controller never resolves contract fields through `fact_id`.

The validator emits complete `validated_evidence` and a bounded
`strategic_view`. The controller sends that view unchanged apart from a generic
transport-size guard. A deterministic fallback selects facts by priority and
round-robins across sources, bounded to 12 facts by default.
