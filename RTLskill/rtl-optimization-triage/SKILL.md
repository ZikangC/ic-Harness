---
name: rtl-optimization-triage
description: "Triage whether a verified RTL module is worth optimizing, plan a safe insertion around an independent integration mainline, and govern bounded functional repair, STA acceptance debt, rollback, and mainline recovery. Use during staged integration or modular PPA work; do not use it alone as authorization to edit RTL or as timing proof."
---

# RTL Optimization Triage

Use this skill when a project can insert one serial, evidence-backed RTL
optimization campaign into a larger integration mainline. It deterministically
selects the highest-scored eligible module above a configured threshold,
invokes a parameterized one-shot edit model through its own recoverable runner,
and preserves a validated reference to the exact Registry-owned mainline.

The skill defines candidate value, insertion plan, owns the durable lifecycle state,
single-file checkpoint/edit enforcement, and a deterministic `step` API. Each
step emits one portable `requested_execution` or one mainline handoff. The
project adapter supplies paths, model argv, verification commands, and maps
project results into the observation schema. The controller only launches the
requested executor and returns its normalized observation; it must not
re-implement `measurement_pending` or ask Pro to select an ordinary Stage-1
candidate.

Read [the state schema](references/state-schema.md) when persisting a campaign
and [the controller API](references/controller-api.md) before integration.

## Invariants

- Require a validated `interrupted_mainline_ref` captured from Module Registry
  before starting a candidate. Never update the mainline from Optimization.
  Success, rejection, or rollback enters `resume_pending` until Registry
  confirms the exact referenced mainline is complete.
- Require attributable quantitative evidence and a deterministic score at or
  above `minimum_value_score`. The highest score auto-starts Stage-1; do not ask
  Pro to choose or authorize that normal-path candidate.
- Use `scripts/optimization_runner.py` for edits. It must journal
  `edit_prepared`, checkpoint the project pre-image, call the configured model,
  accept exactly one changed RTL file, and stop at
  `modified_pending_functional`.
- Use `scripts/optimization_campaign_runner.py` to keep deterministic
  edit/repair/local-verification/rollback iteration inside the Skill until a
  strategic handoff or process-safety boundary is reached. The project
  supplies callbacks for command materialization, execution, and observation
  normalization; the portable runner must not embed project paths or commands.
- Resuming the saved mainline is a contract action, not an overloaded
  `continue` convention. A measurement resume requires a deterministic Closure
  Skill verdict. An exhausted `same_measurement` remains forbidden; a verified
  strategy change may resume.
- Keep historical expansion proof separate from current integration policy.
  A module with `expansion_verified_once=true` may remain locally optimizable
  while `integration_state=temporarily_blackboxed`; it must then carry
  `top_level_acceptance_pending=true`. Current blackbox membership alone must
  not erase prior verification.
- Rank value from measured timing/area/replication evidence, confidence,
  verification cost, risk, and prior failures. Quantitative attribution is a
  hard eligibility condition for autonomous Stage-1 selection; a model may
  explain source-level opportunity and risk but may not invent PPA numbers or
  turn an ineligible candidate into an eligible one.
- A deterministic functional failure blocks all STA. Repair only the same
  candidate within a configurable limit. When the limit is exhausted, restore
  the candidate checkpoint, record the failed hypothesis, and choose another
  candidate or resume the mainline.
- Infrastructure uncertainty never consumes an RTL repair attempt and never
  proves the candidate bad.
- Measurement timeout preserves the candidate in `measurement_pending`; it is
  not a deterministic STA failure and consumes no RTL repair attempt.
- Missing/truncated measurement artifacts are also
  `measurement_incomplete`. A completed measurement whose comparability key
  differs from the baseline is deterministically incomparable and must be
  rolled back/deferred rather than retried as a timeout.
- Permit one initial measurement plus at most two no-edit measurement retries
  by default. A retry must verify the preserved candidate. It must not invoke
  an RTL editor. Once the retry budget is exhausted, enter
  `rollback_required`; only a controller-verified restoration may archive the
  candidate as deferred and move to `resume_pending`. Infrastructure failures
  do not consume this domain retry budget.
- Persist the candidate RTL fingerprint, functional evidence, and Yosys
  preflight evidence. Reuse either proof only when it explicitly passed and is
  bound to the exact current RTL fingerprint and managed-input context
  fingerprint. RTL identity alone does not prove that a check ran or remains
  applicable after harness/filelist changes.
- After functional success, use a separate STA-scope gate to choose local,
  affected-cone, or top-level measurement. This skill does not upgrade scope.
- For `top_level_partial` or `top_level_complete`, do not start a duplicate
  top-level run inside candidate verification. Enter
  `waiting_external_verification` and emit one fingerprint-bound
  `verification_obligation`. A project adapter may merge it with compatible
  mainline work; only a later normalized observation can resolve it.
- Treat the integration target `E` and optimization target `O` independently.
  `O=E` is `fused_same_unit`: optimize first, then use the resulting top-level
  measurement to accept the original integration transaction. `O!=E` is
  `decoupled`: preserve `E` and resume it after the experiment.
- Do not expose a second blackbox merely to optimize it. A decoupled `O` must
  already be represented by RTL, unless a project supplies a separate explicit
  local-only adapter and transaction policy.
- A local or affected-cone STA pass is not top-level acceptance. Record
  `acceptance_debt` when top-level acceptance is required, and clear that debt
  only with matching top-level evidence.
- Track separate verification obligations for optimization effect `O` and
  integration acceptance `E`. Actual RTL diff plus current bounded design
  context must go through the STA-scope gate before either is credited.
- Evidence summaries may explain a bounded Skill-produced projection to Pro,
  but do not own candidate selection or edit authorization. Controllers and
  adapters must not read or mirror durable governance state; they consume only
  campaign boundary outputs, the evidence projection, and an external handoff.
- Count internal edit, functional/STA verification, retry, and rollback steps
  against `max_internal_steps`. These steps do not consume the enclosing Pro
  guidance budget and do not require a fresh Evidence Model summary. When the
  internal budget is exhausted, defer and roll back the candidate.

## Step and execution boundary

The campaign runner calls the internal `optimization_gate.py step` lifecycle
with Skill-owned durable state, optional `automation_intent`, and at most one
project-normalized observation. Project code calls only the campaign runner and
passes `state_path` plus the separate external-observation path; it must not
pass state or a persistence callback. Each internal step contains at most one
`requested_execution` using one of these portable executor refs:

- `optimization.edit`
- `project.verify_optimization`
- `capability.resolve_sta_scope`
- `optimization.rollback`

The adapter maps project refs to paths and commands and maps the generic STA
capability input/output without importing that Skill. The controller does not
select among them. The campaign runner may dispatch all four in one
campaign invocation. Project result names such as `accepted`, raw process exit
codes, artifact availability, and measurement fields must be normalized before
they return to `step`; the adapter must not decide measurement completeness,
PPA improvement, shared-STA acceptance, retry, or rollback.
An external observation may carry the optional normalized
`reported_incomplete` semantic from Closure through a generic envelope. The
Optimization Skill never imports or directly invokes Closure, and it never
reconstructs that semantic from `ppa_exit`.
Only a shared/global STA obligation, user stop, resource limit, concurrency
guard, or infrastructure uncertainty returns control to the outer workflow.

For an active candidate the contract declares allowed and forbidden actions,
action parameter enums, retry budget, no-edit constraints, execution profiles,
and the exact interrupted Registry reference. In `editing` or `functional_repair`, the
contract permits only the same candidate's bounded edit. In `sta_required`,
`measurement_pending`, or `infra_pending`, it permits no-edit verification or
bounded retry and forbids another RTL edit. In `rollback_required`, it
permits only rollback and read-only evidence actions. After verified rollback
or accepted STA, `resume_pending` blocks a new optimization until Module
Registry confirms that the referenced mainline transaction completed
or otherwise reached a deterministic terminal state.

Pro remains responsible for broader integration and closure strategy. Internal
Stage-1 edit, verification, bounded retry, and rollback are not exposed as Pro
choices. The controller checks identity and dispatches the single Skill
request through a policy-free project process port. The legacy derived
`execution_contract` remains a diagnostic view;
it is not a second state machine or the source of internal dispatch.

When no candidate is active but a saved mainline transaction is pending, the
selection contract may also cover its Skill-derived action. Its profile carries
generic `operation_kind`, `intent`, `transaction_kind`, and the Closure Skill's
`measurement_policy`; the adapter supplies only raw Registry/Closure facts and
project module names and command sequences remain opaque adapter parameters.
