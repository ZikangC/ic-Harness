# Scope schema

The controller supplies one JSON object:

```json
{
  "change_mode": "local|architecture",
  "rtl_changes": [
    {
      "module": "module_name",
      "path": "rtl/module_name.sv",
      "interface_changed": false,
      "clock_reset_changed": false,
      "parameter_changed": false,
      "shared_state_changed": false,
      "combinational_boundary_changed": false,
      "boundary_registered": true,
      "impact_known": true
    }
  ],
  "affected_modules": ["module_name"],
  "top_module_changed": false,
  "filelist_changed": false,
  "constraints_changed": false,
  "blackbox_policy_changed": false,
  "measurement_adapter_changed": false,
  "remaining_blackboxes": ["other_module"],
  "design_context": {
    "status": "current",
    "hard_gate": "pass",
    "graph_fingerprint": "sha256",
    "module_contexts": {
      "module_name": {
        "relationships": {"instantiates": [], "instantiated_by": ["top"]},
        "timing_context": {"clock_domains": ["clk"]},
        "semantic_intent": {"summary": null, "source": "unavailable"}
      }
    }
  }
}
```

Boolean fields are evidence facts, not suggestions. Missing safety facts are
treated as unknown and therefore cannot release module-local STA.
For any RTL change, missing/stale design context or a missing changed-module
record also forces a top-level scope. The context supplements the actual diff;
it never replaces diff-derived boundary facts.

Scopes:

| Scope | Meaning |
|---|---|
| `no_rtl_sta` | No RTL timing evidence was invalidated |
| `local_module` | One registered-boundary module is sufficient |
| `affected_cone` | A known multi-module dependency cone is sufficient |
| `top_level_partial` | Top-level integration with allowed timing blackboxes |
| `top_level_complete` | Top-level integration without timing blackboxes |

`top_level_partial` is never full-chip Fmax proof.
