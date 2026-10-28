# Optional readiness integration

This isolated harness exposes `architecture`, `rtl`, and `readiness`. The readiness
entry imports an existing RTL seed into a fresh run and runs a bounded investigation;
it performs no RTL regeneration or full synthesis. By default it reports only the
local investigation and does not declare RTL release. The standalone skill can
complete diagnosis, bounded experiments, comparisons and candidate modifications
without another skill. Its candidate remains unreviewed until downstream review.
Formally adopting modified RTL in this project requires applicable independent
RTL audit; that requirement does not become an internal standalone dependency.
The native release gate requires the candidate-bound independent audit decision
and applicable structural evidence. Provisional readiness adoption into live work
is not release, and an unreviewed candidate cannot be formally released.

The RTL audit skill performs one independent review and publishes its source-bound
report. Scheduling lives in `harness/adapters/rtl_audit.py`, outside the skill package.
`review_once(context, remote)` never selects or invokes a repair skill.
`run(context, remote, repair_handler=...)` can continue the review/repair loop only
with an explicit caller-selected `RepairHandler`; without one, `revise` is returned
unchanged. `insufficient_evidence` and `blocked` return without source repair.
Ordinary RTL generation explicitly selects the implementation repair handler.
The caller also selects the review scope. Ordinary `rtl` always requests
`candidate`, including after a Readiness hook adopts a candidate. Standalone
`readiness` defaults `[readiness] review_scope = "change"`; set it to `"candidate"`
before initializing a new run when whole-candidate acceptance is intended. Reports,
decisions and session receipts retain the exact scope and candidate identity.
A `change` pass completes independent review of the declared change and affected
behavior, yields exit zero, and leaves `rtl_stage_release = false`. It does not
close inherited whole-design obligations. A later candidate-scope request cannot
reuse that narrower pass and still needs the complete applicable native structural
gate. Missing, stale, failed, cancelled or timed-out structure evidence remains
unclosed in both scopes and must never be called structural acceptance.

Its separate `[rtl_audit] max_repairs = 3` limit is frozen with the run and
retained across interruption. The caller routes from candidate origin and the
current flow node, rather than inferring an executor from report prose.

The normal `harness/config/rtl.toml` keeps `[readiness] enabled = false` and
`rtl_review = false`, preserving ordinary RTL generation without an automatic
Readiness campaign. The explicit `readiness` CLI stage is itself a trigger and
freezes `enabled=true` for its independent work; it does not enable formal RTL
release. For an explicit integrated optimization workflow, use a new
configuration/profile with both `enabled = true` and `rtl_review = true` before
initializing its run. Leave `region_roots` and `build_files` empty unless this
project explicitly supplies a regional policy; historical experiment filelists
are not defaults. After a provisional candidate has been adopted, the readiness
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

The supplied `harness/config/rtl.toml` sets `[readiness] max_review_repairs = 3`.
If omitted, `stages/rtl.py` defaults this review-repair bound to `1`;
`runtime/rtl_readiness.settings` separately supplies campaign/job/watchdog limits. The
controller records actor, limit and reserved review/candidate/report bindings on the
server; resuming does not reset the allowance. A further `revise` at the limit remains
unreleased. No implementation skill is required for this route, and a missing
selected executor never silently falls back to another skill. A resumed audit
continues its recorded review/repair state without restarting the readiness campaign.
A pass closes this loop in the controller without starting a Readiness closeout
session. Budget exhaustion preserves the candidate, report and unresolved findings
without release. Unchanged source, architecture and evidence do not trigger an
equivalent review or empty repair attempt. `blocked` and `insufficient_evidence`
remain open until applicable new source/evidence or a verified environmental
change supports an explicit continuation.
Interrupting the readiness entry preserves persistent jobs, including during its
formal review/repair phase; an explicit identical-version resume observes them.
If no candidate was adopted, enabling review does not turn a negative experiment
into a new RTL acceptance run. The optional campaign engineering review remains an
experimental-evidence gate; it does not replace this independent RTL audit.

Enable `[readiness] enabled = true` in the explicit integration configuration to
insert the same hook after initial generation and before the first independent
RTL candidate freeze.
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
can hold exact original RTL at the same paths plus frozen probe/test inputs.
Do not add an enclosing `work/` prefix to original source identities. A probe
wrapper is not an integration RTL addition; a separately extracted probe remains
scoped evidence until a matching original-source candidate is verified.

For regional restructuring, explicitly configure the campaign policy:

```toml
[readiness]
enabled = true
region_roots = ["rtl"]
build_files = ["sources.f"]
```

These are project/run settings, not universal chip parameters. The named project
filelist must already be in the imported work tree and use the standalone
helper's supported plain ordered source-list format. Its bytes join the campaign
source identity and baseline guard. Initialize the standalone workspace with the
same `--region-root` and `--build-file` values. New candidate modules, their callers
and changed filelists may then form one API3 candidate. Existing-file API2
overlays retain their original policy and identities.

When the live project has `RTL_STRUCTURE.json`, a regional handoff must also
include its exact candidate bytes and a configuration matching the effective
ordered source build; stale or omitted declarations are rejected before
publication. The current standalone helper parses every `build_files` item as a
plain source filelist and cannot carry this JSON declaration. That native regional
end-to-end route remains unavailable until a separate hash-bound non-filelist
build-artifact interface exists. Adapter positive tests use injected verification
and establish only publication/identity behavior; they do not close that interface.

The skill chooses a hardware responsibility boundary; a filename or edit count
does not decide architectural significance. Preserve committed cycle/interface,
numeric and resource behavior. A contract change is a proposal to the caller's
architecture flow. The independent RTL audit remains the same stage, and a revise
report returns to the explicitly selected readiness repair executor.

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
allowed changed original RTL can enter legacy provisional work. With an explicitly
matching regional policy, the consumer instead verifies the full add/modify/delete
operation set and baseline/candidate builds, then stages and publishes all changes
with rollback and a durable publication journal. Changed effective builds require
a declared project filelist for native adoption, so the updated source list cannot
exist only in a report. Native inventories currently support `.v/.sv/.vh/.svh`;
other header formats can remain standalone evidence but need an appropriate native
consumer before automatic adoption. Tests and probe-only paths remain frozen.

The plain filelist carries source order, not include-directory flags. This first
native consumer therefore requires unchanged `include_dirs`; a standalone
candidate with changed include configuration stays a proposal until a consumer
can bind and publish the actual project build configuration. Do not infer that
recording a new include path in a receipt updates the project's build.

Publication is a multi-file transaction, not a filesystem-wide atomic swap. A
crash leaves a journal/backups for explicit inspection and recovery; it never
automatically resubmits a model or EDA job. Seed resume accounts for authorized
deletions, newly adopted files and project filelists before and during the existing
formal RTL review/repair flow. Ordinary independent RTL review freezes the whole
result as a new candidate. A scoped local result does not establish full-design
release, and no new full synthesis starts automatically.
