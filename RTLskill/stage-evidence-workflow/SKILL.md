---
name: stage-evidence-workflow
description: "Consolidate evidence for one focused RTL/EDA pipeline stage: combine the current run with relevant structured Skill outputs and scoped controller context, validate one compact Evidence Model summary, and hand it to a strategic agent without importing history or task policy."
---

# Stage Evidence Consolidation Workflow

Use this skill when one completed pipeline stage must be understood before a
separate strategic agent decides what to do next. It consolidates the current
stage's evidence; it is not an optimization agent, policy engine, or
replacement for the project's controller.

The workflow intentionally opens a fresh, single-stage context. The caller
supplies the exact current-run artifacts and explicitly scoped auxiliary
inputs. Auxiliary inputs may include a progress delta, a closure/health audit,
or a small controller-state snapshot. Relevant deterministic Skills may also
provide standard `skill_*` output envelopes. They must describe only the current
stage handoff; the workflow must not discover or import project history.

Do not put task-wide rules, Pro prompts, previous Pro replies, old experiment
tables, or a full cumulative `progress.md` into the model input. The stage
objective and required fields are the only instructions the Evidence Model
needs. Optimization policy and action authorization remain outside this
workflow.

The default history mode is `none`. If a controller has a concrete trend
question, it may supply one explicit `trend_summary` artifact containing at
most a few structured, comparable records. This opt-in trend is not a license
to provide raw historical progress; the model must not expand it or search for
more history.

## Responsibilities

- The workflow script reads the supplied artifacts, streams their hashes, and
  extracts deterministic key/value and JSON facts from the complete available
  artifact rather than from a log tail. It accepts both primary run artifacts
  and explicitly scoped context roles such as `progress_delta`,
  `controller_state`, a compact `run_evidence` summary, and optional
  `trend_summary`.
- A separate Evidence Model receives only one focused stage objective, a
  packet fingerprint, a bounded current-stage Fact Catalog, and optional short
  diagnostic excerpts. It groups current `fact_refs` by topic and writes a
  short explanation. It does not copy values, identities, paths, or types and
  must not choose an optimization action or infer history.
- The validator checks the model JSON against the deterministic facts. The
  model cannot turn an unknown, missing, or conflicting fact into a passing
  gate. A malformed duplicate citation is a warning if the required field is
  already backed by an allowed citation; an unknown role cannot be the only
  citation for a required field.
- Every supplied `skill_*` envelope declares its exact Skill identity,
  current-stage identity, hard gate, summary input, and optionally a
  source-authored `execution_contract`. A deterministic Fact
  Catalog combines those envelopes with parsed result, feedback, progress
  delta, and small controller context. `fact_id` values are valid only for the
  matching `packet_fingerprint`; accidental equality across packets grants no
  authority. Read [the Fact Catalog schema](references/fact-catalog-schema.md)
  when integrating a new structured Skill output.
- The caller must not supply the same semantic evidence twice under Skill and
  non-Skill roles. If a closure Skill envelope contains the current audit, for
  example, omit a second standalone closure-audit artifact.
- The project controller owns the final hard gate. Source integrity and model
  summary quality are separate states. A malformed or unavailable model
  summary may use a deterministic fallback when source integrity passes;
  missing, conflicting, stale, or unreadable source evidence still holds the
  gate. Read [the mismatch taxonomy](references/mismatch-taxonomy.md) when
  changing retry or fallback policy.
- An execution contract is not an Evidence Model summary. The deterministic
  validator checks its shape, action-set consistency, source identity, and
  packet integrity, then emits it under `validated_execution_contracts`.
  Contract fields do not enter the Fact Catalog or Evidence Model prompt. The
  model cannot omit, rewrite, or authorize the contract. A deterministic
  project adapter projects its allowed actions into the same immutable
  DecisionPacket given to the strategic model and retained by the controller
  for generic identity/action/parameter checks.

Raw logs remain forensic artifacts. This workflow writes only small manifests,
structured evidence, and optional bounded model input; it never deletes runs,
copies a large netlist into a prompt, modifies RTL, or starts EDA.

When a strategic agent requested detailed run evidence, the project adapter
must provide `run_evidence` containing identifiers, selectors, counts, and
deterministic path summaries only. Keep cell-by-cell reports and other raw
forensic text in the immutable run; do not copy them into this role.

## Stage boundary

Every invocation declares one stage, for example `functional_gate`,
`yosys_preflight`, `sta_health`, or `ppa_measurement`, plus an exact `run_id`.
Production controllers must supply a project-owned JSON contract naming the
stage objective, required fields, and required Skill roles. The workflow has
no RTL-project-specific required-field defaults. Omitting a contract is
permitted only for generic library use and creates no project-specific hard
requirements. Read [the stage schema](references/stage-schema.md) when writing
a contract. A stage may receive auxiliary artifacts, but each one must be
explicitly named and current-stage scoped.

The model input contains:

1. the stage objective and required evidence fields;
2. the current run's artifact manifest and hashes;
3. deterministic facts and missing/conflicting fields;
4. a bounded Fact Catalog derived from current structured artifacts and
   relevant Skill outputs;
5. explicitly supplied current-stage context, such as a progress delta or
   small controller snapshot;
6. an optional compact controller trend summary only when the stage needs a
   comparable-run trend;
7. the selected artifact text, with an explicit truncation marker if the
   configured input limit was exceeded.

It contains no automatic history. A baseline or trend comparison must be
supplied as a separate, explicitly named compact field or artifact, not
discovered by the workflow. A cumulative progress file is not a valid
substitute for a progress delta or trend summary.

## Integrity and summary states

The validator reports two independent states:

- `integrity_state=pass|hold|infra_uncertain` describes the supplied source
  evidence and current Skill envelopes;
- `summary_state=pass|retry_required|degraded|unavailable` describes only the
  Evidence Model handoff.

Compatibility `review_state` values remain available:

- `NO_INPUT`: no usable artifact was supplied;
- `READY_FOR_MODEL`: current artifacts are readable and deterministic facts
  are available for model review;
- `RECHECK_REQUIRED`: the first read found truncation or a source conflict;
- `MODEL_REVIEW_REQUIRED`: the model output is invalid or disagrees with
  deterministic evidence;
- `CONSISTENT`: required facts agree and the model output is source-backed;
- `DEGRADED_SUMMARY`: source integrity passed but the bounded model review did
  not; deterministic selected facts replace the model summary;
- `BLOCKED_EVIDENCE`: source evidence remained incomplete or conflicting after
  the bounded re-read;
- `INFRA_UNCERTAIN`: the artifact or transport could not be read reliably.

`RECHECK_REQUIRED` is an integrity concern. `MODEL_REVIEW_REQUIRED` is a
summary concern and permits one error-aware model retry against the exact same
immutable packet. If the retry still fails while integrity remains `pass`, the workflow emits
`DEGRADED_SUMMARY`, `summary_source=deterministic_fallback`, and
`hard_gate=pass`. Model formatting or availability must not become a new
single point of failure.

An EDA measurement may also end with a tool timeout such as exit `124`. The
workflow should preserve the current-run facts and expose that the measurement
window is incomplete; it must not infer an RTL or functional failure from the
timeout alone. The caller's controller decides whether to start another
guidance cycle, increase/change the measurement scope, or stop at a resource
limit. A timeout packet still cannot be used as complete-chip PPA, full STA,
or Fmax evidence.

## Controller integration

Use `scripts/evidence_workflow.py run` once at a stage boundary. The project
adapter supplies exact artifact references and a parameterized model argv
template; the Skill owns `prepare -> Evidence Model -> validate -> one
error-aware retry -> deterministic fallback`. The controller must not call the
Evidence Model, inspect the Catalog, construct a retry prompt, or choose the
fallback. The lower-level `prepare`, `validate`, and `render-retry` commands
remain available for tests and library integration, but are not separate
controller states. Store the resulting workflow JSON next to the session
metadata. The strategic Pro receives one immutable DecisionPacket
containing the validator-produced `strategic_view` and a compact deterministic
`allowed_actions` projection. It does not receive full execution profiles, the
Fact Catalog, raw history, separate progress text, controller-state copies, or
second copies of the structured Skill outputs. The controller validates the
Pro proposal against the exact packet it retained; it rejects an invalid
proposal instead of rewriting it into another action.

Read [the controller API](references/controller-api.md) for the command and
Python interfaces. The controller must treat `integrity_state=hold` or
`hard_gate=hold` as a hard stop for downstream work and must classify it as an
evidence problem rather than a functional or RTL failure. It may continue with
the deterministic fallback when `integrity_state=pass` and only the summary is
degraded or unavailable.
That hard gate is separate from the measurement outcome: a validated packet
whose current result says `measurement_status=incomplete_timeout` may be handed
to the next strategic cycle when the functional and preflight gates are clean;
the controller must not turn it into `blocked` merely because the child exit
code is `124`.

The controller must not query the Fact Catalog to reconstruct an execution
contract. Fact references are an Evidence Model interface; contracts are
source-authored control artifacts that the validator passes through only after
source integrity succeeds.
