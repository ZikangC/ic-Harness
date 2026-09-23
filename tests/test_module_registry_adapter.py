#!/usr/bin/env python3
"""No EDA/controller is started; this tests only registry projection and gates."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
from design_context_adapter import design_context_slice, ensure_design_context
from module_registry_adapter import reconcile_project_registry
from rtl_skill_bridge import module_registry_can_expand


SKILL = ROOT.parent / "RTLskill/rtl-module-evidence-registry"


def write_project(root: Path, blackbox: str) -> None:
    (root / "rtl").mkdir(parents=True, exist_ok=True)
    (root / "harness").mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    (root / "rtl/filelist.f").write_text("rtl/design.sv\n", encoding="utf-8")
    (root / "rtl/design.sv").write_text(
        "module compute(input logic clk); endmodule\nmodule top(input logic clk); compute u(); endmodule\n",
        encoding="utf-8")
    (root / "harness/ppa.toml").write_text(
        "[design]\ntop=\"top\"\n[[partitions]]\nname=\"top_level_sta\"\n"
        f"top=\"top\"\nblackbox=\"{blackbox}\"\n",
        encoding="utf-8")


def write_result(root: Path, run: str, *, ppa_exit: int, timing: bool,
                 incomplete: bool = False) -> None:
    path = root / "runs" / f"agent-02_yosys_ppa--{run}" / "result.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "expanded_module": "compute", "finished_at": run,
        "functional_quick_exit": 0, "functional_medium_exit": 0,
        "mandatory_full_exit": 0, "preflight_exit": 0, "ppa_exit": ppa_exit,
        "measurement_status": (
            "measurement_incomplete" if incomplete
            else "complete" if timing and ppa_exit == 0
            else "verification_evidence_missing"),
        "sta_closure_audit": {"sta_link_result": "ok" if timing else "unmeasured",
                              "sta_timing_measured": timing,
                              "measurement_incomplete": incomplete,
                              "repair_domain": "measurement_budget" if incomplete else None},
    }), encoding="utf-8")


def write_stopped_container_result(root: Path, run: str) -> None:
    run_dir = root / "runs" / f"agent-02_yosys_ppa--{run}"
    functional_dir = root / "runs" / f"02_yosys_ppa_functional--{run}"
    run_dir.mkdir(parents=True)
    functional_dir.mkdir(parents=True)
    feedback = functional_dir / "feedback.txt"
    feedback.write_text("transport attempt 1/3 failed (exit=1)\n", encoding="utf-8")
    (functional_dir / "remote_console.log").write_text(
        "Error response from daemon: Container abc is not running\n", encoding="utf-8")
    functional_log = run_dir / "functional_quick.log"
    functional_log.write_text(f"feedback={feedback}\n", encoding="utf-8")
    (run_dir / "result.json").write_text(json.dumps({
        "expanded_module": "compute", "finished_at": run,
        "functional_quick_exit": 1, "functional_log": functional_log.relative_to(root).as_posix(),
        "preflight_exit": None, "ppa_exit": None,
    }), encoding="utf-8")


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary) / "demo"
        write_project(project, "compute missing_body")
        write_result(project, "2026-09-15T10:00:00+08:00", ppa_exit=0, timing=True)
        status = project / "status"
        registry = reconcile_project_registry(
            skill_root=SKILL, session_id="s1", cycle_id=1, phase="pre_decision",
            controller_state={}, project_root=project,
            registry_path=status / "registry.json", events_path=status / "events.jsonl",
            measurements_path=status / "measurements.jsonl")
        assert set(registry["modules"]) == {"compute", "missing_body", "top"}
        assert registry["design_context"]["status"] == "current"
        assert registry["schema_version"] == 3
        assert "optimization_state" not in registry
        assert registry["modules"]["compute"]["observed"]["relationships"]["instantiated_by"] == ["top"]
        graph = ensure_design_context(project)
        context = design_context_slice(project, rtl_paths=["rtl/design.sv"], module_hints=["compute"])
        assert context["hard_gate"] == "pass"
        assert context["graph_fingerprint"] == graph["graph_fingerprint"]
        assert set(context["module_contexts"]) == {"compute", "top"}
        assert registry["modules"]["compute"]["derived"]["integration_state"] == "temporarily_blackboxed"
        assert "emitted_events" not in registry and "new_measurements" not in registry
        assert module_registry_can_expand(SKILL, registry, "compute", "top_level_sta")["allowed"] is True
        unavailable = module_registry_can_expand(
            SKILL, registry, "missing_body", "top_level_sta")
        assert unavailable["allowed"] is False and unavailable["reason"] == "rtl_body_unavailable"
        assert registry["hot_view"]["body_available_blackboxes"] == ["compute"]
        assert registry["hot_view"]["expandable_blackboxes"] == ["compute"]

        write_project(project, "")
        write_result(project, "2026-09-16T10:00:00+08:00", ppa_exit=137, timing=False, incomplete=True)
        registry = reconcile_project_registry(
            skill_root=SKILL, session_id="s1", cycle_id=2, phase="pre_decision",
            controller_state={}, project_root=project,
            registry_path=status / "registry.json", events_path=status / "events.jsonl",
            measurements_path=status / "measurements.jsonl")
        compute = registry["modules"]["compute"]
        assert compute["verification"]["expansion_verified_once"] is True
        assert compute["derived"]["integration_state"] == "measurement_incomplete"
        assert registry["pending_transaction"]["module_id"] == "compute"
        assert module_registry_can_expand(SKILL, registry, "compute", "top_level_sta")["allowed"] is False

        write_stopped_container_result(project, "2026-09-17T10:00:00+08:00")
        registry = reconcile_project_registry(
            skill_root=SKILL, session_id="s1", cycle_id=3, phase="pre_decision",
            controller_state={}, project_root=project,
            registry_path=status / "registry.json", events_path=status / "events.jsonl",
            measurements_path=status / "measurements.jsonl")
        assert registry["modules"]["compute"]["verification"]["latest_expansion_attempt"]["status"] == "unclassified"
        assert registry["pending_transaction"]["module_id"] == "compute"
        assert registry["pending_transaction"]["phase"] == "evidence_pending"
    print("module registry project adapter tests: PASS")


if __name__ == "__main__":
    main()
