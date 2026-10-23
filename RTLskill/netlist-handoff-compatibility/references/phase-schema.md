# Handoff phase schema

Use exactly one phase per gate result. The first blocking phase wins.

| Phase | Deterministic evidence | Repair domain | Required transition |
|---|---|---|---|
| `source_artifact_empty` | Zero bytes or zero module declarations | `frontend_export` | Nonempty artifact with modules |
| `source_artifact_malformed` | Module/end count mismatch or duplicate declarations | `frontend_export` | Structurally consistent artifact |
| `source_top_missing` | Configured top declaration absent | `frontend_hierarchy` | Exact top declaration present |
| `not_attempted` | Structural inventory passes but no backend link result exists | `backend_adapter` | Run the bounded backend probe |
| `backend_unavailable` | Availability stage fails, times out, or misses its marker | `environment_or_adapter` | Availability probe passes |
| `unsupported_hdl_construct` | Read stage diagnostic matches a declared syntax/dialect rule | `normalization_adapter` | Backend read passes on a derived artifact |
| `blackbox_contract_missing` | Unknown/undefined module or blackbox contract diagnostic | `blackbox_contract` | Exact declaration/body contract is supplied |
| `library_coverage_missing` | Instantiated cell is absent from the loaded backend libraries | `library_adapter` | Library/model coverage is complete for the declared scope |
| `backend_read_error` | Other backend read failure | `backend_adapter` | Read probe passes |
| `backend_link_error` | Read passes but backend link fails for another reason | `backend_adapter_or_constraints` | Link probe passes |
| `backend_link_not_attempted` | Availability/read pass but no link stage was configured or completed | `backend_adapter` | Real link stage passes |
| `handoff_compatible` | Exact candidate artifact is read and linked by the configured backend | `none` | Enter STA closure gate without changing scope |

`handoff_compatible` is intentionally narrower than timing closure. Keep
`scope=partial_hierarchical` when intended blackboxes or omitted hierarchy
remain, even if the backend links successfully.
