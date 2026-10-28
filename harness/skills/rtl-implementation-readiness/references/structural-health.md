# Structural health evidence

Use this reference when planning a cost experiment, rechecking a changed candidate, or attaching historical synthesis checks to a proposal. The goal is to keep a local speed result from being mistaken for a structurally healthy full design. It is not a prerequisite to rerun full synthesis for every variant.

## Choose an affordable check

Read applicable evidence before the experiment. Match the actual source/dependency closure, ordered build inputs, top, defines/parameters, models, tool and completed check boundary. Existing full-design results may identify a baseline problem even when the new experiment measures a smaller region. Keep those scopes separate. A local result cannot close an uncovered whole-design issue.

Fill only material gaps within the campaign budget. The existing bounded built-in `proc` or `generic` jobs may provide useful structural evidence where the frontend supports the real design. Their `elaborate` stage checks reading and hierarchy, not the lowered structure. A custom check can use the ordinary `command` interface, but its exit code or success marker alone is not proof of structural health. Do not invent models to obtain a clean result.

Recheck the candidate with an applicable scope after changing RTL. If a wider check is unaffordable, preserve the local proposal with its unexamined obligations. Known baseline problems do not prohibit an authorized controlled synthesis experiment, including measurement to a known failure boundary. Record actual check timestamps and when evidence was reviewed; a post-experiment report cannot establish that a check or review happened beforehand.

Use the skill's [investigation workflow](../SKILL.md#investigate-important-findings) to explain important diagnostics before deciding what they imply. A category count is not an explanation, and a warning alone is not proof of a functional defect. Follow the actual source and configuration, retain competing interpretations when needed, and use a bounded discriminating check only when existing evidence cannot settle the question.

`health` verifies and interprets its supported evidence; it does not infer root causes. Other tool output can support source-backed findings in the ordinary model report while the helper's unsupported scope remains `unknown`. Preserve that distinction rather than rewriting a helper verdict or treating a local clean result as closure of an unrelated finding.

## Reuse workspace receipts

```bash
python3 -B "$S/scripts/readiness.py" health "$W" --variant grouped \
  --baseline-job base-check --candidate-job group-check \
  --out "$W/health-pair.json"
```

The job arguments are repeatable. `--variant baseline` permits a baseline-only inventory before candidate creation. With a sealed candidate, use its actual name. `health` reads and verifies existing evidence only: it does not run EDA, query a live process, repair status or convert failed measurements into successful ones. Use a new output path inside the workspace. Receipt references are read from their pinned original origin; those raw artifacts must remain available.

A completed Yosys CHECK can establish either scoped clean structure or reported issues. The parser inspects actual CHECK output rather than arbitrary warning counts or caller-written verdicts. An absent, partial or timed-out check is `unknown`; an earlier clean summary cannot stand in for a later check that started but did not complete. A nonzero tool exit may still contain useful completed CHECK findings. Unsupported check forms remain unknown.

## Historical or external checks

Use repeatable `--baseline-evidence /absolute/descriptor.json` and `--candidate-evidence /absolute/descriptor.json` for results outside this workspace. The descriptor is a small ordinary JSON file referring to actual source and artifacts; it does not replace them. For a Yosys check, use this shape, replacing every example path and hash with measured values:

```json
{
  "kind": "readiness_structure_evidence_v1",
  "source_root": "/absolute/frozen/source",
  "source_files": {
    "rtl/block.sv": "<actual SHA-256>",
    "rtl/defs.svh": "<actual SHA-256>"
  },
  "build": {
    "top": "block_top",
    "sources": ["rtl/block.sv"],
    "include_dirs": ["rtl"]
  },
  "tool_kind": "yosys",
  "tool": {
    "path": "/absolute/path/yosys",
    "sha256": "<actual SHA-256>"
  },
  "checker": {
    "scope": "selected_top_after_process_lowering",
    "pass": "proc; opt_clean; check -assert",
    "macros": {},
    "models": {},
    "config": {"defines": [], "parameters": {}}
  },
  "artifacts": {
    "script": {"path": "/absolute/run/check.ys", "sha256": "<actual SHA-256>"},
    "log": {"path": "/absolute/run/check.log", "sha256": "<actual SHA-256>"},
    "result": {"path": "/absolute/run/result.json", "sha256": "<actual SHA-256>"}
  }
}
```

`source_files` covers the actual synthesis source/dependency closure, including used headers and generated inputs; it is not the workspace's unrelated frozen test-asset list. Source paths are relative to the absolute `source_root`; source bytes must match the selected baseline or candidate closure. The build source order and include directories must match that version. The three artifact paths and tool path are absolute, and their actual bytes are rehashed. The result artifact is JSON with an actual integer `exit_code`; retain the real lifecycle status and times where available. Keep raw scripts/logs/results immutable on the EDA machine and transfer only small descriptors or summaries when required by the project.

`checker.scope` and `checker.pass` are nonempty descriptions of the real boundary and passes. `macros`, `models` and `config` are objects describing assumptions, models, defines, parameters and other settings; empty objects are appropriate only when no such settings apply. The whole checker object participates in comparability. A caller's matching description is an applicability declaration, not automatic proof that the script, models or selected scope mean what it claims. Inspect the referenced script and logs. This small interface recognizes actual Yosys CHECK evidence; it is not a general parser for every EDA tool.

## Attribution and delivery

The report records each evidence state as `clean_scoped`, `issues` or `unknown`, plus actual findings, evidence/artifact identities and times. Attribution is conservative:

- `baseline_existing`: a corresponding issue is observed in comparable baseline and candidate checks.
- `candidate_new`: the candidate reports an issue after an applicable completed baseline check was clean, with comparable scope, tool and configuration. If the baseline already had other diagnostics, an unmatched candidate diagnostic remains `unknown`; this helper does not infer correspondence through renamed signals or hierarchy.
- `unknown`: coverage is missing or incomplete, checks differ, or the issue cannot be matched reliably. Equal issue counts do not establish equal issues; hierarchy/name changes can prevent confident matching.

These states do not prove that an unmatched baseline issue was fixed. `unresolved_count` counts distinct observed diagnostic signatures; zero with an `unknown` state does not mean all issues are closed. Keep unresolved and unexamined items visible. A local candidate can be functionally verified and faster while unrelated full-design issues remain.

Attach the report with `--health "$W/health-pair.json"` to `compare` or `finalize`. Finalization and verification revalidate the report and its raw bindings; editing a verdict cannot promote a candidate. Confirmed candidate-new structural issues downgrade an otherwise positive recommendation to `evidence_incomplete`. Existing or unknown issues may accompany a scoped proposal with limitations. Without a report, health remains unknown. In all cases, `whole_design_readiness` remains `not_established`: this helper does not certify full-design synthesis readiness, timing, area or acceptance.
