# Standalone commands and evidence

Run on the machine authorized for EDA. The helpers need Python 3.10+ on Linux/macOS, standard library only. Yosys is optional for analysis/export and required for the built-in probes. The invoking model creates the RTL overlay; there is no second model service to configure. Keep this entire skill directory together; no files outside it are imported.

Set `S` to this skill's directory and choose a new `W` within the project's output area. Examples use arbitrary names; substitute the actual design and budgets.

```bash
python3 -B "$S/scripts/readiness.py" init --project /project --workspace "$W" \
  --top block_top --source rtl/block.sv --source rtl/helper.sv \
  --include generated --asset tests/check.py --asset tests/data
python3 -B "$S/scripts/readiness.py" analyze "$W" --out "$W/analysis.json"
```

`--source` is ordered and repeatable; exclude simulation-only modules. Relative paths are rooted at `--project`. Literal includes are recursively copied (including inactive branches conservatively), as are literal `$readmemh/b` data relative to project root. Macro includes, dynamic readmem names and escaped top identifiers are not supported by this snapshot helper. Supply a resolved probe closure or adapt the real frontend without claiming broader coverage. Symlink inputs are rejected. `--asset` copies a file/directory without making it editable; include test scripts and their data to keep verification self-contained. By default only sources and included RTL headers can change; `--allow-edit` narrows that set.

For downstream integration, the default `--project` is the original source root. For example, if original RTL lives at `/run/work/rtl/block.sv`, use `--project /run/work --source rtl/block.sv --allow-edit rtl/block.sv` and overlay `rtl/block.sv`. Rooting at `/run` with source `work/rtl/block.sv` creates different relative identities and an unsuitable integration patch.

If a probe wrapper or additional test assets are needed, create a staging project inside the authorized run workspace. Copy original RTL without changing its relative paths or baseline hashes, add frozen probe sources/assets, and initialize the staging root. Explicitly restrict editable paths to actual original RTL; otherwise default editability includes the new wrapper too, which the downstream consumer cannot match to an original file. For example:

```bash
python3 -B "$S/scripts/readiness.py" init --project "$STAGING" --workspace "$W" \
  --top probe_top --source rtl/block.sv --source probes/wrapper.sv \
  --asset tests/check.py --allow-edit rtl/block.sv
```

Here `probes/wrapper.sv` remains a frozen source, and `tests/check.py` remains a frozen asset. Repeat `--allow-edit` only for original RTL files that may change, including eligible original headers. A separately extracted probe without original path/byte identity remains explicitly labelled probe evidence until a corresponding original-source candidate is created and verified.

The tool is not a security sandbox for arbitrary commands. Run trusted project tools. Custom commands must read copied inputs and place generated outputs outside `work/tree`/`work/baseline_tree`. Source changes invalidate the receipt. Working directory is recorded in `execution.json`; prefer absolute placeholders. Declare toolchain scripts/config/libraries outside the snapshot with `input_files`, and capture versions of any subsidiary tools in the program's output. Environment-specific tool behavior still limits portability.

Write candidate files under an overlay with the same relative paths, then seal it:

```bash
python3 -B "$S/scripts/readiness.py" variant "$W" grouped --overlay "$W/overlays/grouped"
```

The default fixed-file overlay cannot add/remove files, edit verification assets or replace the baseline. For module/caller/filelist changes, initialize an opt-in regional workspace and follow [regional candidates](regional.md); old fixed-file workspaces are not upgraded in place. Never edit `variants/*/tree` after sealing.

### Derived probes and integration paths

A probe can establish a local cost result while its wrapper remains unsuitable for integration into original RTL. When the intended edit is in an original file, keep that file as the editable source and freeze the probe extractor/template and verification program as assets. A `command` measurement may derive the executable probe from the selected version's `{tree}` at runtime, writing generated files under `{job}` outside the copied input trees. Derive baseline and candidate independently using the same frozen extractor, widths, control and multiplicity; retain the emitted input and source hashes. This uses the existing command interface rather than editing a frozen wrapper to stand in for a different original file.

Declare subsidiary tools such as the synthesis executable in `input_files`, capture their versions in the log, and include generation overhead consistently in both measurements. For functional checks, derive both versions from `{baseline_tree}` and `{tree}` and verify the changed source contract. A proof on only one fixed probe setting cannot establish every original call mode. Report that distinction and check all affected modes or the full helper contract as appropriate. A changed probe-only file may be delivered as probe evidence, but it is not an original RTL patch; positive local comparison does not override this path or coverage boundary.

## Structural evidence before and after optimization

Before spending the cost-experiment budget, inspect existing baseline structure checks and their applicability. After candidate changes, collect a comparable candidate check where affordable. Reuse a completed source-bound result; a new full-design synthesis is not required for each local variation. If evidence is missing, use the existing bounded `run` interface for a suitable check and preserve its actual scope and outcome.

After obtaining diagnostics, follow the [investigation workflow](../SKILL.md#investigate-important-findings). Read the relevant raw evidence and source/configuration, explain important findings or retain a specific unresolved hypothesis, and use existing `run` jobs for any authorized discriminating probe. This reasoning belongs in the ordinary findings report; `health` is not a root-cause inference engine and requires no new command or report state for this step.

The read-only `health` command collects existing receipts or external descriptors; it starts no EDA and does not alter jobs. Use a new report path inside `W`:

```bash
python3 -B "$S/scripts/readiness.py" health "$W" --variant baseline \
  --baseline-job baseline-check --out "$W/health-before.json"
python3 -B "$S/scripts/readiness.py" health "$W" --variant grouped \
  --baseline-job baseline-check --candidate-job grouped-check \
  --out "$W/health-pair.json"
```

`--baseline-job`, `--candidate-job`, `--baseline-evidence` and `--candidate-evidence` are repeatable. `--variant` is `baseline` or an actual sealed candidate name, not a fixed word `candidate`. Historical checks outside the workspace use an explicit source/build/tool/artifact descriptor; read [structural health](structural-health.md) for its schema and attribution limits. A failed receipt can still establish observed CHECK problems without becoming a successful cost measurement. Elaboration alone, an incomplete check or a custom-command exit zero cannot establish structural cleanliness.

Pass `--health "$W/health-pair.json"` to `compare` or `finalize` to bind a freshly verified report to the proposal. Omission leaves health unknown and full-design readiness unestablished; it does not fabricate a pass. Confirmed candidate-new structural problems downgrade an otherwise positive comparison to `evidence_incomplete`. Baseline-existing or unclassified issues remain explicit limitations of any scoped proposal. No local health result establishes whole-design readiness, mapped area, STA or acceptance.

## Measurements and jobs

Save each configuration JSON in `W`. A built-in measurement:

```json
{"kind":"yosys","tool":"/absolute/path/yosys","stage":"proc","timeout_seconds":30,"memory_mib":2048,"defines":[],"parameters":{"WIDTH":32}}
```

`elaborate` reads SystemVerilog and checks hierarchy. `proc` additionally lowers processes, cleans and checks structure. `generic` uses `synth -noabc`, including generic memory mapping; it can expand large memories, so start smaller where appropriate. Statistics are raw Yosys objects, not mapped area. Warnings remain in `tool.log`; inspect latches, multi-drivers, memory lowering and unsupported constructs even after a successful job.

All ordered sources are handed to the frontend before hierarchy pruning. Selecting a smaller `--top` while retaining the full source list may still parse or process expensive unused modules. Reduce the source/dependency closure as well, or create an explicit source-bound probe that preserves the suspected mechanism. Record the scope and its limitations rather than treating the smaller top alone as evidence of a smaller run.

Before new tool work for a campaign comparing or delivering an optimization candidate, choose and justify the measurement/repetition strategy (three observations per version by default). Reserve remaining jobs/time for still-needed measurements, paired functional verification, compare/export and required review/delivery, including scheduler/output/cleanup overhead. Reuse freshly verified completed measurements or pilots with matching source/scope/configuration/tool identities instead of reserving duplicate work. If affordability is unestablished, allocate only a small fraction of exploratory time to a short baseline pilot; the per-job timeout is a ceiling. Budget baseline-only diagnostic checks for their own checks and reporting.

After a pilot timeout, inspect its actual inner result/log and the remaining campaign budget. If a representative retry and the selected completion steps still fit, make one narrower attempt by reducing independent multiplicity and/or the necessary source/dependency closure. Retain the actual operation widths, modes, clock/reset/control and output observability, and explain why the suspected mechanism remains present. Establish baseline affordability from applicable completed measurements or a pilot before sealing optimization variants or beginning candidate search. If narrowing removes the mechanism, the bounded retry is still unaffordable, or verification/delivery no longer fits, preserve the evidence and report the limitation honestly. A first timeout does not alone justify stopping with a useful retry still affordable.

```bash
python3 -B "$S/scripts/readiness.py" run "$W" --variant baseline --config "$W/proc.json" --job-id base-1
python3 -B "$S/scripts/readiness.py" status "$W" base-1
```

Default submission is detached. `--foreground` runs the experiment inside the caller and is required under a disposable scheduler container. Same ID plus same request observes the existing job; changed requests are rejected. Status checks PID/start/boot, not PID alone. It never restarts or resumes. Inspect `jobs/<id>/result.json`, `status.json`, `tool.log` and `execution.json`; `worker.log` captures detached launcher output and may be absent in foreground mode. `incomplete` is not evidence of an RTL failure. Timeout/memory limits apply to the job's process group; children that deliberately detach are unsupported. The default concurrency limit is one within this workspace; inspect other machine workloads separately. `max_concurrent_jobs` can be set explicitly.

When a persistent external executor starts one disposable container per submitted command, use `run ... --foreground` inside that command. Keep the entire skill read-only and put the workspace, overlays, configuration, generated outputs and exports in the executor's current writable scope. Observe the external job ID until terminal, then inspect the internal result and raw logs; never infer internal success from the external command's exit code alone. Give the external timeout room for input copies, the internal timeout, result hashing and cleanup. Do not query a live inner job from a different container or start another experiment until its external owner finishes. Linux status records its PID namespace; a foreign or unreadable namespace preserves the existing reservation and reports unavailable process visibility without manufacturing a lost-worker result. Only completed receipts can be reused across such observers.

Built-in frontend cwd is the copied `jobs/<id>/work/tree`; custom commands use `jobs/<id>/work`. Use absolute `{tree}`, `{baseline_tree}` and `{job}` placeholders, copied assets and explicit dependency hashes rather than historical working/output directories. Scheduler aliases are scheduler-specific; they are not expanded inside configuration JSON. Individual job limits and the workspace concurrency limit do not constitute a total campaign budget. Declare a small candidate/job/time budget before starting; an optional external scheduler can enforce persistent totals separately.

If every external submission counts against a job or timeout budget, include inspection/helper commands in that accounting. Use the available read/list tools for source and completed-log inspection. Batch independent cheap checks into one small helper submission when execution is necessary, keeping enough jobs and time for measurements and paired functional checks.

Each job records an immutable request binding source hashes, top, config, tool realpath/hash and explicit tool input files; raw tool logs, generated script and command; actual elapsed time, sampled peak group RSS (with OS child peak fallback), outcome and artifact hashes. The worker also checks frozen/copy source integrity after execution. These are reproducibility/accidental-change receipts, not cryptographically signed attestations against a malicious actor editing all files.

### Continue from completed measurements in a new workspace

When only a functional check and finalization remain, preserve completed measurements rather than rerunning them. Explicitly clone the same sealed sources and selected direct measurement receipts:

```bash
python3 -B "$S/scripts/readiness.py" clone-workspace "$OLD_W" --workspace "$W" \
  --job base-1 --job base-2 --job base-3 --job cand-1 --job cand-2 --job cand-3 \
  --variant grouped
```

`W` must be new and outside `OLD_W`. The clone keeps the original workspace/variant identities and copies only the sealed input trees. Its job directories contain references pinning the original request/result hashes, not rewritten experiment records or copied timings. Every comparison freshly verifies original raw artifacts and source copies at their actual origin; that server evidence must remain available and unchanged. Completed measurements and successful paired functional checks can be selected only when workspace, baseline and variant/source identities match exactly. Functional markers and built-in proof coverage are still checked at the original proof job. Failed/pending jobs and renamed IDs are rejected. Cloning a workspace that already contains references validates and flattens each to its direct origin while retaining its pinned original request/result SHA; persisted reference chains remain invalid. The referenced IDs may appear in comparison/finalize inputs. Run any still-missing functional check as an ordinary new local job (or a finite batch containing that new check plus finalize); do not include imported receipts as batch jobs to execute. The caller still accounts for previously spent jobs/time and for clone/check/finalization overhead within its remaining budget.

If an earlier workspace already completed the paired functional check, select its original ID explicitly too:

```bash
python3 -B "$S/scripts/readiness.py" clone-workspace "$OLD_W" --workspace "$W" \
  --job base-1 --job base-2 --job base-3 --job cand-1 --job cand-2 --job cand-3 \
  --job functional-1 --variant grouped
```

For standalone `compare` and `finalize`, CLI `--out` is resolved from the process current working directory. Prefer an absolute destination such as `--out "$W/final"`; `--out final` can resolve outside `W` and is rejected. In a batch plan, `finalize.out` is instead relative to the workspace. Keep these path conventions distinct.

### Finite grouped experiments

Once baseline affordability is established, put a finite sequence of still-needed measurements/checks in one plan rather than making a model round trip per inner job. The plan is a regular non-symlink JSON file inside `W`; each job has an inline existing configuration and a distinct persistent ID. For example, one short group can explicitly reuse a matching pilot and run another pair:

```json
{"jobs":[
  {"job_id":"pilot","variant":"baseline","reuse":true,"config":{"kind":"yosys","tool":"/absolute/path/yosys","stage":"proc","timeout_seconds":5,"memory_mib":2048}},
  {"job_id":"base-2","variant":"baseline","config":{"kind":"yosys","tool":"/absolute/path/yosys","stage":"proc","timeout_seconds":5,"memory_mib":2048}},
  {"job_id":"group-2","variant":"grouped","config":{"kind":"yosys","tool":"/absolute/path/yosys","stage":"proc","timeout_seconds":5,"memory_mib":2048}}
]}
```

```bash
python3 -B "$S/scripts/readiness.py" batch "$W" --plan "$W/measure-plan.json" --batch-id measure-2 --tool-seconds 10
python3 -B "$S/scripts/readiness.py" verify-batch "$W" --plan "$W/measure-plan.json" --batch-id measure-2 --tool-seconds 10
```

The helper supports 1–64 sequential jobs, no dependencies, recursion or variant creation. Before launching any experiment it stores the plan hash, normalized job/finalization options, source/tool/configuration request identities and explicit invocation in `batches/<id>/request.json`. `reuse:true` requires an already successful, freshly verified exact request; absent, changed, pending, lost and failed jobs cannot be reused. An existing ID without explicit reuse rejects the entire new plan before launch. The same batch ID with the same invocation only observes its existing claim/result, including after disconnect; it never resumes skipped work. Conflicting or corrupt plans/results are rejected. No process status is queried or repaired by `verify-batch`.

`--tool-seconds` is a positive integer admission budget: the sum of configured timeouts of new jobs must fit before any job starts. After each experiment the helper adds its actual measured tool time and starts the next only if its full configured timeout still fits. Configured timeouts and comparison measurement keys are never silently changed. This budget does **not** bound source copies, hashing, tool-version queries or finalization wall time; use an external owner with a full wall deadline and sufficient margin. For a 60-second native limit, split into smaller groups and reserve at least five seconds for setup/output/cleanup, often more for larger inputs. Reused observations consume no new tool time.

The first failed new job stops later experiments. The terminal batch result lists actual job statuses, request/evidence identities, tool elapsed time and an optional finalization; partial/failed evidence is preserved. Missing receipts remain incomplete, and no duplicate work starts. For successful jobs, the compact outcome and finalization usually suffice; inspect individual raw logs for failures, warnings or coverage questions.

Plan finalization before starting the last group by adding this optional object. It references successful prior IDs without rerunning them and uses the unchanged comparison defaults:

```json
"finalize":{"variant":"grouped","baseline_jobs":["pilot","base-2","base-3"],"candidate_jobs":["group-1","group-2","group-3"],"verification_jobs":["equiv-grouped"],"out":"final"}
```

Optional `metric`, `min_ratio` and `min_repeats` match `finalize`. Optional `finalize.health`, for example `"health":"reports/health.json"`, is a workspace-relative path to an existing health report; batch finalization and `verify-batch` freshly verify it. `out` is relative to `W`, outside jobs/variants/batches and reserved metadata. Finalization runs only when every planned job reached terminal and all referenced measurement receipts validate. A failed final paired verification can yield an ordinary `keep_baseline`; insufficient repeats or missing verification can yield `evidence_incomplete`. Unexecuted/failed measurements do not acquire a fabricated bundle. The batch result's `finalization` is either null or the exact freshly verified ordinary `verify-finalization` result. `verify-batch` revalidates this binding, current plan bytes, sources, tool inputs, results and raw artifacts without EDA or writes. Split groups may share prior successful IDs; each new group gets a new batch ID and explicit budget.

Yosys include options use recorded, space-free relative aliases outside the source trees; their targets are checked against the copied inputs. Replay `experiment.ys` from the exact `execution.json` working directory, including when the original project or include directory contains spaces.

Yosys frontend compatibility is tool dependent. The built-in backend is intentionally small; it does not invent SRAM models, a liberty library, clock constraints or a physical flow. For existing tools/regressions use an argv array, never a shell command string:

```json
{"kind":"command","argv":["/absolute/python3","{tree}/tests/check.py","--gold","{baseline_tree}","--candidate","{tree}","--output","{job}/regression.json"],"baseline":"baseline","purpose":"regression","success_markers":["ALL_CASES_PASSED"],"timeout_seconds":30,"memory_mib":1024}
```

To measure another tool, set `purpose` to `measurement` and configure identical commands for both versions. Placeholders are `{tree}`, `{baseline_tree}`, `{job}`. Pair checks must actually use `{baseline_tree}`, run both inputs appropriately and emit their marker only after functional success. A one-sided compile is not paired verification. Output markers are matched only after exit zero; test adequacy remains an engineering judgment.

## Equivalence

```json
{"kind":"equiv","tool":"/absolute/path/yosys","baseline":"baseline","seq_depth":2,"timeout_seconds":30,"memory_mib":2048,"defines":[],"parameters":{"WIDTH":32}}
```

Runs the actual `proc`, `memory`, `flatten`, `check`, `equiv_make`, `equiv_simple -seq N`, `equiv_status -assert` flow. It checks matching port interfaces, nonempty equivalence cells, output coverage and successful proof output. It does not automatically apply temporal induction's synchronization assumptions. Unsupported cells, unproved state, blackboxes and readmem require another functional verification method; failure is not automatically a functional counterexample. The report names the exact tool/proof scope. Read the raw log, especially for initialization/undefined-state behavior; do not infer coverage for other reset assumptions, parameters or macro implementations.

## Compare and export

Run independent repeats under comparable machine load, preferably interleave baseline/variant. Choose a minimum ratio larger than the observed noise and report all samples. Three per version is the default; choose a smaller `--min-repeats` with a justified strategy before budgeting. With few samples, state the limits on noise estimation and evidence strength, and keep claims within the measured scope. A single local result cannot establish full-design acceptance; paired functional verification remains required.

Prefer one finalization command once those job IDs are available:

```bash
python3 -B "$S/scripts/readiness.py" finalize "$W" --variant grouped \
  --baseline-job base-1 --baseline-job base-2 --baseline-job base-3 \
  --candidate-job group-1 --candidate-job group-2 --candidate-job group-3 \
  --verification-job equiv-grouped --out "$W/final"
python3 -B "$S/scripts/readiness.py" verify-finalization "$W" --delivery "$W/final"
```

`finalize` accepts the same repeated measurement/verification arguments and `--metric`, `--min-ratio`, `--min-repeats` defaults as `compare`; `--out` is a directory. It performs no EDA. It prepares and verifies `comparison.json`, `candidate.json`, `candidate.patch`, `delivery.json` and `finalization.json` privately inside `W`, then publishes the complete directory atomically. All outputs, including temporary publication/locking files, stay inside the workspace outside jobs/variants/batches. Interrupted preparation does not expose a completed destination. Use a new destination; the exact same invocation may observe a valid existing bundle idempotently, while differing arguments, corruption and incomplete existing output are rejected rather than repaired.

The standalone `finalization.json` has `kind: readiness_finalization_v1`, the unchanged comparison `decision`/`reason`, absolute `workspace`/`comparison`/`delivery` paths, `workspace_id`, `baseline_identity`, `variant_identity`, `comparison_identity`, optional `limits` and an `identity` hash over all other fields. It can serve as the ordinary source-bound final report without a second model-written JSON document. A negative decision still exports proposal-only artifacts and retains the baseline; finalization does not turn incomplete measurements or failed functional checks into a candidate.

`verify-finalization` is a fresh read-only check of the exact bundle record plus actual core receipts, source identities and patch. It never queries a live inner PID, starts EDA, restarts jobs or writes status files. It returns the existing `verify` fields plus `reason`, absolute `comparison`, `finalization_identity` and `finalization_path`. This is evidence validation, not engineering acceptance. An independent consumer still assesses verification scope and matches only allowed changed original RTL before integration.

The separate commands remain available when individual reports are useful:

```bash
python3 -B "$S/scripts/readiness.py" compare "$W" --variant grouped \
  --baseline-job base-1 --baseline-job base-2 --baseline-job base-3 \
  --candidate-job group-1 --candidate-job group-2 --candidate-job group-3 \
  --verification-job equiv-grouped --out "$W/comparison.json"
python3 -B "$S/scripts/readiness.py" export "$W" --comparison "$W/comparison.json" --out "$W/delivery"
```

Comparison requires identical measurement scope/config/tool, current source identities, completed measurements and functional evidence bound to the pair. It rejects repeated job IDs as repetitions. It does not optimize both runtime and hardware simultaneously: choose `--metric elapsed_seconds` or `peak_rss_bytes`, inspect all other statistics manually and record tradeoffs. Any supplied failed functional check retains baseline. Missing functional evidence or insufficient repeats yields `evidence_incomplete`; no separated measured benefit yields `keep_baseline`. Local benefit plus scoped functional evidence yields `candidate_for_integration`, never full physical acceptance.

Export revalidates receipts and writes `candidate.patch`, `candidate.json`, `comparison.json`, `delivery.json`. Evidence-incomplete/keep-baseline exports are explicitly proposal-only and do not authorize applying the patch. Apply accepted patches only to an independent implementation candidate under the project's normal process. Job artifacts and candidate tree stay in `W`; deliver their paths/hashes with the small summary. There is no dependency on another skill's private JSON, transaction state or controller.

An optional downstream consumer can revalidate the exported source/evidence/patch binding without applying sources or running EDA:

```bash
python3 -B "$S/scripts/readiness.py" verify "$W" \
  --comparison "$W/comparison.json" --delivery "$W/delivery"
```

Inspect this fresh validation's decision, comparison identity, baseline/candidate identities, allowed paths and changed files before integration. It does not replace an independent engineering review or establish full-design acceptance. The consumer must still match every changed original file to its unchanged baseline and copy only authorized RTL edits; tests, tool configuration and probe-only paths are not implementation patches.
