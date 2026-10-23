# Concrete numeric probes and output conversion

Use `numeric_probes` in the existing calculation request for the architecture's actual risky format boundaries. These are executed finite witnesses, not a replacement model of the algorithm. Match the chosen constants, reachable domains or explicitly stated reachability premises, rounding, overflow and guards. A successful probe unrelated to a source risk does not close that risk.

## Execution and interpretation

- `ok` means the declaration was executable. It does **not** mean its numerical assertions passed.
- `coverage_status:not_checked` means the array was empty. `computed_only` means at least one executed probe lacked assertions. Such probes have `pass:null`, never `pass:true`.
- `checked` means every submitted probe had assertions and all passed; it says nothing about completeness of source-risk coverage. `failed` means a declared assertion failed. `invalid` means a field, expression, operation domain or reject-overflow policy failed execution.
- Each result includes assertion count, checked-probe count and informative-probe count. An empty collection or unasserted calculation cannot establish numerical correctness.
- Expressions can use existing input/candidate names and **earlier successful probe** scalar outputs `<probe_id>__<metric>`. This permits testing a denominator formed from the actual rounded preceding result instead of copying a supposed result by hand. Forward references, cycles, arrays, arbitrary Python and overwriting existing names are not supported. These outputs do not silently become budget symbols.

The existing scalar contract is `{id,operation,input,format:{bits,fractional_bits,signed},rounding,overflow,assertions,basis,references?:[...]}`. Operations are `identity`, `exp`, `sigmoid`, `reciprocal`, `sqrt`, `rsqrt`. Nonlinear point functions use binary64, not the chosen hardware approximation. Results include `raw`, `integer`, `quantized`, `lsb`, `minimum`, `maximum`, `abs_error`, `overflow_count` and `zero_count`; `overflowed` is also exposed at probe level. Bits are integer 1..128 and fractional_bits 0..128, independently of the chosen sign convention.

Supported rounding is explicit: `nearest_even`, `nearest_ties_positive` (floor(x+0.5), including negative ties), `half_away` (nearest, ties away from zero), `toward_zero`, `floor`, `ceil`. No ambiguous `half_up` alias is accepted. Overflow is `saturate` or `reject`; reject overflow makes the declaration invalid rather than silently clipping. Assertions are `{metric,relation,expected}` with `== != < <= > >=` and safe expected expressions. A false assertion is executed evidence against the declared property; choose properties justified by the selected path. Zero quantization can be legal with an explicit, checked subsequent guard.

For example, use an identity probe for the retained constant, then an independent reciprocal or rsqrt probe whose input uses its `<id>__quantized` result and the chosen guard. Check overflow through `overflow_count`, representability through `quantized`/`abs_error`, and positivity only where the architecture relies on it. Do not claim these concrete values establish a reachable range or a complete numerical regression.

## Uniform positive row-to-integer conversion

Use `operation:"encode_row"` to execute the selected final output conversion on a small concrete row:

```json
{"id":"row_conversion","operation":"encode_row","input":["1/8","1/4","3/8"],"scale":8,"format":{"bits":16,"fractional_bits":0,"signed":true},"rounding":"nearest_even","overflow":"saturate","assertions":[{"metric":"cosine","relation":">=","expected":".999999"},{"metric":"argmax_match","relation":"==","expected":1},{"metric":"saturation_count","relation":"==","expected":0}],"basis":"synthetic conversion example; replace values, format and required thresholds from the actual design/input"}
```

This adds exactly one field, `scale`, to the scalar contract. `input` is a nonempty array of at most 4096 safe numeric expressions; `scale` is one finite positive value shared by the entire row. The output format is a signed integer (`signed:true`, `fractional_bits:0`). The tool applies scale **before** rounding and saturation. Scaling after a previous lossy conversion cannot recover its discarded values; the declared input must match the actual selected boundary.

`encoding` contains `inputs`, `scaled_values`, actual integer `codes`, and `decoded_values` (code/scale). `results` exposes numeric metrics:

- `count`, `scale`, `cosine`, `cosine_defined`, `max_absolute_error`.
- `reference_argmax`, `encoded_argmax`, `argmax_match`: all use the smallest index among tied maxima. The encoded argmax is computed from the **actual integer codes**, including clipping and new ties, not from the unrounded values.
- `encoded_max_ties`, `introduced_tie_pairs`: the latter counts pairs of unequal input values made equal by encoding; original equal inputs are excluded.
- `saturation_count`/`overflow_count` and `zero_count`.

Cosine compares original values with actual integer codes; positive uniform decode scaling leaves it unchanged. If either row norm is zero, `cosine:null` and `cosine_defined:0`; a direct cosine assertion then fails rather than inventing a value. Null and arrays are not exported as numeric identifiers.

Select rows that challenge the actual format and conversion: close maxima and new ties, negative half steps, small magnitudes, and declared range extremes. Use source requirements for cosine/ranking properties; do not introduce a new exact-ranking requirement if the input permits discrepancies. This probe does not prove the preceding internal arithmetic, all legal rows, or any required end-to-end accuracy statistic.
