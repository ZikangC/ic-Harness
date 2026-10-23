# Optional readiness integration

This isolated harness exposes `architecture`, `rtl`, and `readiness`. The readiness
entry imports an existing RTL seed into a fresh run and runs a bounded investigation;
it performs no RTL regeneration or full synthesis. By default it reports only the
local investigation and does not declare RTL release.

The RTL audit skill performs one independent review and publishes its source-bound
report. Scheduling lives in `harness/adapters/rtl_audit.py`, outside the skill package.
`review_once(context, remote)` never selects or invokes a repair skill.
`run(context, remote, repair_handler=...)` can continue the review/repair loop only
with an explicit caller-selected `RepairHandler`; without one, `revise` is returned
unchanged. `insufficient_evidence` and `blocked` return without source repair.
Ordinary RTL generation explicitly selects the implementation repair handler.

For an isolated optimization workflow, set `[readiness] rtl_review = true` before
initializing its run. After a provisional candidate has been adopted, the readiness
entry invokes the same independent RTL audit skill and explicitly selects
`readiness_repair` for `revise` outcomes. This executor loads the readiness skill,
uses the configured implementation model, edits only the live repair work, and
keeps the frozen candidate and audit evidence unchanged. Its remote tools exclude
architecture delegation and its model session uses the readiness watchdog limits.
The worker also limits each repair's native jobs using the frozen `max_jobs`,
`max_job_seconds` and `tool_seconds`. Its controller receipt binds that policy to
the reviewed candidate and report; omitting a model request flag cannot disable
it. Queued, failed and completed jobs all retain their timeout reservation across
resume. This bounds job count and admitted tool time; it does not count variants
created inside arbitrary helper scripts. It performs bounded experiments rather
than full-design synthesis. Audit scope and
delivery objectives still come from the caller; a local result alone is not release.

`max_review_repairs = 1` is the default bound for that opt-in readiness loop. The
controller records actor, limit and reserved review/candidate/report bindings on the
server; resuming does not reset the allowance. A further `revise` at the limit remains
unreleased. No implementation skill is required for this route, and a missing
selected executor never silently falls back to another skill. A resumed audit
continues its recorded review/repair state without restarting the readiness campaign.
Interrupting the readiness entry preserves persistent jobs, including during its
formal review/repair phase; an explicit identical-version resume observes them.
If no candidate was adopted, enabling review does not turn a negative experiment
into a new RTL acceptance run. The optional campaign engineering review remains an
experimental-evidence gate; it does not replace this independent RTL audit.

Enable `[readiness] enabled = true` in the isolated configuration to insert the same
hook after initial generation and before the first independent RTL candidate freeze.
The hook is disabled by default. A missing optional skill also skips it. Initial
generation and independent audit retain their ordinary behavior. Repair campaigns
require `after_repair = true`; campaign and source identity limits still apply.

Fresh readiness entry (all paths refer to the isolated project/server):

```bash
python3 harness/run.py readiness --config harness/config/environment.toml \
  --run-id readiness-seed-01 --seed-work /work/isolated-source/output/seed/work \
  --seed-file rtl/top.sv --seed-file rtl/helper.sv --seed-file tests/regression.py \
  --seed-provenance seed-provenance.json
```

The seed provenance JSON records the original run, frozen candidate and applicable
architecture/report identities. Native seed import requires a fresh empty work
tree, copies only explicit files, and preserves its read-only source. Include the
actual ordered source closure, generated headers, memory inputs and verification
assets; source discovery is a model task, never a fabricated synthesis manifest.
Use the exact same code/config/inputs and `--resume RUN` without seed options to
inspect an interrupted run. Existing same-source attempts are retained without
automatically invoking another model, including incomplete or active attempts.

The adapter supplies absolute `baseline_project_root=<run>/work` in the model's
context as the default `init --project`. A staging project within campaign work
is also allowed: copy exact original RTL at the same `rtl/...` paths, add frozen
probe sources/test assets, and initialize the staging root. Explicitly repeat
`--allow-edit` only for actual original RTL that may change; new wrappers stay
frozen sources and tests stay frozen assets. Default editability would include
new source wrappers, which cannot be integrated as original-file edits. An
enclosing `work/` prefix changes original file identities and prevents guarded
integration. Separately extracted probes without original path/byte identity
remain evidence until a matching original-source candidate is verified.

Each campaign has separate `readiness/campaign-N/work` and `audit` scopes. Readiness
and engineering-review actors receive only the remote workspace tool, with the
baseline and all other scopes read-only. Existing implementation/review model
configuration is reused. Job limits persist on the server; model watchdogs use
campaign-specific session seconds and assistant-turn limits. These optional limits
do not change normal RTL session behavior.

The scheduler owns persistent outer Docker jobs. Run standalone experiment workers
with `run --foreground` inside those jobs, poll the outer job, and inspect the inner
receipts only after outer completion. Inner PIDs belong to another container
namespace. Outer exit zero does not establish measurement or functional success.
Inspect the inner `result.json`, `tool.log` and execution artifacts in foreground
mode; `worker.log` may be absent because only detached launch creates it. Every
submission, including a cheap helper, consumes a native job/timeout reservation.
Prefer remote read/list and batch independent inspections to reserve experiment
capacity. Choosing a smaller hierarchy top with all source files can still incur
full frontend cost before pruning; select the necessary dependency closure or an
explicit source-bound probe.

After measurements and functional checks, a single standalone `finalize` command
compares and atomically exports `comparison.json`, `candidate.json`, `candidate.patch`,
`delivery.json` and `finalization.json` under a new workspace output directory. It
runs no EDA, changes no proof threshold and creates no harness-private state. The
model can end after that persistent outer job; it need not copy identities into a
separate JSON report. Legacy campaign `result.json` reports remain supported when
there is no trusted finalization, including early evidence-insufficient outcomes.

The controller-only `rtl_readiness_resolve` reads at most the campaign-budget native
job candidates. It recognizes only configured Python (with optional `-B`) invoking
the exact frozen readiness helper's `finalize`, resolves relative workspace/output
arguments against recorded cwd, requires successful container cleanup and current
campaign source pins, then freshly calls `verify-finalization`. Failed jobs, forged
commands, escaped/aliased paths and corrupt bundles do not count; distinct valid
finalizations are conservatively ambiguous. Discovery does not scan arbitrary
workspace trees or expose another model capability. Native completion repeats
resolution of the selected actual job rather than trusting a transported report.

A verified negative finalization may finish an evidence campaign even when its
implementation model hit a watchdog. The controller records the actual model's
completion/isolation/budget status separately and does not label that session a
natural completion. A positive finalization still requires the original completed
isolated implementation actor, a fresh independently scoped engineering review
bound to the actual source/comparison IDs, and the unchanged-baseline/core guards.
Review uses the discovered bundle paths, not an assumed root `result.json`. Only
allowed changed original RTL can enter provisional implementation work; tests and
probe-only paths cannot be adopted. Ordinary independent RTL review then freezes a
new candidate. A scoped local result does not establish full-design release.
