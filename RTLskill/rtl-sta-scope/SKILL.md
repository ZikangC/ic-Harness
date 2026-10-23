---
name: rtl-sta-scope
description: "Choose the smallest defensible STA scope after RTL, constraint, hierarchy, or measurement changes. Use to distinguish module-local, affected-cone, and top-level STA without upgrading local evidence to full-chip timing proof."
---

# RTL STA Scope

Use this skill at a verified change boundary, before scheduling STA. It decides
which timing evidence was invalidated; it does not run EDA, edit RTL, or claim
that a local result proves top-level timing.

The controller supplies structured facts derived from the actual RTL diff plus
a current, fingerprinted design-context slice. Never infer a local scope from
a filename, an isolated code fragment, or an agent's confidence alone. Unknown
context, interface, clock, reset, parameter, shared-state, or
combinational-boundary impact is a reason to broaden the scope.

Call `scope_gate.py` as the stable controller interface. Read
[the scope schema](references/scope-schema.md) when producing change facts and
[the controller API](references/controller-api.md) when integrating it.

## Decision policy

- No RTL or timing-input change requires no RTL STA; a measurement-adapter
  change may still require a measurement smoke selected by the controller.
- A single module may use `local_module` only when its external interface is
  unchanged, all timing boundaries are registered, and clock/reset,
  parameters, shared state, and cross-boundary combinational behavior are
  explicitly unchanged, with a current context record for that module.
- A known dependency set that does not reach the top may use
  `affected_cone`.
- A top/interface/clock/reset/parameter/filelist/constraint/blackbox change,
  cross-boundary combinational change, architectural edit, or unknown impact
  requires top-level STA. It is `top_level_partial` while allowed timing
  blackboxes remain and `top_level_complete` only when none remain.

The output is a scheduling contract. The controller owns commands and hard
gates, and must retain the evidence scope in all reports.
