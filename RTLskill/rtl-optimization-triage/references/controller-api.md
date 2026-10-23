# Controller API

Project integration has one lifecycle entry:

```python
from optimization_campaign_runner import run_campaign
```

The project bridge may expose executor dispatch for requests emitted by that
campaign, but must not expose `initial`, `migrate`, `transition`, or `step` as
parallel project lifecycle entries.

The following CLI commands are Skill-internal diagnostics and unit-test tools;
they are not an alternative controller protocol:

CLI reads JSON from stdin:

```text
python3 optimization_gate.py triage
python3 optimization_gate.py validate
python3 optimization_gate.py transition
python3 optimization_gate.py initial
python3 optimization_gate.py migrate
python3 optimization_gate.py step
python3 optimization_gate.py evaluate-measurement
```

`triage` input contains `candidates`, optional `minimum_value_score`, and an
optional `runtime_context`. The project adapter normalizes live registry
integrity, closure evidence integrity, pending harness verification, functional
baseline, `current_blackboxes`, `pending_mainline`, and
the Closure Skill's deterministic `measurement_policy` into that context. It
must not supply a derived action or `mainline_execution`. The Skill derives the
current `optimization_phase` from its own durable state, then derives the
integration target and mainline execution profile and emits `insertion_gate`
and an `optimization_and_mainline_selection` execution contract.
Candidates declare historical and current integration facts separately:

- `expansion_verified_once`: at least one deterministic expansion verification succeeded;
- `expanded_verified`: whether the latest expansion attempt succeeded;
- `integration_state`: `integrated_verified`, `temporarily_blackboxed`,
  `measurement_incomplete`, `pending_reverify`, or `deterministic_failed`;
- `currently_blackboxed`: current top-level measurement policy only;
- `top_level_acceptance_pending`: local optimization still needs later integration acceptance.

`currently_blackboxed=true` is not by itself an exclusion. Never-verified,
deterministically failed, explicitly disallowed, and cooldown candidates are
reported in `ineligible_candidates` with reason codes.
Each candidate also carries `evidence_eligible` plus reason codes. The highest
eligible score at or above `minimum_value_score` produces an
`automation_intent`; ordinary Stage-1 selection does not require a Pro action.
Measured value fields are normalized to 0..1:

- `timing_value`
- `area_value`
- `replication_value`
- `evidence_confidence`
- `change_risk`
- `verification_cost`

When attributable quantitative value is absent, the candidate is ineligible
and must not auto-start an edit. `validate` then checks its selected module against the same packet and emits an
`insertion_plan` containing `integration_target`, `insertion_mode`, and
`top_level_acceptance_required`. A decoupled candidate that is still blackboxed
is rejected with `decoupled_candidate_still_blackboxed`.

`transition` and `step` are internal lifecycle APIs used by the Skill runners.
Project integration uses the campaign runner only. When rollback is required,
`step` emits `optimization.rollback`, and
`scripts/optimization_runner.py` restores and verifies the pre-image before it
records `rollback_complete`.

`begin_candidate` requires a Registry-derived `interrupted_mainline_ref` and
persists the insertion plan, `campaign_id`, `checkpoint_ref`, and `rtl_ref`.
Durable Optimization state must not update the live mainline.
`sta_passed` must include the real `sta_scope_gate`, `measurement_scope`, and
the successful result from `evaluate-measurement` as `value_evaluation`.
Local evidence may accept the experiment while leaving an `acceptance_debt`;
only top-level partial/complete evidence clears that debt.

`evaluate-measurement` accepts `before`, `after`,
`minimum_improvement_ratio`, and `maximum_regression_ratio`. It requires a
matching non-empty `comparability_key` plus positive area and delay values.
At least one metric must meet the improvement threshold and the other must not
exceed the regression tolerance. `incomparable` and `not_improved` are not
acceptance evidence; send `optimization_not_improved` and perform verified
rollback.

Every state returned by `initial`, `migrate`, `transition`, or `step` contains an
`execution_contract`. Internal edit, verification, retry, and rollback actions
are controller-facing and are not Pro choices. Evidence may summarize them but
does not authorize them.

## Step API

`step` input contains `state`, optional `automation_intent`, `candidate`,
`mainline_ref`, `campaign_id`, optional normalized `observation`, and threshold
`policy`. It returns updated `state`, `disposition`, and at most one
`requested_execution` with `executor_ref=optimization.edit`,
`project.verify_optimization`, `capability.resolve_sta_scope`, or
`optimization.rollback`. `project.verify_optimization` operations are
`functional_preflight` and `measure_candidate`; they must not be collapsed
into a project controller that reselects the phase. A project adapter
maps that request to paths and commands. It also maps raw project results to
one of `infra_uncertain`, `functional_failed`, `functional_passed`,
`sta_scope_resolved`, `sta_scope_unavailable`,
`external_verification_required`, `measurement_incomplete`,
`measurement_observed`, `verification_failed`, or `no_change` before calling
`step` again. `measurement_observed` carries normalized measurement fields and
artifact availability; the Skill alone decides completeness, comparability,
improvement, retry, or rollback. A shared STA run returns
`external_measurement_observed`; the Skill alone evaluates the fingerprint,
obligation identity, link/timing, scope, and timing-target guard.
Its optional `external_measurement.reported_incomplete` field is a normalized
semantic carried through the project envelope. When absent, Optimization does
not infer it from a process exit code and continues evaluating only the
evidence fields that are present.

The controller validates identity and dispatches; it does not map raw result
strings to phases, decide retry versus rollback, compare PPA values, or restore
the checkpoint itself. `max_internal_steps` is an independent campaign bound.
Internal steps do not consume a Pro guidance round and should not trigger an
Evidence Model call.

The active contract includes `allowed_actions`, `forbidden_actions`,
`action_parameters`, `execution_profiles`, `retry_budget`, and
`resume_context`. The generic controller checks action membership and parameter
enums. It must not reinterpret phases. In particular,
`functional_preflight`, `resolve_sta_scope`, and `measure_candidate` are
separate internal operations. None may invoke a planner/executor edit. A
measurement uses the contract's exact module, fingerprint, passing functional
and preflight evidence, partitions, and timeout; project code must not rerun a
different phase implicitly.

For a `resume_pending` state with a pending saved mainline, triage may include an
`execute_mainline_transaction` profile. A measurement operation is allowed
only when `measurement_policy.proposed_measurement_allowed=true`. Registry
pending/hold does not by itself block resuming that same transaction, but a
registry conflict, target mismatch, unavailable closure evidence, dirty
functional baseline, or rejected measurement relation does.

`measurement_incomplete` accepts `retry_attempt`. The initial measurement sends
`false`; each no-edit retry sends `true`. The default maximum is two retries.
When exhausted, the state enters `rollback_required`; only a verified
`rollback_complete` moves the candidate to `deferred`, clears
`current_candidate`, and enters `resume_pending`. `infra_uncertain` never
increments this counter. `defer_candidate` records the strategic intent but
also requires rollback. After executing the saved mainline, the project adapter
reconciles that exact Registry transaction with deterministic evidence, then
sends `mainline_resumed` with its exact `transaction_id` and Registry
confirmation; until then a new optimization candidate remains forbidden.

## Edit runner

`scripts/optimization_runner.py` reads one JSON request from stdin or a file.
Required project-provided fields include `project_root`, `state_path`,
`checkpoint_root`, `operation_id`, `module`, `rtl_path`, `model`, and a
parameterized `model_command`. For a new campaign also provide
`interrupted_mainline_ref` and insertion-plan fields. Success returns
`status=modified_pending_functional`; the next step must run the functional
gate, not another edit. `infra_uncertain` means the runner restored the
pre-image and did not classify the RTL candidate as bad.

## Campaign runner and external STA obligation

`scripts/optimization_campaign_runner.py` is the sole project-facing lifecycle
entry. It accepts transient triage/Registry facts, a Skill-owned `state_path`,
a separate `observation_path`, and project callbacks. The packet must not carry
durable `state` or a pending `observation`, and there is no project persistence
callback. The runner loads, initializes, migrates, and atomically persists the
bare governance state itself. It dispatches only executor references explicitly
allowed by the caller, persists state before dispatch, and returns at the first
undispatched executor, strategic handoff, or explicit process boundary. It
never imports a project adapter. A normal integration should allow
`optimization.edit`, `project.verify_optimization`, and
`capability.resolve_sta_scope`, and `optimization.rollback`, so edit,
functional/preflight, scope resolution, local measurement, bounded retry, and
rollback remain in one campaign invocation. The execution callback may return
an explicit `boundary` for user stop, resource exhaustion, or concurrency;
these are not converted into RTL failures.

An external/shared STA result is the only temporary cross-invocation input. The
controller writes one normalized observation as a separate opaque JSON
envelope and must refuse to overwrite an unconsumed envelope. On the next
campaign invocation, the Skill reads it, applies the idempotent
`observation_id`, persists the resulting governance state, and only then
deletes the envelope. The controller never reads the governance file to decide
whether this result is pending.

When candidate verification receives `top_level_partial` or
`top_level_complete` from the STA-scope gate, it returns
`external_verification_required`. The Skill enters
`waiting_external_verification` and emits one `verification_obligation` bound
to campaign, module, RTL fingerprint, and required scope. Project code may
match that obligation to a compatible mainline measurement. The eventual
measurement returns as a normalized observation; an Evidence Model summary
cannot satisfy or reject the obligation.

A project may accept one shared measurement without module-level causality
attribution only through a deterministic combined guard. That guard must at
least prove unchanged candidate identity, successful link, real timing paths,
required scope coverage, and the project's timing target. Partial top-level
coverage remains provisional and creates final complete-top acceptance debt.

Functional and preflight reuse each require an explicit passing proof naming
the exact current RTL SHA-256 and managed-input context fingerprint. Matching
candidate identity without that proof does not authorize skipping either
check, and a filelist/harness change invalidates the old context.
