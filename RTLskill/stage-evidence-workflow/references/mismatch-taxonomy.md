# Mismatch taxonomy

The workflow separates source integrity from model summary quality.

## Source integrity

Hard-hold conditions include unreadable required artifacts, missing required
facts, deterministic source conflicts, invalid Skill envelopes, stale expected
Skill identity/generation, mutation of the prepared packet, and a controller
hard gate. These conditions produce
`integrity_state=hold|infra_uncertain` and may not use a summary fallback to
authorize downstream work.

## Summary quality

Recoverable conditions include invalid model JSON, a model-echoed fingerprint
mismatch, unknown `fact_refs`, missing required topic/Skill coverage,
unsupported model schema, excess summary length, prohibited action/policy
fields, and Evidence Model unavailability. They produce
`summary_state=retry_required` on the first attempt. The controller supplies
the exact validator-produced retry prompt to one bounded retry; it does not
inspect the Fact Catalog itself.

If the retry still fails while source integrity remains `pass`, the workflow
uses deterministic catalog facts and reports
`summary_state=degraded|unavailable`,
`summary_source=deterministic_fallback`, and `hard_gate=pass`.

Do not use fuzzy field-name matching to pass evidence. Real JSON Pointer,
exact current-packet `fact_id` membership, and declared type/value comparison
are allowed deterministic normalizations. A wrong fingerprint in the model
output invalidates that summary and permits one retry/fallback; a changed
packet fingerprint during validation is a source-integrity hold. Natural-language
summary text remains non-authoritative; controller hard gates and resolved
facts win.
