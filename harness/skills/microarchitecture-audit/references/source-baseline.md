# Source-only baseline entry

This entry receives ONLY the numbered frozen original input closure: spec.md, reference.py and any declared support/<input-relative-path> helpers/configuration. No proposal, candidate budget or designer quantities exist here. Return one baseline object; the caller executes its analysis. Do not emit a lookup/tool request, audit report, second JSON object or code fences.

The complete outer object has ALL of these fields:

```
{scope:string, obligations:[{id,category,requirement,references}], ambiguities:[], analysis:{...}, limits:string}
```

scope and top-level limits are nonempty strings. obligations is nonempty. category is computation|state|interface|numerical|budget|completion; these classify existing obligations, not mandatory new requirements. References are model-selected `{file:original_input_name,start:integer,end:integer}` inclusive one-based bounds in originals. Inspect the primary sources and relevant declared dependencies, retaining exact evidence where it supports an obligation or analysis declaration; source coverage uses valid original references and successfully bound source facts, not an obligation-only placement rule. A valid span proves text identity, not meaning. Include dependencies, state fresh/lifetime effects, initialization, loading, output acceptance and completion alongside numeric obligations. Do not choose hardware resources or assume the designer's grouping.

analysis requires facts, workloads, computations, states, flows, limits. Close the analysis object after its own limits, then supply the outer baseline limits; these are separate nested fields, never duplicate keys in one object. analysis.limits identifies unsupported/omitted derivations as nonempty text or a list of nonempty text; outer limits states the overall review scope. Additional annotation fields are preserved and reported as ignored_annotation_fields; they cannot change formulas, bounds or source values. Concentrate executable facts on decisive shape/scope, normative external fields and known interface capacity; budget/numerical obligations can remain in text. This baseline supports an architecture-draft review, not exhaustive reconstruction of every operation/profile. Preserve omitted derivations in limits without inventing extra mandatory execution coverage.

## Frozen original-source references

The host preserves a catalogue containing evidence_id, file, source_sha256, start/end and exact quote for every nonempty original source line. IDs bind the complete source content and location; changed sources have different IDs. Prompts show each original file's line count, fingerprint and numbered text. Prefer selecting a span; the checker extracts exactly those lines and records the content-bound excerpt without choosing its meaning. An actual returned `{evidence_id:"..."}` is also valid; do not combine it with location overrides. A direct `{file,quote,start,end}` reference may omit bounds when its exact excerpt is unique. An ambiguous excerpt needs valid bounds; quotation wording, punctuation, spacing and case must remain exact. Unknown, stale or proposal IDs cannot supply baseline evidence.

A rejected baseline may include citation_diagnostics:[{obligation_id,reference_index,file,error,kind,scope,...}], with zero-based reference_index. ambiguous_excerpt supplies match_count, matches:[{file,start,end,quote}] (at most six) and matches_truncated; choose the relevant original occurrence. inexact_excerpt supplies suggestions:[{file,start,end,quote}] (at most three complete original lines). invalid_reference has no locator candidates. Diagnostics search only original inputs, do not rewrite a quotation and do not establish semantic support. Recheck the source context before selecting a suggestion.

## Source quantities

facts is a list. Fact IDs are Python identifiers (letters/underscores/digits, no hyphens) because expressions use them. Other workload/computation/state/flow IDs are nonempty strings unique within their collection; they need not be Python identifiers. Never put an invented source value in facts.

Independently record input-defined interface requests/cycle, read latency and permitted backpressure before comparing candidate service. Distinguish peak acceptance from guaranteed ready cycles and command/response ports from memory ports. A quantity bound as count (including a cardinal followed by a counting word) cannot be converted or overridden into requests/cycle. The quantity tool recognizes only explicit adjacent English noun-per-cycle and Chinese per-cycle service phrases; inspect the actual returned unit rather than assuming every counting word stays count. A recognized rate can bind directly through source. If a relationship is not lexically encoded, use a source-backed derivation with exact original references; do not infer multi-cycle ratios or guaranteed ready cycles from a unit label.

For VIRTUAL original lines `1: Every cycle, the port may accept up to 2 requests.` and `2: 每周期可以接受最多两个请求。`, the following derivation form expresses a relationship beyond the deliberately narrow adjacent-unit recognizer, without an opaque count-to-rate conversion:

```json
[{"id":"english_rate","formula":2,"unit":"requests/cycle","citations":[{"file":"spec.md","start":1,"end":1,"quote":"Every cycle, the port may accept up to 2 requests."}]},{"id":"chinese_rate","formula":2,"unit":"requests/cycle","citations":[{"file":"spec.md","start":2,"end":2,"quote":"每周期可以接受最多两个请求。"}]}]
```

These are alternative virtual source statements, not rates to add or assumptions about an unspecified interface. Select the real sentence and derive its actual relationship; neither a bare unit label nor a successful formula establishes source meaning. A known source capability omitted from the baseline is an evidence-preparation gap, not an unknown physical capability.

- `{id,source:{file,line,number}}` selects a numbered lexical quantity in the original-source view. Inspect its columns, notation, complete type/identifier and original field definition. A dtype-width token supplies the width written in that type, not a normative external or internal requirement; section/heading references are not engineering facts. If the view supplies an attached unit, omit unit; the script fixes it. If unattached, add a nonempty semantic unit such as count or bits and justify the interpretation. A bit/byte spelling alias has the same value; a scale or semantic change is not an alias.
- `{id,convert:{fact:earlier_id,to_unit:unit}}` performs fixed compatible conversion, inheriting source citations. No value/formula/unit field in a conversion row.
- `{id,formula:expression,unit,citations:[original spans]}` derives a source relationship, not another source literal. Use only necessary derivations; the tool does not establish semantic relevance.

Expressions use fact identifiers, numbers, arithmetic and ceil/floor/min/max/abs. No code or external paths. Convert scaled quantities explicitly before arithmetic. The tool has no assumptions section in source analysis: uncertainty belongs in limits/ambiguities, not an invented source fact.

## Fixed shape and scope calculations

workloads is a nonempty list of `{id,quantity:expression,unit:string,basis:string,references:[original spans]}`. quantity is the positive integer logical extent of that original evaluated profile's actual task. First identify the input's performance/protocol tasks and choose decisive count/flow witnesses at those extents. A largest legal runtime extent is not automatically the measured task; retain legal maxima separately for state/capacity. Trace each profile to its original definition and count logical computation/input/output at that extent. Use maximum legal source dimensions separately for peak live state; a capacity bound is neither an extra loop nor an enlargement of a shorter task's required output. Candidate padding/full-region transfers are implementation costs, not added source effects. Do not invent a performance workload merely to size state.

computations is a nonempty list of `{id,semantics:string,steps:[primitive],scope:scope_recipe,basis:string,references:[original spans]}`. Choose one to three decisive, fully traceable computations. In existing semantics/basis, preserve consequential intermediate value versions, transformations and consumers on critical state/reduction paths; old/new-state labels alone can lose the distinction between a transformed state and its later commit. These are source-value descriptions, not new fields, hardware buffers or a full symbolic program. Each row names exactly the effects its steps count. Use gemv/dot for a real matrix-vector/dot witness with explicit scope; retain other effects in obligations/limits. Primitive choices are fixed:

- `{id,kind:"gemv",input_elements:K,output_elements:R}` derives `R*K` MAC for an input vector of K elements producing R outputs. `{id,kind:"dot",width:K}` derives K MAC. These describe source work, not hardware engines.
- `{id,kind:"matmul",lhs:[dimensions],rhs:[dimensions]}`: rank 1/2 positive operand shapes, matching contracted dimension. The script derives MAC count. Batch/repetition belongs in scope.
- `{id,kind:"elementwise",shape:[dimensions],ops_per_element:positive_integer_expression}`: explicitly explained scalar effects per element, script derives ops.
- `{id,kind:"reduction",shape:[dimensions],axis:zero_based_integer}`: script derives `(axis_length-1)` effects per other group.

Derive each step from its actual operands and result shape: vector[K] @ matrix[K,R] or matrix[R,K] @ vector[K] costs R*K MAC; dot(vector[K],vector[K]) costs K MAC. Recover state and intermediate shapes rather than pricing a full matrix-vector result as one dot. Steps within a computation ADD their costs, share one unit (MAC or ops) and the same scope; sequential products do not multiply their separate costs. Longer lists remain individually shape/primitive-validated, with unique step IDs and a technical maximum of 4096 entries per source computation or native realization group. That execution-size guard does not limit source operations or prove semantic completeness. Split differing units or scopes and include extra scalar effects in their own computations or explicit limitations. Unsupported semantics are obligations/limits, not arbitrary custom totals.

One scope recipe applies to each original profile; the program substitutes its workload extent. Use exactly one of:

```
{kind:"element",axis:string,multiplicity:expression}
{kind:"task",axis:string}
{kind:"prefix",axis:string,prefix_axis:distinct_string,first:expression,step:expression,multiplicity:expression}
```

Scope controls repetitions. element uses N*multiplicity; prefix uses `N*(2*first+(N-1)*step)/2*multiplicity`; task uses one occurrence. For changing history, declare one history-member primitive and let prefix scope sum the member counts; first/step describe history lengths, not operand width. Dynamic source loop indices are not expression variables in a base shape. Trace each original loop to the base shape or scope once; multiplicity contains only independent loops not already counted. Per-occurrence shape cannot contain workload_elements or its quantity fact a second time. Task shapes may use workload_elements for a genuinely whole-task recipe. Inspect the executed scope trace against source loop bounds. A direct-reference count is not a universal hardware minimum; transformed realizations need later equivalence review.

states is a list of `{id,shape:[dimensions],lifecycle:string,basis:string,references:[original spans]}`. It records logical elements/lifetime only; internal precision, copies, bank count, placement and aliasing remain candidate choices. Source maximum dimensions, not merely the performance task length, govern maximum legal live state. Empty states is appropriate when no persistent/logical state exists; explain source semantics.

flows is a list of `{id,direction:"read"|"write",encoding:"fixed_external"|"abi_dependent",fields:[...],scope:scope_recipe,basis:string,references:[original spans]}`. This collection describes traffic on the external memory request channel. Commands and completion responses on separate control ports belong in interface/completion obligations, not in these read/write payload totals; include control records here only when the original input requires transporting them through memory. fixed_external fields are `{shape:[dimensions],element_bits:expression}` backed by an input-required external encoding; select each width from its normative field/type and each shape from that profile's logical task. The script sums fields, rounds each occurrence to bytes and applies scope once. Keep reference-file packing and Python computation dtype distinct from required external encoding: neither forces a new physical layout or internal precision. ABI-dependent flows use fields=[] with no guessed byte count. Keep loading, output acceptance and completion obligations even when physical encoding is a design choice; the obligation alone does not imply a memory transfer.

## Delivery and bounded repair

The host validates exact source references/structure and executes `{mode:"source",model:baseline.analysis,sources:{"spec.md":...,"reference.py":...}}` with `python3 scripts/source_model.py --calculator <frozen evaluate.py>`. The trusted standard-library calculator bundle includes evaluate.py, source_quantities.py, budget_units.py, work_model.py, storage_model.py, execution_model.py and abi_geometry.py; copying only evaluate.py is insufficient. It records raw requests/results, baseline and audit/calculator script-bundle fingerprints, and reuses only for identical inputs and method/dependency closure. A structural pass alone is not an accepted baseline and no binding is semantic proof.

The host records independent fact, workload and row/profile computation/state/flow transactions. Only successfully executed, original/declaration/dependency-bound outputs enter the proposal-audit verified_data view. A bad declaration blocks its dependencies but does not erase unrelated success. Original declarations and all profiles determine complete coverage; failed or omitted results cannot be promoted by a parent flag. Local success can support a proposal counterexample or a focused engineering argument. Failed declared source analysis must be corrected before acceptance and complete cache reuse; failed rows never become verified facts. Keep the baseline focused on decisive, computable witnesses, and record other source obligations or unneeded derivations in limits rather than constructing an exhaustive implementation checklist. Successful analysis of this selected baseline does not require complete proposal-side profile/count/operand execution coverage. Full raw results and sidecars remain preserved; source-analysis.json is a compact verified view, not a source-semantic proof or a report observation receipt. Report observations use actual proposal/recompute execution, not source transaction IDs.

Standalone reference/structure validation accepts `{mode:"baseline",baseline,sources:{"spec.md":...,"reference.py":...},citations:optional_frozen_catalogue}` through scripts/validate_review.py, using only standard Python. It does not run the quantitative source analysis above. Inspect JSON ok/errors, not a zero process exit.

Initial response is ONLY `<<<AUDIT_BASELINE>>>` followed by the complete outer JSON. Wait for actual script results before claiming a calculation passed. The host preserves the actual raw parser response, parsed content, original-source fingerprints and calculation records. Input and current repair-target identities belong to the host. Never submit packet_sha256, baseline_sha256, base_sha256, base_report_sha256 or provenance in model content; these reserved fields are rejected. No hash copying is required.

Follow the host's CURRENT STEP for the applicable response frame. If only obligation references fail, return only:

```
<<<BASELINE_REPAIR>>>
{reference_updates:[{obligation_id,reference_index,reference:{file,start,end}}]}
```

Indices are zero based and address existing reference entries. At most 64 updates, no duplicate target; no obligation/category/meaning or analysis changes. Each update supplies reference:{file,start,end} or evidence_id, never both. The host preserves the request and merged version, then revalidates the entire baseline. If a full resubmission is requested for reference-only errors, preserve every non-reference semantic field.

If baseline obligations already passed but analysis failed, return ONLY `<<<BASELINE_ANALYSIS_REPAIR>>>` with `{analysis:<complete corrected analysis>,source_revision:{reason:string,references:[original spans]}}`. The host preserves scope, obligations, ambiguities and top-level limits, stores both versions and recomputes the analysis. No field merge, fabricated result or automatic semantic correction. The complete analysis replacement is authored by the model.

For an intentional changed obligation or baseline structure error, return a complete baseline with explicit source_revision={reason,references:[original spans]}; retain required outer fields. The model-authored reason, evidence and both versions are preserved; validation checks references rather than proving the new interpretation. All responses consume the same existing baseline and total limits; the skill cannot increase them. A malformed baseline prevents audit preparation, not proof of a proposal defect.

## Small complete example

For VIRTUAL inputs only: spec line 1 `extent=5`, line 2 `width=7`, line 3 `Return each position result as 16 bits`; reference line 1 `for position in range(extent):`, line 2 `    out[position] = sum(history[member] @ vector for member in range(position+1))`. history rows and vector each have width elements. The witness counts the dots feeding every result; scalar summation is explicitly outside its numeric count. This teaches base-plus-scope accounting and the two limits objects, not a real chip or architecture. Real loading, state, numerical and handshake effects remain source obligations even where the witness does not price them. Do not use zero-operation control placeholders or guessed ABI widths.

```
<<<AUDIT_BASELINE>>>
{
  "scope":"Virtual growing-prefix dot witness from original inputs only",
  "obligations":[{"id":"compute","category":"computation","requirement":"Sum all current-prefix dots for each output","references":[{"file":"reference.py","start":1,"end":2}]},{"id":"output","category":"interface","requirement":"Return one fixed-width result per position","references":[{"file":"spec.md","start":3,"end":3}]}],
  "ambiguities":[],
  "analysis":{
    "facts":[{"id":"extent","source":{"file":"spec.md","line":1,"number":1},"unit":"count"},{"id":"width","source":{"file":"spec.md","line":2,"number":1},"unit":"count"},{"id":"out_bits","source":{"file":"spec.md","line":3,"number":1}}],
    "workloads":[{"id":"positions","quantity":"extent","unit":"positions","basis":"actual task extent","references":[{"file":"spec.md","start":1,"end":1}]}],
    "computations":[{"id":"prefix-dots","semantics":"dots feeding each prefix sum","steps":[{"id":"member-dot","kind":"dot","width":"width"}],"scope":{"kind":"prefix","axis":"position","prefix_axis":"member","first":1,"step":1,"multiplicity":1},"basis":"one width-element dot for each current-prefix member","references":[{"file":"reference.py","start":1,"end":2}]}],
    "states":[],
    "flows":[{"id":"results","direction":"write","encoding":"fixed_external","fields":[{"shape":[1],"element_bits":"out_bits"}],"scope":{"kind":"element","axis":"position","multiplicity":1},"basis":"one result per task position","references":[{"file":"spec.md","start":3,"end":3}]}],
    "limits":["Scalar prefix summations and input transport are not priced by this witness."]
  },
  "limits":"Virtual protocol demonstration; no resource organization or hardware correctness proof."
}
```

The program derives 7 MAC per history member, 15 members over the task, 105 MAC and 10 output payload bytes. No dynamic position variable enters the dot shape or repeats the extent in multiplicity. The unpriced scalar sums still require proposal review; these counts prove only this witness's declared arithmetic. Compare the same complete effects and completion boundary even if a candidate labels one task differently, and review transformed costs separately from direct-count correspondence.

