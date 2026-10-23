# Controller API

The implementation is `scripts/evidence_workflow.py`. It has no fixed network,
EDA, Docker, or model-client dependency. The project adapter supplies a model
argv template; the Skill executes it directly without a shell.

The production controller uses one command:

```bash
python3 scripts/evidence_workflow.py run \
  --stage sta_health \
  --run-id 02_yosys_ppa--example \
  --contract /path/to/project/evidence_contract.json \
  --artifact result=runs/agent-example/result.json \
  --artifact feedback=runs/ppa-example/feedback.txt \
  --artifact skill_module_registry=session/skill_module_registry.json \
  --work-dir session \
  --run-label 7 \
  --project-root /path/to/project \
  --model evidence-model-selector \
  --model-command-json '["omp","-p","--cwd","{project_root}","--model","{model}","{prompt}"]' \
  --output stage_evidence_workflow.json
```

The argv template may use `{prompt}`, `{prompt_file}`, `{project_root}`,
`{work_dir}`, and `{model}`. The Skill owns prepare, model invocation,
validation, one informed retry, and deterministic fallback. A completed run
returns exit 0 even when evidence integrity holds the domain gate; callers read
`hard_gate` and `integrity_state`. Nonzero exit means invocation or workflow
transport failure.

The following lower-level commands remain for tests and library integration;
they are not separate production-controller states.

Prepare a one-stage model input. Primary run artifacts and current-stage
context are passed explicitly:

```bash
python3 scripts/evidence_workflow.py prepare \
  --stage sta_health \
  --run-id 02_yosys_ppa--example \
  --contract /path/to/project/evidence_contract.json \
  --artifact result=runs/agent-example/result.json \
  --artifact feedback=runs/ppa-example/feedback.txt \
  --artifact progress_delta=session/current_progress_delta.json \
  --artifact controller_state=session/controller_state.json \
  --artifact run_evidence=session/run_evidence.json \
  --artifact skill_module_registry=session/skill_module_registry.json \
  --artifact skill_closure_governance=session/skill_closure_governance.json \
  --artifact skill_optimization_triage=session/skill_optimization_triage.json \
  --artifact trend_summary=session/trend_summary.json \
  --max-facts-per-skill 16 \
  --max-total-facts 60 \
  --output stage_evidence_input.json \
  --prompt-output evidence_model_prompt.md
```

The Skill sends only `evidence_model_prompt.md` to the separate Evidence
Model. The prompt contains the focused stage objective, `packet_fingerprint`,
bounded Catalog, and optional short diagnostic excerpts. The model returns
topic-oriented `evidence_groups` containing current `fact_refs`; it does not
copy source values, paths, identities, or types. The prompt contains no
task-wide rules or automatically discovered history. `trend_summary` is opt-in
and must be a compact controller-generated comparison, never raw progress
history.

Production integrations must pass a project-owned `--contract`. The contract
path and content belong to the harness, not this Skill. The requested stage
must match the contract's `stage`; a mismatch is rejected before model review.
Calling `prepare` without a contract is supported only as a generic library
mode and creates no project-specific required fields.

`run_evidence` is optional and current-stage-only. It may contain source run
identity, selector, returned/available counts, truncation state, and
deterministic path summaries. It must not copy cell-by-cell path reports or
other raw forensic text; those stay in the immutable run.

Every `skill_*` artifact uses the envelope described in `stage-schema.md`.
Include only Skills relevant to the declared stage. Their hard gates remain
source-authored and controller-enforced; the model output is only their compact strategic summary.
Do not also supply a semantically equivalent non-Skill artifact. For example,
when `skill_closure_governance` already contains the current closure audit,
omit a second `sta_closure_audit` copy.

`prepare` runs once and creates an immutable packet plus bounded Fact Catalog.
The Evidence Model sees current evidence fact values but returns only topic groups,
`fact_refs`, and prose. The full catalog is retained for validation; only the
validator-produced `strategic_view` may be sent to the strategic agent.

A Skill envelope may include `execution_contract`. The validator checks the
complete contract independently of the model's fact selection and returns:

```json
{
  "validated_execution_contracts": {
    "skill_optimization_triage": {
      "source_identity": {"registry_generation": 7},
      "contract": {"allowed_actions": ["retry_measurement"]}
    }
  }
}
```

The execution contract is excluded from the Fact Catalog and model prompt. A
deterministic project adapter reads this field, creates a compact
`allowed_actions` projection, and combines it with `strategic_view` in one
immutable DecisionPacket. The controller retains that same packet and performs
only identity, action-membership, and parameter checks. It must not ask the
Evidence Model to copy a contract, resolve it from `fact_refs`, reconstruct it
from prose, or rewrite a rejected proposal. If contract identity, generation,
action-set consistency, or packet integrity fails, `integrity_state` holds the
gate.

The low-level validator command validates the model's JSON:

```bash
python3 scripts/evidence_workflow.py validate \
  --input stage_evidence_input.json \
  --model-output evidence_model.json \
  --attempt 0 \
  --output stage_evidence.json
```

If the first review reports `summary_state=retry_required`, render the only
permitted retry from the same packet and first review:

```bash
python3 scripts/evidence_workflow.py render-retry \
  --input stage_evidence_input.json \
  --review stage_evidence_attempt0.json \
  --output evidence_model_retry_prompt.md
```

The Python API is:

```python
from evidence_workflow import (prepare, render_retry_prompt, run_workflow,
                               validate_model_output)

result = run_workflow(
    stage="sta_health", run_id="run-a",
    artifacts={"result": Path("result.json")},
    contract=contract, work_dir=Path("session"),
    project_root=Path("/path/to/project"),
    model="evidence-model-selector",
    model_command=["model-client", "--model", "{model}", "{prompt_file}"])

packet = prepare(stage="sta_health", run_id="run-a",
                 artifacts={"result": Path("result.json")})
review = validate_model_output(packet, model_output, attempt=0)
retry_prompt = render_retry_prompt(packet, review)
```

`attempt=0` permits one bounded Evidence Model review. There is no second
`prepare`: `render-retry` consumes the same packet and validator-produced
`retry_context`, so the controller never queries Catalog internals. If source
integrity passes but the second model review still fails, `validate` emits
complete deterministic `validated_evidence` and `strategic_view` with
`hard_gate=pass`. Only unresolved source integrity failure becomes
`BLOCKED_EVIDENCE`; it must not be converted into an RTL failure or used to
launch PPA.

The project owns model selection and transport through its adapter. Its Evidence Model parameter
should default to the execution/Flash model, with an explicit override such as
`--evidence-model <selector>` for isolation or evaluation. The Skill contains
no hard-coded LLM client; it invokes only the supplied argv template. An
Evidence Model process failure is a summary-availability problem when the
prepared source packet has `integrity_state=pass`; the Skill retries once and
then uses the deterministic fallback. An unreadable result, feedback,
or required Skill envelope remains an infrastructure/source-integrity failure.

For a measurement result with tool exit `124`, the caller should first verify
that the functional and synthesis-preflight fields are successful. If they are,
keep the packet as current-stage evidence with an explicit
`measurement_status=incomplete_timeout`, preserve any pending experiment or
structural verification, and let the next controller guidance cycle consume
this packet. Do not map this state to an RTL failure or generic blocked state.
The controller may still stop on its configured cycle, Flash-attempt, PPA-time,
wall-clock, or user-stop limits. Infrastructure exits and functional/preflight
failures retain their project-specific hard-gate semantics.
