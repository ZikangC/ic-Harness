# Controller API

The stable integration point is `closure_gate.run()`.  It is a pure local
operation: it reads evidence files and returns a compact audit dictionary. It
does not start synthesis, STA, SSH, Docker, an agent, or a PPA loop.

The current `gate_api_version` is 6. Consumers should require the semantic
fields below instead of copying phase-name sets into project controllers:

- `structural_repair_required`
- `diagnostic_required`
- `closure_gate_required`
- `measurement_incomplete`
- `measurement_rerun_admissible`
- `measurement_resource_blocked`
- `strategy_change_required`
- `timing_ready`

A bridge may add transport metadata such as `closure_governance_status`, but
it must not reclassify or merge a project-local audit. Missing Skill code,
invalid JSON, an incompatible API, or missing required fields is
infrastructure uncertainty and must fail closed.

```python
from pathlib import Path
from closure_gate import run

audit = run(
    feedback_path=Path("runs/current/feedback.txt"),  # optional
    probe_path=Path("runs/current/tool_probe.json"),  # optional but required by controller policy before costly actions
    netlist_path=Path("build/closure.v"),             # local to this process
    top="chip_top",
    required_bodies=("control_fsm",),
    artifact_output=Path("runs/current/closure_artifact.json"),
    policy_context={
        "measurement_budget_seconds": 7200,
        "max_measurement_budget_seconds": 7200,
        "exhausted_measurement": {
            "semantic_fingerprint": "sha256:design-a",
            "scope_kind": "full", "targets": [], "budget_seconds": 7200
        },
        "proposed_measurement": {
            "semantic_fingerprint": "sha256:design-a",
            "scope_kind": "targeted", "targets": ["block-a"],
            "intent": "verify_pending_transaction"
        },
    },
)
```

For a split local/controller and remote/execution-host setup, run the bundled
collector on the remote host, transfer only its JSON artifact, then call the
wrapper locally:

```python
audit = run(
    feedback_path=Path("runs/current/feedback.txt"),
    artifact_path=Path("runs/current/closure_artifact.json"),
)
```

The CLI offers the same contract:

```bash
python3 closure_gate.py \
  --feedback runs/current/feedback.txt \
  --probe runs/current/tool_probe.json \
  --netlist build/closure.v --top chip_top \
  --artifact-output runs/current/closure_artifact.json \
  --policy-json '{"measurement_budget_seconds":7200,"max_measurement_budget_seconds":7200}' \
  --output runs/current/sta_closure_audit.json
```

`feedback_path` is optional. Without it, the gate can still report
`closure_export_empty`, `closure_top_missing`, or `closure_body_missing` from
the netlist. If those structural checks pass, it returns `not_attempted`, not
a fabricated STA result. To classify read, link, or timing phases, a
measurement adapter must generate a small key/value evidence packet from tool
logs. The packet is deterministic harness output; an LLM may summarize it but
must not create it.

Before a costly or environment-sensitive command, the controller should run
`scripts/tool_probe.py` and pass its JSON with `probe_path`. A failed probe
returns `phase="tool_probe_failed"`; the controller must repair or stop rather
than ask an agent to guess and launch the full command. See
[the tool-probe contract](tool-probe.md).

The audit also distinguishes `closure_measurement_incomplete` from
`closure_export_empty`: the former requires timeout/truncation evidence and
returns `closure_repair_required=false` and
`repair_domain="measurement_budget"`. With raw current and maximum budgets,
`measurement_rerun_admissible` states whether a strictly larger legal budget
remains available. An empty export without timeout evidence remains a repair
gate.

`policy_context` is optional adapter-supplied state. It never changes the raw
tool classification unless the base phase is `closure_measurement_incomplete`.
The optional raw fields `measurement_budget_seconds` and
`max_measurement_budget_seconds` let the Skill derive whether an incomplete
measurement reached its legal per-measurement ceiling. Callers may omit both;
the gate remains usable, reports `measurement_rerun_admissible=null`, and does
not authorize a same-measurement rerun. Caller-produced retry counters and a
`max_budget_exhausted` semantic verdict are ignored.

When the maximum legal budget is exhausted, the gate returns
`phase="closure_resource_blocked"`, `measurement_rerun_admissible=false`, and
`strategy_change_required=true`.

When both descriptors are present, the result also contains
`measurement_policy`. Identity uses `semantic_fingerprint`, `scope_kind`, and
normalized `targets`; budget, intent, and action names do not change identity.
At a resource boundary, `same_measurement` is rejected, a real scope or
design/config change is eligible as a strategy change, and `unknown` fails
closed. For `same_measurement`, the proposed budget must be strictly larger
than the exhausted budget and no larger than the legal maximum. The Skill
never authorizes RTL optimization. Pass this verdict to the
transaction/lifecycle Skill that owns the proposed operation instead of
reimplementing the comparison in a controller.
