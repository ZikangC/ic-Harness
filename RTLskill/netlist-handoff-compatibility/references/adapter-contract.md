# Backend adapter contract

The controller supplies a JSON probe configuration. It identifies the backend,
declared scope, and up to three ordered stages: `availability`, `read`, and
`link`. Each stage uses an argv array, a timeout no greater than 60 seconds, and
one or more literal success markers.

```json
{
  "backend": "openroad-opensta",
  "scope": "complete",
  "stages": [
    {
      "name": "availability",
      "argv": ["openroad", "-version"],
      "timeout_seconds": 5,
      "success_markers": ["OpenROAD"]
    },
    {
      "name": "read",
      "argv": ["openroad", "-exit", "probe/read_netlist.tcl"],
      "timeout_seconds": 15,
      "success_markers": ["HANDOFF_READ_OK"]
    },
    {
      "name": "link",
      "argv": ["openroad", "-exit", "probe/link_netlist.tcl"],
      "timeout_seconds": 20,
      "success_markers": ["HANDOFF_LINK_OK"]
    }
  ]
}
```

The read probe must parse the actual candidate netlist using the backend's real
input command. The link probe must load the same technology/library/model set
planned for the downstream flow, read the candidate, select the exact top, and
execute the backend's real link operation. A marker must be emitted only after
the operation succeeds.

The generic runner recognizes common diagnostics for unsupported syntax,
unknown module/blackbox contracts, and missing library cells. An adapter may
provide ordered `diagnostic_rules` to match a backend's authoritative messages:

```json
{
  "diagnostic_rules": [
    {"kind": "library_coverage_missing", "pattern": "cell .* not found in library"},
    {"kind": "blackbox_contract_missing", "pattern": "unknown module"}
  ]
}
```

Patterns are case-insensitive regular expressions. Allowed custom kinds are
the failure phases in the phase schema. Keep rules narrow and backed by real
tool messages; do not let an agent label failures manually.

For normalization, use the front-end's supported export/lowering commands and
write a new artifact. Record at least the original SHA-256, derived SHA-256,
command/config path, backend identity, and declared scope. Then probe the
derived artifact from the beginning.
