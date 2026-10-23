#!/usr/bin/env python3
"""Deterministic regression tests for the handoff gate."""
import json
import sys
import tempfile
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(ROOT))

from handoff_gate import run


def probe_config(link_code: str, link_marker: str = "HANDOFF_LINK_OK"):
    python = sys.executable
    return {
        "backend": "test-backend", "scope": "complete",
        "stages": [
            {"name": "availability", "argv": [python, "-c", "print('BACKEND_OK')"],
             "success_markers": ["BACKEND_OK"], "timeout_seconds": 5},
            {"name": "read", "argv": [python, "-c", "print('HANDOFF_READ_OK')"],
             "success_markers": ["HANDOFF_READ_OK"], "timeout_seconds": 5},
            {"name": "link", "argv": [python, "-c", link_code],
             "success_markers": [link_marker], "timeout_seconds": 5},
        ],
    }


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    empty = root / "empty.v"
    empty.write_text("// generated but empty\n", encoding="utf-8")
    assert run(netlist_path=empty, top="chip_top")["phase"] == "source_artifact_empty"

    missing = root / "missing.v"
    missing.write_text("module child (); endmodule\n", encoding="utf-8")
    assert run(netlist_path=missing, top="chip_top")["phase"] == "source_top_missing"

    malformed = root / "malformed.v"
    malformed.write_text("module chip_top ();\n", encoding="utf-8")
    assert run(netlist_path=malformed, top="chip_top")["phase"] == "source_artifact_malformed"

    valid = root / "valid.v"
    valid.write_text("module chip_top (); endmodule\nmodule child (); endmodule\n", encoding="utf-8")
    assert run(netlist_path=valid, top="chip_top")["phase"] == "not_attempted"

    passing_config = root / "passing.json"
    passing_config.write_text(json.dumps(probe_config("print('HANDOFF_LINK_OK')")), encoding="utf-8")
    result = run(netlist_path=valid, top="chip_top", probe_config_path=passing_config)
    assert result["phase"] == "handoff_compatible" and result["scope"] == "complete"

    blackbox_config = root / "blackbox.json"
    blackbox_config.write_text(json.dumps(probe_config("print('unknown module compute'); raise SystemExit(1)")), encoding="utf-8")
    result = run(netlist_path=valid, top="chip_top", probe_config_path=blackbox_config)
    assert result["phase"] == "blackbox_contract_missing" and not result["handoff_compatible"]

    marker_config = root / "marker.json"
    marker_config.write_text(json.dumps(probe_config("print('command exists')")), encoding="utf-8")
    result = run(netlist_path=valid, top="chip_top", probe_config_path=marker_config)
    assert result["phase"] == "backend_link_error"

    unavailable_config = root / "unavailable.json"
    unavailable = probe_config("print('HANDOFF_LINK_OK')")
    unavailable["stages"][0]["argv"] = [str(root / "missing-backend")]
    unavailable_config.write_text(json.dumps(unavailable), encoding="utf-8")
    result = run(netlist_path=valid, top="chip_top", probe_config_path=unavailable_config)
    assert result["phase"] == "backend_unavailable" and result["failed_stage"] == "availability"

print("netlist handoff compatibility tests: PASS")
