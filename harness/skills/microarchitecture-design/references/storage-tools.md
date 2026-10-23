# Derived storage demand

The design model declares represented objects and their simultaneous-live phases.
`scripts/storage_model.py` derives demand rather than accepting a claimed
`required_bits`. The host invokes it through the budget evaluator. The module
uses the host's safe expression evaluator and Python standard library only; it
does not call models or execute reference code.

## Runtime domain

Requirements declare runtime parameters independently of the performance
workload. A performance sample does not define the largest legal configuration.

```json
{"runtime_parameters":[
  {"id":"extent","domain":{"kind":"range","lower":"minimum_fact","upper":"maximum_fact"},
   "basis":"all legal integer extents in the input", "citations":[{"file":"spec.md","quote":"actual input excerpt"}]}
]}
```

A domain is exactly an inclusive `range` with lower/upper input fact IDs, or a
`set` with `values` containing unique-valued input fact IDs. Bounds and members
must evaluate to integers. The runtime parameter identifier cannot overwrite
any input fact or candidate choice. Parameter citations establish where to
audit the domain; this script cannot prove that the extracted domain equals
the legal input domain.

The script validates all domains, then projects their Cartesian product onto
parameters actually referenced by object shape, element bits, copies, alignment
or metadata expressions. It enumerates every combination of those parameters,
including interior values, across all declared static phases. Omitted parameters
cannot affect these declared capacity expressions; this does not establish source
coverage. Unknown dependencies are errors, never silently omitted. No relevant
parameters means one scenario. More than 4096 relevant combinations produces
`unknown`, with no capacity pass. Do not narrow legal domains or discard a relevant
dependency to fit that limit. The script does not assume monotonic demand or sample
only endpoints. A malformed domain remains an error even when unreferenced.

## Phases, allocations and objects

```json
{"storage_phases":[
  {"id":"compute","basis":"input and accumulators simultaneously present"},
  {"id":"drain","basis":"input retired; output retained until accepted"}
],"buffers":[
  {"id":"scratch","kind":"SRAM","count":1,"depth":"chosen_depth","width":"chosen_width",
   "storage_reference":"actual_macro_name","basis":"allocation in this physical macro",
   "objects":[
     {"id":"input_values","shape":["extent","input_dimension_fact"],
      "element_bits":"chosen_data_bits","copies":1,"alignment_bits":"chosen_alignment_bits",
      "metadata_bits":0,"live_in":["compute"],"basis":"declared representation and lifetime"},
     {"id":"result_values","shape":["extent","output_dimension_fact"],
      "element_bits":"chosen_result_bits","copies":1,"alignment_bits":"chosen_alignment_bits",
      "metadata_bits":0,"live_in":["drain"],"basis":"output replaces retired input"}
   ]}
]}
```

This example describes syntax, not a sufficient design or a recommended layout.
Every buffer supplies `id,kind,count,depth,width,basis,objects`. SRAM/ROM buffers
also supply `storage_reference` naming a physical inventory entry of matching
kind. Registers omit it or use null and retain their buffer ID for linked logic
area costs. Allocation expressions use facts and candidate choices, never
runtime parameters: physical hardware cannot resize between legal runs.

Each object supplies exactly `id,shape,element_bits,copies,alignment_bits,
metadata_bits,live_in,basis`. IDs are unique across all buffers. Shape dimensions are nonnegative integers (a zero extent has no elements). Element bits, copies and alignment are positive integers; metadata is nonnegative and still occupies storage for an empty object. An empty shape [] is one scalar. `live_in` is a nonempty list
of unique declared phase IDs. A phase expresses simultaneous liveness, not a
duration or a proof of the schedule. Include overlap during prefetch, double
buffering, state handover and output backpressure when those actually occur.

For every parameter scenario, the script computes:

```text
payload_bits = product(shape) * element_bits
padded_bits_per_copy = ceil((payload_bits + metadata_bits) / alignment_bits) * alignment_bits
object_bits = padded_bits_per_copy * copies
live_bits(buffer, phase) = sum(object_bits of objects live in that phase)
required_bits(buffer) = max over all scenarios and phases of live_bits
allocated_bits(buffer) = count * depth * width
```

Copies repeat the complete padded payload and its metadata. Use a separate
object for shared metadata. Account for block tails, packing waste or layout
overheads in the actual shape/alignment/metadata representation; the script
does not infer them from prose. Lossless representation changes are legal,
subject to equivalence and access evidence. Do not replace the represented
shape with the allocation size merely to pass capacity checks.

Each buffer peak must fit its allocation. Each individual allocation must fit
the referenced macro. Across buffers that reference the same macro, the script
sums actual simultaneous demand within the same phase and parameter scenario
and compares it with physical inventory bits. It does not sum logical
allocations across disjoint lifetimes, so legal time multiplexing is permitted.
That bound alone does not prove addresses are placed without overlap or that
required banks and ports exist.

## Evidence and limits

`derive(candidate, values, requirements, expression, inventory)` returns
`ok,errors,unknown,buffers,macro_live_peaks,scenarios_checked,parameter_projection,scope`.
The projection records all, relevant and omitted domain sizes, total and projected
scenario counts; scenarios_checked is the actual complete enumeration. Buffer
results include `required_bits,allocated_bits,peak_phase,peak_parameters` and
the objects' evaluated sizes at that peak scenario and `live_at_peak` flags.
Macro results identify the
shared physical inventory, peak phase, parameter values and each contributing
buffer's live bits. Inspect those executed values rather than writing another
capacity formula in Markdown. `required_bits` and `allocated_bits` are forbidden
input fields; old declarations are rejected rather than converted.

`ok=true` means the supported declarations passed structural/arithmetic checks.
Nonempty `unknown` blocks release even when `ok=true`. Enumerating the capacity projection is not proof that the full legal domain,
objects, copies or lifetimes
match the original spec and reference. Independent audit must reconstruct
those semantic relationships and test proposed memory reuse against the
concrete execution windows.
