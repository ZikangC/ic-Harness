# Readiness diagnosis

The readiness skill supports a read-only diagnosis before a separately authorized experiment campaign. The runner still has only `architecture`, `rtl` and `readiness` stages. This feature does not add a full synthesis or STA entry point.

For a fresh diagnosis, use the existing readiness seed options plus:

```
python3 harness/run.py readiness --config harness/config/environment.toml \
  --run-id <new-run> --task <task.txt> \
  --seed-work <absolute-server-source-root> \
  --seed-file rtl/top.sv --seed-file rtl/dependency.svh \
  --seed-provenance <local-provenance.json> \
  --diagnose-only --diagnosis-inputs <local-evidence-index.json> \
  --diagnosis-event observation_budget
```

The four event names are `rtl_review_passed`, `stage_completed`, `observation_budget`, and `job_failed`. The caller selects an actual observation opportunity; an event never proves a defect. `diagnosis_seconds` and `diagnosis_turns` in `[readiness]` bound the model session, falling back to existing readiness session budgets. Any observation budget is caller supplied metadata, without a built-in polling schedule.

The evidence input is a small JSON object:

```
{
  "version": 1,
  "source": {"candidate_sha256": "<frozen-candidate-identity>"},
  "files": [
    {"name": "sources.json", "path": "/server/run/source_manifest.json", "kind": "metadata", "sha256": "<optional-frozen-hash>"},
    {"name": "coarse.log", "path": "/server/run/logs/coarse.log", "kind": "log"},
    {"name": "status.json", "path": "/server/run/status.json", "kind": "status"}
  ],
  "processes": [{"pid": 123, "start_ticks": 456, "boot_id": "<recorded-boot>", "kind": "synthesis"}],
  "metadata": {"top": "top", "ordered_sources": ["rtl/top.sv"], "parameters": {}},
  "observation": {"budget_seconds": 3600}
}
```

Use existing absolute plain server files with unique safe names. Kinds are `source`, `log`, `status`, `report`, `metadata`. Include actual tool/configuration/constraints identities and report navigation when available. Do not embed whole source or large logs. Explicit files are copied to the new run as finite snapshots; large checkpoints can be listed in metadata without copying. Live log capture preserves the initial-size prefix, hash and capture point. Current sources and exact actor invocation are bound to the resulting report.

The diagnosis model receives the existing skill and one index path. It can list/read/query status and jobs, with no edit, submit, cancel, architecture or RTL-release actions. It returns ordinary final JSON with `advice`, `summary`, `findings` and optional `experiments`; native completion preserves failed or exhausted outcomes as incomplete. Controller records appear under `control/diagnosis/<observation-id>/` and ordinary session receipts under `control/`.

To reobserve an existing run use `--resume <run>` instead of `--run-id` and omit seed options; keep the original task/configuration. Meaningfully identical evidence reuses the original observation, including an incomplete attempt; it does not automatically restart a failed actor. New source, logs, outcomes or applicable top/configuration can justify a new observation. Timestamps and runtime counters alone do not. Deduplication is scoped to one isolated run, not global across projects/runs.

To permit a recommended experiment, omit `--diagnose-only` and add `--allow-experiments` with diagnosis inputs/event. This requires completed advice `small_experiment`, existing readiness campaign budgets, no conflicting jobs and a caller configured positive `experiment_min_available_memory_mib`. Missing resource admission settings deny dispatch. One admission reserves one dispatch; an interrupted experiment is inspected through the existing readiness/audit path rather than silently resubmitted. Optimization then uses existing comparisons, functional checks, independent engineering review and optional formal RTL review/repairs. Full candidate synthesis remains a separate scheduling decision.

Without this controller, use the same skill with ordinary files and basic tools: inspect the evidence read-only, publish a report, and invoke bounded optimization only under a separate authorization/budget. The skill does not depend on this JSON index or runtime vocabulary.
