---
name: microarchitecture-design
description: Design a resource-organized microarchitecture from a specification and Python reference using candidate comparison, reproducible estimates and critical consistency checks; deliver one unified architecture draft with an RTL exploration contract.
---

# Microarchitecture design

Use the original spec for requirements and Python reference for computation semantics. The frozen originals may also include declared `support/<input-relative-path>` helpers/configuration; follow those definitions when needed. `reference.py` is a logical primary-source name; a noncanonical entry also appears under its original path in `support/`, without duplicating its computation. Do not assume undeclared local imports. Neither Python functions nor array operations prescribe hardware instances. Do not read historical implementations to choose organization. Inputs determine dimensions, operators, budgets, interfaces, workload, numerical acceptance and state/reset obligations. Never change them to make a proposal pass.

Deliver a defensible architecture draft for RTL exploration: resource organization and critical choices have a basis, budgets are reproducible estimates, and no known critical contradiction remains. Coarse scheduling, conservative utilization and justified estimate ranges are appropriate. Complete cycle schedules, lane-by-lane routing, tail control, detailed arbitration, bit-accurate regressions and physical closure are later implementation work unless a particular unresolved issue already invalidates the architecture's semantics or principal budget argument.

The caller freezes inputs and controls model transport, revisions, artifacts and limits. This skill owns the design method and uses its deterministic tools; independent architecture audit is separate.

## 1. Extract requirements and source work

Read [references/requirements-contract.md](references/requirements-contract.md). Extract required effects, shapes/dependencies, state lifetime and initialization, workload/completion boundaries, numerical acceptance, external interfaces and available memory service. Separate hard requirements, targets, design choices and missing conditions. Keep nonnumeric protocol/state obligations as original-source requirements rather than inventing numeric substitutes.

Use source selectors from the caller's quantity view, explicit conversion facts and source-backed engineering derivations. Inspect binding receipts: a valid number does not establish its axis, scope or meaning. Preserve legal runtime domains independently of benchmark profiles. Bind supplied macro density in its original unit/prefix context; do not replace it with an assumed cost model.

Build the source work model before resources using [references/work-model.md](references/work-model.md). On the few computation, reduction or state paths that decide correctness, trace the key value versions and transformations from source operands to consumers. Distinguish intermediate state from both prior and committed state; an old/new label alone can hide a wrong consumer. Record the chain in existing semantics/state_lifetime fields with original references. Preserve current coefficient producers and nonlinear/reduction boundaries without demanding a full per-value cycle schedule.

Derive work from operand shapes and apply each source loop/prefix scope once. Account for nonmatrix work, selection, scaling, state updates, movement and completion as well as dominant MACs. Separate required effects from reference preprocessing. A useful draft may group commensurate work and conservatively bound minor costs; it must not silently omit an effect or encode uncertainty as zero. Mark a material unresolved source interpretation explicitly and resolve it before relying on its budget.

## 2. Compare resource organizations

Choose a small set of materially different candidates based on the bottleneck: resource sharing/concurrency, residency/tiling, storage representation or precision. Explain engines/lanes, logical modules/connections, physical storage inventory, major bank/port use and state residency. Do not create one module per source operation. Concurrency needs independence and supply; residency needs capacity; more lanes need an operand-supply argument.

Use typical paths to explain how data reaches the engines, how reductions and recurrent updates preserve dependencies, and how loading/output complete. Identify the main demand and available service, useful reuse/broadcast, holding capacity and likely stalls. A rough loop/tile order or conservative service/utilization estimate can support this stage; every lane, address, iteration and tail need not be expanded. Where a claimed rate depends on a specific mechanism, describe that mechanism and its uncertainty instead of equating peak issue rate with sustained throughput.

For costly nonlinear operations, choose a plausible shared/pipelined/iterative approach and formats or bounded format assumptions sufficient to price resources and avoid undefined numerical behavior. Latency/initiation interval may be provisional estimates with a basis; an unsupported claim of free or arbitrarily fast nonlinear work is not a budget argument. Represent dynamic weights and parameters faithfully rather than assuming fixed ROM contents.

Use [references/execution-tools.md](references/execution-tools.md) for optional finite windows and compact rules when a concrete service, dependency or storage question could change the choice. They check declared schedules; they are not a mandatory implementation plan for every candidate or resource group. A one-lane or single-cycle witness proves only that declared issue, never a full source operation. A demonstrated contradiction requires repair; absence of an exhaustive window alone does not.

## 3. Execute reproducible estimates

Read [references/budget-tools.md](references/budget-tools.md) and [references/storage-tools.md](references/storage-tools.md). Submit source work, resources and scheduling assumptions to `scripts/evaluate.py` through the caller's tool interface, or its standalone standard-library command. Use returned quantities in the document; do not claim execution without a result or recopy program-derived totals as inputs.

Estimate complete-task compute and noncompute cycles, external payload/wire traffic and finite in-flight storage, allocated macro capacity, peak live data and component area intervals. Include loading, initialization, reductions, state service, output and completion. Use the provided work/count recipes and each profile's actual scope. Account for representation, replication, packing and legal maximum capacity domains. Coarse live phases or conservative simultaneous footprints are sufficient; precise per-cycle temporary lifetimes are not universally required. Register/holding storage still needs allocation and area accounting.

Start from resource/work and external-service lower bounds, then use explained effective rates, utilization, nonoverlapped costs or overlap assumptions. Actually bind the principal operand/traffic service to its charged budget interval using the supported declarations; a detached check or prose claim does not constrain the estimate. Excluding preloaded data from RUN needs support from the original measurement boundary, not a phase label. Conservative estimates/ranges are acceptable with a clear basis and sensitivity; peak issue is not sustained throughput and assumed area is not synthesis. Keep hard-budget comparisons tied to those assumptions; neither a utilization factor nor a changed limit repairs an arithmetic error.

Finite execution evidence may supplement this estimate. If using `composition:"execution"`, follow its exact binding contract: charge the selected template/phase once, derive compatible source repetitions, and avoid adding ideal compute or already covered loads again. Jointly repeated phases must have the same occurrence scope; separate one-time setup. Ordinary serial/parallel/overlap estimates remain valid without an execution template. A tool-size limit is not hardware infeasibility.

Inspect errors and hard-budget outcomes. Malformed declarations, inconsistent arithmetic, capacity/port contradictions and unknown material costs require correction. Optional evidence gaps remain explicit without becoming invented hard constraints. An unsupported cost/traffic/storage model that prevents a defensible required-budget estimate is different from an unperformed detailed schedule check. Recalculate affected data after any design change; preserve unaffected decisions.

## 4. Check critical consistency

For those decisive paths, write the source chain and proposed execution chain separately, then match each transformation and the value version each consumer uses. Include consequential intermediate versions, not only entry and committed state; verify the selected reduction/normalization inputs too. Keep this short correspondence in existing prose rather than a second schema. Fusion, streaming or reordering may omit a physical intermediate but must preserve its mathematical value and numerical behavior. Check retained state and main-engine supply against the assumed service/utilization. Resolve critical semantic, capacity, dominant-work and principal supply/budget contradictions; do not demand a complete RTL schedule.

Keep external interface obligations and chosen data exchange consistent with the original input. Specify representation, major layout/region sizes and ownership well enough to support capacity, traffic and handoff. Detailed internal addresses, command bit placement, arbitration and tail controls can be refined during RTL exploration when they do not change the resource/semantic argument. If such a choice already determines budget feasibility or permits illegal behavior, decide or bound it now.

Choose numerical formats or bounded precision assumptions where they affect storage/area or define a critical arithmetic path. Resolve known contradictions in output encoding, denominator/normalization behavior, rounding or overflow assumptions. The original numerical thresholds remain unchanged; full bit-accurate acceptance need not be proved in this stage.

Read [references/numeric-tools.md](references/numeric-tools.md) for optional scalar/row probes that can resolve a concrete numerical concern. Prefer a few relevant executable points to repeated assertions about risk. Empty arrays mean not checked; computations without assertions mean computed only. Neither is a numerical pass, and neither alone rejects an otherwise defensible architecture draft. When testing a chosen chain, use its actual quantized operands and specified guards. Reachability premises and approximation limits stay explicit. Output encoding/selection must be internally consistent and use only input-permitted scaling; a probe does not establish full-model accuracy.

## 5. Select and hand off

Compare candidates using the executed estimates, assumptions and uncertainty. Stop at a useful defensible choice rather than optimizing indefinitely. If none meets a hard limit, report the gap and useful alternatives; distinguish a proven lower bound from a failed search. Keep targets, conditions and estimated achievement separate from verified implementation results.

Write one complete architecture.md containing requirements/boundaries, comparison/selection, modules/resources/connections, major dataflow/scheduling/state organization, budget evidence and an RTL exploration contract. Include original external obligations, chosen ABI/representation and numerical approach at the detail needed to avoid large structural rework. Identify provisional implementation choices, estimate assumptions and later verification obligations. Do not generate full-chip RTL or claim timing, numerical or physical closure.

Delivery requires source-faithful organization, evidence-backed key choices, reproducible required-budget estimates and no known critical contradiction. Deferred implementation detail is a condition, not automatically a missing architecture. Use the most recently executed structured data; drafts, audit and export contain the same unified document and authoritative estimates.

## Revision entry

Read unchanged inputs, prior design, executed results and independent findings. Resolve or rebut each critical finding with source/evidence, preserve valid corrections, and propagate affected widths, resources, storage, traffic and cycles through a new calculation. A prior implementation-detail request need not become an architectural requirement merely because an auditor requested it. Explain deferral at the current delivery scope rather than falsely claiming it proved.

Work-model corrections use the existing original-source revision contract. Do not silently replace requirements or force agreement with a fallible audit baseline. Preserve versions and unresolved issues; stop at caller limits without changing frozen inputs, hard budgets or numerical thresholds.
