# STA closure phase schema

Use one phase per audit. Raw logs remain the authority.

An artifact collector runs beside the netlist and emits `sta_closure_bytes`,
`sta_closure_module_count`, `sta_closure_top_found`, and `required_body_missing`.
It streams the netlist and emits compact JSON only; it does not copy the
netlist into an agent prompt.  Tool-specific adapters are responsible for
producing the netlist and feedback packet.

| Phase | Evidence | Smallest next action | Exit condition |
|---|---|---|---|
| `tool_probe_failed` | Declared minimal command probe exits nonzero, times out, or lacks its success marker | Repair environment/adapter/command config; do not launch the expensive action | Probe status is `passed` |
| `not_attempted` | No top-level STA packet | Schedule one bounded closure smoke | Any observed phase |
| `closure_export_empty` | Closure bytes are zero **or module count is zero**, without timeout/truncation evidence | Repair selection/export adapter | Nonzero closure with module body |
| `closure_measurement_incomplete` | Closure bytes/module count are zero **and** the same handoff records timeout/truncation (for example exit 124) | Retry the same measurement only with a strictly larger legal budget, or change strategy; no RTL/PPA conclusion | A fresh closure export or the maximum legal budget is reached |
| `closure_resource_blocked` | The measurement remains incomplete at the maximum legal budget | Stop identical reruns; use a bounded local diagnosis, change measurement strategy, or stop for debugging | New structural or measurement evidence |
| `closure_top_missing` | Export nonempty but configured top absent | Repair top selection/name normalization | Top is declared |
| `closure_body_missing` | Required nonblackbox body absent | Diagnose RTL versus hierarchy/export set | Bodies are present |
| `undeclared_blackbox` | Read error names unknown blackbox/STA-0171 | Export compatible blackbox declarations | `sta_read_skip_count=0` |
| `sta_parse_error` | Other read/Verilog parser failure | Normalize generated STA input | `sta_read_skip_count=0` |
| `sta_link_error` | Read succeeds but `link_design` fails | Repair library/top/module linkage | `sta_link_result=ok` |
| `sta_no_timing_path` | Link succeeds but no timing evidence | Repair constraints/clock/report setup | `sta_timing_measured=true` |
| `sta_timing_measured` | Real `report_checks` path value exists | Resume normal optimization | N/A |

`sta_timing_measured` is not final-chip closure. Keep a separate
scope/quality field whenever timing blackboxes, unlinked cells, or hierarchical
proxies remain.

Controllers consume the semantic booleans emitted with every phase rather
than duplicating this table as phase sets. `structural_repair_required` marks
export/read/link defects; `diagnostic_required` marks an unresolved no-path
case lacking counters; `closure_gate_required` covers every unresolved
closure, retry, or resource phase; `timing_ready` is true only for
`sta_timing_measured`.

`closure_measurement_incomplete` is deliberately not a member of the repair
gate. It is a resource-limited measurement state: the controller may permit a
small, monotonic budget retry, but it must keep generic full-PPA milestones
blocked and must never treat the empty artifact as a structural export defect.
Once the resource policy is exhausted, `closure_resource_blocked` keeps the
measurement truth (`measurement_incomplete=true`) but sets
`measurement_rerun_admissible=false` and `strategy_change_required=true`. It is a
closure-measurement strategy boundary, not proof that the RTL is wrong and not
authorization to optimize RTL. Any optimization permission must be decided
outside this Skill from independent registry, functional, and optimization
evidence.

Budget exhaustion and rerun admissibility are derived inside this Skill from
optional raw current and maximum budget values. Missing optional budget facts
do not invalidate the audit; `measurement_rerun_admissible` is then `null` and
no same-measurement action is authorized. Project code must not provide a
preclassified budget-exhausted boolean or persistent retry count.

For a proposed next measurement, `measurement_policy.measurement_relation`
uses `same_measurement`, `changed_scope`, `changed_design_or_config`,
`changed_scope_and_design`, or `unknown`. The comparison uses deterministic
design/config fingerprint, scope kind, and targets. Budget is excluded from
identity, but the same measurement is admissible only when its proposed budget
is strictly larger than the exhausted budget and does not exceed the legal
maximum.
