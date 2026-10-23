# Measurement schema

A measurement record must identify `measurement_id`, `module_id`, `observed_at`,
`method`, `scope`, `source_run`, and `artifact`. Recommended fields include
tool, library, corner, path class, area, delay, instance count, attribution
quality, design fingerprint, artifact hash, comparability key, and explicit
Fmax validity.

Numbers without provenance are not accepted as optimization evidence. A
partition ABC delay is normally `fmax_valid=false`; blackbox or partial scope
must remain explicit. Comparisons are valid only within a compatible
`comparability_key` and matching design context, unless a downstream policy
explicitly accepts lower confidence.
