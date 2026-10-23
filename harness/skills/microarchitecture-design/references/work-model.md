# Source-backed work model

Before candidates, return `<<<WORK_MODEL>>>` and one JSON object `{workloads:[...],items:[...]}`. The host executes `--work-model-only` against frozen requirements and original sources. This is a versioned interpretation of source semantics, not a new requirement or a resource plan. It contains no engines, lanes or implementation costs.

## Declarations

- workloads: `[{id,quantity:expression_over_input_facts,unit:tokens|bytes|samples|other_element_name,basis}]`. Quantity is the positive integer number of elements in that profile's actual task, not work nodes, operations, cycles or a maximum capacity bound. Profile IDs match performance constraints and candidate workloads. Trace quantity to that profile's original input definition; maximum legal state sizing remains a separate runtime-domain obligation. Use supplied facts or justified numeric literals, never free runtime names.
- items: `[{id,category:compute|state|movement|control,semantics,condition,state_lifetime,status:complete|unresolved,dependencies:[work_ids],count:{...},scopes:{profile_id:scope,...},references:[original_input_refs]}]`. Every profile appears exactly once in scopes. Prefer original-input spans `{file:frozen_original_name,start:integer,end:integer}` with actual one-based inclusive bounds; the program extracts the frozen text and records its binding, so the model need not copy quotations. quote is optional; if supplied it must be exact, including whitespace, and agree with any supplied bounds. Ambiguous quotations need valid bounds. Preserve actual current-task producer/consumer dependencies when splitting or revising items; do not clear them to bypass a cycle error. Current-task dependencies cannot be self/cyclic; prior-state recurrence belongs in state_lifetime. Zero derived occurrences need a source-supported inactive condition.

The source item has no `repetitions` field. The program derives its occurrence count from the declared scope; old repetitions are rejected, never converted.

## Scope owns repetition

Each scope uses exactly these fields:

| Scope | Declaration | Derived occurrences |
| --- | --- | --- |
| element | `{kind:"element",axis:"source_workload_axis",multiplicity:expression}` | `quantity * multiplicity` |
| task | `{kind:"task",axis:"source_task_axis"}` | `1` |
| prefix | `{kind:"prefix",axis:"source_workload_axis",prefix_axis:"source_history_axis",first:expression,step:expression,multiplicity:expression}` | `quantity * (2*first + (quantity-1)*step) // 2 * multiplicity` |

Axis labels are nonempty source-meaningful strings, not executable variables. Prefix axis and prefix_axis must be distinct after whitespace normalization. Multiplicity, first and step are nonnegative integer expressions over input facts. They cannot use workload_elements; no extra scope fields, copied totals or independent repeat expression are accepted. Task has no multiplicity field.

Element and prefix recipes describe base work for one element or one prefix member; their expressions cannot use workload_elements because scope already owns that axis. Trace each source loop to either the operand shape/base count or scope, once. multiplicity represents only additional independent loops not already counted by either; a maximum capacity bound is not such a loop. Task recipes may use workload_elements for a genuinely complete-task derivation, including a quadratic formula. Independent dimensions are not rejected merely because their values equal the profile quantity. The program cannot detect every duplicated loop concealed by a fact or multiplier; inspect the original loops and executed scope_trace before trusting totals.

For increasing history lengths `1,2,...,quantity`, declare prefix first=1, step=1. For `0,1,...,quantity-1`, declare first=0, step=1. The script sums the history lengths; do not put that sum in the base recipe or multiply it by quantity again. Zero multiplicity or a zero-length prefix produces inactive work. Empty RUN profiles are not supported by this contract.

## Base count recipes

- matmul: `{kind:"matmul",m,n,k,batch,unit}` → `m*n*k*batch` primitive operations.
- elementwise: `{kind:"elementwise",elements,operations_per_element,unit}` → product.
- reduction: `{kind:"reduction",elements,groups,unit}` → `max(elements-1,0)*groups` reduction combines; loads and nonlinear work are separate.
- sum: `{kind:"sum",unit,terms:[primitive_recipe,...]}` → the script adds each term's fixed count, then applies the item's scope once. Terms are only matmul, elementwise or reduction, with exactly their listed fields and the same unit as the parent. No term scope/repeat, custom/none/unsupported or nested sum; no copied total. The technical maximum is 4096 terms per declaration. Element/prefix workload-axis checks inspect every term; task terms may use workload_elements.
- custom: `{kind:"custom",operations:expression,unit,basis}` → explicitly derived integer count.
- none: `{kind:"none",reason}` for nonarithmetic obligations, or a compute item whose program-derived occurrences are zero in every declared profile.
- unsupported: `{kind:"unsupported",reason,unit:optional}` remains unknown.

Counted recipes require a nonempty operation-unit string naming an actual primitive, such as MAC, multiply, add or compare. Active compute needs an operation count. Inactive compute may use none only when every declared profile has zero derived occurrences; the tool retains its dependencies and evidence and does not prove the branch is unreachable. Movement/control uses none or unsupported; bytes, elements and events belong in candidate traffic/schedule costs. Separate arithmetic effects into compute/state items. Scope counts apply to none/unsupported as well, so inactivity and lifecycle obligations remain visible.

All numeric expressions use the accepted, materialized source/converted/derived fact identifiers, plus workload_elements only in a task recipe. Bind source dimensions before counting; unit conversion belongs in extraction convert facts, not a count multiplier. Allowed operators/functions are `+ - * / // % **` and ceil/floor/min/max/abs. No assignments, loops, generators, sum(), temporary definitions, work IDs or invented *_ops names. A bound scalar does not prove which axis or conditional repetition it represents.

## Derive the source meaning first

Before declaring count recipes, read each path as value production and consumption. For each source effect identify its operands/producers, result shape, consumer, loop scope, prior-state use and last old-value consumer. Record that reasoning in the existing semantics/state_lifetime fields with original-source references. Derive dependencies from these value edges, then check that the declared graph follows them. An acyclic graph is necessary but does not prove the directions or missing edges are correct. A reduction result or freshly computed coefficient is a current-task producer; only the value from an earlier iteration is prior-state recurrence.

Preserve distinct causal boundaries even when their arithmetic units match. For virtual source `p=a*x; q=f(p); r=b*q`, the two multiplies belong to separate work items with `p → q → r` dependencies: a single two-multiply item cannot be placed before or after f without misrepresenting readiness. Reduction/normalization, nonlinear transformations and state read/modify/write have the same rule. The reference expression, not source line order, determines the edge. Resource unit selectors can aggregate separate items on one shared engine later; separate source items do not require separate modules or whole-array barriers. Streaming/fusion remains legal when the implementation preserves per-value readiness and old-value consumers.

For a recurrent update, retain consequential intermediate state versions, not just prior and updated state. A value transformed before observation may be modified again before commit; identify the version each consumer actually needs. Record transformations and consumers in semantics/state_lifetime; temporary labels do not impose separate buffers. The current coefficient chain and reductions need the same value-version discipline. When mapping a proposed realization, derive its chain separately and match consumed values edge by edge; algebraic fusion/reordering is legal only if values and numerical behavior are preserved. Before pricing repetition, reconcile the first, carried and final values; the tool cannot discover a missing semantic edge. Scope owns mathematical repetition, while execution rules implement that extent rather than repeat the task a second time.

Trace actual operand axes before counting. For A[M,K] @ B[K,N], use m=M,k=K,n=N; vector[K] @ matrix[K,N] has m=1,n=N, while matrix[R,K] @ vector[K] has m=R,n=1. A dot of two K-element vectors has K primitive operations; an R-output matrix-vector effect has R*K, including when the matrix holds state. A square state does not itself imply cubic work. Sequential products contribute additive counts, not the product of their costs, but preserve their producer/consumer items when one product consumes the other's result.

Use sum only for commensurate contributions to one effect with one unit and common scope, identifying each term's source extent. For VIRTUAL source `y=A[5,3] @ x[3]`, if the source describes output row groups of 3 and 2, its one matrix-vector effect may be counted as:

```json
{"kind":"sum","unit":"MAC","terms":[{"kind":"matmul","m":3,"n":1,"k":3,"batch":1,"unit":"MAC"},{"kind":"matmul","m":2,"n":1,"k":3,"batch":1,"unit":"MAC"}]}
```

The script derives 9+6=15 MAC per occurrence. For a virtual profile quantity of 4 and element scope multiplicity=1, the total is 60 MAC. This sum describes output extents, not a schedule or a hardware module; ordinary matmul with m=5 is equivalent here. Different units, conditions, scopes/lifetimes or producer/consumer boundaries require separate accounting. A reference function is not a mandatory module, and source data generation, packing or validation is hardware work only when the input contract requires it.

Reconcile every mathematical effect named in an item with its count: stable comparisons/selection, products, reductions, nonlinear functions, quantization and state updates. No MAC does not mean no computation. Different primitive units need separate items or an explicit common primitive derivation. A custom formula must agree with its stated derivation; unknown semantics is unresolved/unsupported, not a zero or placeholder estimate. Quoted operands and correct algebra do not prove that the model omitted nothing or interpreted arbitrary Python correctly.

## Executed evidence and revisions

The report retains program-derived repetitions, operations_per_occurrence and operations for consumers; these are outputs, never source fields. Each item/profile also has scope_trace with normalized kind/axes, quantity, factor and total, plus resolved multiplicity/first/step where applicable. Factor is the workload or summed-prefix contribution, and total is the derived occurrence count. Task factor and total are one.

Use each executed compact_summary.profiles row's actual id for cost_groups.workload. Its by_unit rows provide work_ids_selector for a nonempty unit: copy that object into cost_groups.work_ids to bind all active work of that exact unit in this profile, or use the documented exclude to partition it explicitly. A null unit has no unit selector; bind such work explicitly or with a valid category selector. Separate semantic items and unit groups may name the same hardware resource with justified primitive rates and scheduling; a unit selector can aggregate their cost without creating a module per item. Inspect expanded bindings, detailed counts and scope_trace; occurrences sum scope totals, not task quantity, and summary values are not new expression identifiers. Unknown work remains unknown. The summary does not choose resources or prove semantic completeness or supply; nonarithmetic cycle costs still need their actual movement/storage paths.

The caller supplies the accepted model to subsequent design turns; omit an unchanged model from candidate data. A correction requires complete work_model data and work_model_revision=`{reason,references:[original_input_refs]}`, using the same original-input span contract above. These are tool-data constraints; response markers and outer request nesting follow the caller's current prompt. All candidates are recalculated; versions and fingerprints remain available. Correct source count errors before claiming fusion or transformed implementation costs. Frozen inputs, hard budgets and requirement extraction do not change.

