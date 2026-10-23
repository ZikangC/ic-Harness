# Tool-probe contract

Use a probe only before costly, fragile, or environment-dependent actions:
remote/container access, synthesis, STA, implementation, or a newly introduced
tool command. Ordinary file reads and harmless local inspection do not need a
probe.

The controller writes a JSON config with argv arrays; `tool_probe.py` never
uses a shell. Each probe passes only if it exits zero and emits every declared
success marker. A marker must be printed only after the real minimal operation
has succeeded.

```json
{
  "probes": [
    {
      "name": "openroad_sta_link",
      "argv": ["openroad", "-exit", "scripts/sta_link_probe.tcl"],
      "cwd": "/work/project",
      "timeout_seconds": 15,
      "success_markers": ["STA_LINK_OK"]
    }
  ]
}
```

For OpenROAD, `sta_link_probe.tcl` should read the same LEF/Liberty and a small
or current closure netlist, execute `link_design`, then print `STA_LINK_OK`.
For Yosys, read the relevant filelist and run `hierarchy -top <top>; stat`,
then print a marker. A bare version check is only supplementary evidence.

```bash
python3 scripts/tool_probe.py \
  --config runs/current/tool_probe_config.json \
  --output runs/current/tool_probe.json
```

The controller must pass that JSON to `closure_gate.run(probe_path=...)` or
stop immediately on the nonzero probe exit. A failed probe is evidence of an
adapter/environment issue, not an RTL failure.
