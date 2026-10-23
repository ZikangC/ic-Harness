---
name: netlist-handoff-compatibility
description: "Audit and govern synthesized Verilog handoff compatibility between front-end synthesis and downstream STA or implementation tools. Use when a netlist must be proven readable and linkable, or when dialect, hierarchy, blackbox, library-cell, or backend-command incompatibilities block the handoff; do not use it to claim timing closure."
---

# Netlist Handoff Compatibility

Use this skill after synthesis exports a candidate netlist and before formal
top-level STA or physical implementation. Its success means the configured
backend can read and link the declared handoff artifact. It does not prove
functional correctness, timing closure, routability, or tapeout readiness.

Preserve the original synthesis netlist and raw logs. If normalization is
needed, create a separate derived artifact and record its source hash, command,
backend, and scope. Never patch generated Verilog text merely to silence a
parser, invent cell declarations, or upgrade a partial/blackbox scope to a
complete-chip claim.

## Evidence and gate

Use `handoff_gate.py` as the stable controller interface. It combines:

- a streamed structural inventory of the candidate netlist;
- a short backend capability/read/link probe;
- deterministic phase classification and a bounded next-action category.

Read [the controller API](references/controller-api.md) when integrating the
gate. Read [the adapter contract](references/adapter-contract.md) when defining
a backend probe. Read [the phase schema](references/phase-schema.md) when
interpreting or extending classifications.

The probe must execute the backend's real read and link operations and print a
declared success marker only after each operation succeeds. Tool existence,
version output, model recollection, and a front-end exit code are insufficient.
Commands are argv arrays and never shell strings.

## Workflow

1. Inventory the exact candidate netlist and configured top. Reject empty,
   truncated, structurally inconsistent, or top-missing artifacts before the
   backend probe.
2. Run one short backend-specific probe in order: availability, read, link.
   Stop downstream stages after the first failure.
3. Classify the first blocking phase. Permit only a repair in the reported
   domain: environment/adapter, export/hierarchy, HDL normalization, blackbox
   contract, or library coverage.
4. If normalization is required, derive a new netlist through a declared tool
   transformation, preserve the source artifact, and rerun the same probe on
   the derived artifact.
5. After two identical probes with the same artifact/config fingerprint and no
   phase transition, stop for debugging. Do not increase an expensive flow's
   budget or ask an agent to guess alternative syntax.
6. Only `handoff_compatible` may enter the top-level STA closure gate. Preserve
   the declared handoff scope; compatibility with a partial hierarchy is not
   complete-chip closure.

## Controller policy

The controller, not the planning agent, executes the inventory and probe. Give
the agent the compact gate JSON and paths to immutable raw evidence. While the
gate is unresolved, block formal STA/P&R launches except for the bounded probe
needed to test the current handoff hypothesis. The agent may choose a repair
hypothesis within authorization, but cannot override the phase or success
criteria.
