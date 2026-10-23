#!/usr/bin/env python3
from __future__ import annotations

from module_registry import can_expand, module_context, reconcile, validate_hot_view


def packet(reconciliation_id: str = "r1") -> dict:
    return {
        "project_id": "demo", "session_id": "s1", "cycle_id": 1,
        "phase": "pre_decision", "observed_at": "2026-09-16T12:00:00+08:00",
        "reconciliation_id": reconciliation_id, "primary_partition": "top_level_sta",
        "observation": {
            "config_fingerprint": "c1", "filelist_fingerprint": "f1",
            "rtl_fingerprint": "rtl1", "evidence_watermark": "e1",
            "design_context": {
                "status": "current", "graph_fingerprint": "graph-1",
                "graph_ref": "status/design_graph.json", "generation": 1,
                "source": "test_adapter",
                "source_fingerprints": {
                    "config_fingerprint": "c1", "filelist_fingerprint": "f1",
                    "rtl_fingerprint": "rtl1",
                },
            },
            "modules": [
                {"module_id": "a", "rtl_path": "rtl/a.sv", "in_filelist": True,
                 "body_present": True, "design_context_fingerprint": "graph-1",
                 "relationships": {"instantiated_by": ["top"]},
                 "timing_context": {"has_sequential_logic": True},
                 "partitions": {"top_level_sta": {"representation": "blackbox"}}},
                {"module_id": "b", "rtl_path": "rtl/b.sv", "in_filelist": True,
                 "body_present": True, "design_context_fingerprint": "graph-1",
                 "relationships": {"instantiates": []},
                 "timing_context": {"has_sequential_logic": True},
                 "partitions": {"top_level_sta": {"representation": "rtl_body"}}},
            ],
        },
        "verification_evidence": [{
            "evidence_id": "v-b", "module_id": "b", "status": "verified",
            "run_id": "run-b", "artifact": "result.json", "observed_at": "2026-09-15T10:00:00+08:00",
            "functional_passed": True, "preflight_passed": True,
            "sta_link_result": "ok", "sta_timing_measured": True,
        }],
        "measurements": [{
            "measurement_id": "m-b", "module_id": "b", "observed_at": "2026-09-15T10:00:00+08:00",
            "method": "yosys_abc", "scope": "partition", "source_run": "run-b",
            "artifact": "feedback.txt", "area_um2": 10.0, "delay_ps": 20.0,
            "fmax_valid": False, "comparability_key": "abc:nangate45:typical:b",
        }],
    }


def main() -> None:
    first = reconcile(packet())
    assert first["schema_version"] == 3
    assert "optimization_state" not in first
    assert first["registry_status"] == "current"
    assert can_expand(first, "a", "top_level_sta")["allowed"] is True
    assert can_expand(first, "b", "top_level_sta")["allowed"] is False
    assert first["modules"]["b"]["derived"]["integration_state"] == "integrated_verified"
    assert first["modules"]["b"]["measurements"]["latest"]["fmax_valid"] is False
    context = module_context(first, "a")
    assert context["hard_gate"] == "pass"
    assert context["relationships"]["instantiated_by"] == ["top"]
    expected = {"session_id": "s1", "cycle_id": 1, "phase": "pre_decision",
                "evidence_watermark": "e1", "fingerprints": first["fingerprints"]}
    assert validate_hot_view(first, expected)["hard_gate"] == "pass"
    assert validate_hot_view(first, {**expected, "cycle_id": 2})["hard_gate"] == "hold"
    stale_context_packet = packet("stale")
    stale_context_packet["observation"]["design_context"]["source_fingerprints"]["rtl_fingerprint"] = "old"
    stale_context = reconcile(stale_context_packet)
    assert stale_context["design_context"]["status"] == "stale"
    assert module_context(stale_context, "a")["hard_gate"] == "hold"
    replay = packet()
    replay["previous_registry"] = first
    replayed = reconcile(replay)
    assert replayed["generation"] == first["generation"]
    assert replayed["emitted_events"] == []
    assert replayed["new_measurements"] == []

    legacy = {
        **first,
        "schema_version": 2,
        "optimization_state": {"phase": "measurement_pending"},
    }
    migrated_packet = packet("legacy-v2")
    migrated_packet["previous_registry"] = legacy
    migrated = reconcile(migrated_packet)
    assert migrated["schema_version"] == 3
    assert "optimization_state" not in migrated

    changed = packet("r2")
    changed["previous_registry"] = first
    changed["observation"]["modules"][0]["partitions"]["top_level_sta"]["representation"] = "rtl_body"
    changed["verification_evidence"].append({
        "evidence_id": "v-a-incomplete", "module_id": "a", "status": "measurement_incomplete",
        "run_id": "run-a", "artifact": "result.json", "observed_at": "2026-09-16T11:00:00+08:00",
    })
    second = reconcile(changed)
    assert second["modules"]["a"]["derived"]["integration_state"] == "measurement_incomplete"
    assert second["modules"]["a"]["verification"]["expansion_verified_once"] is False
    assert second["hot_view"]["hard_gate"] == "hold"
    assert any(item["kind"] == "verification_pending_without_transaction"
               for item in second["conflicts"])

    resumed_packet = packet("r2-resumed")
    resumed_packet["previous_registry"] = first
    resumed_packet["observation"]["modules"][0]["partitions"]["top_level_sta"]["representation"] = "rtl_body"
    resumed_packet["verification_evidence"].append(changed["verification_evidence"][-1])
    resumed_packet["pending_transaction"] = {
        "module_id": "a", "partition": "top_level_sta", "phase": "measurement_incomplete",
    }
    resumed_packet["mainline"] = {
        "kind": "blackbox_expansion", "target": "a", "status": "measurement_incomplete",
    }
    resumed = reconcile(resumed_packet)
    assert resumed["registry_status"] == "current"
    assert resumed["hot_view"]["hard_gate"] == "pass"

    # An optional project normalizer may be absent.  Preserve only the affected
    # transaction without reconstructing status from tool exit codes.
    unclassified_packet = packet("r2-unclassified")
    unclassified_packet["previous_registry"] = first
    unclassified_packet["observation"]["modules"][0]["partitions"]["top_level_sta"]["representation"] = "rtl_body"
    unclassified_packet["verification_evidence"].append({
        "evidence_id": "v-a-unclassified", "module_id": "a", "status": "unclassified",
        "run_id": "run-a-legacy", "artifact": "result.json",
        "observed_at": "2026-09-16T11:30:00+08:00",
    })
    unclassified_packet["pending_transaction"] = {
        "module_id": "a", "partition": "top_level_sta", "phase": "evidence_pending",
    }
    unclassified = reconcile(unclassified_packet)
    assert unclassified["registry_status"] == "current"
    assert unclassified["modules"]["a"]["derived"]["integration_state"] == "pending_reverify"
    assert unclassified["hot_view"]["hard_gate"] == "pass"

    # Historical verification survives a later temporary blackbox policy.
    third_packet = packet("r3")
    third_packet["previous_registry"] = first
    third_packet["observation"]["modules"][1]["partitions"]["top_level_sta"]["representation"] = "blackbox"
    third = reconcile(third_packet)
    assert third["modules"]["b"]["derived"]["integration_state"] == "temporarily_blackboxed"
    assert third["modules"]["b"]["verification"]["expansion_verified_once"] is True
    print("rtl-module-evidence-registry tests: PASS")


if __name__ == "__main__":
    main()
