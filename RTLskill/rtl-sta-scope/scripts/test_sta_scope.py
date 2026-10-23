#!/usr/bin/env python3
"""Policy tests for the portable STA-scope gate."""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scope_gate import run


local = run({
    "change_mode": "local",
    "rtl_changes": [{
        "module": "stage_a", "path": "rtl/stage_a.sv",
        "interface_changed": False, "clock_reset_changed": False,
        "parameter_changed": False, "shared_state_changed": False,
        "combinational_boundary_changed": False,
        "boundary_registered": True, "impact_known": True,
    }],
    "affected_modules": ["stage_a"],
    "remaining_blackboxes": ["stage_b"],
    "design_context": {
        "status": "current", "hard_gate": "pass", "graph_fingerprint": "graph-1",
        "module_contexts": {"stage_a": {
            "relationships": {"instantiated_by": ["top"]},
            "timing_context": {"has_sequential_logic": True},
        }},
    },
})
assert local["scope"] == "local_module" and local["measurement_plan"] == "module_sta"
assert local["minimum_scope"] == "local_module"

comb = run({
    "change_mode": "local",
    "rtl_changes": [{
        "module": "stage_a", "combinational_boundary_changed": True,
        "boundary_registered": False, "impact_known": True,
    }],
    "remaining_blackboxes": ["stage_b"],
    "design_context": {
        "status": "current", "hard_gate": "pass", "graph_fingerprint": "graph-1",
        "module_contexts": {"stage_a": {}},
    },
})
assert comb["scope"] == "top_level_partial"

unknown = run({"change_mode": "local", "rtl_changes": [{"module": "stage_a"}], "remaining_blackboxes": []})
assert unknown["scope"] == "top_level_complete" and "local_scope_not_proven" in unknown["reason_codes"]

stale = run({
    "change_mode": "local",
    "rtl_changes": [{
        "module": "stage_a", "interface_changed": False, "clock_reset_changed": False,
        "parameter_changed": False, "shared_state_changed": False,
        "combinational_boundary_changed": False, "boundary_registered": True,
        "impact_known": True,
    }],
    "remaining_blackboxes": ["stage_b"],
    "design_context": {"status": "stale", "hard_gate": "hold", "module_contexts": {}},
})
assert stale["scope"] == "top_level_partial"
assert "design_context_not_current" in stale["reason_codes"]

measurement = run({"measurement_adapter_changed": True})
assert measurement["scope"] == "no_rtl_sta" and measurement["measurement_plan"] == "measurement_smoke"

print("rtl sta scope tests: PASS")
