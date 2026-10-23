---
name: rtl-module-evidence-registry
description: "Maintain an evidence-backed current-state registry for RTL modules, including blackbox representation, verification history, PPA measurements, and the current integration-mainline projection. Use when a long-running RTL/EDA workflow must stop inferring current module state from prose or old logs."
---

# RTL Module Evidence Registry

Use this skill to maintain one authoritative, compact view of every module in
the current RTL/filelist and its evidence-backed lifecycle state. The registry
separates current observed configuration from historical verification and PPA
measurements. It never treats a historical narrative as current truth.

Read [the registry schema](references/registry-schema.md),
[event schema](references/event-schema.md), and
[measurement schema](references/measurement-schema.md) before producing a
project adapter. Read [the controller API](references/controller-api.md) before
using the output as a hard gate.

The generic entry point is `registry_gate.py`. Project-specific discovery of
filelists, partition configuration, immutable result files, and measurement
logs belongs in an adapter.

## Source precedence

1. Current configuration, filelist, and RTL scan define current representation.
2. Immutable structured run results define historical verification.
3. Structured measurement records define timing and area evidence.
4. Structured mainline events and the previous Registry generation define the
   current integration-mainline projection. A project may source those events
   from an optional Transaction Ledger, but the Registry projection remains
   authoritative for Registry gates and consumers.
5. Progress prose and model summaries are context only; they cannot establish
   any verified or current state.

## Invariants

- Register every module discovered from the current filelist, RTL, or active
  configuration, not only blackboxes or optimization candidates. A
  configuration-only module remains visible but cannot be expanded until a
  usable RTL body is present in the current inputs.
- Removing a module from a blackbox list means only `rtl_body` is currently
  selected. It does not prove functional, synthesis, link, or timing success.
- Preserve `expansion_verified_once` independently from current blackbox
  policy. Re-blackboxing cannot erase valid historical proof.
- An incomplete measurement after a verified expansion remains
  `measurement_incomplete`; it is neither a deterministic RTL failure nor a
  new successful verification.
- A project that does not provide an optional normalized verification status
  records `unclassified`. The Registry keeps only that module's transaction in
  `pending_reverify`; it must not infer a verdict from tool exit codes or make
  the Registry interface unavailable.
- If any module is `measurement_incomplete` or `pending_reverify`, the current
  Registry mainline must reference the matching module. Missing or conflicting
  ownership is a registry conflict and holds the gate instead of silently
  selecting another blackbox.
- `can-expand` passes only for a module that is currently a blackbox in the
  requested partition and is not blocked by a conflicting pending transaction.
- Regenerate the hot view at every controller decision boundary and after each
  execution result. If session, cycle, phase, fingerprints, or evidence
  watermark do not match the request, fail closed.
- Feed models the compact hot view and selected module records, not the full
  event or measurement history.
- Keep state events and measurements append-only. The current registry is a
  rebuildable projection, not the sole copy of historical proof.
- Maintain and serve a versioned design-context projection supplied by the
  project adapter: hierarchy relations, source ownership, timing-boundary
  facts, and optional semantic intent. Reject module-context queries when its
  source fingerprints do not match current RTL/filelist/configuration.

## Boundary

This skill does not scan or construct an arbitrary project's design graph,
edit RTL, modify a blackbox list, start EDA tools, choose an optimization
candidate, schedule commands, or decide STA scope. It does own the validated
current integration-mainline projection, but not a general parent/child
transaction system. A separate project extractor constructs or refreshes
design context; the registry only maintains, versions, validates, and serves
that supplied context. The controller enforces returned gates.
