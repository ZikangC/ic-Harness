#!/usr/bin/env python3
"""No-network tests for the recoverable one-file optimization runner."""
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from optimization_governance import initial_state, transition
from optimization_runner import run_edit, run_rollback


MAINLINE_REF = {
    "transaction_id": "mainline:one", "kind": "blackbox_expansion",
    "module_id": "next_block", "partition": "top_level_sta",
    "identity_fingerprint": "sha256:mainline",
}


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    project = root / "project"
    (project / "rtl").mkdir(parents=True)
    target = project / "rtl" / "candidate.sv"
    other = project / "rtl" / "other.sv"
    target.write_text("module candidate; wire a; endmodule\n", encoding="utf-8")
    other.write_text("module other; endmodule\n", encoding="utf-8")
    state_path = root / "optimization.json"
    state_path.write_text(json.dumps(initial_state(MAINLINE_REF)), encoding="utf-8")
    editor = root / "edit.py"
    editor.write_text(
        "from pathlib import Path\n"
        "p=Path('rtl/candidate.sv')\n"
        "p.write_text(p.read_text().replace('wire a', 'wire optimized_a'))\n",
        encoding="utf-8")
    request = {
        "project_root": str(project), "state_path": str(state_path),
        "checkpoint_root": str(root / "checkpoints"), "operation_id": "edit-1",
        "campaign_id": "campaign-1", "module": "candidate",
        "rtl_path": "rtl/candidate.sv", "interrupted_mainline_ref": MAINLINE_REF,
        "integration_target": "next_block", "insertion_mode": "decoupled",
        "model_command": [sys.executable, str(editor)], "model": "offline-test",
        "evidence": {"candidate": {
            "value_score": 2.0,
            "value_evidence": {"comparability_key": "same", "area_um2": 10.0,
                               "delay_ps": 100.0},
        }},
    }
    output = run_edit(request)
    assert output["status"] == "modified_pending_functional"
    assert output["governance_state"]["phase"] == "modified_pending_functional"
    assert output["governance_state"]["current_candidate"]["baseline_measurement"]["area_um2"] == 10.0
    assert "optimized_a" in target.read_text(encoding="utf-8")
    assert other.read_text(encoding="utf-8") == "module other; endmodule\n"
    assert run_edit(request)["rtl_fingerprint"] == output["rtl_fingerprint"]

    repair_state = transition(
        output["governance_state"], {"kind": "functional_failed", "repair_attempt": False})
    state_path.write_text(json.dumps(repair_state), encoding="utf-8")
    repair_editor = root / "repair.py"
    repair_editor.write_text(
        "from pathlib import Path\n"
        "p=Path('rtl/candidate.sv')\n"
        "p.write_text(p.read_text().replace('optimized_a', 'repaired_a'))\n",
        encoding="utf-8")
    repair_request = {
        **request, "operation_id": "edit-2",
        "model_command": [sys.executable, str(repair_editor)],
    }
    repaired = run_edit(repair_request)
    assert repaired["status"] == "modified_pending_functional"
    assert "repaired_a" in target.read_text(encoding="utf-8")
    assert repaired["rtl_fingerprint"] != output["rtl_fingerprint"]
    after_repair_failure = transition(
        repaired["governance_state"], {"kind": "functional_failed"})
    assert after_repair_failure["current_candidate"]["repair_attempts"] == 1
    rollback_state = transition(after_repair_failure, {"kind": "sta_failed"})
    state_path.write_text(json.dumps(rollback_state), encoding="utf-8")
    rolled_back = run_rollback({
        "project_root": str(project), "state_path": str(state_path),
        "checkpoint_root": str(root / "checkpoints"),
        "operation_id": "rollback-1", "operation": "rollback_candidate",
    })
    assert rolled_back["status"] == "rollback_complete"
    assert rolled_back["governance_state"]["phase"] == "resume_pending"
    assert target.read_text(encoding="utf-8") == "module candidate; wire a; endmodule\n"

print("rtl optimization runner tests: PASS")
