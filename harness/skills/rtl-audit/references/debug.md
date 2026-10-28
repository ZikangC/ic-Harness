# Investigate an RTL acceptance failure

Use this method when a failed end-to-end result needs localization. The audit maintains independent tests and returns source-bound findings; the caller chooses the executor for candidate instrumentation and changes. Supply enough information to reproduce the failure without requiring an external operator to guide the investigation. Do not edit the frozen candidate or launch repairs from the audit.

## Stabilize the failure

Keep the candidate identity, legal input/configuration, seed or dataset identifier, packing format, tool command and actual job result. Record the prescribed acceptance metric as well as a useful example of the mismatch. Check that the test consumed complete output and associated it with the correct task before interpreting numerical errors. Preserve timeout and protocol failures even if some numerical outputs are correct.

When a full acceptance run is expensive, find a smaller reproducer with the same failure mechanism. Keep the failing sequence prefix and relevant state; arbitrarily resetting between steps can erase the defect. A reduced case is a debug aid, not replacement acceptance evidence.

## Structural preflight and disposition

Before passing a candidate or launching long whole-design mapping, inspect the frozen `@candidate/RTL_STRUCTURE.json` compile declaration. It has exactly `version: 1`, nonempty `configurations` and `blackboxes`. Configuration entries contain a unique `id` (at most 128 characters), `top`, ordered `sources`, and optional `include_dirs`, `defines`, `parameters` with empty defaults. Independently compare all declared delivery configurations with the original requirements, selected architecture and actual build. A missing or incomplete declaration is a repair/evidence gap; the audit must not modify the frozen candidate to manufacture it.

For each declared configuration, omit its label `id` and submit an independent `rtl_remote` action `structure_check` with the delivered `top`, ordered candidate-relative `sources`, `include_dirs`, `defines` and `parameters` in `configuration`, plus `timeout_seconds`. `defines` maps macro names to string values or `null`; `parameters` maps names to Verilog numeric-constant strings. Use the supplied schema for the remaining fields. The native response supplies `job_id` and `configuration_sha256`; query normal job status and inspect the actual protected result/log. Native config/result version 2 records a full-configuration fast-lint stage followed by Yosys; the compile declaration and report `structure_checks` remain version 1. For final acceptance or supported independent diagnostic disposition, request explicit `execution_plan: "complete"` at the action's top level (also the default), require both completed stages against their bound source/configuration, and check each actual tool identity, command, effective conditions, original transcript and outcome. For early negative diagnosis, explicit `execution_plan: "diagnostic_first"` returns complete lint evidence when target diagnostics exist without waiting for Yosys. This has `ok: false`, `status: findings`, diagnostic-only coverage, bitwise `unclosed` and Yosys `not_run`; it can justify `revise`, never `pass`. Lint clean still follows the complete path. Inspect actual terminal cleanup before any subsequent source change. Inspect `fast_lint.profile` (`yosys_synthesis_v1`): its effective defines and recorded tool-default undefines establish the supported synthesis view. Do not confuse a default Verilator simulation branch with that view. The runner rejects unsupported synthesis overrides and undeclared tool-specific macro dependencies rather than claiming all intrinsic macros are equivalent. The trusted script inventories modules and runs `proc -noopt` followed by `check -assert` without mapping; it cannot be replaced with a model-supplied script. Dynamic/absolute include/readmem dependencies are explicitly unsupported rather than silently excluded from coverage. Implementation lint and earlier checks are supporting evidence; they do not replace this current-candidate independent check.

Establish the complete synthesizable compilation closure, with include/generated sources and declared external dependencies, from the source requirements, selected architecture and actual build configuration. Cover every delivered compilation configuration relevant to those obligations; explain how runtime parameter classes map to them. A leaf-only or successful-subset check cannot support full-design coverage. Preserve top, source order, include/define/parameter settings, tool/version and actual source inventories. Source changes invalidate dependent results. The runtime mechanically requires native coverage of every normalized configuration in the frozen declaration, including its top/source order/defines/parameters; matching a whole-tree inventory alone does not prove that the delivery top was compiled. This does not replace your judgment that the declaration itself is complete and faithful to the source requirements.

Inspect every `blackboxes` entry: `{module, kind, reason, source_refs}`, with `kind` defaulting to `blackbox` or explicitly `whitebox`. Each entry needs a nonempty engineering reason and exact requirement/candidate RTL-interface/selected-architecture references. The candidate reference must identify the actual module/interface source, not the declaration itself. `@architecture/` in these evidence references is bound to this review's frozen architecture copy; an exact `@run/<frozen architecture scope>/` reference is also supported. Check that it is an authorized external interface, rather than missing required implementation. The runtime reparses actual `compile.blackbox_modules`, `whitebox_modules` and `module_inventory_complete` from the protected tool transcript; the union across checked configurations must exactly match the source-backed declarations. A black/white-box delivery top cannot establish full-design coverage. External-interface authorization does not supply functional models or physical signoff, and it does not waive the original requirements.

Classify all diagnostics for those configurations. Fast lint should expose concrete driver/assignment findings early so the implementation skill can repair them before another expensive frontend attempt. Verilator signal-level UNDRIVEN coverage may miss partly assigned used vectors; a clean fast lint does not replace bitwise Yosys structure checks. Use explicit `execution_plan: "complete"` when lint target warnings may be legitimate and need the existing exact independent finding/source/evidence disposition; an early diagnostic return is not complete coverage and cannot be waived into one. Other lint warnings remain visible for independent applicability review; do not make all warnings automatically fatal or automatically harmless. Used undriven bits, partly driven used vectors, conflicting drivers or unintended assignment-path gaps require implementation repair unless a precise diagnostic disposition is independently demonstrated. Distinguish legal input ports, established black-box interfaces, unused bits and sequential state holds from missing drivers. Treat syntax/elaboration failures, unsupported coverage, missing execution, timeout and source-integrity failures as unclosed checks; they cannot be waived into a pass. Unknown warning classes cannot be waived. Recognized memory-lowering warnings remain findings requiring exact independent disposition. An abnormal tool exit remains a tool failure even if the partial transcript contains a check-assert summary. A successful process exit, warning suppression, sample numerical agreement or two-state local equivalence is insufficient. Keep the complete logs and failed commands; check that wrappers propagate failures. While running, stderr carries the fixed version-2 stage markers and original bytes; final stdout is one complete JSON result. Missing/partial output, cancellation, tool error or timeout remains open/blocked, not an RTL repair completion. Do not ask for equivalent blind reruns or timeout increases without a new source-backed explanation or decisive configuration/tool change.

Supporting tools need demonstrated coverage for the actual construct and parameter context. Read their complete raw stdout and stderr, not only a JSON diagnostic view: when using configured Slang-tidy, `--diag-json` can omit later rule diagnostics, so an empty JSON list, quiet output or `--lint-only` invocation cannot establish a clean rule check. Signal-level lint or a tool with known bit-use blind spots needs relevant real-module/parameter-context bitwise or structural evidence; state its boundary and leave missing coverage open. Do not substitute a toy wrapper, omit dependencies or treat a supporting tool as a new mechanical release profile.

For a supplied Slang-tidy v12 tool, its configuration parser uses the following literal format, not JSON or TOML. Write these bytes to a current-scope file (including the final newline), then pass that path with `--config-file`:

```text
Checks:
 -*,
 synthesis-undriven-range=error
```

Invoke a supplied supporting binary through the configured Python executable when it is not a registered executable alias. In that wrapper, use a subprocess argument list with the exact binary and config paths; first retain `--config-file <path> --dump-config`, then run normal analysis with the actual complete compilation arguments. The rule name is a configuration entry, not a command-line flag. Preserve the configuration bytes/hash, binary identity, complete argv, both raw output streams and actual subprocess return code; propagate failures to the job. A dump verifies enabled rules but may omit severity, so it does not replace execution or the original diagnostic bytes. Keep explicit core driver warnings enabled as well: this rule alone can miss a wholly undriven signal. Check the actual published version's help and demonstrated construct coverage rather than trying unrelated configuration formats or interpreting parser failure as an RTL finding.

Live status is a bounded preview. If `counts_complete` is false, totals in `counts` are unknown (`null`); `observed_counts` are only prefix lower bounds. Follow the protected log's read continuation when the preview is truncated or lacks the fast-lint end marker. Zero observed findings cannot support a claim of clean lint, and a later tool timeout does not erase earlier warnings.

Add this account to a `pass` report using actual native jobs and exact source references:

```json
{
  "structure_checks": {
    "version": 1,
    "required_configurations": [
      {"job_id": "<actual structure-check job>", "configuration_sha256": "<native configuration SHA-256>"}
    ],
    "coverage": {
      "claim": "full_design",
      "reason": "<why these configurations and their complete closures cover the delivered top>",
      "source_refs": [
        {"path": "@input/<actual requirement source>", "sha256": "<actual source SHA-256>"},
        {"path": "@candidate/RTL_STRUCTURE.json", "sha256": "<actual declaration SHA-256>"},
        {"path": "@architecture/<actual architecture source>", "sha256": "<actual source SHA-256>"}
      ]
    },
    "waivers": []
  }
}
```

Coverage references must include the exact `@candidate/RTL_STRUCTURE.json` declaration plus frozen requirement and selected architecture sources using `@input/` and `@architecture/` (or exact `@run/` frozen-architecture) paths. Include every required configuration and every unresolved diagnostic disposition; do not manufacture placeholders in the submitted report. For a diagnostic judged intentional, each waiver contains the actual `job_id`, exact `finding_id`, a source-backed `reason`, nonempty `source_refs` with actual frozen paths and SHA-256 values, and `evidence` listing relevant real independent audit job IDs. Check those jobs and the exact bit/path/configuration before authorizing the waiver. A broad category exclusion, omitted source, implementation self-description or unrelated successful test is not sufficient. Waivers do not authorize unsupported/timeout/source-changed runs. For `revise`, `insufficient_evidence` or `blocked`, retain checks already executed and describe missing configurations and their reason; a real failure can justify repair without executing every remaining configuration or claiming complete coverage. A structural pass closes this limited structural obligation, not full functional or physical acceptance.

## Compare semantic boundaries

Start at coarse boundaries such as input decoding, producer output, state update, reduction, normalization or output conversion; choose boundaries from the implementation and reference. Add only the observations needed to narrow the failure. Align each comparison by transaction/element identity and logical state version rather than simulator cycle alone.

Account for numeric representation before comparing: dtype, signedness, scale, accumulation width, rounding/saturation point, layout and allowed error. A floating reference intermediate need not equal a quantized hardware value bit for bit. Conversely, an aggregate error metric can hide a discrete-selection or state-order failure. Use the source's metric for acceptance and justified local comparisons for diagnosis.

Locate the first deviation not explained by these rules, then compare that producer's actual operands and retained state. Shared resources require observing which operation consumed which operand and when the state became visible. For protocol failures, narrow to accepted transactions, address/byte-enable boundaries and output/DONE ordering instead of treating missing data as zeros.

Full snapshots and traces remain on the server. Return a short mismatch summary, selected indices/time windows and paths to the stored artifacts. Use deterministic summaries to narrow large data before requesting more model context.

## Findings and subsequent review

The review report should identify the source obligation, reproducible command/job, expected versus actual behavior and useful observed boundary. Return `revise` for an established implementation defect, or `insufficient_evidence` when decisive evidence is missing; do not prescribe an unsupported patch. Publish or return this report and end the audit, leaving repair routing to the caller.

The chosen repair executor can add development instrumentation and local regressions in its own work. Audit-owned tests and the frozen reviewed candidate remain unchanged. If the original audit test is wrong, correct it in the audit scope with a source-backed explanation and retain the original failure; changing the RTL to satisfy an incorrect oracle is not progress.

If the caller later submits a repaired candidate, a new independent audit context reviews it and reruns affected checks. Reuse unaffected jobs only with valid source/configuration dependencies. Exercise the regression and the prescribed full acceptance workload before passing. Check the delivered configuration as well as any debug build: instrumentation must observe computation, not replace it or become the only path that produces correct results.
