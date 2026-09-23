#!/usr/bin/env python3
"""Pure policy checks for the Pro supervisor; never calls OMP or remote tools."""
from __future__ import annotations

import sys
import os
import json
import tempfile
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
import pro_supervisor  # noqa: E402
import sta_evidence_extract  # noqa: E402
from pro_guidance_runner import (EXPAND_BLACKBOX_ACTION, PRO_DECISION_PROMPT_MAX_CHARS,
                                 RUN_EVIDENCE_ACTION,
                                 ProDecisionRejected, ProGuidanceInputError,
                                 ProGuidanceRunner, ProModelInvocationError,
                                 ProOutputError,
                                 build_prompt, parse_decision)  # noqa: E402
from project_adapter_api import decision_error, load_project_adapter  # noqa: E402
from task02_project_adapter import Task02ProjectAdapter  # noqa: E402
from decision_packet_adapter import (DecisionPacketError, build_decision_packet,
                                     materialize_executor_request)  # noqa: E402


def stage_fixture(*, contract: dict[str, object] | None = None) -> dict[str, object]:
    contracts = ({"skill_optimization_triage": {
        "source_identity": {"registry_generation": 7},
        "contract": contract,
    }} if contract is not None else {})
    return {
        "workflow": "stage-evidence-workflow", "stage": "sta_health", "run_id": "run-a",
        "packet": {"input_status": "ready_for_model", "deterministic_state": "ready_for_model",
                   "packet_fingerprint": "stage-packet-a", "missing": [], "conflicts": []},
        "review": {
            "review_state": "CONSISTENT", "integrity_state": "pass",
            "summary_state": "pass", "hard_gate": "pass", "attempt": 0,
            "packet_fingerprint": "stage-packet-a", "model_status": "consistent",
            "strategic_view": {"evidence_groups": [], "summary": "current stage",
                               "summary_source": "model"},
            "validated_execution_contracts": contracts,
        },
        "hard_gate": "pass",
    }


def decision_packet_fixture() -> dict[str, object]:
    return {
        "schema_version": 1,
        "identity": {"transaction_id": "tx-a", "generation": 7,
                     "packet_fingerprint": "packet-a"},
        "evidence": {"stage": "sta_health", "summary": "current stage",
                     "evidence_groups": []},
        "allowed_actions": {
            "continue": {
                "purpose": "continue one bounded batch", "executor_ref": "flash.run",
                "execution_mode": "standard_batch",
                "parameter_schema": {"flash_attempts": {"minimum": 1, "maximum": 6}},
                "source": "test",
            },
            "inspect_existing_sta_evidence": {
                "purpose": "read evidence", "executor_ref": "evidence.inspect",
                "execution_mode": "inspect_existing_sta_evidence",
                "parameter_schema": {"flash_attempts": {"enum": [0]}}, "source": "test",
            },
        },
    }
def main() -> None:
    decision = parse_decision(
        'analysis\n```json\n'
        '{"action":"continue","flash_attempts":2,"flash_directive":"measure closure",'
        '"rationale":"new STA error","reusable_knowledge":"none"}\n```'
    )
    assert decision["flash_attempts"] == 2
    budget_decision = parse_decision(
        '```json\n{"action":"increase_budget","flash_attempts":1,"flash_directive":"use 3600 seconds",'
        '"rationale":"timeout-only closure evidence","reusable_knowledge":"none"}\n```'
    )
    assert budget_decision["action"] == pro_supervisor.INCREASE_BUDGET_ACTION
    readonly = parse_decision(
        '```json\n{"action":"inspect_existing_sta_evidence","flash_attempts":0,"flash_directive":"read existing",'
        '"rationale":"need path facts","reusable_knowledge":"none"}\n```'
    )
    assert readonly["action"] == pro_supervisor.READ_ONLY_EVIDENCE_ACTION
    run_evidence = parse_decision(
        '```json\n{"action":"inspect_run_evidence","flash_attempts":0,"flash_directive":"read path",'
        '"rationale":"need cell chain","reusable_knowledge":"none",'
        '"evidence_request":{"run_id":"02_yosys_ppa--20260909-194017--88282",'
        '"artifact":"sta.log","selector":"worst_slack","max_lines":180}}\n```'
    )
    assert run_evidence["action"] == RUN_EVIDENCE_ACTION
    alias_evidence = parse_decision(
        '```json\n{"action":"inspect_run_evidence","flash_attempts":0,"flash_directive":"read path",'
        '"rationale":"need cell chain","reusable_knowledge":"none",'
        '"evidence_request":{"run_id":"latest_measured_sta",'
        '"artifact":"sta.log","selector":"max_arrival","max_lines":180}}\n```'
    )
    assert alias_evidence["evidence_request"]["run_id"] == "latest_measured_sta"
    try:
        parse_decision(
            '```json\n{"action":"inspect_run_evidence","flash_attempts":0,"flash_directive":"",'
            '"rationale":"","reusable_knowledge":"none",'
            '"evidence_request":{"run_id":"../secret","artifact":"sta.log",'
            '"selector":"worst_slack","max_lines":180}}\n```'
        )
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe evidence run id was accepted")
    echoed_template = (
        '模型决策\n```json\n'
        '{"action":"repair_harness","flash_attempts":0,"flash_directive":"fix parse",'
        '"rationale":"evidence","reusable_knowledge":"none"}\n```\n'
        '日志回显的 prompt 模板\n```json\n'
        '{"action":"continue|revise_task_card|repair_harness|record_reusable_knowledge|stop_for_debug",'
        '"flash_attempts":1,"flash_directive":"","rationale":"","reusable_knowledge":"append|none"}\n```'
    )
    assert parse_decision(echoed_template)["action"] == "repair_harness"
    try:
        parse_decision(
            '```json\n{"action":"continue","flash_attempts":7,"flash_directive":"",'
            '"rationale":"","reusable_knowledge":"none"}\n```'
        )
    except ValueError:
        pass
    else:
        raise AssertionError("out-of-range Flash batch was accepted")

    weak_knowledge = parse_decision(
        '```json\n{"action":"continue","flash_attempts":1,"flash_directive":"",'
        '"rationale":"","reusable_knowledge":"append"}\n```'
    )
    assert weak_knowledge["action"] == "continue"
    with tempfile.TemporaryDirectory() as temporary:
        packet = decision_packet_fixture()
        accepted = {
            **packet["identity"], "action": "continue", "flash_attempts": 2,
            "flash_directive": "two bounded attempts", "rationale": "current evidence",
            "reusable_knowledge": "none",
        }
        invocations: list[list[str]] = []

        def fake_model(command: list[str], log_path: Path, *,
                       record_command: bool = True) -> int:
            invocations.append(command)
            assert record_command is False
            log_path.write_text(
                "model analysis\n```json\n"
                + json.dumps(accepted, ensure_ascii=False)
                + "\n```\n",
                encoding="utf-8",
            )
            return 0

        runner = ProGuidanceRunner(ROOT)
        before_invocations: list[str] = []
        guidance = runner.run(
            packet, model="pro-selector",
            log_path=Path(temporary) / "pro.log", execute=fake_model,
            before_invoke=lambda: before_invocations.append("invoked"))
        assert guidance.decision == accepted
        assert before_invocations == ["invoked"]
        assert invocations[0][:6] == [
            "omp", "-p", "--cwd", str(ROOT), "--model", "pro-selector"]
        assert "DecisionPacket" in invocations[0][-1]

        rejected = {**accepted, "action": "repair_harness", "flash_attempts": 0}

        def fake_rejected_model(command: list[str], log_path: Path, *,
                                record_command: bool = True) -> int:
            log_path.write_text(
                "```json\n" + json.dumps(rejected) + "\n```\n", encoding="utf-8")
            return 0

        try:
            runner.run(
                packet, model="pro-selector",
                log_path=Path(temporary) / "rejected.log", execute=fake_rejected_model)
        except ProDecisionRejected as error:
            assert error.decision == rejected
        else:
            raise AssertionError("runner accepted an action outside its DecisionPacket")

        def fake_failed_model(command: list[str], log_path: Path, *,
                              record_command: bool = True) -> int:
            log_path.write_text("model unavailable\n", encoding="utf-8")
            return 9

        try:
            runner.run(
                packet, model="pro-selector",
                log_path=Path(temporary) / "failed.log", execute=fake_failed_model)
        except ProModelInvocationError as error:
            assert error.exit_code == 9
            assert "model unavailable" in error.raw_output
        else:
            raise AssertionError("runner hid a failed model invocation")

        def fake_invalid_output(command: list[str], log_path: Path, *,
                                record_command: bool = True) -> int:
            log_path.write_text("no decision here\n", encoding="utf-8")
            return 0

        try:
            runner.run(
                packet, model="pro-selector",
                log_path=Path(temporary) / "invalid.log", execute=fake_invalid_output)
        except ProOutputError:
            pass
        else:
            raise AssertionError("runner accepted output without a decision")
        try:
            runner.run(
                {}, model="pro-selector",
                log_path=Path(temporary) / "input.log", execute=fake_model)
        except ProGuidanceInputError:
            pass
        else:
            raise AssertionError("runner accepted an incomplete DecisionPacket")
    assert pro_supervisor.resolve_evidence_model(None, "flash-selector") == "flash-selector"
    assert pro_supervisor.resolve_evidence_model("evidence-selector", "flash-selector") == "evidence-selector"
    assert (pro_supervisor.default_evidence_workflow_path() / "scripts" / "evidence_workflow.py").is_file()
    assert pro_supervisor.DEFAULT_EVIDENCE_CONTRACT.is_file()
    assert json.loads(pro_supervisor.DEFAULT_EVIDENCE_CONTRACT.read_text(encoding="utf-8"))["stage"] == "sta_health"
    closure_skill = pro_supervisor.default_closure_governance_path()
    assert (closure_skill / "closure_gate.py").is_file()
    with tempfile.TemporaryDirectory() as temporary:
        closure_feedback = Path(temporary) / "feedback.txt"
        timeout_feedback = Path(temporary) / "timeout_feedback.txt"
        closure_feedback.write_text(
            "sta_closure_bytes=0\nsta_closure_module_count=0\nsta_status=failed\n"
            "sta_link_result=未测得\nsta_timing_measured=no\n",
            encoding="utf-8",
        )
        pro_supervisor.configure_closure_governance(
            skill_root=closure_skill, feedback_path=closure_feedback,
        )
        closure_audit = pro_supervisor.controller_closure_audit()
        assert closure_audit["phase"] == "closure_export_empty"
        assert closure_audit["closure_governance_status"] == "ready"
        assert closure_audit["audit_source"] == "rtl-closure-governance"
        timeout_feedback.write_text(
            "sta_closure_bytes=0\nsta_closure_module_count=0\nsta_status=not_attempted\n"
            "sta_link_result=未测得\nsta_timing_measured=no\n"
            "measurement_status=incomplete_timeout\n"
            "sta_error=closure partition top_level_sta exited 124; timeout/truncation\n",
            encoding="utf-8",
        )
        pro_supervisor.configure_closure_governance(
            skill_root=closure_skill, feedback_path=timeout_feedback,
        )
        timeout_audit = pro_supervisor.controller_closure_audit()
        assert timeout_audit["phase"] == "closure_measurement_incomplete"
        assert not timeout_audit["closure_repair_required"]
        assert timeout_audit["measurement_rerun_admissible"] is None
        assert timeout_audit["repair_domain"] == "measurement_budget"
        old_latest = pro_supervisor.latest_result_data
        try:
            pro_supervisor.latest_result_data = lambda: {
                "functional_quick_exit": 0, "functional_medium_exit": 0,
                "mandatory_full_exit": 0, "preflight_exit": 0,
                "ppa_exit": 137, "measurement_timeout_seconds": 7200,
                "measurement_fingerprint": pro_supervisor.content_fingerprint(
                    pro_supervisor.ROOT, pro_supervisor.TASK02_MEASUREMENT_INPUTS),
                "measurement_plan_mode": "targeted",
                "measurement_partitions": ["top_level_sta"],
            }
            resource_audit = pro_supervisor.controller_closure_audit({
                "module_registry_hot_view": {
                    "pending_transaction": {"module_id": "compute", "partition": "top_level_sta"}},
            })
            assert resource_audit["phase"] == "closure_resource_blocked"
            assert resource_audit["strategy_change_required"]
            assert resource_audit["measurement_policy"]["measurement_relation"] == "same_measurement"
            assert resource_audit["measurement_policy"]["proposed_measurement_allowed"] is False
            assert "rtl_optimization_allowed" not in resource_audit
            assert "optimization_candidate" not in resource_audit
            pro_supervisor.latest_result_data = lambda: {
                "functional_quick_exit": 0, "functional_medium_exit": 0,
                "mandatory_full_exit": 0, "preflight_exit": 0,
                "ppa_exit": 137, "measurement_timeout_seconds": 7200,
                "measurement_fingerprint": pro_supervisor.content_fingerprint(
                    pro_supervisor.ROOT, pro_supervisor.TASK02_MEASUREMENT_INPUTS),
                "measurement_plan_mode": "full", "measurement_partitions": [],
            }
            changed_scope = pro_supervisor.controller_closure_audit({
                "module_registry_hot_view": {
                    "pending_transaction": {"module_id": "compute", "partition": "top_level_sta"}},
            })
            assert changed_scope["measurement_policy"]["measurement_relation"] == "changed_scope"
            assert changed_scope["measurement_policy"]["proposed_measurement_allowed"] is True
        finally:
            pro_supervisor.latest_result_data = old_latest
        pro_supervisor.configure_closure_governance(
            skill_root=Path(temporary) / "missing-closure-skill",
            feedback_path=closure_feedback,
        )
        unavailable_audit = pro_supervisor.controller_closure_audit()
        assert unavailable_audit["closure_governance_status"] == "unavailable"
        assert unavailable_audit["closure_gate_required"] is True
        assert unavailable_audit["repair_domain"] == "infrastructure"
    pro_supervisor.configure_closure_governance(skill_root=closure_skill)
    old_latest_result_data = pro_supervisor.latest_result_data
    try:
        pro_supervisor.latest_result_data = lambda: {
            "functional_quick_exit": 0, "functional_medium_exit": 0, "mandatory_full_exit": None,
        }
        assert pro_supervisor.latest_functional_regression_passed()
        pro_supervisor.latest_result_data = lambda: {
            "functional_quick_exit": 0, "functional_medium_exit": 1, "mandatory_full_exit": None,
        }
        assert not pro_supervisor.latest_functional_regression_passed()
    finally:
        pro_supervisor.latest_result_data = old_latest_result_data
    # Optimization execution authority no longer travels through the
    # Evidence Model path; the Skill step emits the executable request.
    assert not hasattr(pro_supervisor, "validated_optimization_contract")
    resume_context = Task02ProjectAdapter._optimization_runtime_facts(
            registry={
                "hard_gate": "pass", "conflicts": [],
                "current_blackboxes": ["compute"],
                "pending_transaction": {
                    "kind": "blackbox_expansion", "module_id": "compute",
                    "transaction_id": "blackbox_expansion:top_level_sta:compute",
                },
            },
            closure_audit={"closure_governance_status": "unavailable"},
            functional_baseline_clean=True,
            pending_harness_verification=False)
    assert "optimization_phase" not in resume_context
    assert resume_context["pending_mainline"]["module"] == "compute"
    assert resume_context["current_blackboxes"] == ["compute"]
    assert "mainline_execution" not in resume_context
    with tempfile.TemporaryDirectory() as temporary:
        old_observation_path = pro_supervisor.OPTIMIZATION_OBSERVATION
        try:
            pro_supervisor.OPTIMIZATION_OBSERVATION = (
                Path(temporary) / "optimization-observation.json")
            pro_supervisor.save_optimization_observation({
                "observation_id": "external:one", "status": "measurement_observed"})
            saved_observation = json.loads(
                pro_supervisor.OPTIMIZATION_OBSERVATION.read_text(encoding="utf-8"))
            assert saved_observation["observation_id"] == "external:one"
            try:
                pro_supervisor.save_optimization_observation({
                    "observation_id": "external:two", "status": "measurement_observed"})
                raise AssertionError("unconsumed observation must not be overwritten")
            except RuntimeError:
                pass
        finally:
            pro_supervisor.OPTIMIZATION_OBSERVATION = old_observation_path
    expansion = parse_decision(
        '```json\n{"action":"expand_blackbox","module":"p16_tensor_loader","flash_attempts":0,\n'
        '"flash_directive":"expand one","rationale":"link and STA are real",'
        '"reusable_knowledge":"none"}\n```'
    )
    assert expansion["action"] == EXPAND_BLACKBOX_ACTION
    assert expansion["module"] == "p16_tensor_loader"
    unknown_expansion = parse_decision(
        '```json\n{"action":"expand_blackbox","module":"does_not_exist","flash_attempts":0,\n'
        '"flash_directive":"","rationale":"","reusable_knowledge":"none"}\n```'
    )
    expansion_packet = decision_packet_fixture()
    expansion_packet["allowed_actions"]["expand_blackbox"] = {
        "purpose": "expand", "executor_ref": "project.expand_blackbox",
        "execution_mode": "expand_blackbox",
        "parameter_schema": {"module": {"enum": ["p16_tensor_loader"]},
                             "flash_attempts": {"enum": [0]}},
        "source": "test",
    }
    assert decision_error(unknown_expansion, expansion_packet)
    try:
        parse_decision(
            '```json\n{"action":"record_reusable_knowledge","flash_attempts":0,"flash_directive":"",'
            '"rationale":"","reusable_knowledge":"none"}\n```'
        )
    except ValueError:
        pass
    else:
        raise AssertionError("knowledge recording without append was accepted")

    first = {"measurement_fingerprint": "a", "measurement_timeout_seconds": 600,
             "measurement_plan_mode": "full", "measurement_partitions": [],
             "measurement_scope": "PARTIAL_HIERARCHICAL", "ppa_exit": 124}
    second = {**first, "measurement_timeout_seconds": 1800}
    assert pro_supervisor.experiment_signature(first) != pro_supervisor.experiment_signature(second)
    prompt = build_prompt(decision_packet_fixture())
    assert len(prompt) <= PRO_DECISION_PROMPT_MAX_CHARS
    assert "任务硬边界：" in prompt
    assert "DecisionPacket" in prompt
    assert "当前 result 摘要" not in prompt
    assert "当前必要治理计数" not in prompt
    assert "最近 progress 摘录（最多" not in prompt
    assert "最近 controller result：" not in prompt
    assert "最后只输出一个 fenced JSON" in prompt
    assert "inspect_existing_sta_evidence" in prompt
    assert "compact_governance_state" not in prompt
    packet = decision_packet_fixture()
    valid_proposal = {
        **packet["identity"], "action": "continue", "flash_attempts": 1,
        "flash_directive": "bounded batch", "rationale": "evidence",
        "reusable_knowledge": "none",
    }
    assert decision_error(valid_proposal, packet) is None
    assert decision_error(
        {**valid_proposal, "executor_ref": "project.expand_blackbox"}, packet)
    assert decision_error(
        {**valid_proposal, "action": "repair_harness"}, packet)
    assert decision_error(
        {**valid_proposal, "packet_fingerprint": "stale"}, packet)
    structured_prompt = build_prompt(decision_packet_fixture())
    assert len(structured_prompt) <= PRO_DECISION_PROMPT_MAX_CHARS
    assert '"evidence"' in structured_prompt
    assert '"allowed_actions"' in structured_prompt
    assert "最近 PPA 结构化事实" not in structured_prompt
    assert "当前 result 摘要" not in structured_prompt
    compact_stage = pro_supervisor.compact_stage_evidence_record({
        "workflow": "stage-evidence-workflow", "stage": "sta_health", "run_id": "run-a",
        "packet": {"input_status": "ready_for_model", "deterministic_state": "ready_for_model",
                   "packet_fingerprint": "packet-a",
                   "missing": [], "conflicts": []},
        "review": {"review_state": "CONSISTENT", "integrity_state": "pass",
                    "summary_state": "pass", "hard_gate": "pass", "attempt": 0,
                    "packet_fingerprint": "packet-a", "model_status": "consistent",
                    "strategic_view": {"evidence_groups": [{
                        "topic": "closure", "facts": [{"name": "retry_allowed",
                        "value": True, "source_role": "skill_closure"}],
                        "summary": "retry allowed"}],
                        "summary": "current run only", "summary_source": "model"}},
        "hard_gate": "pass",
    })
    assert compact_stage is not None
    assert compact_stage["review_state"] == "CONSISTENT" and compact_stage["hard_gate"] == "pass"
    assert compact_stage["integrity_state"] == "pass" and compact_stage["summary_state"] == "pass"
    assert compact_stage["validated_evidence"]["summary_source"] == "model"
    assert compact_stage["validated_evidence"]["evidence_groups"][0]["facts"][0]["name"] == "retry_allowed"
    assert compact_stage["warnings"] == []
    assert compact_stage["scope"] == "current_stage" and compact_stage["history_included"] is False
    old_latest_run = pro_supervisor.latest_measured_sta_run
    try:
        pro_supervisor.latest_measured_sta_run = lambda: "02_yosys_ppa--20260909-194017--88282"
        assert pro_supervisor.resolve_sta_evidence_run_id("latest_measured_sta") == "02_yosys_ppa--20260909-194017--88282"
        assert pro_supervisor.resolve_sta_evidence_run_id("../secret") is None
    finally:
        pro_supervisor.latest_measured_sta_run = old_latest_run
    missing_probe = {"phase": "sta_no_timing_path", "no_timing_path_cause": "diagnostic_missing",
                     "diagnostic_required": True}
    assert pro_supervisor.needs_no_path_probe(missing_probe)
    no_path_state = {
        "latest_closure_governance_audit": missing_probe,
        "module_registry_hot_view": {"hard_gate": "pass", "generation": 7},
    }
    no_path_packet = build_decision_packet(
        state=no_path_state, stage_record=stage_fixture(), evidence_fingerprint="evidence-a",
        stop_requested=False, functional_baseline_clean=True, expandable_blackboxes=[])
    assert set(no_path_packet["allowed_actions"]) == {
        "continue", "inspect_existing_sta_evidence", "inspect_run_evidence", "stop_for_debug"}
    assert no_path_packet["allowed_actions"]["continue"]["executor_ref"] == "flash.run"
    assert no_path_packet["allowed_actions"]["continue"]["execution_mode"] == "diagnose_no_path"
    retry_state = {
        "latest_closure_governance_audit": {
            "closure_governance_status": "ready",
            "measurement_rerun_admissible": True,
        },
        "module_registry_hot_view": {
            "hard_gate": "pass", "generation": 7,
            "pending_transaction": {"module_id": "compute", "partition": "top_level_sta"}},
    }
    retry_packet = build_decision_packet(
        state=retry_state, stage_record=stage_fixture(), evidence_fingerprint="retry",
        stop_requested=False, functional_baseline_clean=True, expandable_blackboxes=[])
    assert retry_packet["allowed_actions"]["increase_budget"]["parameter_schema"][
        "flash_attempts"] == {"enum": [1]}
    assert "retry_measurement" not in retry_packet["allowed_actions"]
    blocked_state = {
        **retry_state,
        "latest_closure_governance_audit": {
            "closure_governance_status": "ready",
            "measurement_rerun_admissible": False,
            "measurement_resource_blocked": True,
        },
    }
    blocked_packet = build_decision_packet(
        state=blocked_state, stage_record=stage_fixture(), evidence_fingerprint="blocked",
        stop_requested=False, functional_baseline_clean=True, expandable_blackboxes=[])
    assert "increase_budget" not in blocked_packet["allowed_actions"]
    unavailable_state = {
        **retry_state,
        "latest_closure_governance_audit": {"closure_governance_status": "unavailable"},
    }
    try:
        build_decision_packet(
            state=unavailable_state, stage_record=stage_fixture(), evidence_fingerprint="unavailable",
            stop_requested=False, functional_baseline_clean=True, expandable_blackboxes=[])
    except DecisionPacketError:
        pass
    else:
        raise AssertionError("unavailable closure governance reached Pro")
    assert not pro_supervisor.needs_no_path_probe(
        {"phase": "sta_no_timing_path", "no_timing_path_cause": "no_registers_in_linked_netlist",
         "diagnostic_required": False})
    pending_from_registry = pro_supervisor.registry_mainline_pending({
        "module_registry_hot_view": {
            "pending_transaction": {"module_id": "p16_tensor_loader",
                                    "partition": "top_level_sta"}},
    })
    assert pending_from_registry["module"] == "p16_tensor_loader"
    sample_paths = sta_evidence_extract.parse_timing_paths(
        "STA_TIMING_BEGIN\nStartpoint: writer/_1\nEndpoint: writer/_2\nPath Group: clk\n"
        "data arrival time 1689.858\ndata required time 10.000\nslack -1679.905\nSTA_TIMING_END\n"
    )
    assert sample_paths == [{"startpoint": "writer/_1", "endpoint": "writer/_2", "path_group": "clk",
                             "arrival_ns": 1689.858, "required_ns": 10.0, "slack_ns": -1679.905}]
    actual_layout = sta_evidence_extract.parse_timing_paths(
        "STA_TIMING_BEGIN\nStartpoint: writer/_3\nEndpoint: writer/_4\nPath Group: clk\n"
        "         1689.858   data arrival time\n            9.954   data required time\n"
        "         -1689.858   data arrival time\n         -1679.905   slack (VIOLATED)\nSTA_TIMING_END\n"
    )
    assert actual_layout[0]["arrival_ns"] == 1689.858
    assert actual_layout[0]["required_ns"] == 9.954
    assert actual_layout[0]["slack_ns"] == -1679.905
    old_remote_timing = sta_evidence_extract._remote_timing_text
    try:
        sta_evidence_extract._remote_timing_text = lambda _run_id: (
            "STA_TIMING_BEGIN\nStartpoint: writer/_3\nEndpoint: writer/_4\nPath Group: clk\n"
            "  3.000 3.000 writer/_3/Q (DFF_X1)\n  4.000 7.000 writer/_5/ZN (INV_X1)\n"
            "  7.000 data arrival time\n  10.000 data required time\n  3.000 slack (MET)\nSTA_TIMING_END\n"
        )
        expanded = sta_evidence_extract.query_sta_timing_paths(
            "02_yosys_ppa--20260909-194017--88282", "worst_slack", 40)
        assert expanded["status"] == "ok"
        assert "writer/_5/ZN" in expanded["path_blocks"][0]["report"]
        assert sta_evidence_extract.query_sta_timing_paths("../secret", "worst_slack", 40)["status"] == "invalid_request"
    finally:
        sta_evidence_extract._remote_timing_text = old_remote_timing
    raw_prompt = build_prompt(decision_packet_fixture())
    assert len(raw_prompt) <= PRO_DECISION_PROMPT_MAX_CHARS
    assert "cell\ncell" not in raw_prompt
    compact_run = Task02ProjectAdapter._compact_run_evidence({
        "status": "ok", "source_run_id": "run-a", "selector": "worst_slack",
        "path_blocks": [{"summary": {"slack_ns": -1.0}, "report": "raw-cell-report"}],
    })
    assert compact_run["path_summaries"][0]["slack_ns"] == -1.0
    assert "raw-cell-report" not in json.dumps(compact_run)
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        result_dir = temp_root / "runs/agent-02_yosys_ppa--run-current"
        feedback_dir = temp_root / "runs/02_yosys_ppa--run-current"
        result_dir.mkdir(parents=True)
        feedback_dir.mkdir(parents=True)
        result_path = result_dir / "result.json"
        result_path.write_text(json.dumps({"sta_closure_audit": {
            "sta_link_result": "ok", "sta_timing_measured": True,
            "sta_status": "partial", "sta_timing_path_count": 2,
            "sta_closure_bytes": 100, "sta_closure_module_count": 3,
        }}), encoding="utf-8")
        feedback_path = feedback_dir / "feedback.txt"
        feedback_path.write_text(
            "sta_link_result=ok\nsta_timing_measured=yes\nsta_status=partial\n"
            "sta_timing_path_count=2\nsta_closure_bytes=100\nsta_closure_module_count=3\n",
            encoding="utf-8")
        contract_path = temp_root / "evidence_contract.json"
        contract_path.write_text(json.dumps({
            "stage": "sta_health", "objective": "test adapter handoff",
            "required_fields": [
                "sta_link_result", "sta_timing_measured", "sta_status",
                "sta_timing_path_count", "sta_closure_bytes", "sta_closure_module_count"],
            "required_skill_roles": [
                "skill_closure_governance", "skill_module_registry",
                "skill_optimization_triage"],
        }), encoding="utf-8")
        adapter = Task02ProjectAdapter(project_root=temp_root)
        stage_state = {
            "session": "s",
            "module_registry_hot_view": {
                "session_id": "s", "cycle_id": 1, "generation": 1,
                "hard_gate": "pass", "expandable_blackboxes": [],
            },
            "recent_stage_trend": [
                {"phase": "sta_health", "measurement_fingerprint": "a"},
                {"phase": "sta_health", "measurement_fingerprint": "a"},
            ],
        }
        optimization_projection = {
            "hard_gate": {"phase": "idle", "active_candidate": False},
            "summary_input": {
                "triage": {"eligible_candidates": []},
                "campaign": {"phase": "idle"},
            },
            "execution_contract": None,
        }
        execution_1 = adapter.materialize_stage_evidence_execution(
            state=stage_state,
            closure_audit={"phase": "sta_health", "closure_governance_status": "ready"},
            optimization_projection=optimization_projection,
            session_root=temp_root / "session", cycle=1,
            workflow_root=pro_supervisor.default_evidence_workflow_path(),
            evidence_model="test-model", stage="sta_health", contract_path=contract_path,
            max_artifact_bytes=262144, prompt_max_chars=180000,
            max_facts_per_skill=16, max_total_facts=60)
        roles = set(execution_1["artifacts"])
        assert {"result", "feedback", "skill_closure_governance",
                "skill_module_registry", "skill_optimization_triage"}.issubset(roles)
        assert "trend_summary" not in roles
        assert "progress_delta" not in roles and "controller_state" not in roles
        registry_envelope = json.loads(
            execution_1["artifacts"]["skill_module_registry"].read_text(encoding="utf-8"))
        assert registry_envelope["skill"] == "rtl-module-evidence-registry"
        assert registry_envelope["scope"] == "current_stage"
        fingerprint_1 = execution_1["request_fingerprint"]
        stage_state["module_registry_hot_view"]["generation"] = 2
        execution_2 = adapter.materialize_stage_evidence_execution(
            state=stage_state,
            closure_audit={"phase": "sta_health", "closure_governance_status": "ready"},
            optimization_projection=optimization_projection,
            session_root=temp_root / "session", cycle=2,
            workflow_root=pro_supervisor.default_evidence_workflow_path(),
            evidence_model="test-model", stage="sta_health", contract_path=contract_path,
            max_artifact_bytes=262144, prompt_max_chars=180000,
            max_facts_per_skill=16, max_total_facts=60)
        assert fingerprint_1 != execution_2["request_fingerprint"]
        manifest_fingerprint = execution_2["manifest"]["fingerprint"]
        feedback_path.write_text(feedback_path.read_text(encoding="utf-8") +
                                 "measurement_status=incomplete_timeout\n", encoding="utf-8")
        execution_3 = adapter.materialize_stage_evidence_execution(
            state=stage_state,
            closure_audit={"phase": "sta_health", "closure_governance_status": "ready"},
            optimization_projection=optimization_projection,
            session_root=temp_root / "session", cycle=3,
            workflow_root=pro_supervisor.default_evidence_workflow_path(),
            evidence_model="test-model", stage="sta_health", contract_path=contract_path,
            max_artifact_bytes=262144, prompt_max_chars=180000,
            max_facts_per_skill=16, max_total_facts=60)
        assert execution_3["manifest"]["fingerprint"] != manifest_fingerprint

    # Evidence Model availability is a summary-quality concern. With readable,
    # internally consistent source evidence, two failed model calls must
    # produce a deterministic fallback instead of blocking the harness.
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        result_dir = temp_root / "runs/agent-02_yosys_ppa--run-fallback"
        result_dir.mkdir(parents=True)
        result_path = result_dir / "result.json"
        result_path.write_text(json.dumps({
            "sta_closure_audit": {
                "sta_link_result": "ok", "sta_timing_measured": True,
                "sta_status": "partial", "sta_timing_path_count": 2,
                "sta_closure_bytes": 100, "sta_closure_module_count": 3,
            }
        }), encoding="utf-8")
        contract_path = temp_root / "evidence_contract.json"
        contract_path.write_text(json.dumps({
            "stage": "sta_health",
            "objective": "test deterministic fallback",
            "required_fields": [
                "sta_link_result", "sta_timing_measured", "sta_status",
                "sta_timing_path_count", "sta_closure_bytes", "sta_closure_module_count",
            ],
        }), encoding="utf-8")
        unavailable = temp_root / "unavailable_model.py"
        unavailable.write_text("raise SystemExit(1)\n", encoding="utf-8")
        fallback_adapter = Task02ProjectAdapter(project_root=temp_root)
        fallback_adapter.evidence_model_argv_template = (
            sys.executable, unavailable.as_posix(), "{prompt_file}")
        fallback_execution = fallback_adapter.materialize_stage_evidence_execution(
            state={
                "session": "s",
                "module_registry_hot_view": {
                    "session_id": "s", "cycle_id": 1, "generation": 2,
                    "hard_gate": "pass", "expandable_blackboxes": []},
            },
            closure_audit={"phase": "sta_health", "closure_governance_status": "ready"},
            optimization_projection={
                "hard_gate": {"phase": "idle", "active_candidate": False},
                "summary_input": {"campaign": {"phase": "idle"}},
                "execution_contract": None,
            },
            session_root=temp_root / "session", cycle=1,
            workflow_root=pro_supervisor.default_evidence_workflow_path(),
            evidence_model="unavailable-model", stage="sta_health",
            contract_path=contract_path,
            max_artifact_bytes=262144, prompt_max_chars=180000,
            max_facts_per_skill=16, max_total_facts=60)
        completed = subprocess.run(
            fallback_execution["command"], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, check=False)
        fallback_record = fallback_adapter.normalize_stage_evidence_execution(
            fallback_execution, completed.returncode)
        fallback_review = fallback_record["review"]
        assert fallback_record["hard_gate"] == "pass"
        assert fallback_review["integrity_state"] == "pass"
        assert fallback_review["summary_state"] == "unavailable"
        assert fallback_review["summary_source"] == "deterministic_fallback"
        assert fallback_review["validated_evidence"]
        assert len(fallback_record["model_attempts"]) == 2
        retry_prompt = (temp_root / "session" /
                        "evidence_model_prompt_1_attempt1.md").read_text(encoding="utf-8")
        assert "previous_validation_errors" in retry_prompt
        assert "allowed_fact_refs" in retry_prompt
        assert "required_skill_roles" in retry_prompt
        assert (temp_root / "session" / "stage_evidence_input_1.json").is_file()
        assert not (temp_root / "session" / "stage_evidence_input_1_attempt1.json").exists()
    bounded_prompt = build_prompt(decision_packet_fixture())
    assert len(bounded_prompt) <= PRO_DECISION_PROMPT_MAX_CHARS
    assert "最后只输出一个 fenced JSON" in bounded_prompt
    assert "inspect_external_evidence" not in bounded_prompt

    with tempfile.TemporaryDirectory() as temp_dir:
        report = Path(temp_dir) / "pro_progress.md"
        pro_supervisor.append_pro_progress(
            report, "session-a", 1, "Pro 战略决策", raw_output="依据\n```json\n{}",
            details={"action": "continue", "flash_attempts": 1},
        )
        text = report.read_text(encoding="utf-8")
        assert "Task 02 Pro supervisor progress" in text
        assert "session-a" in text and "flash_attempts" in text
        assert "``\\`json" in text
        model_log = Path(temp_dir) / "model.log"
        assert pro_supervisor.run_logged(
            [sys.executable, "-c", "print('model output')"], model_log, record_command=False
        ) == 0
        logged = model_log.read_text(encoding="utf-8")
        assert logged.startswith("# OMP 模型原始输出")
        assert "model output" in logged and "-c" not in logged

        # Detached supervisor children still need a valid stdin.  Without
        # this, environment.py can fail before exporting remote variables.
        saved_stdin = os.dup(0)
        os.close(0)
        try:
            stdin_log = Path(temp_dir) / "stdin.log"
            assert pro_supervisor.run_logged(
                [sys.executable, "-c", "import os; os.fstat(0); print('stdin ok')"], stdin_log
            ) == 0
            assert "stdin ok" in stdin_log.read_text(encoding="utf-8")
        finally:
            os.dup2(saved_stdin, 0)
            os.close(saved_stdin)

        old_pending = pro_supervisor.PENDING_VERIFICATION
        try:
            pro_supervisor.PENDING_VERIFICATION = Path(temp_dir) / "pending.json"
            pending = pro_supervisor.save_pending_verification(
                ["scripts/remote_yosys_round.sh"],
                "已验证 OpenROAD 使用 all_inputs -no_clocks；等待一次 smoke。",
                "sta_timing_measured|sta_no_timing_path",
            )
            assert pro_supervisor.load_pending_verification() == pending
            pending_state = {
                "pending_verification": pending,
                "latest_closure_governance_audit": {"closure_governance_status": "ready"},
                "module_registry_hot_view": {
                    "hard_gate": "pass", "generation": 7,
                    "pending_transaction": {"module_id": "compute",
                                            "partition": "top_level_sta"}},
            }
            pending_packet = build_decision_packet(
                state=pending_state, stage_record=stage_fixture(),
                evidence_fingerprint="pending", stop_requested=False,
                functional_baseline_clean=True, expandable_blackboxes=[])
            assert "continue" in pending_packet["allowed_actions"]
            assert "repair_harness" not in pending_packet["allowed_actions"]
            assert pending_packet["allowed_actions"]["continue"]["executor_ref"] == "flash.run"
            assert pending_packet["allowed_actions"]["continue"]["execution_mode"] == \
                "verify_pending_harness"
            identity = pending_packet["identity"]
            pending_decision = {
                **identity, "action": "continue", "flash_attempts": 1,
                "flash_directive": "strategic text", "rationale": "pending",
                "reusable_knowledge": "none",
            }
            assert decision_error(pending_decision, pending_packet) is None
            materialized = materialize_executor_request(
                pending_decision, pending_packet, pending_verification=pending)
            assert materialized["action"] == "continue"
            assert materialized["executor_ref"] == "flash.run"
            assert materialized["operation_id"]
            assert "all_inputs -no_clocks" in materialized["flash_directive"]

            resume_contract = {
                "schema_version": 1, "contract_kind": "mainline_resume",
                "enforcement": "hard", "current_phase": "resume_pending",
                "applies_to_actions": ["continue"], "allowed_actions": ["continue"],
                "forbidden_actions": [],
                "action_parameters": {"continue": {
                    "module": {"enum": ["compute"]}, "flash_attempts": {"enum": [1]}}},
                "execution_profiles": {"continue": {
                    "execution_mode": "execute_mainline_transaction"}},
                "hard_constraints": {}, "recommended_actions": [],
                "strategic_questions": [], "retry_budget": {}, "resume_context": {},
            }
            resume_packet = build_decision_packet(
                state=pending_state, stage_record=stage_fixture(contract=resume_contract),
                evidence_fingerprint="resume", stop_requested=False,
                functional_baseline_clean=True, expandable_blackboxes=[])
            assert resume_packet["allowed_actions"]["continue"]["parameter_schema"]["module"] == {
                "enum": ["compute"]}

            active_contract = {
                "schema_version": 1, "contract_kind": "active_optimization",
                "enforcement": "hard", "current_phase": "measurement_pending",
                "applies_to_actions": ["*"],
                "allowed_actions": ["retry_measurement", "defer_optimization"],
                "forbidden_actions": ["continue", "optimize_rtl"],
                "action_parameters": {
                    "retry_measurement": {"module": {"enum": ["compute"]},
                                          "flash_attempts": {"enum": [1]}},
                    "defer_optimization": {"module": {"enum": ["compute"]}},
                },
                "execution_profiles": {}, "hard_constraints": {},
                "recommended_actions": [], "strategic_questions": [],
                "retry_budget": {}, "resume_context": {},
            }
            active_packet = build_decision_packet(
                state=pending_state, stage_record=stage_fixture(contract=active_contract),
                evidence_fingerprint="active", stop_requested=False,
                functional_baseline_clean=True, expandable_blackboxes=[])
            assert "continue" not in active_packet["allowed_actions"]
            assert set(active_packet["allowed_actions"]) == {"stop_for_debug"}

            selection_contract = {
                **resume_contract,
                "contract_kind": "optimization_selection", "current_phase": "idle",
                "applies_to_actions": ["optimize_rtl"],
                "allowed_actions": ["optimize_rtl"], "forbidden_actions": [],
                "action_parameters": {"optimize_rtl": {"module": {"enum": ["compute"]}}},
                "execution_profiles": {},
            }
            selection_state = {
                **pending_state, "pending_verification": None,
                "limits": {"max_rtl_repair_attempts": 2},
            }
            selection_packet = build_decision_packet(
                state=selection_state, stage_record=stage_fixture(contract=selection_contract),
                evidence_fingerprint="selection", stop_requested=False,
                functional_baseline_clean=True, expandable_blackboxes=[])
            assert "optimize_rtl" not in selection_packet["allowed_actions"]
            pro_supervisor.clear_pending_verification()
            assert not pro_supervisor.load_pending_verification()
        finally:
            pro_supervisor.PENDING_VERIFICATION = old_pending

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        (temp_root / "harness").mkdir()
        ppa = temp_root / "harness/ppa.toml"
        ppa.write_text(
            '[[partitions]]\nname = "top_level_sta"\nblackbox = "p16_tensor_loader l2norm_q"\n',
            encoding="utf-8",
        )
        (temp_root / "harness/task02_project_executor.py").write_text(
            (ROOT / "harness/task02_project_executor.py").read_text(encoding="utf-8"),
            encoding="utf-8")
        adapter = Task02ProjectAdapter(project_root=temp_root)
        assert adapter.current_blackboxes() == ["p16_tensor_loader", "l2norm_q"]
        assert not hasattr(adapter, "evidence_model_command")
        assert not hasattr(adapter, "evidence_artifact_refs")
        prepared = adapter.prepare_cycle(
            state={
                "latest_closure_governance_audit": {
                    "closure_governance_status": "ready",
                    "sta_link_result": "ok", "sta_timing_measured": True,
                    "sta_status": "partial",
                },
                "module_registry_hot_view": {
                    "hard_gate": "pass", "generation": 7,
                    "expandable_blackboxes": ["p16_tensor_loader"],
                    "pending_transaction": None,
                },
            },
            stage_record=stage_fixture(), evidence_fingerprint="adapter",
            stop_requested=False, functional_baseline_clean=True,
            optimization_handoff=None,
        )
        assert prepared["allowed_actions"]["expand_blackbox"]["parameter_schema"][
            "module"] == {"enum": ["p16_tensor_loader"]}
        waiting_contract = {
            "schema_version": 1, "enforcement": "hard",
            "applies_to_actions": ["*"],
            "allowed_actions": ["inspect_existing_sta_evidence"],
            "forbidden_actions": ["expand_blackbox"],
            "action_parameters": {}, "execution_profiles": {},
            "recommended_actions": [],
        }
        obligation_packet = adapter.prepare_cycle(
            state={
                "latest_closure_governance_audit": {
                    "closure_governance_status": "ready",
                    "sta_link_result": "ok", "sta_timing_measured": True,
                    "sta_status": "partial",
                },
                "module_registry_hot_view": {
                    "hard_gate": "pass", "generation": 7,
                    "expandable_blackboxes": [], "pending_transaction": None,
                },
            },
            stage_record=stage_fixture(contract=waiting_contract),
            evidence_fingerprint="obligation", stop_requested=False,
            functional_baseline_clean=True,
            optimization_handoff={"resolution": {
                "status": "standalone_ready", "obligation_id": "sta:one"}})
        assert obligation_packet["allowed_actions"]["continue"]["executor_ref"] == \
            "project.optimization_external_sta"
        request = {
            "operation_id": "a" * 64,
            "executor_ref": "project.expand_blackbox",
            "module": "p16_tensor_loader",
        }
        execution = adapter.materialize_project_execution(
            request, session_root=temp_root / "session", cycle=1)
        result = adapter.normalize_project_execution(
            execution, subprocess.run(execution["command"], check=False).returncode)
        assert result["status"] == "success"
        details = result["details"]
        assert details["old_blackbox_count"] == 2 and details["new_blackbox_count"] == 1
        assert adapter.current_blackboxes() == ["l2norm_q"]
        repeated_execution = adapter.materialize_project_execution(
            request, session_root=temp_root / "session", cycle=2)
        repeated = adapter.normalize_project_execution(
            repeated_execution,
            subprocess.run(repeated_execution["command"], check=False).returncode)
        assert repeated == result
        stale_request = {
            "operation_id": "b" * 64,
            "executor_ref": "project.expand_blackbox",
            "module": "p16_tensor_loader",
        }
        stale_execution = adapter.materialize_project_execution(
            stale_request, session_root=temp_root / "session", cycle=3)
        stale = adapter.normalize_project_execution(
            stale_execution,
            subprocess.run(stale_execution["command"], check=False).returncode)
        assert stale["status"] == "stale"

    status_script = ROOT / "scripts/pro_supervisor_status.sh"
    assert subprocess.run(["bash", "-n", str(status_script)], check=False).returncode == 0
    status_source = status_script.read_text(encoding="utf-8")
    assert "--history" in status_source
    assert "--- cumulative Pro progress ---" not in status_source
    ppa_source = (ROOT / "harness/ppa_agent_loop.py").read_text(encoding="utf-8")
    assert "stdin=subprocess.DEVNULL" in ppa_source
    assert "def measurement_timeout_is_incomplete(" not in ppa_source
    assert "normalize_measurement_outcome(" in ppa_source
    assert "run_with_infra_retry(final_command, final_log)" in ppa_source
    assert "final_regression.json" in ppa_source
    assert "forced-blackbox-expansion-module" in ppa_source
    supervisor_source = (ROOT / "harness/pro_supervisor.py").read_text(encoding="utf-8")
    assert "def measurement_timeout_is_incomplete(" not in supervisor_source
    assert "def measurement_timeout_batch_is_incomplete(" not in supervisor_source
    assert 'result.get("ppa_exit") in (124, 137)' not in supervisor_source
    decision_packet_source = (ROOT / "harness/decision_packet_adapter.py").read_text(
        encoding="utf-8")
    for removed_closure_counter in (
            "closure_measurement_retries", "max_closure_measurement_retries",
            "max-closure-measurement-retries", "measurement_retry_count",
            "measurement_retry_limit", "retry_limit_reached",
            "measurement_retry_allowed"):
        assert removed_closure_counter not in supervisor_source
        assert removed_closure_counter not in decision_packet_source
    registry_source = (ROOT / "harness/module_registry_adapter.py").read_text(encoding="utf-8")
    assert "TIMEOUT_EXITS" not in registry_source
    assert "def _functional_transport_uncertain(" not in registry_source
    assert 'result.get("ppa_exit") in (0, 124)' not in registry_source
    adapter_source = (ROOT / "harness/task02_project_adapter.py").read_text(encoding="utf-8")
    assert adapter_source.count('"measurement_incomplete": True') == 1
    assert 'audit.get("measurement_incomplete") is True' in adapter_source
    closure_source = (ROOT.parent / "RTLskill/rtl-closure-governance/scripts/sta_closure_audit.py").read_text(
        encoding="utf-8")
    assert 'policy.get("max_budget_exhausted")' not in closure_source
    for removed_closure_counter in (
            "measurement_retry_count", "measurement_retry_limit",
            "retry_limit_reached", "measurement_retry_allowed"):
        assert removed_closure_counter not in closure_source
    optimization_source = (ROOT.parent / "RTLskill/rtl-optimization-triage/scripts/optimization_governance.py").read_text(
        encoding="utf-8")
    assert 'external.get("ppa_exit") in {124, 137}' not in optimization_source
    guidance_source = (ROOT / "harness/pro_guidance_runner.py").read_text(encoding="utf-8")
    assert 'EXPAND_BLACKBOX_ACTION = "expand_blackbox"' not in supervisor_source
    assert 'EXPAND_BLACKBOX_ACTION = "expand_blackbox"' in guidance_source
    assert "ProGuidanceRunner(ROOT)" in supervisor_source
    assert "guidance_runner.run(" in supervisor_source
    for removed_guidance_implementation in (
            "def pro_task_constraints(", "def extract_decision(", "def pro_prompt(",
            "def parse_decision(", "def build_prompt(", "decision_packet_error"):
        assert removed_guidance_implementation not in supervisor_source
    assert "def parse_decision(" in guidance_source
    assert "def build_prompt(" in guidance_source
    assert "packet_error = decision_error(" in guidance_source
    assert 'EXTERNAL_EVIDENCE_ACTION = "inspect_external_evidence"' not in supervisor_source
    assert "pending_external_evidence" not in supervisor_source
    assert "inspect_external_evidence" not in supervisor_source
    assert "latest_functional_regression_passed" in supervisor_source
    assert "MAX_INVALID_EXPANSION_RETRIES" not in supervisor_source
    assert "Pro will choose another candidate" not in supervisor_source
    assert "closure_retry_gate" not in supervisor_source
    assert "closure_resource_gate" not in supervisor_source
    assert "run_automatic_optimization_edit" not in supervisor_source
    assert "rollback_optimization_candidate" not in supervisor_source
    assert "optimization_measurement_verdict" not in supervisor_source
    assert "optimization_step(optimization_root" not in supervisor_source
    assert "optimization_run_campaign" in supervisor_source
    assert "optimization_execute_request" in supervisor_source
    assert '"project.verify_optimization"' in supervisor_source
    assert "direct_optimization" not in supervisor_source
    assert '"action": "execute_skill_request"' not in supervisor_source
    assert "match_optimization_obligation" not in supervisor_source
    assert "normalize_optimization_observation" not in supervisor_source
    assert "OptimizationCampaignCoordinator" not in supervisor_source
    for removed_optimization_owner in (
            "load_optimization_state", "persist_campaign_state",
            'state["optimization_governance"]',
            'state["optimization_triage"]',
            'state["optimization_obligation_match"]',
            'state["pending_optimization_observation"]'):
        assert removed_optimization_owner not in supervisor_source
    bridge_source = (ROOT / "harness/rtl_skill_bridge.py").read_text(
        encoding="utf-8")
    assert "compact_optimization_context" not in bridge_source
    assert "persist_state" not in bridge_source
    adapter_source = (ROOT / "harness/task02_project_adapter.py").read_text(
        encoding="utf-8")
    for forbidden in (
            "advance_optimization_campaign", "refresh_optimization_campaign",
            "consume_optimization_plan", "reconcile_optimization_state",
            "optimization_step(", "optimization_transition(",
            "optimization_run_campaign(", "materialize_optimization_plan"):
        assert forbidden not in adapter_source
    assert "materialize_external_optimization_plan" in adapter_source
    assert "def execute(" not in adapter_source
    assert "def reconcile(" not in adapter_source
    assert "def expand_blackbox(" not in adapter_source
    assert "_extract_json_object" not in supervisor_source
    assert '"render-retry"' not in supervisor_source
    for removed_evidence_implementation in (
            "def compact_stage_evidence(", "def current_progress_delta(",
            "def controller_trend_summary(", "def compact_run_evidence(",
            "def stage_evidence_input_fingerprint(", "def write_stage_context_files(",
            "def run_stage_evidence_workflow(", "def external_evidence_packet(",
            "def observe_external_evidence(", "stage_evidence_manifest_fingerprint",
            '"--model-command-json"'):
        assert removed_evidence_implementation not in supervisor_source
    assert "materialize_stage_evidence_execution(" in supervisor_source
    assert "normalize_stage_evidence_execution(" in supervisor_source
    assert '"--model-command-json"' in adapter_source
    assert "def evidence_artifact_refs(" not in adapter_source
    assert "def evidence_model_command(" not in adapter_source
    assert 'state["guidance_cycles"] = int(state.get("guidance_cycles", 0)) + 1' in supervisor_source
    assert load_project_adapter(ROOT / "harness.toml", ROOT).api_version == 10
    functional_source = (ROOT / "scripts/remote_task02_functional.sh").read_text(encoding="utf-8")
    assert "environment export failed before remote functional run" in functional_source
    assert "INFRA_EXIT=75" in functional_source
    print("Pro supervisor policy tests: PASS")


if __name__ == "__main__":
    main()
