#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/ppa-scheduler-test.XXXXXX")
trap 'rm -rf "$TMP_ROOT"' EXIT

python3 "$ROOT/harness/ppa_agent_loop.py" --help >"$TMP_ROOT/ppa-help.log"
if grep -F -- '--optimization-' "$TMP_ROOT/ppa-help.log" >/dev/null; then
  printf '%s\n' 'Flash controller still exposes an Optimization entry' >&2
  exit 1
fi
python3 "$ROOT/harness/task02_optimization_executor.py" --help \
  >"$TMP_ROOT/optimization-executor-help.log"
grep -F -- 'functional_preflight' "$TMP_ROOT/optimization-executor-help.log" >/dev/null
grep -F -- 'measure_candidate' "$TMP_ROOT/optimization-executor-help.log" >/dev/null

printf '%s\n' \
  'HARNESS_PARTITION_BEGIN=control' \
  '2 memories' \
  '245760 memory bits' \
  '2 memories' \
  '245760 memory bits' \
  '"MEMORY_STAT_COMPLETE=control"' \
  'HARNESS_PARTITION_BEGIN=timed_out' \
  '999999 memory bits' \
  'HARNESS_PARTITION_BEGIN=no_memory' \
  '        - memory bits' \
  '"MEMORY_STAT_COMPLETE=no_memory"' \
  | awk -f "$ROOT/harness/parse_partition_memory.awk" > "$TMP_ROOT/actual-memory.tsv"
printf 'control\t245760\ntimed_out\tunobserved\nno_memory\t0\n' > "$TMP_ROOT/expected-memory.tsv"
diff -u "$TMP_ROOT/expected-memory.tsv" "$TMP_ROOT/actual-memory.tsv"

PARTITIONS_TSV=$(awk -F= '
  /^\[\[partitions\]\]/ { if (started && name != "") emit(); reset_parts(); started = 1; next }
  started && /^name[ \t]*=/       { name = val(); next }
  started && /^top[ \t]*=/        { top = val(); next }
  started && /^chparam[ \t]*=/    { chparam = val(); next }
  started && /^blackbox[ \t]*=/   { blackbox = val(); next }
  started && /^timeout_s[ \t]*=/  { timeout_s = val(); next }
  END { if (started && name != "") emit() }
  function val() { v = substr($0, index($0, "=") + 1); gsub(/^[ \t"]+/, "", v); gsub(/[ \t"]+$/, "", v); return v }
  function reset_parts() { name = ""; top = ""; chparam = ""; blackbox = ""; timeout_s = "" }
  function emit() { printf "%s@%s@%s@%s@%s\n", name, top, chparam, blackbox, timeout_s }
' "$ROOT/harness/ppa.toml")
EXPECTED=$(printf '%s\n' "$PARTITIONS_TSV" | awk 'NF { n++ } END { print n + 0 }')
OBSERVED=0
while IFS='@' read -r name _top _chparam _blackbox _timeout_s; do
  [[ -n "$name" ]] || continue
  OBSERVED=$((OBSERVED + 1))
done <<< "$PARTITIONS_TSV"
[[ "$EXPECTED" -gt 1 && "$EXPECTED" -eq "$OBSERVED" ]]

mkdir -p "$TMP_ROOT/runs/agent-02_yosys_ppa--old" "$TMP_ROOT/runs/agent-02_yosys_ppa--new"
printf '{"measurement_candidates": 1, "accepted_measurements": 0}\n' > "$TMP_ROOT/runs/agent-02_yosys_ppa--old/result.json"
printf '{"measurement_candidates": 2, "accepted_measurements": 0}\n' > "$TMP_ROOT/runs/agent-02_yosys_ppa--new/result.json"
touch -t 202601010101 "$TMP_ROOT/runs/agent-02_yosys_ppa--old/result.json"
touch -t 202601010102 "$TMP_ROOT/runs/agent-02_yosys_ppa--new/result.json"
python3 - "$ROOT" "$TMP_ROOT" <<'PY'
import sys
import hashlib
import json
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "harness"))
import ppa_agent_loop
from flash_campaign_contract import (REQUIRED_ESCALATIONS, actual_changes_error,
                                     parse_work_package, validate_campaign_contract)
from rtl_skill_bridge import (CLOSURE_GOVERNANCE_ENV, closure_governance_audit,
                              default_skill_path)

ppa_agent_loop.ROOT = Path(sys.argv[2])
ppa_agent_loop.STRATEGY_ARCHIVE = ppa_agent_loop.ROOT / "runs/strategy_archive"
assert ppa_agent_loop.persisted_measurement_counters() == (2, 0)

closure_skill = default_skill_path("rtl-closure-governance", CLOSURE_GOVERNANCE_ENV)
closure_feedback = Path(sys.argv[2]) / "closure-feedback.txt"
def classify_feedback(text):
    closure_feedback.write_text(text, encoding="utf-8")
    return closure_governance_audit(closure_skill, feedback_path=closure_feedback)

quota_log = Path(sys.argv[2]) / "quota.log"
quota_log.write_text('500 {"error":{"message":"token plan quota exhausted","type":"runtime_error"}}\n')
assert ppa_agent_loop.agent_failure_reason(quota_log, 1) == "token_plan_quota_exhausted"

runtime_log = Path(sys.argv[2]) / "runtime.log"
runtime_log.write_text('{"type": "runtime_error"}\n')
assert ppa_agent_loop.agent_failure_reason(runtime_log, 1) == "model_runtime_error"

exit_log = Path(sys.argv[2]) / "exit.log"
exit_log.write_text("OMP stopped without a structured reply\n")
assert ppa_agent_loop.agent_failure_reason(exit_log, 9) == "omp_exit_9"
assert ppa_agent_loop.agent_failure_reason(exit_log, 0) is None

same = {"measurement_timeout_seconds": 600, "ppa_exit": 124,
        "measurement_fingerprint": "same"}
assert ppa_agent_loop.measurement_budget_from_plan(
    "measurement_budget_seconds: 180\n", (180, 600, 1800), 180, same, "same") == \
    (600, "controller_prevented_unexplained_downgrade")
assert ppa_agent_loop.measurement_budget_from_plan(
    "measurement_budget_seconds: 180\n", (180, 600, 1800), 180, same, "changed") == \
    (180, "planner_evidence_request")
assert ppa_agent_loop.measurement_budget_from_plan(
    "`measurement_budget_seconds: 3600`\n", (180, 600, 1800, 3600, 7200), 180) == \
    (3600, "planner_evidence_request")
assert ppa_agent_loop.measurement_budget_from_plan(
    "measurement_budget_seconds: 600\n", (180, 600, 1800, 3600, 7200), 180, minimum=1800) == \
    (1800, "controller_raised_to_full_measurement_floor")
assert ppa_agent_loop.measurement_budget_from_plan(
    "no budget line here\n", (180, 600, 1800, 3600, 7200), 180, minimum=1800) == \
    (1800, "controller_full_measurement_floor")
assert ppa_agent_loop.measurement_budget_from_plan(
    "`measurement_budget_seconds: 3600`\n", (180, 600, 1800, 3600, 7200), 180, minimum=1800) == \
    (3600, "planner_evidence_request")
same_hi = {"measurement_timeout_seconds": 3600, "ppa_exit": 124,
           "measurement_fingerprint": "same"}
assert ppa_agent_loop.measurement_budget_from_plan(
    "measurement_budget_seconds: 600\n", (180, 600, 1800, 3600, 7200), 180, same_hi, "same", minimum=1800) == \
    (3600, "controller_prevented_unexplained_downgrade")

paths, full = ppa_agent_loop.regression_requirements([Path("rtl/p16_tensor_loader.sv")])
assert paths == ["rtl/p16_tensor_loader.sv"] and full
paths, full = ppa_agent_loop.regression_requirements([Path("rtl/descriptor_lookup.sv")])
assert paths == ["rtl/descriptor_lookup.sv"] and full
paths, full = ppa_agent_loop.regression_requirements([Path("rtl/small_constant_pkg.sv")])
assert paths == ["rtl/small_constant_pkg.sv"] and not full
paths, full = ppa_agent_loop.regression_requirements([Path("harness/ppa_measure.ys")])
assert paths == [] and not full

assert ppa_agent_loop.decide_measurement_plan(
    "sta_targeted", ["p16_tensor_loader"], ["rtl/foo.sv"], []) == \
    ("targeted", ["p16_tensor_loader"], "pro_campaign_contract")
assert ppa_agent_loop.decide_measurement_plan(
    "sta_targeted", ["p16_tensor_loader"], ["rtl/foo.sv", "rtl/bar.sv"], []) == \
    ("targeted", ["p16_tensor_loader"], "pro_campaign_contract")
assert ppa_agent_loop.decide_measurement_plan(
    "sta_targeted", ["cp_fsm"], [], ["harness/ppa_measure.ys"]) == \
    ("smoke", ["cp_fsm"], "pro_campaign_contract")
assert ppa_agent_loop.decide_measurement_plan(
    "sta_targeted", ["mla_attention_head", "cp_fsm"], [],
    ["harness/ppa_measure.ys"]) == \
    ("targeted", ["mla_attention_head", "cp_fsm"], "pro_campaign_contract")
assert ppa_agent_loop.decide_measurement_plan(
    "sta_targeted", ["p16_tensor_loader"], [], []) == \
    ("targeted", ["p16_tensor_loader"], "pro_campaign_contract")
assert ppa_agent_loop.decide_measurement_plan(
    "functional", [], [], []) == \
    ("functional_counting", [], "pro_campaign_contract")
assert ppa_agent_loop.decide_measurement_plan(
    "sta_full", [], [], []) == \
    ("full", [], "pro_campaign_contract")
assert ppa_agent_loop.decide_measurement_plan(
    "sta_full", [], [], [], True) == \
    ("smoke", ["top_level_sta"], "controller_sta_closure_gate")

contract = {
    "schema_version": 1, "operation_id": "1" * 64,
    "objective": "only update one local RTL file", "repair_domain": "rtl_optimization",
    "mutation_policy": "local", "allowed_paths": ["rtl"],
    "verification_level": "sta_targeted", "measurement_partitions": [],
    "measurement_budget_ceiling_seconds": 1800, "max_steps": 2,
    "escalation_conditions": list(REQUIRED_ESCALATIONS),
}
contract["contract_id"] = hashlib.sha256(json.dumps(
    contract, ensure_ascii=False, sort_keys=True,
    separators=(",", ":")).encode()).hexdigest()
contract = validate_campaign_contract(contract)
package = parse_work_package(
    '```json\n{"kind":"flash_work_package","objective":"edit foo",'
    '"disposition":"execute","escalation_reason":"none",'
    '"access":"edit","verification_request":"contract",'
    '"hypothesis":"remove redundant mux","planned_paths":["rtl/foo.sv"],'
    '"expected_evidence":"functional and STA evidence"}\n```', contract)
assert actual_changes_error(["rtl/foo.sv"], package, contract) is None
assert actual_changes_error(["rtl/bar.sv"], package, contract) is not None
audit_package = parse_work_package(
    '```json\n{"kind":"flash_work_package","disposition":"execute",'
    '"escalation_reason":"none","access":"read_only",'
    '"verification_request":"none","objective":"audit prior edit",'
    '"hypothesis":"the prior diff preserves widths","planned_paths":[],'
    '"expected_evidence":"a read-only risk report"}\n```', contract)
assert audit_package["access"] == "read_only"
escalation = parse_work_package(
    '```json\n{"kind":"flash_work_package","disposition":"escalate",'
    '"escalation_reason":"architecture_change_required","objective":"handoff",'
    '"access":"read_only","verification_request":"none",'
    '"hypothesis":"local scope is insufficient","planned_paths":[],'
    '"expected_evidence":"Pro must authorize architecture scope"}\n```', contract)
assert escalation["disposition"] == "escalate"
assert not hasattr(ppa_agent_loop, "planning_policy_from_reply")
assert not hasattr(ppa_agent_loop, "targeted_measurements_since_full")
ppa_source = (Path(sys.argv[1]) / "harness/ppa_agent_loop.py").read_text(encoding="utf-8")
for removed_periodic_full_policy in (
        "TARGETED_MEASUREMENTS_BEFORE_FULL",
        "controller_periodic_full_after_targeted"):
    assert removed_periodic_full_policy not in ppa_source
assert "measurement_command(measurement_budget)" not in ppa_source
assert 'verification_level == "sta_targeted" and not baseline_partitions' in ppa_source

audit = classify_feedback(
    "sta_status=read_error\nsta_error=STA-0171 unknown blackbox\n"
    "sta_closure_bytes=123\nsta_closure_module_count=2\n"
    "sta_closure_top_found=1\nsta_read_skip_count=1\n"
    "sta_link_result=未测得\nsta_timing_measured=no\n")
assert audit["phase"] == "undeclared_blackbox" and audit["closure_repair_required"]
audit = classify_feedback(
    "sta_status=read_error\nsta_error=[ERROR STA-0171] /work/x/build/sta_norm/ppa_sta_all.v line 6716012, syntax error\n"
    "sta_closure_bytes=133661072\nsta_closure_module_count=8\n"
    "sta_closure_top_found=1\nsta_read_skip_count=1\n"
    "sta_link_result=未测得\nsta_timing_measured=no\n")
assert audit["phase"] == "sta_parse_error" and audit["closure_repair_required"]
audit = classify_feedback(
    "sta_status=partial\nsta_closure_bytes=123\nsta_closure_module_count=2\n"
    "sta_closure_top_found=1\nsta_read_skip_count=0\nsta_link_result=ok\n"
    "sta_timing_measured=yes (real report_checks)\n")
assert audit["phase"] == "sta_timing_measured" and not audit["closure_repair_required"]
audit = classify_feedback(
    "sta_status=ok_no_path\nsta_closure_bytes=123\nsta_closure_module_count=2\n"
    "sta_closure_top_found=1\nsta_read_skip_count=0\nsta_link_result=ok\n"
    "sta_timing_measured=no\nsta_diag_status=ok\nsta_diag_clock_port_count=1\n"
    "sta_diag_clock_count=1\nsta_diag_register_count=0\nsta_diag_data_input_count=5\n")
assert audit["phase"] == "sta_no_timing_path"
assert audit["no_timing_path_cause"] == "no_registers_in_linked_netlist"
assert audit["repair_domain"] == "harness_or_constraints"

timeout_audit = classify_feedback(
    "sta_status=not_attempted\nsta_closure_bytes=0\nsta_closure_module_count=0\n"
    "sta_link_result=未测得\nsta_timing_measured=no\n"
    "measurement_status=incomplete_timeout\n"
    "sta_error=top_level_sta exited 124; timeout/truncation\n")
assert timeout_audit["phase"] == "closure_measurement_incomplete"
assert not timeout_audit["closure_repair_required"]
assert timeout_audit["measurement_rerun_admissible"] is None
assert timeout_audit["repair_domain"] == "measurement_budget"
assert ppa_agent_loop.decide_measurement_plan(
    "sta_full", [], [], [], True) == \
    ("smoke", ["top_level_sta"], "controller_sta_closure_gate")
assert ppa_agent_loop.measurement_budget_from_plan(
    "measurement_budget_seconds: 3600\n", (180, 600, 1800, 3600, 7200), 180,
    minimum=1800, floor_kind="closure_monotonic_rerun") == (3600, "planner_evidence_request")

assert ppa_agent_loop.parse_functional_counting_feedback(
    '{"requested_seq_len": 16, "pass": true}\n=== simulation.log ===\nPASS cycles=274906\n') == \
    {"requested_seq_len": 16, "pass_cycles": 274906}
assert ppa_agent_loop.parse_functional_counting_feedback("no usable numbers here\n") == \
    {"requested_seq_len": None, "pass_cycles": None}
assert ppa_agent_loop.functional_counting_marginal(161556, 274906, 8, 16) == 14168.75
assert ppa_agent_loop.functional_counting_marginal(None, 274906, 8, 16) is None
assert ppa_agent_loop.functional_counting_marginal(161556, 274906, 8, 8) is None

ppa_agent_loop.STRATEGY_ARCHIVE.mkdir(parents=True)
(ppa_agent_loop.STRATEGY_ARCHIVE / "latest_ppa_feedback.txt").write_text("measurement_scope=PARTIAL_HIERARCHICAL\n")
assert ppa_agent_loop.latest_measurement_scope() == "PARTIAL_HIERARCHICAL"
assert "PARTIAL_HIERARCHICAL" in ppa_agent_loop.latest_ppa_feedback()
PY

# The generated blackbox glob must expand before rsync.  A quoted `*.v` makes
# rsync look for a literal filename and turns a valid PPA run into exit 75.
grep -F '"$LOCAL_RUN"/ppa_bb_*.v' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
! grep -F '"$LOCAL_RUN/ppa_bb_*.v"' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F -- '--partitions) REQUESTED_PARTITIONS=$2' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'measurement_partition_selection=${REQUESTED_PARTITIONS:-all}' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
# The container may expose OpenSTA through OpenROAD rather than a standalone
# `sta` executable.  Its mapped-netlist flow requires LEFs before Verilog.
grep -F '/opt/OpenROAD/build/bin/openroad' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'NangateOpenCellLibrary.tech.lef' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'NangateOpenCellLibrary.macro.lef' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
# STA timing setup: report_checks option names drift across OpenSTA versions.
# This build removed the legacy `-nworst` (left as an unknown keyword -> STA-0514)
# and deprecated `-endpoint_count`/`-group_count` in favour of
# `-endpoint_path_count`/`-group_path_count`.  Assert the current names and that
# the removed `-nworst` family stays absent, so a single-command patch cannot
# regress it.
grep -F 'report_checks -path_delay max -endpoint_path_count 1 -group_path_count 8 -digits 3' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
! grep -F -- '-nworst' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
# STA timing setup: the actual OpenROAD/OpenSTA build exposes the native
# `all_inputs -no_clocks` selector, but none of the Synopsys/DC collection
# commands.  Signature drift (filter_collection, then remove_from_collection,
# both "invalid command name") proved the post-link Tcl must not depend on ANY
# collection-command family.  Keep both templates on the verified native
# interface and assert the whole family is absent so a single-command patch
# cannot regress.
grep -F 'set_input_delay -clock clk 2.0 [all_inputs -no_clocks]' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
! grep -F 'remove_from_collection' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
! grep -F 'filter_collection' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
! grep -F 'add_to_collection' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
! grep -F 'sort_collection' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
! grep -F 'foreach_in_collection' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
! grep -F 'sizeof_collection' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
! grep -F 'copy_collection' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
# A real OpenSTA report puts the numeric fields FIRST, not in `$NF`.  Keep a
# fixture with both MET and VIOLATED paths: the parser must retain the maximum
# arrival and minimum slack, and ignore similarly shaped text outside markers.
STA_PARSE_OUTPUT=$(awk -f "$ROOT/harness/parse_sta_timing.awk" <<'STA_PARSE_EOF'
outside 9999.000 data arrival time
STA_TIMING_BEGIN
Startpoint: reset (input port clocked by clk)
         1686.395   data arrival time
         -1684.275   slack (VIOLATED)
Startpoint: writer/a (rising edge-triggered flip-flop clocked by clk)
         1689.858   data arrival time
         -1679.905   slack (VIOLATED)
Startpoint: writer/b (rising edge-triggered flip-flop clocked by clk)
            2.000   data arrival time
            8.054   slack (MET)
STA_TIMING_END
STA_PARSE_EOF
)
[[ "$STA_PARSE_OUTPUT" == *"arrival_ns=1689.858"* ]]
[[ "$STA_PARSE_OUTPUT" == *"slack_ns=-1684.275"* ]]
[[ "$STA_PARSE_OUTPUT" == *"path_count=3"* ]]
# Round-78 (harness repair): the remote STA capture prepends a complete
# STA_TIMING_BEGIN..END sed extraction before the bounded `tail -n 500`, so a
# report_checks block taller than the tail window can no longer drop the begin
# marker and misreport a linked design as ok_no_path.  The timing parser must
# therefore keep only the FIRST complete block and ignore a duplicated second
# block (a small report appears in both the sed extraction and the tail).
grep -F 'STA_TIMING_BEGIN/,/STA_TIMING_END/p' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
STA_DUP_OUTPUT=$(awk -f "$ROOT/harness/parse_sta_timing.awk" <<'STA_DUP_EOF'
STA_TIMING_BEGIN
Startpoint: writer/a (rising edge-triggered flip-flop clocked by clk)
         1689.858   data arrival time
         -1679.905   slack (VIOLATED)
STA_TIMING_END
STA_TIMING_BEGIN
Startpoint: writer/dup (rising edge-triggered flip-flop clocked by clk)
            9.000   data arrival time
            1.000   slack (MET)
STA_TIMING_END
STA_DUP_EOF
)
[[ "$STA_DUP_OUTPUT" == *"path_count=1"* ]]
[[ "$STA_DUP_OUTPUT" == *"arrival_ns=1689.858"* ]]
[[ "$STA_DUP_OUTPUT" == *"slack_ns=-1679.905"* ]]
grep -F 'parse_sta_timing.awk' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'sta_timing_path_count=' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
# Round-53/54: a netlist OpenSTA cannot parse must be skipped and reported as a
# read error, not mislabeled ok_no_path; the read is a single normalized file.
grep -F 'STA_READ_SKIP=$all_net' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'STA_STATUS="read_error"' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
# Round-71: the STA reader normalizes the SINGLE self-contained closure
# netlist (build/ppa_closure_top.v, written from a transitive set of complete
# hierarchy modules; `%n` is inversion and `-blackboxes` would write only
# blackbox modules in Yosys 0.64) into build/sta_norm/ppa_sta_all.v via
# sta_paramod_norm.awk.  Freshness is guaranteed by the measure-loop prologue
# `rm -f` of build/ppa_closure_top.v, so a stale earlier-round netlist cannot
# re-enter the reader (no Tcl `glob -nocomplain` over the persistent build/ dir).
grep -F 'select -set closure_modules @PART_TOP@' "$ROOT/harness/ppa_measure.ys" >/dev/null
grep -F 'select -set closure_modules @closure_modules %c %s %u' "$ROOT/harness/ppa_measure.ys" >/dev/null
grep -F 'select @closure_modules' "$ROOT/harness/ppa_measure.ys" >/dev/null
grep -F 'write_verilog -selected build/ppa_closure_top.v' "$ROOT/harness/ppa_measure.ys" >/dev/null
! grep -F 'write_verilog -selected -blackboxes build/ppa_closure_top.v' "$ROOT/harness/ppa_measure.ys" >/dev/null
grep -F 'awk -f runs/$RUN_ID/sta_paramod_norm.awk build/ppa_closure_top.v' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'read_verilog -sv -lib build/ppa_bb_top_level_sta.v' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'write_verilog -blackboxes build/sta_norm/ppa_sta_blackboxes.v' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'sta_strip_bb_params.awk' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'ppa_sta_closure_noparam.v' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'if (ty in defined) continue' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'cat build/sta_norm/ppa_sta_blackboxes.v build/sta_norm/ppa_sta_closure_noparam.v > build/sta_norm/ppa_sta_all.v' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
! grep -F 'glob -nocomplain' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
# Round-69: the STA link target must never fall back to the last declared
# (compute blackbox stub) module; a missing closure top is an explicit
# link_error with STA_CLOSURE_TOP_FOUND=0, and the timing result carries an
# explicit measured flag.
! grep -F 'set sta_top $sta_last_module' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'STA_CLOSURE_TOP_FOUND=' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'STA_TIMING_MEASURED=' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'STA_DIAG_STATUS=ok' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'STA_DIAG_REGISTER_COUNT=' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'sta_diag_register_count=' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'sta_timing_measured=' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'sta_closure_top_found=' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'sta_closure_bytes=' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'sta_closure_module_count=' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'sta_blackbox_export_status=' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
# Round-79/86: the best-effort OpenSTA closure step must be skipped (sta_status
# stays not_attempted) when the closure partition (top=nano_golden_decoder_top)
# was not scheduled this round OR was scheduled but did not exit 0 (typically
# rc=124 timeout, leaving build/ppa_closure_top.v unwritten).  Either way a
# non-closure smoke must not be misclassified as closure_export_empty via a
# spurious STA_NO_COMBINED_NETLIST.
grep -F 'CLOSURE_SCHEDULED=0' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'CLOSURE_SCHEDULED=1' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'CLOSURE_PART_RC=""' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'CLOSURE_PART_RC=$part_rc' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'if [[ "${CLOSURE_SCHEDULED:-0}" == "1" && "${CLOSURE_PART_RC:-1}" == "0" ]]; then' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'STA_ERROR="closure partition (top=nano_golden_decoder_top) ran but exited ${CLOSURE_PART_RC:-unknown}' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'STA_ERROR="closure partition (top=nano_golden_decoder_top) not scheduled in this round' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
# Round-70: Yosys may emit the configured closure top as an escaped Verilog
# identifier (`module \\nano_golden_decoder_top`).  The resolver strips that
# syntax marker before link_design and still supports normalized paramod names.
grep -F 'module[ \t]+([^ \t(]+)' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'string range $_m 1 end' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null
grep -F 'string match "_paramod_*_nano_golden_decoder_top" $_m' "$ROOT/scripts/remote_yosys_round.sh" >/dev/null

printf 'PPA scheduler tests: PASS (partitions=%s)\n' "$EXPECTED"
