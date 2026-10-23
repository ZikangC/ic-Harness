---
name: rtl-closure-governance
description: "Govern RTL top-level synthesis and STA closure from Yosys/OpenSTA/OpenROAD evidence: classify the blocking phase, choose the smallest falsifiable check, and define controller gates. Use for closure failures, not ordinary RTL timing optimization after closure is proven."
---

# RTL Closure Governance

Use this skill when an RTL project needs an evidence-driven path from synthesis
to a trustworthy top-level STA result. It covers failure diagnosis and
controller policy, not RTL implementation authority.

This skill never authorizes RTL edits, deletion/archival of runs, a full PPA
rerun, or a claim of full-chip Fmax. Preserve raw logs as evidence and keep
large netlists on the execution server.

## Required evidence

Prefer a machine-readable closure audit. If it is absent, derive one from the
same run's feedback and only the smallest necessary artifact metadata. At
minimum establish:

- closure export byte count and module count;
- configured top declaration present or absent;
- OpenSTA read skip/error count;
- `link_design` result;
- whether `report_checks` produced a real timing value;
- a short first error signature.

Read [the phase schema](references/phase-schema.md) when classifying a run or
designing controller policy.

Before a costly or environment-sensitive action, require a short declared
capability probe. It must execute the minimal real operation and emit a
success marker; command existence or model recollection is not evidence. Read
[the tool-probe contract](references/tool-probe.md) when defining probes.

Use `closure_gate.py` as the controller-facing stable interface; its `run()`
function and CLI accept an optional deterministic feedback packet plus either
a local netlist or an artifact collected on another host.  Read
[the controller API](references/controller-api.md) before integrating it.
`feedback.txt` is not an agent answer: the measurement adapter extracts it
from Yosys/STA tool logs.  The collector streams the large netlist locally and
never copies it into an agent prompt.

## Workflow

1. Identify exactly one phase from the schema using real evidence. A Yosys exit
   code, a filename, or a scope label alone is never timing proof.
2. Before an expensive action, have the controller run its declared probe. A
   failed probe is `tool_probe_failed`; do not ask the agent to retry or guess
   command syntax until its adapter/environment is repaired.
3. State one falsifiable cause and an expected phase transition. Keep the
   proposed change inside the user's authorized scope.
4. Run the smallest meaningful verification: normally one bounded
   `top_level_sta` smoke after a closure/harness repair. If the export is empty
   only because the same measurement timed out or was truncated, classify it as
   `closure_measurement_incomplete`. The same measurement is admissible again
   only with a strictly larger legal budget. When the maximum legal budget is
   reached, transition to `closure_resource_blocked`; do not keep issuing the
   same measurement. This phase requires a different
   measurement strategy, bounded local diagnosis, or a debug stop; it does not
   authorize an RTL optimization action.
   When a project proposes a next measurement, pass normalized descriptors for
   the exhausted and proposed measurements. The gate compares design/config
   fingerprint, scope kind, and targets while excluding budget from identity.
   It reports `same_measurement`, a real strategy change, or `unknown`; never
   infer this relation from an action name such as `continue`.
   Controllers may provide the current measurement budget and legal maximum as
   optional raw resource facts. The Skill derives budget exhaustion only when
   both are present; absence of this optional capability does not make the gate
   unavailable. Never accept a caller-produced `max_budget_exhausted` verdict.
5. Feed the new phase into the next decision. The controller's generic total
   budget, no-progress, and cycle limits remain the execution safety boundary;
   this Skill does not persist or count measurement retries.
6. Only after `sta_timing_measured` may normal PPA optimization resume. If the
   closure contains timing blackboxes or unlinked logic, retain a partial scope
   label; this skill never upgrades it to final full-chip STA.

## Controller policy

Implement classification in this Skill's deterministic script and make the
controller gate on its JSON. A project adapter may invoke `closure_gate.py`,
but it must not maintain a second classifier, merge an older embedded audit,
or duplicate phase membership sets. Consume the semantic booleans
`structural_repair_required`, `diagnostic_required`,
`closure_gate_required`, `measurement_rerun_admissible`,
`measurement_resource_blocked`, `strategy_change_required`, and
`timing_ready`. Give the model the compact audit plus raw-log paths, not a
large copied log. While a closure phase is unresolved, block generic full-PPA
milestones and functional-counting fallbacks; require the appropriate bounded
closure smoke. `closure_repair_required=true` is reserved for structural,
syntax, link, or constraint defects. A timeout-only empty export has
`closure_repair_required=false`. With current and maximum budgets present,
`measurement_rerun_admissible` reports whether a strictly larger legal budget
still exists; missing optional budget facts produce `null`, not Skill failure.
Budget exhaustion changes the policy phase to
`closure_resource_blocked`, where another identical retry is forbidden. The
Skill reports `strategy_change_required=true` but does not decide whether RTL
optimization, rollback, or another project-level strategy is legal. It may
approve a proposed measurement as a distinct strategy, while a downstream
transaction Skill owns the surrounding operation. The controller enforces the
validated execution contract plus generic budget and no-progress rules.
If the Skill cannot be invoked or its API/required fields are invalid, report
infrastructure uncertainty and fail closed; never silently fall back to a
project-local interpretation.
