# Controller API

Add the skill directory to the controller's Python import path, then call the
stable `handoff_gate.run()` function. It performs local reads and commands only;
run it on the host/container that holds the candidate netlist and backend.

```python
from pathlib import Path
from handoff_gate import run

gate = run(
    netlist_path=Path("build/candidate.v"),
    top="chip_top",
    probe_config_path=Path("runs/current/handoff_probe_config.json"),
    inventory_output=Path("runs/current/handoff_inventory.json"),
    probe_output=Path("runs/current/handoff_probe.json"),
)
```

For split local/remote orchestration, run the CLI remotely and transfer only
the small JSON outputs:

```bash
python3 handoff_gate.py \
  --netlist build/candidate.v \
  --top chip_top \
  --probe-config runs/current/handoff_probe_config.json \
  --inventory-output runs/current/handoff_inventory.json \
  --probe-output runs/current/handoff_probe.json \
  --output runs/current/handoff_gate.json
```

The controller gate is:

```python
if not gate["handoff_compatible"]:
    block_formal_sta_and_implementation()
    constrain_agent_to(gate["repair_domain"], gate["next_action"])
else:
    launch_sta_closure_gate(scope=gate["scope"])
```

Alternatively, a controller may collect inventory and probe evidence in an
execution adapter and call `run(inventory_path=..., probe_path=...)` locally.
Do not transfer the large netlist merely to run the classifier.
