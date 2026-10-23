# Source extraction contract

Extract requirements only, not candidate designs. Return ONLY `<<<REQUIREMENTS>>>` followed by one complete requirements JSON object. No bare JSON, narration, architecture document or second frame.

## Requirements object

`{facts:[fact rows],constraints:[{id,kind:hard|target,limit:fact_id,relation:<=|>=|==,unit,verification:estimate|implementation,quantity:area|capacity|clock|numerical|functional|task_cycles|cycles_per_item|items_per_second|other,workload:id when performance}],functional_and_state:prose,numerical:prose,workloads:prose,interfaces_and_storage:prose,choices:prose,conflicts:[],open_conditions:[],storage_area_model:{status:supplied,density_fact:fact_id} OR {status:unspecified,reason},runtime_parameters:[{id,domain:{kind:range,lower:fact_id,upper:fact_id} OR {kind:set,values:[fact_ids]},basis,citations:[source citations]}],storage_rules:[{kind:SRAM|ROM,min_depth:expression,width_multiple:expression,read_ports:expression,write_ports:expression,read_latency_cycles:expression}]}`.

IDs are identifier strings, not numeric indices. Constraint limits reference facts expressed in the constraint unit; use convert when the source unit differs. Capture requirements, shapes, encoding widths and memory service needed by budgets; do not declare candidate lanes, internal precision or area assumptions here. Preserve other constants and functional detail in prose rather than cataloguing every reference scalar.

Numeric constraint rows represent only <=, >= or == comparisons. Preserve legal sets and nonnumeric protocol/state obligations explicitly as hard obligations in prose; a continuous range or scalar proxy cannot replace them. Include target clock and numeric acceptance thresholds. Use verification=estimate for calculable area/capacity/performance budgets; implementation applies only to evidence requiring RTL, simulation, synthesis or physical work. Performance rows identify their common workload IDs. External service conditions do not become chip task-latency requirements. Keep conflicts and missing conditions explicit.

## Bind quantities before using them

The host supplies frozen, numbered original sources and a per-line quantity view from `scripts/source_quantities.py`, including token columns, notation and complete type/identifier context. Original names are `spec.md`, `reference.py` and any supplied `support/<input-relative-path>`; cite the exact supplied file name for a helper/configuration fact. Read the named field or operand definition before selecting a quantity as a shape, limit, width or service condition. A dtype-width token names the width written in that type; a section/heading reference is not an engineering quantity. The script binds the selected literal and attached supported unit, not its role or whether the source makes it normative. Use exactly one of these fact forms:

- Source: `{id,source:{file:frozen_original_name,line:integer,number:integer},unit:optional_declared_unit}`. Both line and number are one based; number selects the displayed quantity on that source line. Copy the selector, not its numeric value. A source fact has no value, formula or citations fields. If the view has an attached unit, use that unit; a contradictory declaration is invalid. When no unit is attached, unit is required and remains the model's semantic assertion to audit; the tool does not default to count. Unsupported or ambiguous source text remains an explicit extraction/tool issue, not a guessed value.
- Conversion: `{id,convert:{fact:existing_fact_id,to_unit:target_unit},citations:optional_exact_source_references}`. The calculator converts an already bound, converted or correctly derived quantity between compatible supported units. Select the target unit; the tool owns the scale, bit/byte factor and supported prefix conversion. Omitted citations inherit the referenced fact's citation ancestry. The conversion row has no value, formula or unit field. Do not encode a unit conversion as arithmetic in formula.
- Derivation: `{id,formula:expression,unit,citations:[exact_source_references]}`. Use engineering relationships over source or converted facts, such as inclusive range cardinality, shape products or fixed-point scale. The calculator owns arithmetic; the model remains responsible for the relationship and iteration scope. Convert operands to a common unit before combining quantities whose prefix scales differ.

Source references in conversion/derivation and other requirement fields are exact `{file,start,end,quote}` original-input excerpts. Do not change punctuation, Markdown or unit spelling in quotations. Fact identifiers may be declared in any order; conversion/formula dependencies must exist and be acyclic. Derived units and citations remain nonempty.

For virtual frozen source lines `1: rate = 7 kHz` and `2: last_index = 5`, a quantity view with one quantity on each line supports:

```json
{"facts":[{"id":"rate_raw","source":{"file":"spec.md","line":1,"number":1}},{"id":"rate_hz","convert":{"fact":"rate_raw","to_unit":"Hz"}},{"id":"last_index","source":{"file":"spec.md","line":2,"number":1},"unit":"count"},{"id":"entries","formula":"last_index+1","unit":"count","citations":[{"file":"spec.md","start":2,"end":2,"quote":"last_index = 5"}]}]}
```

This illustrates fact forms, not a complete requirements object. The actual receipt yields rate_raw=7 kHz, rate_hz=7000 Hz and entries=6. The source index is a literal; its inclusive cardinality is a derivation. A quoted signed exponent retains its sign; a positive shift magnitude uses a formula over that exponent. Select a Qm.n component or dtype width from its complete written token, not an adjacent value or document reference. Separate normative external encoding, reference-file encoding and Python computation dtype: only an input-required external representation fixes exchanged field widths; a Python dtype alone does not fix the new ABI or internal precision. A valid source binding does not prove this distinction.

Extract any supplied macro density in its original storage/area unit and exact binary/decimal context. The binding preserves attached units and source prefix context; an ambiguous basis needs a precise supported source selection, not a guessed prefix_base. storage_area_model points to the raw density fact; the program converts that density when costing actual macro inventory. Never replace a supplied model with a design assumption. With no supplied model, use unspecified with a reason. Capacity and area conversion are separate calculations.

## Runtime domains and storage conditions

Runtime parameter IDs are new expression names distinct from facts. Capture every input-defined legal integer range or discrete set needed for sizing; domains reference bound facts. Keep each measured profile's actual task extent separate from the maximum legal extent used for peak live state. A maximum allocation bound does not repeat every operation or enlarge the required logical output of a shorter task; any candidate padding or full-region transfer is a separate implementation cost. runtime_parameters=[] is explicit only if no such domain exists. Preserve all legal values and functional detail in prose.

Extract storage rules only from supplied capabilities. min_depth and width_multiple are positive numeric expressions such as "2" or a fact ID, never comparisons like ">=2". Read/write ports and read latency are nonnegative numeric expressions; zero write ports or asynchronous latency are valid. Missing conditions remain unknown.

## Repair the unaccepted extraction

After binding/unit checks fail, use a local complete-row fact repair:

```text
<<<REQUIREMENTS_REPAIR>>>
{"base_sha256":"actual last raw declaration hash","facts":[{"id":"existing_or_new_fact_id","source":{"file":"spec.md","line":1,"number":1}}],"remove_fact_ids":["obsolete_unused_fact"]}
```

Use actual original-source selectors. The base hash identifies the last unaccepted raw declaration, not a materialized extraction or budget. Each submitted fact replaces its entire existing row or appends a new ID; omitted obsolete fields disappear. Unsubmitted facts remain. remove_fact_ids explicitly removes only facts with no remaining dependencies; it cannot delete a constraint limit, domain endpoint, density or another required fact. The host does not infer deletions or fill values. Optional storage_area_model replaces that entire object. No other top-level fields are permitted in this local repair. If constraints, prose or other fields need correction, submit the complete `<<<REQUIREMENTS>>>` object.

Full and local outputs undergo the same binding, dependency, dimensional and storage checks. Raw declarations, materialized facts and binding receipts are saved separately. Accepted extraction freezes; later disagreement uses cited requirement_corrections, not this transport operation. Every response remains versioned and consumes the host's existing execution limits.
