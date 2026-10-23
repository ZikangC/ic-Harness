# Investigate an RTL acceptance failure

Use this method when a failed end-to-end result needs localization. The audit maintains independent tests and returns source-bound findings; the caller chooses the executor for candidate instrumentation and changes. Supply enough information to reproduce the failure without requiring an external operator to guide the investigation. Do not edit the frozen candidate or launch repairs from the audit.

## Stabilize the failure

Keep the candidate identity, legal input/configuration, seed or dataset identifier, packing format, tool command and actual job result. Record the prescribed acceptance metric as well as a useful example of the mismatch. Check that the test consumed complete output and associated it with the correct task before interpreting numerical errors. Preserve timeout and protocol failures even if some numerical outputs are correct.

When a full acceptance run is expensive, find a smaller reproducer with the same failure mechanism. Keep the failing sequence prefix and relevant state; arbitrarily resetting between steps can erase the defect. A reduced case is a debug aid, not replacement acceptance evidence.

## Compare semantic boundaries

Start at coarse boundaries such as input decoding, producer output, state update, reduction, normalization or output conversion; choose boundaries from the implementation and reference. Add only the observations needed to narrow the failure. Align each comparison by transaction/element identity and logical state version rather than simulator cycle alone.

Account for numeric representation before comparing: dtype, signedness, scale, accumulation width, rounding/saturation point, layout and allowed error. A floating reference intermediate need not equal a quantized hardware value bit for bit. Conversely, an aggregate error metric can hide a discrete-selection or state-order failure. Use the source's metric for acceptance and justified local comparisons for diagnosis.

Locate the first deviation not explained by these rules, then compare that producer's actual operands and retained state. Shared resources require observing which operation consumed which operand and when the state became visible. For protocol failures, narrow to accepted transactions, address/byte-enable boundaries and output/DONE ordering instead of treating missing data as zeros.

Full snapshots and traces remain on the server. Return a short mismatch summary, selected indices/time windows and paths to the stored artifacts. Use deterministic summaries to narrow large data before requesting more model context.

## Findings and subsequent review

The review report should identify the source obligation, reproducible command/job, expected versus actual behavior and useful observed boundary. Return `revise` for an established implementation defect, or `insufficient_evidence` when decisive evidence is missing; do not prescribe an unsupported patch. Publish or return this report and end the audit, leaving repair routing to the caller.

The chosen repair executor can add development instrumentation and local regressions in its own work. Audit-owned tests and the frozen reviewed candidate remain unchanged. If the original audit test is wrong, correct it in the audit scope with a source-backed explanation and retain the original failure; changing the RTL to satisfy an incorrect oracle is not progress.

If the caller later submits a repaired candidate, a new independent audit context reviews it and reruns affected checks. Reuse unaffected jobs only with valid source/configuration dependencies. Exercise the regression and the prescribed full acceptance workload before passing. Check the delivered configuration as well as any debug build: instrumentation must observe computation, not replace it or become the only path that produces correct results.
