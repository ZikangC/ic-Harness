#!/usr/bin/env bash
# Run Task 02 synthesis for the isolated doule RTL harness environment.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
eval "$(python3 "$ROOT/harness/environment.py" --shell)"
REMOTE=$HARNESS_REMOTE_HOST
RROOT=$HARNESS_REMOTE_PROJECT
CONTAINER=$HARNESS_CONTAINER
CONTAINER_WORKDIR=$HARNESS_CONTAINER_WORKDIR
INFRA_EXIT=75
TRANSPORT_RETRIES=3
SSH_PARTITION_GRACE_SECONDS=60
# Heavy partitions (long tool budgets) get extra local-SSH headroom: 1/10 of
# the tool budget is added to the fixed grace, so a slow channel cannot kill
# the local SSH client while the remote is still writing ppa_<name>.exit_code
# after its own `timeout -k` fires.  Recovery reads that file with a fresh
# connection (recover_partition_exit), so this only widens the window and
# never resends a partition command (no duplicate synthesis).
SSH_RECOVERY_TIMEOUT_SECONDS=30
# Wall-clock ceiling for every non-partition remote_ssh call (setup, exit-code
# recovery, log/stat fetches, status update).  A half-open SSH channel with a
# bare `-o ConnectTimeout` never returns; this bound turns that hang into a
# finite timeout, then exit-75 transport semantics apply.
REMOTE_SSH_TIMEOUT_SECONDS=30
# Round-80 (measurement 口径 fix, zero RTL): the un-buffered-fanout audit
# streams the assembled closure netlist (build/sta_norm/ppa_sta_all.v, >100 MiB)
# twice inside the container plus the Nangate45 Liberty once.  It gets its own
# bounded wall clock instead of the generic 30 s fetch bound.  It is a single
# best-effort read -- deliberately NOT wrapped in retry_transport, so a lost
# channel cannot spend three more audit budgets inside one round -- and on
# expiry its own fields stay 未测得 while every existing STA field keeps its
# value.
FANOUT_AUDIT_TIMEOUT_SECONDS=120
# Set to 1 when the preflight ssh is killed by its wall-clock watchdog, so the
# post-loop recovery maps an unreadable remote exit code to INFRA_EXIT (75)
# instead of leaking the raw kill signal as a bogus tool result.
PREFLIGHT_TIMED_OUT=0
TASK=02_yosys_ppa
MODE=measure
TIMEOUT_SECONDS=7200
REQUESTED_PARTITIONS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --preflight) TASK=02_yosys_preflight; MODE=preflight; TIMEOUT_SECONDS=600; shift ;;
    --timeout-seconds) TIMEOUT_SECONDS=$2; shift 2 ;;
    --partitions) REQUESTED_PARTITIONS=$2; shift 2 ;;
    *) echo "usage: $0 [--preflight] [--timeout-seconds N] [--partitions name1,name2]" >&2; exit 2 ;;
  esac
done
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || { echo "timeout must be positive" >&2; exit 2; }
[[ "$REQUESTED_PARTITIONS" =~ ^[A-Za-z0-9_,.-]*$ ]] || { echo "partition names must be comma-separated identifiers" >&2; exit 2; }
[[ "$MODE" == "measure" || -z "$REQUESTED_PARTITIONS" ]] || { echo "--partitions is valid only in measure mode" >&2; exit 2; }
RUN_ID="${TASK}--$(date +%Y%m%d-%H%M%S)--$$"
LOCAL_RUN="$ROOT/runs/$RUN_ID"
mkdir -p "$LOCAL_RUN"

retry_transport() {
  local attempt rc=0
  for ((attempt = 1; attempt <= TRANSPORT_RETRIES; attempt++)); do
    "$@" && return 0
    rc=$?
    printf 'transport attempt %d/%d failed (exit=%d)\n' "$attempt" "$TRANSPORT_RETRIES" "$rc" >&2
    (( attempt == TRANSPORT_RETRIES )) || sleep 3
  done
  return "$INFRA_EXIT"
}

# macOS does not provide GNU `timeout`.  Keep an independent wall-clock around
# the local SSH client so a lost SSH channel cannot outlive a bounded remote
# yosys timeout indefinitely.  The remote command still writes its own exit
# code; callers recover that file with a fresh connection after this watchdog
# fires.
SSH_LAST_TIMED_OUT=0
ssh_with_wall_timeout() {
  local wall_seconds=$1 marker command_pid watchdog rc=0
  shift
  marker=$(mktemp "${TMPDIR:-/tmp}/rtl-harness-ssh-timeout.XXXXXX") || return 1
  rm -f "$marker"
  SSH_LAST_TIMED_OUT=0
  "$@" &
  command_pid=$!
  (
    sleep "$wall_seconds"
    if kill -0 "$command_pid" 2>/dev/null; then
      : > "$marker"
      kill -TERM "$command_pid" 2>/dev/null || true
      sleep 5
      kill -KILL "$command_pid" 2>/dev/null || true
    fi
  ) &
  watchdog=$!
  if wait "$command_pid"; then rc=0; else rc=$?; fi
  kill "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  if [[ -f "$marker" ]]; then SSH_LAST_TIMED_OUT=1; fi
  rm -f "$marker"
  return "$rc"
}

# Every non-partition ssh call in this script goes through remote_ssh so a
# half-open channel (TCP established, no response) cannot hang the round the
# way a bare `ssh -o ConnectTimeout` can.  $1 is the wall-clock ceiling in
# seconds (tool budget plus grace); on timeout the caller either recovers the
# on-disk exit code (partitions/preflight) or treats the fetch as empty (exit
# 75 via retry_transport).  A partition command is NEVER resent after a
# watchdog fire, so a lost channel cannot trigger duplicate synthesis.
remote_ssh() {
  local wall_seconds=$1
  shift
  ssh_with_wall_timeout "$wall_seconds" \
    ssh -n -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
    "$REMOTE" "$@"
}

RECOVERED_PARTITION_RC=""
recover_partition_exit() {
  local name=$1 attempt value
  RECOVERED_PARTITION_RC=""
  for ((attempt = 1; attempt <= TRANSPORT_RETRIES; attempt++)); do
    value=$(ssh_with_wall_timeout "$SSH_RECOVERY_TIMEOUT_SECONDS" \
      ssh -n -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
      "$REMOTE" "cat '$RROOT/runs/$RUN_ID/ppa_$name.exit_code' 2>/dev/null" 2>/dev/null || true)
    value=$(printf '%s' "$value" | tr -d '[:space:]')
    if [[ "$value" =~ ^[0-9]+$ ]]; then
      RECOVERED_PARTITION_RC=$value
      printf 'recovered remote partition %s exit=%s\n' "$name" "$value" >&2
      return 0
    fi
    (( attempt == TRANSPORT_RETRIES )) || sleep 3
  done
  return 1
}

# Run one partition's yosys via ssh+docker exec.  A genuine yosys/tool exit
# (0/124/2/...) passes through unmodified so a partition timeout is reported
# as 124 and is never masked as a 75 infrastructure exit.
ssh_partition() {
  local name=$1 wall_seconds=$2 rc=0
  shift 2
  # This function is called inside a `while ... done <<< "$PARTITIONS_TSV"`
  # loop.  Without -n, ssh inherits that stdin and consumes the remaining
  # partition rows, so only the first partition ever runs.
  ssh_with_wall_timeout "$wall_seconds" \
    ssh -n -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
    "$REMOTE" "$@" || rc=$?
  # A completed remote tool may lose its SSH response.  Its per-partition
  # exit file is authoritative, so recover it before deciding whether this is
  # a transport failure.  A watchdog timeout or an unrecoverable 255 does NOT
  # resend the command: the remote may already be running yosys, and a second
  # invocation against the same run dir would duplicate synthesis.  Unrecovered
  # transport failures surface as exit 75 for the controller to retry the
  # interrupted verification.
  if [[ "$SSH_LAST_TIMED_OUT" == 1 || "$rc" == 255 ]]; then
    if recover_partition_exit "$name"; then
      return "$RECOVERED_PARTITION_RC"
    fi
    printf 'partition %s ssh failed (timeout=%s rc=%s); remote exit file unavailable; not resending\n' \
      "$name" "$SSH_LAST_TIMED_OUT" "$rc" >&2
    return "$INFRA_EXIT"
  fi
  return "$rc"
}

# Only Task-02 dependencies are synchronized; nano-kpu is never referenced.
retry_transport rsync -az --delete "$ROOT/rtl/" "$REMOTE:$RROOT/rtl/" || exit "$INFRA_EXIT"
retry_transport rsync -az "$ROOT/harness/ppa.toml" "$ROOT/harness/ppa_measure.ys" "$ROOT/harness/ppa_macro.ys" "$ROOT/harness/ppa_preflight.ys" "$ROOT/harness/ppa_leaves_blackbox.v" "$ROOT/harness/parse_partition_memory.awk" "$ROOT/harness/parse_sta_timing.awk" "$REMOTE:$RROOT/harness/" || exit "$INFRA_EXIT"
retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" "mkdir -p '$RROOT/runs/$RUN_ID' '$RROOT/status'; printf '%s\n' 'state=running' 'task=$TASK' 'run_id=$RUN_ID' 'container=$CONTAINER' > '$RROOT/status/current.status'" || exit "$INFRA_EXIT"

set +e
PART_RESULTS=()
EXPECTED_PARTITIONS=0
OBSERVED_PARTITIONS=0
SCHEDULER_AUDIT="not_applicable"
# Round-46 (measurement completion, end-to-end convergence): OpenSTA top-level
# closure results.  The post-partition STA step in measure mode overwrites these
# defaults; preflight mode keeps them (the STA fields are only meaningful for a
# measure round).
STA_STATUS="not_attempted"
STA_CRIT_PATH_PS="未测得"
STA_EST_FREQ_MHZ="未测得"
STA_MEETS_10NS="未测得"
STA_ERROR=""
# Round-53: raw STA read/link markers and the resolved closure top, emitted at
# the tail of sta.log by the Tcl and captured locally.  Defaults keep preflight
# rounds well-formed.
STA_LINK_RESULT="未测得"
STA_DIAG_STATUS="未测得"
STA_DIAG_CLOCK_PORT_COUNT="未测得"
STA_DIAG_CLOCK_COUNT="未测得"
STA_DIAG_REGISTER_COUNT="未测得"
STA_DIAG_DATA_INPUT_COUNT="未测得"
STA_CLOSURE_TOP_NAME="未测得"
STA_CLOSURE_TOP_FOUND="未测得"
STA_TIMING_MEASURED="no"
STA_TIMING_PATH_COUNT="未测得"
# Round-76 (measurement口径 fix, zero RTL): per-path attribution plus a
# credible-vs-artifact split for the OpenSTA report_checks block.  A path is
# credible only when it stays inside the Nangate45 Liberty tables (arrival
# < 1e6 ps -- the same non-physical bound the ABC wire-load口径 already filters
# -- and every cell arc < 1000 ps with no negative arc).  `sta_crit_path_ps`
# reports the largest credible path; the unfiltered maximum is kept verbatim in
# `sta_raw_max_arrival_ps` and the out-of-table paths in `sta_artifact_*`.
STA_RAW_MAX_ARRIVAL_PS="未测得"
STA_RAW_MIN_SLACK_NS="未测得"
STA_CRIT_PATH_NS="未测得"
STA_CRIT_PATH_START="未测得"
STA_CRIT_PATH_END="未测得"
STA_CRIT_PATH_GROUP="未测得"
STA_CRIT_PATH_SLACK_NS="未测得"
STA_CRIT_PATH_CLASS="未测得"
STA_CREDIBLE_PATH_COUNT="未测得"
STA_CREDIBLE_MEETS_10NS="未测得"
STA_ARTIFACT_PATH_COUNT="未测得"
STA_ARTIFACT_MAX_ARRIVAL_PS="未测得"
STA_ARTIFACT_MAX_ARRIVAL_START="未测得"
STA_ARTIFACT_MAX_ARRIVAL_END="未测得"
STA_ARTIFACT_MAX_ARRIVAL_GROUP="未测得"
STA_ARTIFACT_SETUP_NS="未测得"
STA_ARTIFACT_WORST_ARC_PS="未测得"
STA_ARTIFACT_WORST_ARC_PIN="未测得"
STA_ARTIFACT_NEG_ARC_COUNT="未测得"
STA_UNBUFFERED_FANOUT_ARTIFACT="未测得"
# Round-80 (measurement 口径 fix, zero RTL): the "un-buffered high-fanout net"
# claim behind the artifact arcs was never measured -- the Round-75 note
# estimated "~8k gate pins" per `beat` select net from a cell count, without
# counting anything.  These fields carry a real audit of the closure netlist
# OpenSTA read (build/sta_norm/ppa_sta_all.v): the driver instances named by the
# artifact report (worst-arc pin + worst-path startpoint) are located in that
# netlist, the pins connected to the net each one drives are counted, their
# Nangate45 Liberty input-pin capacitances are summed, and that load is compared
# against the `total_output_net_capacitance` axis of the driver cell's own delay
# tables.  A load beyond that axis maximum means report_checks extrapolated the
# table, i.e. the arc is an un-buffered-fanout mapping artifact, not logic
# depth.  Every field stays 未测得 when the audit cannot run, so a failed audit
# can never invalidate or restate an existing STA field.
STA_ARTIFACT_FANOUT_STATUS="未测得"
STA_ARTIFACT_FANOUT_SOURCES="未测得"
STA_ARTIFACT_FANOUT_NET="未测得"
STA_ARTIFACT_FANOUT_DRIVER_CELL="未测得"
STA_ARTIFACT_FANOUT_PINS="未测得"
STA_ARTIFACT_FANOUT_LOAD="未测得"
STA_ARTIFACT_FANOUT_LOAD_UNIT="未测得"
STA_ARTIFACT_FANOUT_LOAD_RESOLVED_PINS="未测得"
STA_ARTIFACT_FANOUT_LOAD_UNRESOLVED_PINS="未测得"
STA_ARTIFACT_FANOUT_LOAD_OVER_TABLE="未测得"
STA_ARTIFACT_FANOUT_VERDICT="未测得"
STA_ARTIFACT_LIB_CAP_AXIS="未测得"
STA_ARTIFACT_LIB_MAX_CAP="未测得"
# Round-82 (measurement 口径 fix, zero RTL): FSM ABC-vs-STA reconciliation.
# The per-partition ABC `stime` block attributes a 195.026 ns delay to the
# mapped nano_golden_datapath_fsm while the closure report_checks on the SAME
# netlist showed ~2 ns of in-table timing, but the fsm's own timing was never
# measured.  These fields carry that measurement: where the fsm instances are
# (path + ref_name + locator that resolved them), how many of their registers
# are clocked by clk, the clock table, the whole-design clk-group worst path and
# the fsm-endpoint-restricted worst path (each with per-stage cells), and the
# in-table-STA / ABC-stime ratio with an explicit verdict.  All of it comes from
# the SAME PRE-LAYOUT PARTIAL_HIERARCHICAL closure STA netlist, so it can never
# become a chip Fmax; every field stays 未测得 unless the measurement really
# resolved.
STA_FSM_STATUS="未测得"
STA_FSM_LOCATOR="未测得"
STA_FSM_CELL_COUNT="未测得"
STA_FSM_CELLS="未测得"
STA_FSM_CELL_REG_TOTAL="未测得"
STA_FSM_CELL_CLK_REG_TOTAL="未测得"
STA_FSM_CLOSURE_REGISTER_COUNT="未测得"
STA_FSM_CLK_REGISTER_COUNT="未测得"
STA_FSM_REPORT_CLOCKS="未测得"
STA_FSM_PATH_STATUS="未测得"
STA_FSM_PATH_BLOCK_COMPLETE="未测得"
STA_FSM_PATH_ARRIVAL_NS="未测得"
STA_FSM_PATH_SLACK_NS="未测得"
STA_FSM_PATH_START="未测得"
STA_FSM_PATH_END="未测得"
STA_FSM_PATH_GROUP="未测得"
STA_FSM_PATH_STAGE_COUNT="未测得"
STA_FSM_PATH_CELL_TYPES="未测得"
STA_FSM_PATH_CELL_HISTO="未测得"
STA_FSM_PATH_STAGE_HEAD="未测得"
STA_FSM_PATH_STAGE_HEAD_TRUNCATED="未测得"
STA_FSM_PATH_MAX_ARC_NS="未测得"
STA_FSM_PATH_MAX_ARC_PIN="未测得"
STA_FSM_PATH_NEG_ARC_COUNT="未测得"
STA_FSM_PATH_OUT_OF_TABLE_ARC_COUNT="未测得"
STA_FSM_OWN_PATH_STATUS="未测得"
STA_FSM_OWN_PATH_BLOCK_COMPLETE="未测得"
STA_FSM_OWN_PATH_ARRIVAL_NS="未测得"
STA_FSM_OWN_PATH_SLACK_NS="未测得"
STA_FSM_OWN_PATH_START="未测得"
STA_FSM_OWN_PATH_END="未测得"
STA_FSM_OWN_PATH_GROUP="未测得"
STA_FSM_OWN_PATH_STAGE_COUNT="未测得"
STA_FSM_OWN_PATH_CELL_TYPES="未测得"
STA_FSM_OWN_PATH_CELL_HISTO="未测得"
STA_FSM_OWN_PATH_STAGE_HEAD="未测得"
STA_FSM_OWN_PATH_STAGE_HEAD_TRUNCATED="未测得"
STA_FSM_OWN_PATH_MAX_ARC_NS="未测得"
STA_FSM_OWN_PATH_MAX_ARC_PIN="未测得"
STA_FSM_OWN_PATH_NEG_ARC_COUNT="未测得"
STA_FSM_OWN_PATH_OUT_OF_TABLE_ARC_COUNT="未测得"
# Round-85 (measurement 口径 fix, zero RTL): load-axis/fanout audit of the
# worst clk-group path's largest-increment driver.  Round-80 measured the
# fanout of the ARTIFACT report's drivers; the current question needs the same
# measurement for the real clk-group worst path (sta_fsm_path_max_arc_pin) to
# decide whether its 29.821 ns is logic depth or an un-buffered high-fanout
# mapping artifact.  These fields carry the audit's own STAFANOUT /
# STAFANOUT_LIB rows verbatim for that one source plus the same load-vs-cap-axis
# verdict the artifact audit uses.  They stay separate from
# sta_artifact_fanout_* so a clk-group-path driver is never reported as an
# artifact driver, and every one of them stays 未测得 unless the audit resolved.
STA_FSM_PATH_FANOUT_STATUS="未测得"
STA_FSM_PATH_FANOUT_ROW="未测得"
STA_FSM_PATH_FANOUT_LIB="未测得"
STA_FSM_PATH_FANOUT_VERDICT="未测得"
STA_FSM_STA_PATH_NS="未测得"
STA_FSM_PATH_BASIS="未测得"
STA_FSM_VS_ABC_RATIO="未测得"
STA_FSM_ABC_DELAY_PS="未测得"
STA_FSM_ABC_DELAY_SOURCE="未测得"
STA_FSM_VERDICT="未测得"
STA_FSM_ERROR="未测得"
STA_FSM_DIGEST=""
STA_TIMING_VERDICT="未测得"
STA_NETLIST_PORT_MISMATCH_COUNT="未测得"
STA_NETLIST_PORT_MISMATCH_SAMPLE="未测得"
STA_READ_OK_COUNT="未测得"
STA_READ_SKIP_COUNT="未测得"
STA_CLOSURE_READ_SKIP_COUNT="未测得"
STA_BODY_CHECK="未测得"
STA_CLOSURE_BYTES="未测得"
STA_CLOSURE_MODULE_COUNT="未测得"
STA_BLACKBOX_EXPORT_STATUS="未测得"
STA_TIMEOUT_SECONDS=300
# Round-51 (measurement completion, harness fix): the closure partition
# (top_level_sta, top=nano_golden_decoder_top) always blackboxes the compute
# modules, so a successful STA read+link+report is a PARTIAL pre-layout closure,
# never a full no-blackbox Fmax.  This flag is set in the partition loop when the
# closure partition declares a non-empty blackbox list; the STA classification
# below forces `partial` (not `ok`) in that case so sta_est_freq_mhz can never be
# mistaken for a full-chip Fmax.
CLOSURE_HAS_BLACKBOX=0
# Round-79 (harness repair): the post-partition OpenSTA closure step is only
# meaningful when the closure partition (top=nano_golden_decoder_top) actually
# executed this round.  A targeted smoke that selects only a non-closure
# partition (e.g. cp_writer) never writes build/ppa_closure_top.v, so the STA
# step would otherwise report STA_NO_COMBINED_NETLIST and the audit would
# hard-gate it as closure_export_empty even though no closure synthesis was
# attempted.  This flag is set inside the partition loop only after the closure
# partition passes the budget check; while it stays 0 the STA step is skipped
# and sta_status stays not_attempted (audit: closure_repair_required=false).
CLOSURE_SCHEDULED=0
# Round-86 (harness repair): the closure partition's own exit code, captured
# after it runs.  CLOSURE_SCHEDULED only records that it passed the budget
# check; a closure partition that then times out (rc=124) leaves
# build/ppa_closure_top.v unwritten after the measure-loop prologue rm -f'd it.
# The post-partition STA step must be skipped for a non-zero closure rc (a
# truncated closure is resumable incomplete evidence), not misreported as
# STA_NO_COMBINED_NETLIST -> closure_export_empty.
CLOSURE_PART_RC=""
if [[ "$MODE" == "measure" ]]; then
  # Round-49 (measurement completion, harness fix): remove stale per-partition
  # mapped netlists and the previous round's closure top from the shared
  # build/ dir BEFORE this round's partition loop writes fresh ones.  The
  # post-partition OpenSTA step normalizes a build/ppa_mapped_*.v glob, so
  # this cleanup is the freshness guarantee: any leftover netlist from an
  # earlier run (truncated write, budget-skip, or a pre-Round-48 file without
  # `memory_map`) is removed before the loop, and only this round's fresh
  # mapped netlists can reach the STA reader (Round-48 evidence:
  # ppa_mapped_{cp_fsm, decay_from_sigmoid_mod,kda_delta_head}.v).
  retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
    "mkdir -p '$RROOT/build'; rm -f '$RROOT'/build/ppa_mapped_*.v '$RROOT'/build/ppa_closure_top.v" || exit "$INFRA_EXIT"
  # ---- layered per-partition measure (one yosys invocation per partition) ----
  # Each partition runs under its own timeout so a slow/heavy module cannot
  # discard the per-module area/timing already measured for the others.  The
  # ordered partition list and per-partition timeout come from harness/ppa.toml
  # ([[partitions]] blocks); the LAST partition is deliberately a cheap one, so
  # the round exits 0 whenever real partial measurements were produced even if
  # a heavy earlier partition times out.
  PARTITIONS_TSV=$(awk -F= '
    /^\[\[partitions\]\]/ { if (started && name != "") emit(); reset_parts(); started = 1; next }
    started && /^name[ \t]*=/       { name     = val(); next }
    started && /^top[ \t]*=/        { top      = val(); next }
    started && /^chparam[ \t]*=/    { chparam  = val(); next }
    started && /^blackbox[ \t]*=/   { blackbox = val(); next }
    started && /^timeout_s[ \t]*=/  { timeout_s = val(); next }
    started && /^macro_blackbox[ \t]*=/ { macro_blackbox = val(); next }
    END { if (started && name != "") emit() }
    function val()   { v = substr($0, index($0, "=") + 1); gsub(/^[ \t"]+/, "", v); gsub(/[ \t"]+$/, "", v); return v }
    function reset_parts() { name = ""; top = ""; chparam = ""; blackbox = ""; timeout_s = ""; macro_blackbox = "false" }
    function emit()  { printf "%s@%s@%s@%s@%s@%s\n", name, top, chparam, blackbox, timeout_s, macro_blackbox }
  ' "$ROOT/harness/ppa.toml" 2>/dev/null || true)
  if [[ -z "$PARTITIONS_TSV" ]]; then
    echo "measure mode: no [[partitions]] found in harness/ppa.toml" >&2
    RC=2
    SCHEDULER_AUDIT="scheduler_error(no_partitions)"
  else
    # A controller-selected targeted probe may use only exact names declared
    # in ppa.toml.  Reject missing/duplicate names rather than silently
    # falling back to the full list: the feedback's scheduler audit must
    # describe exactly what was requested.
    if [[ -n "$REQUESTED_PARTITIONS" ]]; then
      FILTERED_PARTITIONS=""
      SEEN_PARTITIONS="," 
      IFS=',' read -r -a REQUESTED_PARTITION_ARRAY <<< "$REQUESTED_PARTITIONS"
      for requested in "${REQUESTED_PARTITION_ARRAY[@]}"; do
        [[ -n "$requested" ]] || { echo "empty partition name" >&2; RC=2; break; }
        case "$SEEN_PARTITIONS" in *",$requested,"*) echo "duplicate requested partition: $requested" >&2; RC=2; break;; esac
        match=$(printf '%s\n' "$PARTITIONS_TSV" | awk -F@ -v n="$requested" '$1 == n { print; exit }')
        [[ -n "$match" ]] || { echo "unknown requested partition: $requested" >&2; RC=2; break; }
        SEEN_PARTITIONS+="$requested,"
        FILTERED_PARTITIONS+="${FILTERED_PARTITIONS:+$'\n'}$match"
      done
      if [[ "${RC:-0}" == 2 ]]; then
        PARTITIONS_TSV=""
        SCHEDULER_AUDIT="scheduler_error(invalid_targeted_selection)"
      else
        PARTITIONS_TSV=$FILTERED_PARTITIONS
      fi
    fi
    if [[ -z "$PARTITIONS_TSV" ]]; then
      : # Invalid targeted request was already diagnosed above.
    else
    EXPECTED_PARTITIONS=$(printf '%s\n' "$PARTITIONS_TSV" | awk 'NF { count++ } END { print count + 0 }')
    # Round-25: ppa.toml `blackbox` is now a space-separated MODULE-NAME LIST
    # (previously a single blackbox-file path).  Filter the master declaration
    # file harness/ppa_leaves_blackbox.v down to exactly those names for each
    # partition so its OWN top (never in the list) stays real while the heavy
    # q_bus x x_bus MAC subtree is read as (-lib) blackboxes.  The filtered
    # files are staged in this run dir and rsynced to the container's build/;
    # the docker exec injects `read_verilog -sv -lib build/ppa_bb_<name>.v`.
    while IFS='@' read -r _bb_name _bb_top _bb_chparam _bb_blackbox _bb_timeout_s _bb_macro_blackbox; do
      [[ -n "$_bb_name" && -n "$_bb_blackbox" ]] || continue
      awk -v list="$_bb_blackbox" '
        BEGIN { split(list, want, /[ \t]+/) }
        /^\(\* blackbox \*\)/ { in_bb = 1; keep = 0; buf = $0 "\n"; next }
        in_bb {
          buf = buf $0 "\n"
          if ($1 == "module") {
            nm = $2; sub(/[#(].*/, "", nm)
            keep = 0
            for (i in want) if (want[i] == nm) keep = 1
          }
          if ($0 ~ /^endmodule[ \t]*$/) {
            if (keep) printf "%s", buf
            in_bb = 0
          }
        }
      ' "$ROOT/harness/ppa_leaves_blackbox.v" > "$LOCAL_RUN/ppa_bb_${_bb_name}.v"
      # Round-62 (measurement completion, harness fix): the closure partition
      # (top=nano_golden_decoder_top) must ALSO blackbox mla_kv_cache, the only
      # memory-bearing submodule in the control-plane closure.  The prologue
      # `blackbox m:*mla_kv_cache*` in ppa_measure.ys does not empty a module
      # body in this Yosys build, so the closure's whole-design `memory_map`
      # expanded its $paramod$ body into ~245,760 write-mux/$dff cells (closure
      # log pass-69) and the subsequent whole-closure write_verilog dropped the
      # six control-plane module bodies (sta_body_check 6/6 missing).  Reading
      # mla_kv_cache from a (-lib) blackbox stub keeps that body out of the
      # closure netlist from the start; the 245,760 bits are still counted by
      # the cp_fsm partition's real body and top_level_sta's memory row is
      # excluded from the aggregate, so no SRAM bit is lost or double-counted.
      # The stub's parameters/ports are byte-for-byte synced with rtl/mla_kv_cache.sv.
      if [[ "$_bb_top" == "nano_golden_decoder_top" ]]; then
        cat >> "$LOCAL_RUN/ppa_bb_${_bb_name}.v" <<'MLAKV_CACHE_BB_EOF'
(* blackbox *)
module mla_kv_cache #(
  parameter int MAX_SEQ = 64,
  parameter int HEADS   = 2,
  parameter int KD      = 48,
  parameter int DV      = 32,
  parameter int DATA_W  = 24
) (
  input  logic                               clk,
  input  logic                               rst_n,
  input  logic                               write_valid,
  input  logic [$clog2(MAX_SEQ)-1:0]         write_pos,
  input  logic signed [HEADS*KD*DATA_W-1:0]  write_key_bus,
  input  logic signed [HEADS*DV*DATA_W-1:0]  write_value_bus,
  input  logic [$clog2(MAX_SEQ)-1:0]         read_pos,
  output logic signed [HEADS*KD*DATA_W-1:0]  read_key_bus,
  output logic signed [HEADS*DV*DATA_W-1:0]  read_value_bus
);
endmodule
MLAKV_CACHE_BB_EOF
      fi
    done <<< "$PARTITIONS_TSV"
    if compgen -G "$LOCAL_RUN/ppa_bb_*.v" >/dev/null 2>&1; then
      # Quote the directory, not the wildcard: rsync must receive the actual
      # generated blackbox files, never the literal string `ppa_bb_*.v`.
      retry_transport rsync -az "$LOCAL_RUN"/ppa_bb_*.v "$REMOTE:$RROOT/build/" || exit "$INFRA_EXIT"
    fi
    FIRST_PART=""; RC=0; DEADLINE=$((SECONDS + TIMEOUT_SECONDS))
    while IFS='@' read -r name top chparam blackbox timeout_s macro_blackbox; do
      [[ -n "$name" ]] || continue
      [[ -z "$FIRST_PART" ]] && FIRST_PART="$name"
      if (( SECONDS >= DEADLINE )); then
        echo "measurement budget exhausted; skipping partition $name" >&2
        RC=124; PART_RESULTS+=("partition $name exit=skipped(budget)")
        continue
      fi
      [[ "$timeout_s" =~ ^[1-9][0-9]*$ ]] || timeout_s=30
      # Cap each partition's yosys timeout to the round's remaining budget so
      # a heavy partition cannot overrun the controller-chosen measurement
      # budget and starve the later (cheaper) partitions of their slice.
      remaining=$(( DEADLINE - SECONDS ))
      if (( timeout_s > remaining )); then timeout_s=$remaining; fi
      ssh_wall_seconds=$((timeout_s + SSH_PARTITION_GRACE_SECONDS + timeout_s / 10))
      local_redir=">>"; [[ "$name" == "$FIRST_PART" ]] && local_redir=">"
      bbcmd=""; [[ -n "$blackbox" ]] && bbcmd="read_verilog -sv -lib build/ppa_bb_$name.v"
      # Round-35 (synthesis-usability repair): a partition declared
      # `macro_blackbox = true` in ppa.toml is a SRAM/ROM macro.  Its yosys
      # invocation uses the standalone harness/ppa_macro.ys template: reads the
      # real RTL body, emits the early memory-stat (MEMORY_STAT_COMPLETE), logs
      # the macro markers, then runs off the end of the script (EOF => exit 0)
      # BEFORE techmap/ABC -- the flattened 9.2-Mbit memory would otherwise map
      # to ~792k gates / 26 us wire-load.  Round-34's injected `exit 0` came
      # back rc=1 because this container's Yosys has no `exit` command
      # (per-partition log: "ERROR: No such command: exit"), so the macro
      # markers were logged by the harness tail but the partition rc was 1.
      # The macro template now ends without any `exit` (EOF is a clean exit 0);
      # the @PART_MACRO@ slot in ppa_measure.ys stays empty for normal
      # partitions.
      macrocmd=""
      ys_template=ppa_measure.ys
      if [[ "${macro_blackbox:-false}" == "true" ]]; then
        ys_template=ppa_macro.ys
      fi
      # Round-46 (measurement completion, end-to-end convergence): the
      # top_level_sta partition (top=nano_golden_decoder_top) runs the closure
      # flow (mapped control-plane closure -> build/ppa_closure_top.v -> OpenSTA); every
      # ordinary partition keeps the two-pass measure flow.  The matching flow
      # block in harness/ppa_measure.ys is deleted by sed below.  The scope
      # token is substituted AFTER the s|TOP|nano_golden_decoder_top|g
      # substitution so the "TOP" inside "TOP_LEVEL_COMPLETE" survives
      # verbatim.
      flow_del_expr='-e "/# ===CLOSURE_FLOW_BEGIN===/,/# ===CLOSURE_FLOW_END===/d"'
      scope_token="PARTIAL_HIERARCHICAL"
      if [[ "$top" == "nano_golden_decoder_top" ]]; then
        flow_del_expr='-e "/# ===NORMAL_FLOW_BEGIN===/,/# ===NORMAL_FLOW_END===/d"'
        scope_token="TOP_LEVEL_COMPLETE"
        # Round-79: record that the closure partition actually ran (it passed
        # the budget check), so the post-partition STA step knows a combined
        # netlist was attempted this round.
        CLOSURE_SCHEDULED=1
        # Round-51: record that this round's closure blackboxes compute modules,
        # so the post-partition STA result is classified `partial`, not `ok`.
        CLOSURE_HAS_BLACKBOX=0
        [[ -n "$blackbox" ]] && CLOSURE_HAS_BLACKBOX=1
      fi
      ssh_partition "$name" "$ssh_wall_seconds" \
        "docker exec -w '$CONTAINER_WORKDIR' '$CONTAINER' sh -lc 'run=runs/$RUN_ID; mkdir -p \"\$run\" build; printf \"HARNESS_PARTITION_BEGIN=$name\\n\" $local_redir \"\$run/yosys.log\"; printf \"%s\\n\" \"\$\$\" > \"\$run/container.pid\"; READFILES=\$(tr \"\\n\" \" \" < rtl/filelist.f); sed -e \"s|@PART_TAG@|$name|g\" -e \"s|@PART_TOP@|$top|g\" -e \"s|@PART_CHPARAM@|$chparam|g\" -e \"s|@PART_BLACKBOX@|$bbcmd|g\" -e \"s|@PART_BLACKBOX_LIST@|$blackbox|g\" -e \"s|@PART_MACRO@|$macrocmd|g\" -e \"s|@READFILES@|\$READFILES|g\" -e \"s|TOP|nano_golden_decoder_top|g\" -e \"s|LIB|/opt/OpenROAD-flow-scripts/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib|g\" -e \"s|@PART_SCOPE@|$scope_token|g\" $flow_del_expr harness/$ys_template > build/ppa_measure.$name.ys; timeout -k 10s ${timeout_s}s yosys -s build/ppa_measure.$name.ys >> \"\$run/yosys.log\" 2>&1; rc=\$?; rm -f \"\$run/container.pid\"; printf \"%s\\n\" \"\$rc\" > \"\$run/ppa_$name.exit_code\"; echo \"==== PARTITION $name rc=\$rc ====\"; exit \$rc'"
      part_rc=$?
      if [[ "$top" == "nano_golden_decoder_top" ]]; then
        CLOSURE_PART_RC=$part_rc
      fi
      RC=$part_rc
      PART_RESULTS+=("partition $name exit=$part_rc")
      echo "partition $name exit=$part_rc" >&2
    done <<< "$PARTITIONS_TSV"
    OBSERVED_PARTITIONS=${#PART_RESULTS[@]}
    if (( OBSERVED_PARTITIONS == EXPECTED_PARTITIONS )); then
      SCHEDULER_AUDIT="pass"
    else
      SCHEDULER_AUDIT="scheduler_error(expected=$EXPECTED_PARTITIONS,observed=$OBSERVED_PARTITIONS)"
      RC=76
    fi
    # The last partition's rc is the round rc; tail partitions are the cheap
    # fast ones, so a heavy earlier partition timing out alone does not fail
    # the round when real partial measurements were produced.
    retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
      "printf '%s\n' '$RC' > '$RROOT/runs/$RUN_ID/exit_code'" || true

    # Round-79 (harness repair): run the best-effort OpenSTA closure step only
    # when the closure partition actually executed this round.  A targeted
    # smoke that omits top_level_sta must not emit STA_NO_COMBINED_NETLIST and
    # must not be misclassified as a closure export failure.
    if [[ "${CLOSURE_SCHEDULED:-0}" == "1" && "${CLOSURE_PART_RC:-1}" == "0" ]]; then
    # ---- Round-46 (measurement completion, end-to-end convergence): bounded
    # OpenSTA pre-layout STA on the hierarchical mapped netlists + the
    # top-level closure.  ABC partition `stime` (Round-45) measures boundary
    # combinational cones, not register-to-register Fmax; only linking the
    # per-partition mapped netlists under the top-level closure and running a
    # 10 ns clock gives the first pre-layout critical-path estimate.  This step
    # is best-effort: a missing STA backend, a link failure or a missing
    # closure netlist is reported (STA_UNAVAILABLE / STA_ERROR) and never
    # changes the round exit code.  This container ships OpenROAD, whose
    # timing engine is OpenSTA, even though it does not ship a standalone
    # `sta`/`opensta` executable.  OpenROAD also requires the technology and
    # macro LEFs before reading a mapped Verilog netlist.
    # Round-60 (harness repair): the STA read set is the SELF-CONTAINED closure
    # netlist (build/ppa_closure_top.v), which ppa_measure.ys now writes with
    # its own mapped control-plane bodies plus the compute/SRAM blackbox stubs.
    # The measure-loop prologue still `rm -f`'s build/ppa_mapped_*.v and
    # build/ppa_closure_top.v, so the closure file the normalization reads is
    # this round's fresh output -- a stale earlier-round netlist cannot re-enter
    # the reader.  Round-57 evidence: all six partitions exited 0 and
    # cp_arbiter mapped mem_arbiter4 (area row present), yet link_design still
    # raised ORD-2013 "LEF master mem_arbiter4 not found" because the top-level
    # closure netlist carried only the top's glue and the glob of separately
    # mapped bodies was not assembled into one linkable hierarchy.  Round-60
    # closes that by making the closure netlist itself carry the bodies.
    # STA_MAPPED_LIST stays defined for the legacy @STA_MAPPED_LIST@ Tcl
    # substitution (currently a no-op).
    STA_MAPPED_LIST=""
    # Round-52 (measurement completion, harness fix): every STA-0171
    # read_verilog failure lands on a mapped netlist that contains Yosys
    # `$paramod$<hash>\<name>` escaped identifiers (module definitions AND
    # instance types), while netlists without such tokens parse cleanly.  The
    # awk below normalizes those escaped `$paramod$` tokens to plain,
    # deterministic Verilog identifiers BEFORE OpenSTA/OpenROAD reads them:
    # `$paramod$<hash>\<name>` -> `_paramod_<hash>_<name>`, so a module
    # definition and every instantiation of it - inside the same file or across
    # the per-partition mapped netlists and the closure netlist - collapse to
    # the SAME plain name and link_design still matches.  The original
    # build/ppa_mapped_*.v artifacts stay untouched; normalized copies live in
    # build/sta_norm/ and only this best-effort STA step consumes them.
    # Measurement scope stays PARTIAL_HIERARCHICAL; no rtl/ change, so the
    # functional risk is zero.
# Round-74 (harness repair): the Round-52 normalizer rewrote ANY whitespace-
# delimited token that merely CONTAINED `$paramod$`, so it also matched the
# double-quoted `(* reprocess_after = "$paramod$<hash>\\<name>" *)` attribute
# value Yosys emits on re-derived blackbox instances (the two
# mla_attention_head cells inside mla_cache_attention_contract).  gsub then
# turned the surrounding quotes and the inner `$`/`\` into underscores, which
# stripped the quotes and left `(* reprocess_after = __paramod_<hash>__<name>_ *)`
# -- a bare identifier attribute value OpenSTA rejects as STA-0171 "syntax
# error" (evidence: build/sta_norm/ppa_sta_all.v line 6716012).  The rewrite is
# now anchored to the token start (`^\\?\$paramod\$`) so it only normalizes the
# escaped-identifier form (`\$paramod$<hash>\<name>` module/instance names) and
# leaves quoted string literals byte-for-byte intact.
    cat > "$LOCAL_RUN/sta_paramod_norm.awk" <<'STA_PARAMOD_NORM_AWK_EOF'
{
  out = ""
  for (i = 1; i <= NF; i++) {
    tok = $i
    if (tok ~ /^\\?\$paramod\$/) {
      s = tok
      if (substr(s, 1, 1) == "\\") s = substr(s, 2)
      gsub(/[^A-Za-z0-9_]/, "_", s)
      if (s !~ /^[A-Za-z_]/) s = "_" s
      tok = s
    }
    out = (out == "" ? "" : out OFS) tok
  }
  print out
}
STA_PARAMOD_NORM_AWK_EOF
    retry_transport rsync -az "$LOCAL_RUN/sta_paramod_norm.awk" "$REMOTE:$RROOT/runs/$RUN_ID/" || true
    cat > "$LOCAL_RUN/sta_strip_bb_params.awk" <<'STA_STRIP_BB_PARAMS_AWK_EOF'
# OpenROAD's Verilog reader rejects parameter overrides on timing-blackbox
# instances when the Yosys blackbox-only export has intentionally omitted the
# parameter declarations.  After mapping, port widths are concrete, so remove
# only `#(...)` overrides whose type is declared in the generated blackbox file.
BEGIN {
  while ((getline line < bb_file) > 0) {
    s = line
    sub(/^[ \t]*/, "", s)
    if (s ~ /^module[ \t]+/) {
      sub(/^module[ \t]+/, "", s)
      sub(/[ \t(].*$/, "", s)
      sub(/^\\/, "", s)
      blackbox[s] = 1
    }
  }
  close(bb_file)
}
function consume(chunk, i, ch) {
  for (i = 1; i <= length(chunk); i++) {
    ch = substr(chunk, i, 1)
    if (ch == "(") depth++
    else if (ch == ")") {
      depth--
      if (depth == 0) {
        print prefix substr(chunk, i + 1)
        stripping = 0
        return
      }
    }
  }
}
{
  if (stripping) { consume($0); next }
  s = $0
  trimmed = s
  sub(/^[ \t]*/, "", trimmed)
  if (trimmed ~ /^[^ \t#]+[ \t]*#\(/) {
    ty = trimmed
    sub(/[ \t#].*$/, "", ty)
    sub(/^\\/, "", ty)
    if (ty in blackbox) {
      p = index(s, "#")
      prefix = substr(s, 1, p - 1)
      depth = 0
      stripping = 1
      consume(substr(s, p))
      next
    }
  }
  print
}
STA_STRIP_BB_PARAMS_AWK_EOF
    retry_transport rsync -az "$LOCAL_RUN/sta_strip_bb_params.awk" "$REMOTE:$RROOT/runs/$RUN_ID/" || true
    # Round-68 (harness repair): ppa_measure.ys now writes the closure as ONE
    # self-contained whole-design netlist (build/ppa_closure_top.v): the mapped
    # control-plane bodies (mem_arbiter4 / nano_golden_microop_controller /
    # image_header_reader / logit_writer / $paramod$ nano_golden_datapath_fsm),
    # the mapped top glue, the Nangate cell modules (read_liberty -lib), and the
    # compute/SRAM blackbox stubs, via the proven explicit re-selection
    # (`select <top> %n` + `select -add <top>`) plus ONE
    # `write_verilog -selected -blackboxes` (the Round-64..67 `hierarchy -top` +
    # `select *` + no-`-selected` path dropped the bodies; see
    # questionAndsolution.md).  The STA read set below is the normalized form of
    # that one file.  The old glob of build/ppa_mapped_*.v stays dropped so a
    # full round's per-partition files cannot duplicate these closure bodies.
    cat > "$LOCAL_RUN/sta_body_check.awk" <<'STA_BODY_CHECK_AWK_EOF'
# Emit STA_BODY_CHECK=<module>=present|missing for each control-plane body the
# closure STA link requires.  Module names appear in the normalized netlist as
# `module <name>(`, `module \<name> ` (escaped), or `module _paramod_*_<name>(`
# (paramod-prefixed), so the old bare `grep "module <name> "` missed all of them.
BEGIN {
  want[1] = "mem_arbiter4"
  want[2] = "nano_golden_microop_controller"
  want[3] = "image_header_reader"
  want[4] = "logit_writer"
  want[5] = "nano_golden_datapath_fsm"
  want[6] = "nano_golden_decoder_top"
  n = 6
}
/^[ \t]*module[ \t]+/ {
  nm = $0
  sub(/^[ \t]*module[ \t]+/, "", nm)
  sub(/[ \t(].*$/, "", nm)
  sub(/^\\/, "", nm)
  for (i = 1; i <= n; i++) {
    m = want[i]
    if (nm == m) { found[m] = 1; continue }
    if (length(nm) > length(m)) {
      tail = substr(nm, length(nm) - length(m) + 1)
      if (tail == m && substr(nm, length(nm) - length(m), 1) == "_") found[m] = 1
    }
  }
}
END {
  for (i = 1; i <= n; i++)
    printf "STA_BODY_CHECK=%s=%s\n", want[i], (found[want[i]] ? "present" : "missing")
}
STA_BODY_CHECK_AWK_EOF
    retry_transport rsync -az "$LOCAL_RUN/sta_body_check.awk" "$REMOTE:$RROOT/runs/$RUN_ID/" || true
    # Round-84 (measurement completion, harness fix): storage (SRAM/ROM)
    # macro geometry from the blackbox DECLARATION.  The top_level_sta closure
    # blackboxes mla_kv_cache, so the closure netlist carries no $mem and a
    # targeted smoke that runs only that partition has no partition memory row
    # to sum -- sram_total_bits/sram_total_mib/macro_area_mm2 stayed 未测得.
    # The declaration still states the macro's size parameters, so the
    # capacity is read from it.  mla_kv_cache is the only memory-bearing
    # submodule in the golden closure:
    #   key_mem   [0:MAX_SEQ-1] of HEADS*KD*DATA_W bits
    #   value_mem [0:MAX_SEQ-1] of HEADS*DV*DATA_W bits
    #   storage bits = MAX_SEQ * HEADS * (KD + DV) * DATA_W
    # (245 760 bits for the RTL defaults -- the same value the cp_fsm partition
    # independently measures from the real mapped body, which is the
    # cross-check the report quotes).  The five size parameters are required
    # together, so a module declaration that does not carry them (e.g. a
    # Yosys export that dropped parameter declarations) yields NO line and the
    # caller keeps 未测得 -- no bit count is ever invented.  mla_attention_head
    # also declares MAX_SEQ/KD/DV/DATA_W but no HEADS, so it can never be
    # mistaken for this storage macro.
    cat > "$LOCAL_RUN/sta_storage_macro.awk" <<'STA_STORAGE_MACRO_AWK_EOF'
# Emit "mla_kv_cache<TAB>storage_bits" for the ONE storage macro in the
# closure blackbox declaration, or nothing at all when the declaration does
# not state every size parameter.  Read-only; never writes the design.
# Tolerant of both declaration styles Yosys may emit (legacy `module n(...);`
# with `parameter x = v;` in the body, and an ANSI `module n #(parameter ...)`
# header): the module line falls through to the same parameter scan.
function resolved(   i) {
  for (i = 1; i <= n; i++) if (val[i] == "") return 0
  return 1
}
function emit(   i) {
  if (!done && resolved()) {
    printf "mla_kv_cache\t%d\n", val[1] * val[2] * (val[3] + val[4]) * val[5]
    done = 1
  }
}
BEGIN { n = split("MAX_SEQ HEADS KD DV DATA_W", want, " "); in_mod = 0; done = 0 }
/^[ \t]*module[ \t]/ {
  nm = $2
  sub(/[#(].*$/, "", nm)
  sub(/^\\/, "", nm)
  if (nm == "mla_kv_cache" || nm ~ /_mla_kv_cache$/) {
    in_mod = 1
    for (i = 1; i <= n; i++) val[i] = ""
  } else {
    in_mod = 0
  }
  # no `next`: an ANSI header carries its parameters on this same line
}
in_mod && $1 == "endmodule" { emit(); in_mod = 0; next }
in_mod {
  for (i = 1; i <= n; i++) {
    if (val[i] != "") continue
    # Pad with one space so a parameter that starts a line still has a
    # non-word prefix (`[^A-Za-z0-9_]`); this avoids the non-portable
    # `(^|...)` alternation entirely.
    t = " " $0
    re = "[^A-Za-z0-9_]" want[i] "[[:space:]]*=[[:space:]]*[0-9]+"
    if (match(t, re)) {
      s = substr(t, RSTART, RLENGTH)
      sub(/^.*=[[:space:]]*/, "", s)
      val[i] = s + 0
    }
  }
}
END { if (in_mod) emit() }
STA_STORAGE_MACRO_AWK_EOF
    retry_transport rsync -az "$LOCAL_RUN/sta_storage_macro.awk" "$REMOTE:$RROOT/runs/$RUN_ID/" || true
    # Round-72: OpenROAD must see parser-safe declarations for compute modules
    # before their parameterized instances in the mapped closure.  The source
    # ppa_bb file is SystemVerilog; re-export it through Yosys as blackbox-only
    # Verilog, then prepend it to the normalized real closure.  This keeps
    # compute timing blackboxed without dropping any mapped control-plane body.
    NORM_CMD="mkdir -p build/sta_norm; if [ -f build/ppa_closure_top.v ]; then if [ -f build/ppa_bb_top_level_sta.v ]; then yosys -p \"read_verilog -sv -lib build/ppa_bb_top_level_sta.v; write_verilog -blackboxes build/sta_norm/ppa_sta_blackboxes.v\" > runs/$RUN_ID/sta_blackbox_export.log 2>&1; else : > build/sta_norm/ppa_sta_blackboxes.v; fi; awk -f runs/$RUN_ID/sta_paramod_norm.awk build/ppa_closure_top.v > build/sta_norm/ppa_sta_closure.v; awk -v bb_file=build/sta_norm/ppa_sta_blackboxes.v -f runs/$RUN_ID/sta_strip_bb_params.awk build/sta_norm/ppa_sta_closure.v > build/sta_norm/ppa_sta_closure_noparam.v; cat build/sta_norm/ppa_sta_blackboxes.v build/sta_norm/ppa_sta_closure_noparam.v > build/sta_norm/ppa_sta_all.v; wc -l build/ppa_closure_top.v 2>/dev/null | sed \"s/^/STA_NORM_SRC_LINES: /\" >> runs/$RUN_ID/yosys.log 2>/dev/null; else rm -f build/sta_norm/ppa_sta_all.v build/sta_norm/ppa_sta_closure.v build/sta_norm/ppa_sta_closure_noparam.v build/sta_norm/ppa_sta_blackboxes.v; fi"
    # Bounded like the STA step itself (not the 30 s generic-ssh ceiling):
    # a full 29-partition normalization can exceed 30 s on the multi-hundred-
    # MB mapped netlists.  Idempotent overwrite, so a transport retry cannot
    # duplicate synthesis or corrupt an original artifact.
    if retry_transport remote_ssh "$((STA_TIMEOUT_SECONDS + SSH_PARTITION_GRACE_SECONDS))" \
        "docker exec -w '$CONTAINER_WORKDIR' '$CONTAINER' sh -lc '$NORM_CMD'"; then
      echo "STA_PARAMOD_NORM=ok"
    else
      echo "STA_PARAMOD_NORM=failed" >&2
    fi
    # Round-61 (measurement completion, harness fix): the Round-60
    # "self-contained closure" assembly was falsified -- ppa_sta_all.v carries
    # ONLY the top glue; the five control-plane modules the top instantiates
    # directly (mem_arbiter4, nano_golden_microop_controller,
    # image_header_reader, logit_writer and the _paramod_*_nano_golden_datapath_fsm)
    # have NO body in the read set and NO Nangate45 LEF/liberty master, so
    # OpenROAD link_design raises `[ERROR ORD-2013] ... LEF master ... not
    # found` on the first one (instance arb).  The fix appends, into the SAME
    # single file link_design reads, `(* blackbox *)` timing-blackbox stubs for
    # exactly the instance types that are referenced but not declared.  This is
    # the identical mechanism the compute/SRAM leaves already use (write_verilog
    # -blackboxes markers), so link_design tolerates them as timing blackboxes
    # instead of missing-LEF leaves; it adds no fake timing arcs, and the STA
    # row can only cover top glue + blackbox boundaries.  Zero RTL change;
    # Round-64 reinstates the ppa_measure.ys body-assembly route as a single
    # whole-design write (build/ppa_closure_top.v, normalized above), so this
    # stub-append fix is now a FALLBACK that fires only for an instance type
    # still genuinely undefined after the merge.  sta_cp_bb.awk detects each
    # referenced-but-undefined control-plane instance type on the fly
    # (so the paramod hash is never hard-coded) and emits a stub whose module
    # name matches the normalized instance type verbatim.
    cat > "$LOCAL_RUN/sta_cp_bb.awk" <<'STA_CP_BB_AWK_EOF'
# Round-61: emit `(* blackbox *)` stub declarations for control-plane modules
# that ppa_sta_all.v references as cell types but never declares as modules.
# Reads ppa_sta_all.v, prints ONLY the missing stubs to stdout.
function emit_mem(t,   i) {
  print "(* blackbox *)"
  print "module " t " ("
  print "  input  clk,"
  print "  input  rst_n,"
  for (i = 0; i < 4; i++) {
    printf "  input  m%d_req_valid,\n", i
    printf "  input  m%d_req_write,\n", i
    printf "  input  [35:0]  m%d_req_addr,\n", i
    printf "  input  [127:0] m%d_req_wdata,\n", i
    printf "  input  [15:0]  m%d_req_wstrb,\n", i
    printf "  output m%d_req_ready,\n", i
    printf "  output m%d_rsp_valid,\n", i
    printf "  input  m%d_rsp_ready,\n", i
    printf "  output [127:0] m%d_rsp_rdata,\n", i
  }
  print "  output mem_req_valid,"
  print "  input  mem_req_ready,"
  print "  output mem_req_write,"
  print "  output [35:0]  mem_req_addr,"
  print "  output [127:0] mem_req_wdata,"
  print "  output [15:0]  mem_req_wstrb,"
  print "  input  mem_rsp_valid,"
  print "  output mem_rsp_ready,"
  print "  input  [127:0] mem_rsp_rdata"
  print ");"
  print "endmodule"
  print ""
}
function emit_ctrl(t) {
  print "(* blackbox *)"
  print "module " t " ("
  print "  input  clk,"
  print "  input  rst_n,"
  print "  input  start_run,"
  print "  input  [31:0] seq_len,"
  print "  output busy,"
  print "  output run_done,"
  print "  output token_read_valid,"
  print "  output [31:0] token_index,"
  print "  input  token_read_done,"
  print "  output begin_token,"
  print "  input  datapath_busy,"
  print "  input  datapath_token_done,"
  print "  input  datapath_error,"
  print "  output write_row_valid,"
  print "  output [31:0] write_token_index,"
  print "  output write_final_status,"
  print "  input  write_row_done,"
  print "  output error"
  print ");"
  print "endmodule"
  print ""
}
function emit_hdr(t) {
  print "(* blackbox *)"
  print "module " t " ("
  print "  input  clk,"
  print "  input  rst_n,"
  print "  input  start,"
  print "  output busy,"
  print "  output done,"
  print "  output error,"
  print "  output mem_req_valid,"
  print "  input  mem_req_ready,"
  print "  output [35:0]  mem_req_addr,"
  print "  input  mem_rsp_valid,"
  print "  output mem_rsp_ready,"
  print "  input  [127:0] mem_rsp_rdata,"
  print "  output [31:0]  n_layers,"
  print "  output [31:0]  d_model,"
  print "  output [31:0]  vocab,"
  print "  output [31:0]  max_seq,"
  print "  output [31:0]  seq_len,"
  print "  output [31:0]  group_size,"
  print "  output [31:0]  layer_type_bitmap,"
  print "  output [31:0]  descriptor_count,"
  print "  output [35:0]  descriptor_addr,"
  print "  output [35:0]  tokens_addr,"
  print "  output [35:0]  logits_addr,"
  print "  output [35:0]  argmax_addr,"
  print "  output [35:0]  status_addr,"
  print "  output [31:0]  logit_stride"
  print ");"
  print "endmodule"
  print ""
}
function emit_wr(t) {
  print "(* blackbox *)"
  print "module " t " ("
  print "  input  clk,"
  print "  input  rst_n,"
  print "  input  start_row,"
  print "  input  write_status_after_row,"
  print "  input  [35:0]  logits_base_addr,"
  print "  input  [35:0]  argmax_base_addr,"
  print "  input  [35:0]  status_addr,"
  print "  input  [31:0]  logit_row_stride,"
  print "  input  [31:0]  token_index,"
  print "  input  [16383:0] logits_bus,"
  print "  input  [31:0]  argmax_id,"
  print "  output busy,"
  print "  output row_done,"
  print "  output run_done,"
  print "  output mem_req_valid,"
  print "  input  mem_req_ready,"
  print "  output mem_req_write,"
  print "  output [35:0]  mem_req_addr,"
  print "  output [127:0] mem_req_wdata,"
  print "  output [15:0]  mem_req_wstrb"
  print ");"
  print "endmodule"
  print ""
}
function emit_dp(t) {
  print "(* blackbox *)"
  print "module " t " ("
  print "  input  clk,"
  print "  input  rst_n,"
  print "  input  begin_token,"
  print "  input  [8:0]  token_id,"
  print "  input  [5:0]  token_position,"
  print "  input  [35:0]  descriptor_base,"
  print "  input  [31:0]  descriptor_count,"
  print "  input  group_is_128,"
  print "  output busy,"
  print "  output token_done,"
  print "  output error,"
  print "  output [12287:0] logits_bus,"
  print "  output [8:0]  argmax_index,"
  print "  output desc_mem_req_valid,"
  print "  input  desc_mem_req_ready,"
  print "  output [35:0]  desc_mem_req_addr,"
  print "  input  desc_mem_rsp_valid,"
  print "  output desc_mem_rsp_ready,"
  print "  input  [127:0] desc_mem_rsp_rdata,"
  print "  output dma_mem_req_valid,"
  print "  input  dma_mem_req_ready,"
  print "  output [35:0]  dma_mem_req_addr,"
  print "  input  dma_mem_rsp_valid,"
  print "  output dma_mem_rsp_ready,"
  print "  input  [127:0] dma_mem_rsp_rdata"
  print ");"
  print "endmodule"
  print ""
}
{
  # module definition lines -> record declared module names
  s = $0
  sub(/^[ \t]+/, "", s)
  if (s ~ /^module[ \t]+/) {
    rest = s
    sub(/^module[ \t]+/, "", rest)
    nm = rest
    sub(/[ \t(].*$/, "", nm)
    sub(/^\\/, "", nm)
    defined[nm] = 1
    next
  }
  # A referenced-but-undefined control-plane cell type appears as a
  # whitespace-delimited token on a cell-instantiation line.  Field-scan the
  # whole line (format-independent: "<type> inst (" or "<type> inst(" both
  # work, and a token can never be an instance name here).
  nf = split(s, f, /[ \t]+/)
  for (i = 1; i <= nf; i++) {
    ty = f[i]
    sub(/^\\/, "", ty)
    base = ""
    if (ty == "mem_arbiter4") base = "mem_arbiter4"
    else if (ty == "nano_golden_microop_controller") base = "nano_golden_microop_controller"
    else if (ty == "image_header_reader") base = "image_header_reader"
    else if (ty == "logit_writer") base = "logit_writer"
    else if (ty ~ /^_paramod_[0-9a-fA-F]+_nano_golden_datapath_fsm$/) base = "nano_golden_datapath_fsm"
    else if (ty == "nano_golden_datapath_fsm") base = "nano_golden_datapath_fsm"
    if (base != "" && !(ty in declared) && !defined[ty]) {
      declared[ty] = base
    }
  }
}
END {
  n = 0
  for (ty in declared) {
    # References can occur before the real declaration later in the same
    # netlist.  Decide only at EOF, after every module body was observed.
    if (ty in defined) continue
    if (n++ == 0) print ""
    base = declared[ty]
    if (base == "mem_arbiter4") emit_mem(ty)
    else if (base == "nano_golden_microop_controller") emit_ctrl(ty)
    else if (base == "image_header_reader") emit_hdr(ty)
    else if (base == "logit_writer") emit_wr(ty)
    else if (base == "nano_golden_datapath_fsm") emit_dp(ty)
  }
}
STA_CP_BB_AWK_EOF
    retry_transport rsync -az "$LOCAL_RUN/sta_cp_bb.awk" "$REMOTE:$RROOT/runs/$RUN_ID/" || true
    # Append stubs to the SAME normalized netlist file the STA step reads.  The
    # append is idempotent per round (the file is re-created from
    # build/ppa_closure_top.v by NORM_CMD), best-effort, and does not change
    # the round exit code; a stub-generation failure leaves the previous
    # link_error evidence intact.
    CP_BB_CMD="if [ -f build/sta_norm/ppa_sta_all.v ]; then awk -f runs/$RUN_ID/sta_cp_bb.awk build/sta_norm/ppa_sta_all.v > build/sta_norm/sta_cp_bb_extra.v 2>/dev/null; if [ -s build/sta_norm/sta_cp_bb_extra.v ]; then cat build/sta_norm/sta_cp_bb_extra.v >> build/sta_norm/ppa_sta_all.v; echo STA_CP_BLACKBOX_STUBS_APPENDED=ok >> runs/$RUN_ID/yosys.log; else echo STA_CP_BLACKBOX_STUBS_APPENDED=none >> runs/$RUN_ID/yosys.log; fi; else echo STA_CP_BLACKBOX_STUBS_APPENDED=no_netlist >> runs/$RUN_ID/yosys.log; fi"
    retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
      "docker exec -w '$CONTAINER_WORKDIR' '$CONTAINER' sh -lc '$CP_BB_CMD'" 2>/dev/null \
      || true
    # Round-60 (harness repair): after the self-contained closure netlist is
    # assembled, verify the six control-plane module declarations actually
    # reached it.  A `stat -liberty` area row proves a body was MAPPED in
    # memory, NOT that `write_verilog` emitted it; and the old bare
    # `grep "module <name> "` missed `module <name>(`, escaped `module \<name>`
    # and `_paramod_*_<name>` forms.  sta_body_check.awk matches all three and
    # names exactly which body is present/missing so a link_design failure can
    # never be a bare ORD-2013 with no per-module evidence.
    BODY_CHECK_CMD="awk -f runs/$RUN_ID/sta_body_check.awk build/sta_norm/ppa_sta_all.v 2>/dev/null"
    STA_BODY_CHECK=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
      "docker exec -w '$CONTAINER_WORKDIR' '$CONTAINER' sh -lc '$BODY_CHECK_CMD'" 2>/dev/null || true)
    [[ -n "$STA_BODY_CHECK" ]] || STA_BODY_CHECK="STA_BODY_CHECK=unavailable"
    cat > "$LOCAL_RUN/ppa_sta_opensta.tcl" <<'PPA_STA_TCL_EOF'
set lib /opt/OpenROAD-flow-scripts/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib
read_liberty $lib
set all_net /work/k3harness/build/sta_norm/ppa_sta_all.v
if {![file exists $all_net]} {
  puts "STA_NO_COMBINED_NETLIST"
  exit 1
}
set mapped [list $all_net]
set sta_ok 0
# Round-54 (measurement completion, harness fix): ORD-2013 root cause.
# Round-53 evidence: every per-partition mapped netlist AND the closure
# netlist read cleanly (STA_READ_SKIP_COUNT=0); the ONLY failure was
# link_design -> [ERROR ORD-2013] instance arb LEF master mem_arbiter4 not
# found, although mem_arbiter4's real body IS inside the read set
# (ppa_mapped_cp_arbiter.v / ppa_mapped_cp_fsm.v) and no partition blackboxes
# mem_arbiter4, so no empty -blackboxes stub can shadow it.  Separate per-file
# read_verilog calls therefore do NOT merge into one linkable hierarchy:
# OpenROAD roots each read as its own block and treats a module referenced
# from another read's cone as an unlinked leaf needing a LEF master.  Fix: the
# harness now concatenates the normalized per-partition netlists and appends
# the normalized closure netlist LAST into ONE self-consistent hierarchy file
# (build/sta_norm/ppa_sta_all.v); this step reads that single file, so
# link_design resolves every instance to a body in the same elaboration.  The
# closure file is scanned below only to resolve the link target (cheap), not
# re-read.
if {[catch {read_verilog $all_net} read_err]} {
  puts "STA_READ_SKIP=$all_net :: $read_err"
} else {
  incr sta_ok
}
puts "STA_READ_OK_COUNT=$sta_ok"
set closure_net /work/k3harness/build/sta_norm/ppa_sta_all.v
if {![file exists $closure_net]} {
  puts "STA_NO_CLOSURE_NETLIST"
  exit 1
}
set closure_read_ok 1
# Round-53 (measurement completion, harness fix): after the $paramod$
# normalization (Round-52) the intended top module may no longer exist under
# the plain name nano_golden_decoder_top, so OpenROAD/OpenSTA fails
# Resolve the configured closure top in the normalized netlist, accepting both
# plain and escaped Yosys identifiers.  Do not fall back to the last declared
# module: that can be a compute-blackbox stub (for example swiglu_expert).
set sta_top "nano_golden_decoder_top"
set sta_top_found 0
set _fh [open $closure_net r]
while {[gets $_fh _line] >= 0} {
  if {![regexp {^[ \t]*module[ \t]+([^ \t(]+)} $_line -> _decl]} { continue }
  # Escaped Verilog identifiers end at whitespace.  OpenSTA uses the logical
  # name, so strip the leading escape before link_design.
  set _m $_decl
  if {[string index $_m 0] eq "\\"} { set _m [string range $_m 1 end] }
  if {$_m eq "nano_golden_decoder_top" || [string match "_paramod_*_nano_golden_decoder_top" $_m]} {
    set sta_top $_m
    set sta_top_found 1
    break
  }
}
close $_fh
if {!$sta_top_found} { set sta_top "not_found" }
puts "STA_CLOSURE_TOP_FOUND=[expr {$sta_top_found ? 1 : 0}]"
puts "STA_CLOSURE_TOP_NAME=$sta_top"
if {!$sta_top_found} {
  puts "STA_LINK_SKIP :: closure top nano_golden_decoder_top (or _paramod_*_nano_golden_decoder_top) not declared in normalized closure netlist"
  puts "STA_LINK_RESULT=error"
  puts "STA_READ_OK_COUNT=$sta_ok"
  puts "STA_READ_SKIP_COUNT=[expr {[llength $mapped] - $sta_ok}]"
  puts "STA_CLOSURE_READ_SKIP_COUNT=[expr {$closure_read_ok ? 0 : 1}]"
  puts "STA_CLOSURE_TOP_NAME=$sta_top"
  exit 3
}
if {$sta_top ne "nano_golden_decoder_top"} { puts "STA_CLOSURE_TOP_RENAMED=$sta_top" }
if {[catch {link_design $sta_top} _link_err]} {
  puts "STA_LINK_SKIP :: $_link_err"
  puts "STA_LINK_RESULT=error"
  puts "STA_READ_OK_COUNT=$sta_ok"
  puts "STA_READ_SKIP_COUNT=[expr {[llength $mapped] - $sta_ok}]"
  puts "STA_CLOSURE_READ_SKIP_COUNT=[expr {$closure_read_ok ? 0 : 1}]"
  puts "STA_CLOSURE_TOP_NAME=$sta_top"
  exit 3
}
# A linked top with no report_checks path is ambiguous: it can mean the clock
# was not created, all sequential logic was abstracted behind blackboxes, or
# there is genuinely no constrained register-to-register path.  Capture the
# inexpensive structural facts in THIS real STA process before asking a model
# to choose a repair.  `llength` is the verified native Tcl representation in
# this OpenROAD build; do not introduce unavailable Synopsys collection APIs.
if {[catch {
  set _sta_diag_clock_ports [get_ports clk]
  create_clock -period 10.0 -name clk $_sta_diag_clock_ports
  set _sta_diag_clocks [get_clocks *]
  set _sta_diag_registers [all_registers]
  set _sta_diag_data_inputs [all_inputs -no_clocks]
  puts "STA_DIAG_STATUS=ok"
  puts "STA_DIAG_CLOCK_PORT_COUNT=[llength $_sta_diag_clock_ports]"
  puts "STA_DIAG_CLOCK_COUNT=[llength $_sta_diag_clocks]"
  puts "STA_DIAG_REGISTER_COUNT=[llength $_sta_diag_registers]"
  puts "STA_DIAG_DATA_INPUT_COUNT=[llength $_sta_diag_data_inputs]"
} _sta_diag_err]} {
  puts "STA_DIAG_STATUS=error"
  puts "STA_DIAG_ERROR=$_sta_diag_err"
  puts "STA_LINK_SKIP :: post-link diagnostic setup failed :: $_sta_diag_err"
  puts "STA_LINK_RESULT=error"
  puts "STA_READ_OK_COUNT=$sta_ok"
  puts "STA_READ_SKIP_COUNT=[expr {[llength $mapped] - $sta_ok}]"
  puts "STA_CLOSURE_READ_SKIP_COUNT=[expr {$closure_read_ok ? 0 : 1}]"
  puts "STA_CLOSURE_TOP_NAME=$sta_top"
  exit 3
}
if {[catch {
  # OpenROAD/OpenSTA provides the native `all_inputs -no_clocks` selector.
  # This container lacks the whole Synopsys/DC collection-subtraction family
  # (every `*_collection` helper raises "invalid command name"), so exclude
  # clk via the native selector instead of subtracting a clock collection.
  set_input_delay -clock clk 2.0 [all_inputs -no_clocks]
  set_output_delay -clock clk 2.0 [all_outputs]
  catch { set_ideal_network [get_pins -hier -filter "is_blackbox"] }
  puts "STA_TIMING_BEGIN"
  # Round-75 (harness repair): this OpenSTA build removed the legacy
  # report_checks "worst-N-paths" flag; leaving it raises STA-0514 ("not a
  # known keyword or flag").  `-endpoint_count`/`-group_count` still parse but
  # are deprecated aliases, so use the current `-endpoint_path_count`/
  # `-group_path_count` names (identical in both templates).
  # Round-76 (measurement口径 fix, zero RTL): 8 paths per group (was 2) so the
  # local per-path attribution below has a real sample to split into credible
  # in-table paths and out-of-table extrapolation artifacts.  Option names and
  # the constraint set are unchanged; only the reported sample size grows.
  report_checks -path_delay max -endpoint_path_count 1 -group_path_count 8 -digits 3
  puts "STA_TIMING_END"
} _sta_err]} {
  puts "STA_LINK_SKIP :: post-link timing setup failed :: $_sta_err"
  puts "STA_LINK_RESULT=error"
  puts "STA_READ_OK_COUNT=$sta_ok"
  puts "STA_READ_SKIP_COUNT=[expr {[llength $mapped] - $sta_ok}]"
  puts "STA_CLOSURE_READ_SKIP_COUNT=[expr {$closure_read_ok ? 0 : 1}]"
  puts "STA_CLOSURE_TOP_NAME=$sta_top"
  exit 3
}
puts "STA_LINK_RESULT=ok"
puts "STA_READ_OK_COUNT=$sta_ok"
puts "STA_READ_SKIP_COUNT=[expr {[llength $mapped] - $sta_ok}]"
puts "STA_CLOSURE_READ_SKIP_COUNT=[expr {$closure_read_ok ? 0 : 1}]"
puts "STA_CLOSURE_TOP_NAME=$sta_top"
puts "STA_CLOSURE_TOP_FOUND=[expr {$sta_top_found ? 1 : 0}]"
# Round-77 (harness repair): STA_CLOSURE_TOP_FOUND and the STA_DIAG_* structural
# facts are emitted BEFORE report_checks in the body above, so they sit ahead of
# the report's per-arc output.  remote_yosys_round.sh builds STA_OUT from
# `tail -n 500` of sta.log; a report_checks block taller than that window drops
# every pre-report marker, so rtl-closure-governance sees
# sta_diag_status=未测得 -> no_timing_path_cause=diagnostic_missing even though
# the diagnostic ran.  Re-emit the same facts here, after the timing block, so
# they land inside the tail window and the audit can classify
# clock_port_missing / clock_not_created / no_registers_in_linked_netlist /
# registers_present_but_no_reported_path instead of diagnostic_missing.
puts "STA_DIAG_STATUS=ok"
puts "STA_DIAG_CLOCK_PORT_COUNT=[llength $_sta_diag_clock_ports]"
puts "STA_DIAG_CLOCK_COUNT=[llength $_sta_diag_clocks]"
puts "STA_DIAG_REGISTER_COUNT=[llength $_sta_diag_registers]"
puts "STA_DIAG_DATA_INPUT_COUNT=[llength $_sta_diag_data_inputs]"
# ---- Round-82 (measurement 口径 fix, zero RTL): FSM ABC-vs-STA reconciliation
# The per-partition ABC `stime` block reports a 195.026 ns delay for the mapped
# nano_golden_datapath_fsm, while this closure's report_checks showed only ~2 ns
# of in-table timing -- but the fsm's own timing was never measured, so the gap
# had no falsifiable evidence.  This block measures it on the SAME linked
# design: locate the fsm instances (path + ref_name), count their registers and
# how many are clocked by clk, print the clock table, then the whole-design
# clk-group worst path and (when fsm registers resolve) the
# fsm-endpoint-restricted worst path, each limited to ONE path with its
# per-stage cells so the digest can attribute every arc.  It sits AFTER the
# STA_TIMING block -- both timing awk parsers stop at the first STA_TIMING_END,
# so no existing STA field can change -- and every step is inside its own catch
# plus one bounded report per sub-block, so a failure here degrades to
# STA_FSM_STATUS=error / 未测得 instead of breaking the round.  Measurement
# scope stays the assembled PRE-LAYOUT PARTIAL_HIERARCHICAL closure netlist.
if {[catch {
  puts "STA_FSM_TIMING_BEGIN"
  set _fsm_cells {}
  set _fsm_locator "none"
  # `ref_name =~` is a glob in some OpenSTA builds and a regex in others, so try
  # both spellings and record the one that resolved; `*fsm*` is an invalid
  # regex and `.*fsm.*` matches no glob, so exactly one of them can be the
  # right flavour -- a rejected operand only empties that attempt's catch.
  foreach _pat [list "*fsm*" ".*fsm.*"] {
    set _c {}
    if {[catch { set _c [get_cells -hier -filter "ref_name =~ $_pat"] } _fsm_e]} { set _c {} }
    if {[llength $_c] > 0} { set _fsm_cells $_c; set _fsm_locator "ref_name_filter:$_pat"; break }
  }
  if {[llength $_fsm_cells] == 0} {
    set _c {}
    if {[catch { set _c [get_cells -hier *fsm*] } _fsm_e]} { set _c {} }
    if {[llength $_c] > 0} { set _fsm_cells $_c; set _fsm_locator "cell_name_glob" }
  }
  if {[llength $_fsm_cells] == 0} {
    # rtl/nano_golden_decoder_top.sv instantiates the fsm as `dp`; the mapped
    # closure keeps that instance name even when the module is parameterized.
    set _c {}
    if {[catch { set _c [get_cells -hier dp] } _fsm_e]} { set _c {} }
    if {[llength $_c] > 0} { set _fsm_cells $_c; set _fsm_locator "instance_name_dp" }
  }
  puts "STA_FSM_STATUS=ok"
  puts "STA_FSM_LOCATOR=$_fsm_locator"
  puts "STA_FSM_CELL_COUNT=[llength $_fsm_cells]"
  set _fsm_all {}
  set _fsm_dpins {}
  set _fsm_i 0
  foreach _fc [lrange $_fsm_cells 0 7] {
    incr _fsm_i
    set _fregs {}
    set _fdpins {}
    set _fclk {}
    catch { set _fregs [all_registers -cells [list $_fc]] }
    catch { set _fdpins [all_registers -cells [list $_fc] -data_pins] }
    catch { set _fclk [all_registers -cells [list $_fc] -clock clk] }
    set _fref "unknown"
    catch { set _fref [get_property ref_name $_fc] }
    set _fpath "unknown"
    catch { set _fpath [get_full_name $_fc] }
    puts "STA_FSM_CELL=index=$_fsm_i path=$_fpath ref=$_fref regs=[llength $_fregs] clk_regs=[llength $_fclk]"
    set _fsm_all [concat $_fsm_all $_fregs]
    set _fsm_dpins [concat $_fsm_dpins $_fdpins]
  }
  puts "STA_FSM_CLOSURE_REGISTER_COUNT=[llength $_sta_diag_registers]"
  set _fsm_clk_regs {}
  catch { set _fsm_clk_regs [all_registers -clock clk] }
  puts "STA_FSM_CLK_REGISTER_COUNT=[llength $_fsm_clk_regs]"
  puts "STA_FSM_REPORT_CLOCKS_BEGIN"
  catch { report_clocks } _fsm_clk_err
  puts "STA_FSM_REPORT_CLOCKS_END"
  puts "STA_FSM_PATH_STATUS=attempted"
  puts "STA_FSM_PATH_BEGIN"
  if {[catch { report_checks -path_delay max -path_group clk -endpoint_path_count 1 -group_path_count 1 -digits 3 } _fsm_p_err]} {
    # A rejected `-path_group` (or a group this build spells differently) must
    # not lose the single-path report entirely: retry once without the group
    # filter and record the fallback so the digest can name the group it got.
    if {[catch { report_checks -path_delay max -endpoint_path_count 1 -group_path_count 1 -digits 3 } _fsm_p_err2]} {
      puts "STA_FSM_PATH_STATUS=error"
      puts "STA_FSM_PATH_ERROR=$_fsm_p_err"
    } else {
      puts "STA_FSM_PATH_STATUS=attempted_group_fallback"
      puts "STA_FSM_PATH_GROUP_FALLBACK=$_fsm_p_err"
    }
  }
  puts "STA_FSM_PATH_END"
  if {[llength $_fsm_dpins] > 0 || [llength $_fsm_all] > 0} {
    set _fsm_tos $_fsm_all
    if {[llength $_fsm_dpins] > 0} { set _fsm_tos $_fsm_dpins }
    puts "STA_FSM_OWN_PATH_STATUS=attempted"
    puts "STA_FSM_OWN_PATH_BEGIN"
    if {[catch { report_checks -path_delay max -to $_fsm_tos -endpoint_path_count 1 -group_path_count 1 -digits 3 } _fsm_o_err]} {
      puts "STA_FSM_OWN_PATH_STATUS=error"
      puts "STA_FSM_OWN_PATH_ERROR=$_fsm_o_err"
    }
    puts "STA_FSM_OWN_PATH_END"
  } else {
    puts "STA_FSM_OWN_PATH_STATUS=no_fsm_registers"
  }
  puts "STA_FSM_TIMING_END"
} _fsm_err]} {
  puts "STA_FSM_STATUS=error"
  puts "STA_FSM_ERROR=$_fsm_err"
  puts "STA_FSM_TIMING_END"
}

# Round-85 (measurement 口径 fix, zero RTL): the STA_LINK_RESULT / read-count /
# closure-top / STA_DIAG_* markers are emitted before this template's report
# blocks, and the FSM report below prints one per-arc line per stage of the
# worst path (~hundreds of lines).  remote_yosys_round.sh captures the tail of
# sta.log, so a report taller than that window dropped every one of those
# markers: the last round reported sta_link_result / sta_read_ok_count /
# sta_closure_top_found as 未测得 although link and timing really ran.  Re-emit
# the same facts here, after the last report block, so they always land in the
# captured window.  Values are the already-measured ones; nothing is recomputed
# and no STA field can change (every parser takes the last occurrence).
puts "STA_LINK_RESULT=ok"
puts "STA_READ_OK_COUNT=$sta_ok"
puts "STA_READ_SKIP_COUNT=[expr {[llength $mapped] - $sta_ok}]"
puts "STA_CLOSURE_READ_SKIP_COUNT=[expr {$closure_read_ok ? 0 : 1}]"
puts "STA_CLOSURE_TOP_NAME=$sta_top"
puts "STA_CLOSURE_TOP_FOUND=[expr {$sta_top_found ? 1 : 0}]"
puts "STA_DIAG_STATUS=ok"
puts "STA_DIAG_CLOCK_PORT_COUNT=[llength $_sta_diag_clock_ports]"
puts "STA_DIAG_CLOCK_COUNT=[llength $_sta_diag_clocks]"
puts "STA_DIAG_REGISTER_COUNT=[llength $_sta_diag_registers]"
puts "STA_DIAG_DATA_INPUT_COUNT=[llength $_sta_diag_data_inputs]"
puts "STA_MARKERS_REEMIT=1"
exit
PPA_STA_TCL_EOF
    cat > "$LOCAL_RUN/ppa_sta_openroad.tcl" <<'PPA_STA_OPENROAD_TCL_EOF'
read_lef /opt/OpenROAD-flow-scripts/flow/platforms/nangate45/lef/NangateOpenCellLibrary.tech.lef
read_lef /opt/OpenROAD-flow-scripts/flow/platforms/nangate45/lef/NangateOpenCellLibrary.macro.lef
read_liberty /opt/OpenROAD-flow-scripts/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib
set all_net /work/k3harness/build/sta_norm/ppa_sta_all.v
if {![file exists $all_net]} {
  puts "STA_NO_COMBINED_NETLIST"
  exit 1
}
set mapped [list $all_net]
set sta_ok 0
# Round-54 (measurement completion, harness fix): ORD-2013 root cause.
# Round-53 evidence: every per-partition mapped netlist AND the closure
# netlist read cleanly (STA_READ_SKIP_COUNT=0); the ONLY failure was
# link_design -> [ERROR ORD-2013] instance arb LEF master mem_arbiter4 not
# found, although mem_arbiter4's real body IS inside the read set
# (ppa_mapped_cp_arbiter.v / ppa_mapped_cp_fsm.v) and no partition blackboxes
# mem_arbiter4, so no empty -blackboxes stub can shadow it.  Separate per-file
# read_verilog calls therefore do NOT merge into one linkable hierarchy:
# OpenROAD roots each read as its own block and treats a module referenced
# from another read's cone as an unlinked leaf needing a LEF master.  Fix: the
# harness now concatenates the normalized per-partition netlists and appends
# the normalized closure netlist LAST into ONE self-consistent hierarchy file
# (build/sta_norm/ppa_sta_all.v); this step reads that single file, so
# link_design resolves every instance to a body in the same elaboration.  The
# closure file is scanned below only to resolve the link target (cheap), not
# re-read.
if {[catch {read_verilog $all_net} read_err]} {
  puts "STA_READ_SKIP=$all_net :: $read_err"
} else {
  incr sta_ok
}
puts "STA_READ_OK_COUNT=$sta_ok"
set closure_net /work/k3harness/build/sta_norm/ppa_sta_all.v
if {![file exists $closure_net]} {
  puts "STA_NO_CLOSURE_NETLIST"
  exit 1
}
set closure_read_ok 1
# Round-53 (measurement completion, harness fix): after the $paramod$
# normalization (Round-52) the intended top module may no longer exist under
# the plain name nano_golden_decoder_top, so OpenROAD/OpenSTA fails
# Resolve the configured closure top in the normalized netlist, accepting both
# plain and escaped Yosys identifiers.  Do not fall back to the last declared
# module: that can be a compute-blackbox stub (for example swiglu_expert).
set sta_top "nano_golden_decoder_top"
set sta_top_found 0
set _fh [open $closure_net r]
while {[gets $_fh _line] >= 0} {
  if {![regexp {^[ \t]*module[ \t]+([^ \t(]+)} $_line -> _decl]} { continue }
  # Escaped Verilog identifiers end at whitespace.  OpenSTA uses the logical
  # name, so strip the leading escape before link_design.
  set _m $_decl
  if {[string index $_m 0] eq "\\"} { set _m [string range $_m 1 end] }
  if {$_m eq "nano_golden_decoder_top" || [string match "_paramod_*_nano_golden_decoder_top" $_m]} {
    set sta_top $_m
    set sta_top_found 1
    break
  }
}
close $_fh
if {!$sta_top_found} { set sta_top "not_found" }
puts "STA_CLOSURE_TOP_FOUND=[expr {$sta_top_found ? 1 : 0}]"
puts "STA_CLOSURE_TOP_NAME=$sta_top"
if {!$sta_top_found} {
  puts "STA_LINK_SKIP :: closure top nano_golden_decoder_top (or _paramod_*_nano_golden_decoder_top) not declared in normalized closure netlist"
  puts "STA_LINK_RESULT=error"
  puts "STA_READ_OK_COUNT=$sta_ok"
  puts "STA_READ_SKIP_COUNT=[expr {[llength $mapped] - $sta_ok}]"
  puts "STA_CLOSURE_READ_SKIP_COUNT=[expr {$closure_read_ok ? 0 : 1}]"
  puts "STA_CLOSURE_TOP_NAME=$sta_top"
  exit 3
}
if {$sta_top ne "nano_golden_decoder_top"} { puts "STA_CLOSURE_TOP_RENAMED=$sta_top" }
if {[catch {link_design $sta_top} _link_err]} {
  puts "STA_LINK_SKIP :: $_link_err"
  puts "STA_LINK_RESULT=error"
  puts "STA_READ_OK_COUNT=$sta_ok"
  puts "STA_READ_SKIP_COUNT=[expr {[llength $mapped] - $sta_ok}]"
  puts "STA_CLOSURE_READ_SKIP_COUNT=[expr {$closure_read_ok ? 0 : 1}]"
  puts "STA_CLOSURE_TOP_NAME=$sta_top"
  exit 3
}
if {[catch {
  set _sta_diag_clock_ports [get_ports clk]
  create_clock -period 10.0 -name clk $_sta_diag_clock_ports
  set _sta_diag_clocks [get_clocks *]
  set _sta_diag_registers [all_registers]
  set _sta_diag_data_inputs [all_inputs -no_clocks]
  puts "STA_DIAG_STATUS=ok"
  puts "STA_DIAG_CLOCK_PORT_COUNT=[llength $_sta_diag_clock_ports]"
  puts "STA_DIAG_CLOCK_COUNT=[llength $_sta_diag_clocks]"
  puts "STA_DIAG_REGISTER_COUNT=[llength $_sta_diag_registers]"
  puts "STA_DIAG_DATA_INPUT_COUNT=[llength $_sta_diag_data_inputs]"
} _sta_diag_err]} {
  puts "STA_DIAG_STATUS=error"
  puts "STA_DIAG_ERROR=$_sta_diag_err"
  puts "STA_LINK_SKIP :: post-link diagnostic setup failed :: $_sta_diag_err"
  puts "STA_LINK_RESULT=error"
  puts "STA_READ_OK_COUNT=$sta_ok"
  puts "STA_READ_SKIP_COUNT=[expr {[llength $mapped] - $sta_ok}]"
  puts "STA_CLOSURE_READ_SKIP_COUNT=[expr {$closure_read_ok ? 0 : 1}]"
  puts "STA_CLOSURE_TOP_NAME=$sta_top"
  exit 3
}
if {[catch {
  # Keep the standalone-STA and embedded-OpenSTA templates semantically
  # identical: both use `all_inputs -no_clocks` and never a collection command.
  set_input_delay -clock clk 2.0 [all_inputs -no_clocks]
  set_output_delay -clock clk 2.0 [all_outputs]
  catch { set_ideal_network [get_pins -hier -filter "is_blackbox"] }
  puts "STA_TIMING_BEGIN"
  # Round-75: keep the same current option names as the OpenSTA template above.
  # Round-76: 8 paths per group (was 2) for the same per-path attribution.
  report_checks -path_delay max -endpoint_path_count 1 -group_path_count 8 -digits 3
  puts "STA_TIMING_END"
} _sta_err]} {
  puts "STA_LINK_SKIP :: post-link timing setup failed :: $_sta_err"
  puts "STA_LINK_RESULT=error"
  puts "STA_READ_OK_COUNT=$sta_ok"
  puts "STA_READ_SKIP_COUNT=[expr {[llength $mapped] - $sta_ok}]"
  puts "STA_CLOSURE_READ_SKIP_COUNT=[expr {$closure_read_ok ? 0 : 1}]"
  puts "STA_CLOSURE_TOP_NAME=$sta_top"
  exit 3
}
puts "STA_LINK_RESULT=ok"
puts "STA_READ_OK_COUNT=$sta_ok"
puts "STA_READ_SKIP_COUNT=[expr {[llength $mapped] - $sta_ok}]"
puts "STA_CLOSURE_READ_SKIP_COUNT=[expr {$closure_read_ok ? 0 : 1}]"
puts "STA_CLOSURE_TOP_NAME=$sta_top"
puts "STA_CLOSURE_TOP_FOUND=[expr {$sta_top_found ? 1 : 0}]"
# Round-77 (harness repair): STA_CLOSURE_TOP_FOUND and the STA_DIAG_* structural
# facts are emitted BEFORE report_checks in the body above, so they sit ahead of
# the report's per-arc output.  remote_yosys_round.sh builds STA_OUT from
# `tail -n 500` of sta.log; a report_checks block taller than that window drops
# every pre-report marker, so rtl-closure-governance sees
# sta_diag_status=未测得 -> no_timing_path_cause=diagnostic_missing even though
# the diagnostic ran.  Re-emit the same facts here, after the timing block, so
# they land inside the tail window and the audit can classify
# clock_port_missing / clock_not_created / no_registers_in_linked_netlist /
# registers_present_but_no_reported_path instead of diagnostic_missing.
puts "STA_DIAG_STATUS=ok"
puts "STA_DIAG_CLOCK_PORT_COUNT=[llength $_sta_diag_clock_ports]"
puts "STA_DIAG_CLOCK_COUNT=[llength $_sta_diag_clocks]"
puts "STA_DIAG_REGISTER_COUNT=[llength $_sta_diag_registers]"
puts "STA_DIAG_DATA_INPUT_COUNT=[llength $_sta_diag_data_inputs]"
# ---- Round-82 (measurement 口径 fix, zero RTL): FSM ABC-vs-STA reconciliation
# The per-partition ABC `stime` block reports a 195.026 ns delay for the mapped
# nano_golden_datapath_fsm, while this closure's report_checks showed only ~2 ns
# of in-table timing -- but the fsm's own timing was never measured, so the gap
# had no falsifiable evidence.  This block measures it on the SAME linked
# design: locate the fsm instances (path + ref_name), count their registers and
# how many are clocked by clk, print the clock table, then the whole-design
# clk-group worst path and (when fsm registers resolve) the
# fsm-endpoint-restricted worst path, each limited to ONE path with its
# per-stage cells so the digest can attribute every arc.  It sits AFTER the
# STA_TIMING block -- both timing awk parsers stop at the first STA_TIMING_END,
# so no existing STA field can change -- and every step is inside its own catch
# plus one bounded report per sub-block, so a failure here degrades to
# STA_FSM_STATUS=error / 未测得 instead of breaking the round.  Measurement
# scope stays the assembled PRE-LAYOUT PARTIAL_HIERARCHICAL closure netlist.
if {[catch {
  puts "STA_FSM_TIMING_BEGIN"
  set _fsm_cells {}
  set _fsm_locator "none"
  # `ref_name =~` is a glob in some OpenSTA builds and a regex in others, so try
  # both spellings and record the one that resolved; `*fsm*` is an invalid
  # regex and `.*fsm.*` matches no glob, so exactly one of them can be the
  # right flavour -- a rejected operand only empties that attempt's catch.
  foreach _pat [list "*fsm*" ".*fsm.*"] {
    set _c {}
    if {[catch { set _c [get_cells -hier -filter "ref_name =~ $_pat"] } _fsm_e]} { set _c {} }
    if {[llength $_c] > 0} { set _fsm_cells $_c; set _fsm_locator "ref_name_filter:$_pat"; break }
  }
  if {[llength $_fsm_cells] == 0} {
    set _c {}
    if {[catch { set _c [get_cells -hier *fsm*] } _fsm_e]} { set _c {} }
    if {[llength $_c] > 0} { set _fsm_cells $_c; set _fsm_locator "cell_name_glob" }
  }
  if {[llength $_fsm_cells] == 0} {
    # rtl/nano_golden_decoder_top.sv instantiates the fsm as `dp`; the mapped
    # closure keeps that instance name even when the module is parameterized.
    set _c {}
    if {[catch { set _c [get_cells -hier dp] } _fsm_e]} { set _c {} }
    if {[llength $_c] > 0} { set _fsm_cells $_c; set _fsm_locator "instance_name_dp" }
  }
  puts "STA_FSM_STATUS=ok"
  puts "STA_FSM_LOCATOR=$_fsm_locator"
  puts "STA_FSM_CELL_COUNT=[llength $_fsm_cells]"
  set _fsm_all {}
  set _fsm_dpins {}
  set _fsm_i 0
  foreach _fc [lrange $_fsm_cells 0 7] {
    incr _fsm_i
    set _fregs {}
    set _fdpins {}
    set _fclk {}
    catch { set _fregs [all_registers -cells [list $_fc]] }
    catch { set _fdpins [all_registers -cells [list $_fc] -data_pins] }
    catch { set _fclk [all_registers -cells [list $_fc] -clock clk] }
    set _fref "unknown"
    catch { set _fref [get_property ref_name $_fc] }
    set _fpath "unknown"
    catch { set _fpath [get_full_name $_fc] }
    puts "STA_FSM_CELL=index=$_fsm_i path=$_fpath ref=$_fref regs=[llength $_fregs] clk_regs=[llength $_fclk]"
    set _fsm_all [concat $_fsm_all $_fregs]
    set _fsm_dpins [concat $_fsm_dpins $_fdpins]
  }
  puts "STA_FSM_CLOSURE_REGISTER_COUNT=[llength $_sta_diag_registers]"
  set _fsm_clk_regs {}
  catch { set _fsm_clk_regs [all_registers -clock clk] }
  puts "STA_FSM_CLK_REGISTER_COUNT=[llength $_fsm_clk_regs]"
  puts "STA_FSM_REPORT_CLOCKS_BEGIN"
  catch { report_clocks } _fsm_clk_err
  puts "STA_FSM_REPORT_CLOCKS_END"
  puts "STA_FSM_PATH_STATUS=attempted"
  puts "STA_FSM_PATH_BEGIN"
  if {[catch { report_checks -path_delay max -path_group clk -endpoint_path_count 1 -group_path_count 1 -digits 3 } _fsm_p_err]} {
    # A rejected `-path_group` (or a group this build spells differently) must
    # not lose the single-path report entirely: retry once without the group
    # filter and record the fallback so the digest can name the group it got.
    if {[catch { report_checks -path_delay max -endpoint_path_count 1 -group_path_count 1 -digits 3 } _fsm_p_err2]} {
      puts "STA_FSM_PATH_STATUS=error"
      puts "STA_FSM_PATH_ERROR=$_fsm_p_err"
    } else {
      puts "STA_FSM_PATH_STATUS=attempted_group_fallback"
      puts "STA_FSM_PATH_GROUP_FALLBACK=$_fsm_p_err"
    }
  }
  puts "STA_FSM_PATH_END"
  if {[llength $_fsm_dpins] > 0 || [llength $_fsm_all] > 0} {
    set _fsm_tos $_fsm_all
    if {[llength $_fsm_dpins] > 0} { set _fsm_tos $_fsm_dpins }
    puts "STA_FSM_OWN_PATH_STATUS=attempted"
    puts "STA_FSM_OWN_PATH_BEGIN"
    if {[catch { report_checks -path_delay max -to $_fsm_tos -endpoint_path_count 1 -group_path_count 1 -digits 3 } _fsm_o_err]} {
      puts "STA_FSM_OWN_PATH_STATUS=error"
      puts "STA_FSM_OWN_PATH_ERROR=$_fsm_o_err"
    }
    puts "STA_FSM_OWN_PATH_END"
  } else {
    puts "STA_FSM_OWN_PATH_STATUS=no_fsm_registers"
  }
  puts "STA_FSM_TIMING_END"
} _fsm_err]} {
  puts "STA_FSM_STATUS=error"
  puts "STA_FSM_ERROR=$_fsm_err"
  puts "STA_FSM_TIMING_END"
}

# Round-85 (measurement 口径 fix, zero RTL): same re-emission as the OpenSTA
# template above -- the pre-report STA_LINK_RESULT / read-count / closure-top /
# STA_DIAG_* markers must survive the bounded tail of sta.log that
# remote_yosys_round.sh captures, and the FSM per-arc report above is taller
# than that window.  Values are the already-measured ones; nothing is recomputed.
puts "STA_LINK_RESULT=ok"
puts "STA_READ_OK_COUNT=$sta_ok"
puts "STA_READ_SKIP_COUNT=[expr {[llength $mapped] - $sta_ok}]"
puts "STA_CLOSURE_READ_SKIP_COUNT=[expr {$closure_read_ok ? 0 : 1}]"
puts "STA_CLOSURE_TOP_NAME=$sta_top"
puts "STA_CLOSURE_TOP_FOUND=[expr {$sta_top_found ? 1 : 0}]"
puts "STA_DIAG_STATUS=ok"
puts "STA_DIAG_CLOCK_PORT_COUNT=[llength $_sta_diag_clock_ports]"
puts "STA_DIAG_CLOCK_COUNT=[llength $_sta_diag_clocks]"
puts "STA_DIAG_REGISTER_COUNT=[llength $_sta_diag_registers]"
puts "STA_DIAG_DATA_INPUT_COUNT=[llength $_sta_diag_data_inputs]"
puts "STA_MARKERS_REEMIT=1"
exit
PPA_STA_OPENROAD_TCL_EOF
    # The Tcl templates need container-absolute paths.  Keep them scoped to
    # this experiment instead of accidentally reading the original project's
    # shared build/ directory.
    sed -i.bak -e "s|/work/k3harness|$CONTAINER_WORKDIR|g" \
      -e "s|@STA_MAPPED_LIST@|$STA_MAPPED_LIST|g" \
      "$LOCAL_RUN/ppa_sta_opensta.tcl" "$LOCAL_RUN/ppa_sta_openroad.tcl"
    rm -f "$LOCAL_RUN/ppa_sta_opensta.tcl.bak" "$LOCAL_RUN/ppa_sta_openroad.tcl.bak"
    retry_transport rsync -az "$LOCAL_RUN/ppa_sta_opensta.tcl" "$LOCAL_RUN/ppa_sta_openroad.tcl" "$REMOTE:$RROOT/runs/$RUN_ID/" || true
    # Round-82 (measurement 口径 fix, zero RTL): bounded digest of the FSM
    # reconciliation block above.  The RAW per-stage dump of the fsm path is
    # ~hundreds of lines, so awk writes it to runs/<RUN_ID>/sta_fsm_path.log
    # inside the container (the raw dump the planner can re-read) and returns
    # only a bounded summary: one scalar per field, the COMPLETE per-stage arc
    # chain per path, and a first-seen-ordered cell-type histogram.  It reads
    # the immutable server-side sta.log -- STA is never re-run for the digest --
    # and an unreadable/absent block simply produces no field, so every
    # sta_fsm_* field stays 未测得 rather than restating an existing one.
    #
    # Round-85 (measurement 口径 fix, zero RTL): the per-stage chain used to be
    # capped at 10 lines by this awk (`nhead[pfx] < 10`), which is where the
    # "only the first 10 stages" window came from -- the STA Tcl already prints
    # every arc of the reported path.  The cap is now `maxchain` (1024) and the
    # digest emits STA_FSM_<P>_STAGE_HEAD_TRUNCATED so a chain longer than the
    # bound can never be read as a complete one.  The plan for this round is to
    # attribute the 29.821 ns clk-group path arc-by-arc, which needs the whole
    # chain, not a 10-arc sample.
    cat > "$LOCAL_RUN/sta_fsm_digest.awk" <<'STA_FSM_DIGEST_AWK_EOF'
# Round-82 (measurement 口径 fix, zero RTL): bounded digest of the FSM
# ABC-vs-STA reconciliation block the STA Tcl templates print into sta.log.
# Output contract: `STA_FSM_<FIELD>=<value>` scalars on stdout (all of them also
# emitted verbatim from the block's own marker lines), the raw block copied to
# the -v raw=<file> path, and nothing else.  `BLOCK_COMPLETE=0` marks a sub-block
# that never reached its END marker (a STA timeout mid-report), so bash can tell
# a truncated report from a complete one.
#
# Round-85: STAGE_HEAD carries the COMPLETE arc chain up to `maxchain` lines
# (was 10), and STAGE_HEAD_TRUNCATED=1 flags a chain that exceeded the bound, so
# a reader can always tell "complete" from "cut".
function q(v) { return (v == "" || v == "-") ? "-" : v }
function num(v) { return (v ~ /^-?[0-9]+([.][0-9]+)?$/) ? v : "" }
function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
function firsttok(s) { sub(/[ \t].*$/, "", s); return s }
function aftercolon(s,   t) { t = substr(s, index(s, ":") + 1); return firsttok(trim(t)) }
function note(line,   t, cell, key, d, ad) {
  if (pfx == "") return
  t = trim(line)
  if (nhead[pfx] < maxchain) { head[pfx] = (nhead[pfx] > 0) ? head[pfx] " | " t : t }
  nhead[pfx]++
  nstage[pfx]++
  d = num($1)
  if (d != "") {
    ad = (d + 0) < 0 ? -(d + 0) : (d + 0)
    if (maxarc[pfx] == "" || ad > (maxarc[pfx] + 0)) { maxarc[pfx] = d; maxpin[pfx] = $4 }
    if ((d + 0) < 0) neg[pfx]++
    if (ad >= oot_ns) oot[pfx]++
  }
  if (match(t, /\([A-Za-z_][A-Za-z0-9_]*\)$/)) {
    cell = substr(t, RSTART + 1, RLENGTH - 2)
    key = pfx SUBSEP cell
    if (!(key in cnt)) { order[pfx] = (length(order[pfx]) > 0) ? order[pfx] "," cell : cell }
    cnt[key]++
  }
}
function flush(   n, a, c, s, i) {
  if (pfx == "") return
  printf "STA_FSM_%s_BLOCK_COMPLETE=%d\n", pfx, (complete[pfx] + 0)
  printf "STA_FSM_%s_ARRIVAL_NS=%s\n", pfx, q(arr[pfx])
  printf "STA_FSM_%s_SLACK_NS=%s\n", pfx, q(slk[pfx])
  printf "STA_FSM_%s_START=%s\n", pfx, q(st[pfx])
  printf "STA_FSM_%s_END=%s\n", pfx, q(en[pfx])
  printf "STA_FSM_%s_GROUP=%s\n", pfx, q(grp[pfx])
  printf "STA_FSM_%s_STAGE_COUNT=%d\n", pfx, nstage[pfx] + 0
  printf "STA_FSM_%s_STAGE_HEAD_TRUNCATED=%d\n", pfx, ((nstage[pfx] + 0) > maxchain ? 1 : 0)
  printf "STA_FSM_%s_STAGE_HEAD=%s\n", pfx, q(head[pfx])
  printf "STA_FSM_%s_MAX_ARC_NS=%s\n", pfx, q(maxarc[pfx])
  printf "STA_FSM_%s_MAX_ARC_PIN=%s\n", pfx, q(maxpin[pfx])
  printf "STA_FSM_%s_NEG_ARC_COUNT=%d\n", pfx, neg[pfx] + 0
  printf "STA_FSM_%s_OUT_OF_TABLE_ARC_COUNT=%d\n", pfx, oot[pfx] + 0
  n = split(order[pfx], a, ",")
  s = ""
  for (i = 1; i <= n; i++) { c = a[i]; s = (i > 1) ? s "," c ":" cnt[pfx SUBSEP c] : c ":" cnt[pfx SUBSEP c] }
  printf "STA_FSM_%s_CELL_TYPES=%d\n", pfx, n
  printf "STA_FSM_%s_CELL_HISTO=%s\n", pfx, q(s)
  pfx = ""
}
BEGIN { oot_ns = 1000; maxchain = 1024; fsm_in = 0; pfx = ""; inclk = 0; nraw = 0; nclk = 0 }
$0 ~ /^STA_FSM_TIMING_BEGIN[ \t]*$/ { fsm_in = 1 }
fsm_in { if (raw != "" && nraw < 4000) { nraw++; rawline[nraw] = $0 } }
$0 ~ /^STA_FSM_TIMING_END[ \t]*$/ { flush(); fsm_in = 0 }
fsm_in && $0 ~ /^STA_FSM_REPORT_CLOCKS_BEGIN[ \t]*$/ { inclk = 1; next }
fsm_in && $0 ~ /^STA_FSM_REPORT_CLOCKS_END[ \t]*$/ { inclk = 0; next }
inclk { nclk++; if (nclk <= 8) { t = trim($0); if (t != "") clocks = (clocks == "") ? t : clocks " | " t } ; next }
fsm_in && $0 ~ /^STA_FSM_CELL=/ {
  print
  # `regs=` must not match the `regs=` inside `clk_regs=`, hence [^_] / a space.
  if (match($0, /[^_]regs=[0-9]+/)) { s = substr($0, RSTART + 6, RLENGTH - 6); cellregs += s }
  if (match($0, / clk_regs=[0-9]+/)) { s = substr($0, RSTART + 10, RLENGTH - 10); cellclk += s }
  next
}
fsm_in && $0 ~ /^STA_FSM_[A-Z_]+=/ { print; next }
fsm_in && $0 ~ /^STA_FSM_PATH_BEGIN[ \t]*$/ { pfx = "PATH"; next }
fsm_in && $0 ~ /^STA_FSM_PATH_END[ \t]*$/ { complete["PATH"] = 1; flush(); next }
fsm_in && $0 ~ /^STA_FSM_OWN_PATH_BEGIN[ \t]*$/ { pfx = "OWN_PATH"; next }
fsm_in && $0 ~ /^STA_FSM_OWN_PATH_END[ \t]*$/ { complete["OWN_PATH"] = 1; flush(); next }
fsm_in && pfx != "" {
  if ($0 ~ /^Startpoint:/) { st[pfx] = aftercolon($0); next }
  if ($0 ~ /^Endpoint:/) { en[pfx] = aftercolon($0); next }
  if ($0 ~ /^Path Group:/) { grp[pfx] = aftercolon($0); next }
  if ($0 ~ /data arrival time/) { if (arr[pfx] == "") arr[pfx] = num($1); next }
  if ($0 ~ / slack \(/) { if (slk[pfx] == "") slk[pfx] = num($1); next }
  if ($1 ~ /^-?[0-9]+([.][0-9]+)?$/ && $2 ~ /^-?[0-9]+([.][0-9]+)?$/ && ($3 == "^" || $3 == "v") && $4 ~ /\//) { note($0) }
  next
}
END {
  flush()
  printf "STA_FSM_REPORT_CLOCKS=%s\n", q(clocks)
  printf "STA_FSM_CELL_REG_TOTAL=%d\n", cellregs + 0
  printf "STA_FSM_CELL_CLK_REG_TOTAL=%d\n", cellclk + 0
  if (raw != "" && nraw > 0) { for (i = 1; i <= nraw; i++) print rawline[i] > raw }
}
STA_FSM_DIGEST_AWK_EOF
    retry_transport rsync -az "$LOCAL_RUN/sta_fsm_digest.awk" "$REMOTE:$RROOT/runs/$RUN_ID/" || true
    # Round-78 (harness repair): a `-group_path_count 8` report_checks block can
    # be taller than the `tail -n 500` window below, which dropped the
    # STA_TIMING_BEGIN marker and made the audit mislabel a fully linked,
    # clocked, register-rich design as ok_no_path.  Prepend the COMPLETE
    # STA_TIMING_BEGIN..END section (extracted from the immutable server-side
    # sta.log) before the bounded tail so the timing block is always
    # self-contained; both timing awk parsers keep only the first block.
    STA_OUT=$(retry_transport remote_ssh "$((STA_TIMEOUT_SECONDS + SSH_PARTITION_GRACE_SECONDS))" \
    "docker exec -w '$CONTAINER_WORKDIR' '$CONTAINER' sh -lc 'if command -v sta >/dev/null 2>&1; then timeout -k 10s ${STA_TIMEOUT_SECONDS}s sta -no_init -exit runs/$RUN_ID/ppa_sta_opensta.tcl > runs/$RUN_ID/sta.log 2>&1; rc=\$?; printf \"STA_BACKEND=sta\\nSTA_RC=%s\\n\" \"\$rc\"; elif command -v opensta >/dev/null 2>&1; then timeout -k 10s ${STA_TIMEOUT_SECONDS}s opensta -no_init -exit runs/$RUN_ID/ppa_sta_opensta.tcl > runs/$RUN_ID/sta.log 2>&1; rc=\$?; printf \"STA_BACKEND=opensta\\nSTA_RC=%s\\n\" \"\$rc\"; elif [ -x /opt/OpenROAD/build/bin/openroad ]; then timeout -k 10s ${STA_TIMEOUT_SECONDS}s /opt/OpenROAD/build/bin/openroad -no_init runs/$RUN_ID/ppa_sta_openroad.tcl > runs/$RUN_ID/sta.log 2>&1; rc=\$?; printf \"STA_BACKEND=openroad_opensta\\nSTA_RC=%s\\n\" \"\$rc\"; elif command -v openroad >/dev/null 2>&1; then timeout -k 10s ${STA_TIMEOUT_SECONDS}s openroad -no_init runs/$RUN_ID/ppa_sta_openroad.tcl > runs/$RUN_ID/sta.log 2>&1; rc=\$?; printf \"STA_BACKEND=openroad_opensta\\nSTA_RC=%s\\n\" \"\$rc\"; else printf \"STA_UNAVAILABLE\\n\"; fi; sed -n \"/STA_TIMING_BEGIN/,/STA_TIMING_END/p\" runs/$RUN_ID/sta.log 2>/dev/null; tail -n 500 runs/$RUN_ID/sta.log 2>/dev/null'" || true)
    if [[ -z "$STA_OUT" ]]; then
      STA_STATUS="unavailable"
      STA_ERROR="no output from sta step (transport failure or missing binary)"
    elif [[ "$STA_OUT" == *"STA_UNAVAILABLE"* ]]; then
      STA_STATUS="unavailable"
      STA_ERROR="no standalone STA or OpenROAD/OpenSTA backend found in container"
    else
      STA_RC=$(printf '%s\n' "$STA_OUT" | sed -n 's/.*STA_RC=\([0-9][0-9]*\).*/\1/p' | tail -1)
      STA_UNLINKED=$(printf '%s\n' "$STA_OUT" | grep -c 'not defined' 2>/dev/null || true)
      # Round-48 (measurement completion, harness fix): a mapped netlist that
      # OpenSTA/OpenROAD cannot parse (STA-0171 syntax error on a Yosys
      # write_verilog ROM-init `initial` block) previously aborted the read
      # loop, so link_design/report_checks never ran a real timing pass and the
      # harness mislabeled the result `ok_no_path` -- a READ FAILURE is not a
      # "no timing path" verdict.  Detect the read/parse error text plus the
      # per-file STA_READ_SKIP= markers the Tcl now emits when `catch` skips an
      # unparseable netlist, and classify it distinctly from a genuine no-path.
      # Round-50 (measurement completion, harness fix): capture the COMPLETE
      # failing-file set (every STA_READ_SKIP marker + its error line), not the
      # first five lines -- a third failing netlist's marker was previously
      # dropped by head-5, hiding which files still fail to parse.
      STA_READ_FAIL=$(printf '%s\n' "$STA_OUT" | grep -E 'STA-0171|syntax error|parse error|Cannot read|Error reading|Unable to read|STA_READ_SKIP=|STA_CLOSURE_READ_SKIP=' | head -100 || true)
      # Round-53 (measurement completion, harness fix): surface the raw read
      # counters and the resolved closure top, and detect a link_design
      # failure (ORD-2013 etc.) that the Tcl now emits as STA_LINK_SKIP /
      # STA_LINK_RESULT=error before exit 3.  A link failure must classify as
      # link_error/failed and can no longer fall through to ok_no_path.
      # Round-69: drop the bare ORD-[0-9]+ pattern -- the OpenROAD backend emits
      # the benign ORD-2056 "liberty cell without LEF master" warning on every
      # run, which previously forced a spurious link_error even when the link
      # and timing succeeded.  Real link failures are always emitted as
      # STA_LINK_SKIP / STA_LINK_RESULT=error by the Tcl's catch blocks.
      STA_LINK_FAIL=$(printf '%s\n' "$STA_OUT" | grep -E 'STA_LINK_SKIP|STA_LINK_RESULT=error|cannot link|can.t find top|Cannot find top|Unable to link' | head -30 || true)
      _v=$(printf '%s\n' "$STA_OUT" | grep -Eo 'STA_LINK_RESULT=(ok|error)' | tail -1 | sed 's/STA_LINK_RESULT=//'); STA_LINK_RESULT=${_v:-未测得}
      _v=$(printf '%s\n' "$STA_OUT" | sed -n 's/.*STA_CLOSURE_TOP_NAME=\([A-Za-z_][A-Za-z0-9_]*\).*/\1/p' | tail -1); STA_CLOSURE_TOP_NAME=${_v:-未测得}
      _v=$(printf '%s\n' "$STA_OUT" | grep -Eo 'STA_CLOSURE_TOP_FOUND=[01]' | tail -1 | sed 's/STA_CLOSURE_TOP_FOUND=//'); STA_CLOSURE_TOP_FOUND=${_v:-未测得}
      _v=$(printf '%s\n' "$STA_OUT" | sed -n 's/.*STA_READ_OK_COUNT=\([0-9][0-9]*\).*/\1/p' | tail -1); STA_READ_OK_COUNT=${_v:-未测得}
      _v=$(printf '%s\n' "$STA_OUT" | sed -n 's/.*STA_READ_SKIP_COUNT=\([0-9][0-9]*\).*/\1/p' | tail -1); STA_READ_SKIP_COUNT=${_v:-未测得}
      _v=$(printf '%s\n' "$STA_OUT" | sed -n 's/.*STA_CLOSURE_READ_SKIP_COUNT=\([0-9][0-9]*\).*/\1/p' | tail -1); STA_CLOSURE_READ_SKIP_COUNT=${_v:-未测得}
      _v=$(printf '%s\n' "$STA_OUT" | grep -Eo 'STA_DIAG_STATUS=(ok|error)' | tail -1 | sed 's/STA_DIAG_STATUS=//'); STA_DIAG_STATUS=${_v:-未测得}
      _v=$(printf '%s\n' "$STA_OUT" | sed -n 's/.*STA_DIAG_CLOCK_PORT_COUNT=\([0-9][0-9]*\).*/\1/p' | tail -1); STA_DIAG_CLOCK_PORT_COUNT=${_v:-未测得}
      _v=$(printf '%s\n' "$STA_OUT" | sed -n 's/.*STA_DIAG_CLOCK_COUNT=\([0-9][0-9]*\).*/\1/p' | tail -1); STA_DIAG_CLOCK_COUNT=${_v:-未测得}
      _v=$(printf '%s\n' "$STA_OUT" | sed -n 's/.*STA_DIAG_REGISTER_COUNT=\([0-9][0-9]*\).*/\1/p' | tail -1); STA_DIAG_REGISTER_COUNT=${_v:-未测得}
      _v=$(printf '%s\n' "$STA_OUT" | sed -n 's/.*STA_DIAG_DATA_INPUT_COUNT=\([0-9][0-9]*\).*/\1/p' | tail -1); STA_DIAG_DATA_INPUT_COUNT=${_v:-未测得}
      # Round-76: the linked read set is not width-consistent.  The Round-75 log
      # printed STA-0200 "port size N does not match net size M" on the closure's
      # parameterised compute-leaf instantiations (for example
      # `instance head_mm_g128 port q_bus size 16384 does not match net size
      # 131072`, `instance kda_b_mm port q_bus size 16384 ... net size 512`).
      # A mismatch means the reader binds pins of one width onto a net of
      # another, so per-net loads/aliases in that netlist cannot be trusted as
      # physical evidence.  Record the count plus one sample instead of hiding
      # it: it is part of why the closure timing stays a partial pre-layout
      # estimate.
      STA_NETLIST_PORT_MISMATCH_COUNT=$(printf '%s\n' "$STA_OUT" | grep -c 'STA-0200' 2>/dev/null || true)
      _v=$(printf '%s\n' "$STA_OUT" | sed -n 's/.*instance \([^ ]*\) port \([^ ]*\) size \([0-9][0-9]*\) does not match net size \([0-9][0-9]*\).*/instance=\1 port=\2 port_bits=\3 net_bits=\4/p' | head -1)
      STA_NETLIST_PORT_MISMATCH_SAMPLE=${_v:-未测得}
      if [[ "$STA_RC" == "0" ]]; then
        # OpenSTA puts measured values at the START of its report lines
        # (`1689.858 data arrival time`, `-1684.275 slack (VIOLATED)`).
        # `$NF` is prose and previously turned a real STA report into the
        # false verdict `ok_no_path`.
        # Round-76 (measurement口径 fix, zero RTL): attribute every reported
        # path to its own startpoint/endpoint/group and split the paths into
        # physically credible ones and out-of-table extrapolation artifacts.
        #
        # Round-75 evidence (real sta.log for run 02_yosys_ppa--20260909-194017):
        # both worst paths print single-arc increments of 1482.919 ns
        # (writer/_37622_/ZN, INV_X1), 169.582 ns (writer/_37885_/ZN, NOR2_X1)
        # and 32.959 ns (writer/_75389_/Q, DFFR_X1 clk->Q), plus a NEGATIVE arc
        # (-4.671 ns on writer/_37886_/ZN) and a -7.880 ns library setup time.
        # Nangate45's Liberty tables cannot express those values, so those arcs
        # are extrapolated load/slew points, not logic depth.  The underlying
        # netlist property is real and belongs to the mapping flow, not to the
        # RTL semantics: the closure-mapped logit_writer is 37,788 cells with 10
        # DFFR_X1 for a 128:1 x 128-bit output mux (rtl/logit_writer.sv
        # `logits_bus[beat*128 +: 128]`), i.e. the seven `beat` select nets alone
        # fan out to ~8k gate pins each, and no buffer tree is inserted anywhere
        # in this Yosys/ABC flow.  ABC reports the same pathology from the same
        # netlist (logit_writer ABC delay 37,474.02 ps, seq_pct 0.14 %).
        #
        # Policy: keep the artifact as reusable knowledge (net names, arcs,
        # fanout source) and report the largest *in-table* path as
        # `sta_crit_path_ps`.  Artifact predicates: arrival >= 1e6 ps (the same
        # non-physical bound the ABC wire-load口径 already filters) or any cell
        # arc >= 1000 ps or any negative arc (a standard cell cannot have a
        # negative delay).  This never turns a PARTIAL_HIERARCHICAL closure into
        # a chip Fmax: `sta_meets_10ns` keeps the raw unfiltered verdict and
        # `sta_status` stays `partial` whenever artifacts were seen.
        cat > "$LOCAL_RUN/sta_path_attr.awk" <<'STA_PATH_ATTR_EOF'
function q(v) { return (v == "") ? "-" : v }
function num(v) { return (v ~ /^-?[0-9]+([.][0-9]+)?$/) ? v : "" }
function firsttok(s) { sub(/[ ].*$/, "", s); return s }
function flush(  ) {
  if (!have) return
  np++
  cred = 0
  if (arrival != "" && (arrival + 0) < 1000) {
    if (maxabs == "" || (maxabs + 0) < 1.0) {
      if (neg == 0) cred = 1
    }
  }
  printf "path %d arrival_ns=%s slack_ns=%s group=%s setup_ns=%s max_arc_ns=%s max_arc_pin=%s negative_arcs=%d credible=%d start=%s end=%s\n", np, q(arrival), q(slack), q(group), q(setup), q(maxarc), q(maxpin), neg, cred, q(start), q(endp) > table
  if (arrival != "") {
    if (raw_max == "" || (arrival + 0) > (raw_max + 0)) raw_max = arrival
    if (slack != "" && (raw_min == "" || (slack + 0) < (raw_min + 0))) raw_min = slack
  }
  if (cred == 1) {
    ncred++
    if (cred_max == "" || (arrival + 0) > (cred_max + 0)) {
      cred_max = arrival; cred_start = start; cred_end = endp
      cred_group = group; cred_slack = slack
    }
    if (group == "clk") {
      nreg++
      if (reg_max == "" || (arrival + 0) > (reg_max + 0)) {
        reg_max = arrival; reg_start = start; reg_end = endp; reg_slack = slack
      }
    }
  } else {
    nart++
    if (arrival != "" && (art_max == "" || (arrival + 0) > (art_max + 0))) {
      art_max = arrival; art_start = start; art_end = endp; art_group = group
      art_setup = setup; art_arc = maxarc; art_pin = maxpin; art_neg = neg
    }
  }
  have = 0
}
BEGIN { in_t = 0; done = 0; post = 0; have = 0; np = 0; ncred = 0; nreg = 0; nart = 0; art_neg = 0; neg = 0 }
done { next }
/STA_TIMING_BEGIN/ { in_t = 1; next }
/STA_TIMING_END/ { flush(); in_t = 0; done = 1; next }
in_t == 0 { next }
/^Startpoint:/ {
  flush()
  have = 1; post = 0
  start = $0; sub(/^Startpoint: */, "", start); start = firsttok(start)
  endp = ""; group = ""; arrival = ""; slack = ""; setup = ""
  maxarc = ""; maxpin = ""; maxabs = ""; neg = 0
  next
}
/^Endpoint:/ { endp = $0; sub(/^Endpoint: */, "", endp); endp = firsttok(endp); next }
/^Path Group:/ { group = $0; sub(/^Path Group: */, "", group); group = firsttok(group); next }
/^Path Type:/ { next }
/data arrival time/ { if (arrival == "") arrival = num($1); post = 1; next }
/library (setup|recovery) time/ { setup = num($1); next }
# Round-81 (measurement 口径 fix, zero RTL): the pre-Round-81 `post == 1 { next }`
# guard sat directly after the arrival/setup rules, so it also swallowed
# OpenSTA's trailing `-1684.275   slack (VIOLATED)` line.  Every per-path
# `slack_ns` therefore stayed `-` (011543's sta_paths.txt: all 16 paths), which
# left `sta_crit_path_slack_ns`/`sta_credible_meets_10ns` permanently `未测得`
# and pushed the whole verdict onto the report's single minimum slack -- a value
# an out-of-table artifact (224144) or a boundary recovery path (011543) can
# dominate.  Parse the slack value here, with the same leading-value convention
# `parse_sta_timing.awk` already uses, BEFORE the catch-all guard.
/ slack \(/ { if (slack == "") slack = num($1); next }
post == 1 { next }
{
  if ($3 != "^" && $3 != "v") next
  pin = $4
  if (pin !~ /\//) next
  d = num($1)
  if (d == "") next
  if ((d + 0) < 0) neg++
  ad = (d + 0) < 0 ? -(d + 0) : (d + 0)
  if (maxabs == "" || ad > (maxabs + 0)) { maxabs = ad; maxarc = d; maxpin = pin }
  next
}
END {
  flush()
  printf "path_count=%d\n", np
  printf "credible_path_count=%d\n", ncred
  printf "credible_registered_path_count=%d\n", nreg
  printf "artifact_path_count=%d\n", nart
  printf "raw_max_arrival_ns=%s\n", q(raw_max)
  printf "raw_min_slack_ns=%s\n", q(raw_min)
  printf "credible_max_arrival_ns=%s\n", q(cred_max)
  printf "credible_max_arrival_start=%s\n", q(cred_start)
  printf "credible_max_arrival_end=%s\n", q(cred_end)
  printf "credible_max_arrival_group=%s\n", q(cred_group)
  printf "credible_max_arrival_slack_ns=%s\n", q(cred_slack)
  printf "credible_registered_max_arrival_ns=%s\n", q(reg_max)
  printf "credible_registered_max_arrival_start=%s\n", q(reg_start)
  printf "credible_registered_max_arrival_end=%s\n", q(reg_end)
  printf "credible_registered_max_arrival_slack_ns=%s\n", q(reg_slack)
  printf "artifact_max_arrival_ns=%s\n", q(art_max)
  printf "artifact_max_arrival_start=%s\n", q(art_start)
  printf "artifact_max_arrival_end=%s\n", q(art_end)
  printf "artifact_max_arrival_group=%s\n", q(art_group)
  printf "artifact_max_arrival_setup_ns=%s\n", q(art_setup)
  printf "artifact_max_arc_ns=%s\n", q(art_arc)
  printf "artifact_max_arc_pin=%s\n", q(art_pin)
  printf "artifact_negative_arcs=%d\n", art_neg + 0
}
STA_PATH_ATTR_EOF
        STA_PATH_ATTR=$(printf '%s\n' "$STA_OUT" | awk -v table="$LOCAL_RUN/sta_paths.txt" -f "$LOCAL_RUN/sta_path_attr.awk")
        _sta_attr() { printf '%s\n' "$STA_PATH_ATTR" | sed -n "s/^$1=//p" | tail -1; }
        _sta_known() { if [[ -n "$1" && "$1" != "-" ]]; then printf '%s' "$1"; else printf '%s' '未测得'; fi; }
        _sta_ps() { awk -v n="$1" 'BEGIN{printf "%.0f", n*1000}'; }
        STA_TIMING_FACTS=$(printf '%s\n' "$STA_OUT" | awk -f "$ROOT/harness/parse_sta_timing.awk")
        ARRIVAL_NS=$(printf '%s\n' "$STA_TIMING_FACTS" | sed -n 's/^arrival_ns=//p' | tail -1)
        SLACK_NS=$(printf '%s\n' "$STA_TIMING_FACTS" | sed -n 's/^slack_ns=//p' | tail -1)
        _v=$(printf '%s\n' "$STA_TIMING_FACTS" | sed -n 's/^path_count=\([0-9][0-9]*\)$/\1/p' | tail -1); STA_TIMING_PATH_COUNT=${_v:-未测得}
        if [[ "$ARRIVAL_NS" =~ ^[0-9.]+$ ]]; then
          # Round-76: `sta_crit_path_ps` is now the largest *credible* (in-table)
          # path arrival.  The unfiltered maximum that the pre-Round-76 harness
          # reported is preserved verbatim in `sta_raw_max_arrival_ps`, and the
          # out-of-table paths are recorded in `sta_artifact_*` so nothing is
          # hidden by the filter.
          STA_RAW_MAX_ARRIVAL_PS=$(_sta_ps "$ARRIVAL_NS")
          STA_RAW_MIN_SLACK_NS=$(_sta_known "$SLACK_NS")
          STA_CREDIBLE_PATH_COUNT=$(_sta_known "$(_sta_attr credible_path_count)")
          STA_ARTIFACT_PATH_COUNT=$(_sta_known "$(_sta_attr artifact_path_count)")
          _cpaths=$(_sta_attr credible_path_count)
          if [[ "$_cpaths" =~ ^[0-9]+$ ]] && (( _cpaths > 0 )); then
            _cns=$(_sta_attr credible_max_arrival_ns)
            STA_CRIT_PATH_PS=$(_sta_ps "$_cns")
            STA_CRIT_PATH_NS=$_cns
            STA_CRIT_PATH_START=$(_sta_known "$(_sta_attr credible_max_arrival_start)")
            STA_CRIT_PATH_END=$(_sta_known "$(_sta_attr credible_max_arrival_end)")
            STA_CRIT_PATH_GROUP=$(_sta_known "$(_sta_attr credible_max_arrival_group)")
            _cslack=$(_sta_attr credible_max_arrival_slack_ns)
            STA_CRIT_PATH_SLACK_NS=$(_sta_known "$_cslack")
            if [[ "$_cslack" =~ ^-?[0-9.]+$ ]]; then
              if awk -v s="$_cslack" 'BEGIN{exit !(s >= 0)}'; then STA_CREDIBLE_MEETS_10NS=yes; else STA_CREDIBLE_MEETS_10NS=no; fi
            fi
            # Round-81 (measurement 口径 fix, zero RTL): `sta_crit_path_class`
            # must describe the SAME path `sta_crit_path_ps`/`_group` report.
            # The pre-Round-81 predicate only asked whether ANY credible clk-group
            # path existed, so a closure whose credible worst path is an
            # asynchronous recovery path was still labelled `registered`
            # (011543: sta_crit_path_ps=2000 ps, group=asynchronous), while
            # `sta_est_freq_mhz` was derived from the separate clk-group path
            # (1.856 ns) -- one field claiming the worst path is registered, the
            # other silently describing a different path.  Derive the class from
            # the credible worst path's own group instead.
            _cgroup=$(_sta_attr credible_max_arrival_group)
            if [[ "$_cgroup" == "clk" ]]; then
              STA_CRIT_PATH_CLASS=registered
            else
              STA_CRIT_PATH_CLASS=non_registered
            fi
            # est_freq keeps the echo口径 "only from a credible clk-group
            # registered path, else 未测得"; the class label no longer overstates
            # it as the worst path.
            _rns=$(_sta_attr credible_registered_max_arrival_ns)
            if [[ -n "$_rns" && "$_rns" != "-" ]]; then
              STA_EST_FREQ_MHZ=$(awk -v n="$_rns" 'BEGIN{printf "%.1f", 1000/n}')
            fi
          elif [[ ! "$_cpaths" =~ ^[0-9]+$ ]]; then
            # Round-76 fail-safe: the per-path attribution produced no parseable
            # count (awk unavailable, or a report shape this parser does not
            # know).  Keep the pre-Round-76 semantics verbatim so the field can
            # never silently disappear; `sta_credible_path_count` stays 未测得.
            STA_CRIT_PATH_PS=$(_sta_ps "$ARRIVAL_NS")
            STA_CRIT_PATH_NS=$ARRIVAL_NS
            STA_EST_FREQ_MHZ=$(awk -v n="$ARRIVAL_NS" 'BEGIN{printf "%.1f", 1000/n}')
            STA_CRIT_PATH_CLASS=unattributed
          fi
          _apaths=$(_sta_attr artifact_path_count)
          if [[ "$_apaths" =~ ^[0-9]+$ ]] && (( _apaths > 0 )); then
            STA_UNBUFFERED_FANOUT_ARTIFACT=yes
            _ans=$(_sta_attr artifact_max_arrival_ns)
            if [[ -n "$_ans" && "$_ans" != "-" ]]; then STA_ARTIFACT_MAX_ARRIVAL_PS=$(_sta_ps "$_ans"); fi
            STA_ARTIFACT_MAX_ARRIVAL_START=$(_sta_known "$(_sta_attr artifact_max_arrival_start)")
            STA_ARTIFACT_MAX_ARRIVAL_END=$(_sta_known "$(_sta_attr artifact_max_arrival_end)")
            STA_ARTIFACT_MAX_ARRIVAL_GROUP=$(_sta_known "$(_sta_attr artifact_max_arrival_group)")
            STA_ARTIFACT_SETUP_NS=$(_sta_known "$(_sta_attr artifact_max_arrival_setup_ns)")
            _aarc=$(_sta_attr artifact_max_arc_ns)
            if [[ -n "$_aarc" && "$_aarc" != "-" ]]; then STA_ARTIFACT_WORST_ARC_PS=$(_sta_ps "$_aarc"); fi
            STA_ARTIFACT_WORST_ARC_PIN=$(_sta_known "$(_sta_attr artifact_max_arc_pin)")
            STA_ARTIFACT_NEG_ARC_COUNT=$(_sta_known "$(_sta_attr artifact_negative_arcs)")
          else
            STA_UNBUFFERED_FANOUT_ARTIFACT=no
            STA_ARTIFACT_PATH_COUNT=0
          fi
          STA_TIMING_MEASURED=yes
          if [[ "$SLACK_NS" =~ ^-?[0-9.]+$ ]]; then
            if awk -v s="$SLACK_NS" 'BEGIN{exit !(s >= 0)}'; then STA_MEETS_10NS=yes; else STA_MEETS_10NS=no; fi
          fi
          if [[ -n "$STA_READ_FAIL" ]]; then
            STA_STATUS="partial"
            STA_ERROR=$STA_READ_FAIL
          elif [[ "${STA_UNLINKED:-0}" -gt 0 ]]; then
            STA_STATUS="partial"
            STA_ERROR=$(printf '%s\n' "$STA_OUT" | grep -E 'not defined|unlinked' | head -3)
          elif [[ "${CLOSURE_HAS_BLACKBOX:-0}" == "1" ]]; then
            STA_STATUS="partial"
            STA_ERROR="top-level closure blackboxes compute modules (pre-layout partial closure, not a full no-blackbox Fmax)"
          elif [[ "$STA_UNBUFFERED_FANOUT_ARTIFACT" == "yes" ]]; then
            # Round-76: a reported path outside the Liberty tables is an
            # un-buffered-fanout extrapolation artifact, never a logic-depth
            # result, so it can never promote the closure to a clean `ok`.
            STA_STATUS="partial"
            STA_ERROR="report_checks reported out-of-table load/slew extrapolation on un-buffered high-fanout nets (see sta_artifact_*): pre-layout estimate, not a chip Fmax"
          else
            STA_STATUS="ok"
          fi
        else
          if [[ -n "$STA_READ_FAIL" ]]; then
            STA_STATUS="read_error"
            STA_ERROR=$STA_READ_FAIL
            if [[ -n "$STA_LINK_FAIL" ]]; then STA_ERROR="${STA_ERROR}"$'\n'"${STA_LINK_FAIL}"; fi
          elif [[ -n "$STA_LINK_FAIL" ]]; then
            STA_STATUS="link_error"
            STA_ERROR=$STA_LINK_FAIL
          else
            STA_STATUS="ok_no_path"
            STA_ERROR=$(printf '%s\n' "$STA_OUT" | grep -E 'no path|Error|error' | head -3)
          fi
        fi
      else
        if [[ -n "$STA_READ_FAIL" ]]; then
          STA_STATUS="read_error"
          STA_ERROR=$STA_READ_FAIL
          if [[ -n "$STA_LINK_FAIL" ]]; then STA_ERROR="${STA_ERROR}"$'\n'"${STA_LINK_FAIL}"; fi
        elif [[ -n "$STA_LINK_FAIL" ]]; then
          STA_STATUS="link_error"
          STA_ERROR=$STA_LINK_FAIL
        else
          STA_STATUS="failed"
          STA_ERROR=$(printf '%s\n' "$STA_OUT" | grep -E 'STA_NO|Error|error|not defined|unlinked|Unknown|Fatal' | head -5)
        fi
      fi
      # Round-82 (measurement 口径 fix, zero RTL): bounded FSM reconciliation
      # digest, read from the immutable server-side sta.log (STA is never
      # re-run for it) and mapped onto the sta_fsm_* fields.  Deliberately
      # single best-effort (NOT retry_transport): one lost channel must not
      # spend three more fetch budgets, and every unmapped field stays 未测得
      # while all pre-existing STA fields keep their values.  A "-" sentinel
      # from the digest (no value measured) maps to 未测得 too.
      if [[ "${CLOSURE_SCHEDULED:-0}" == "1" && "$STA_STATUS" != "unavailable" && "$STA_STATUS" != "not_attempted" ]]; then
        STA_FSM_DIGEST=$(remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
          "docker exec -w '$CONTAINER_WORKDIR' '$CONTAINER' sh -lc 'awk -v raw=$CONTAINER_WORKDIR/runs/$RUN_ID/sta_fsm_path.log -f runs/$RUN_ID/sta_fsm_digest.awk runs/$RUN_ID/sta.log 2>/dev/null'" 2>/dev/null || true)
      fi
      if [[ -n "$STA_FSM_DIGEST" ]]; then
        for _ff in STATUS LOCATOR CELL_COUNT CELL_REG_TOTAL CELL_CLK_REG_TOTAL CLOSURE_REGISTER_COUNT CLK_REGISTER_COUNT REPORT_CLOCKS PATH_STATUS PATH_BLOCK_COMPLETE PATH_ARRIVAL_NS PATH_SLACK_NS PATH_START PATH_END PATH_GROUP PATH_STAGE_COUNT PATH_CELL_TYPES PATH_CELL_HISTO PATH_STAGE_HEAD PATH_STAGE_HEAD_TRUNCATED PATH_MAX_ARC_NS PATH_MAX_ARC_PIN PATH_NEG_ARC_COUNT PATH_OUT_OF_TABLE_ARC_COUNT OWN_PATH_STATUS OWN_PATH_BLOCK_COMPLETE OWN_PATH_ARRIVAL_NS OWN_PATH_SLACK_NS OWN_PATH_START OWN_PATH_END OWN_PATH_GROUP OWN_PATH_STAGE_COUNT OWN_PATH_CELL_TYPES OWN_PATH_CELL_HISTO OWN_PATH_STAGE_HEAD OWN_PATH_STAGE_HEAD_TRUNCATED OWN_PATH_MAX_ARC_NS OWN_PATH_MAX_ARC_PIN OWN_PATH_NEG_ARC_COUNT OWN_PATH_OUT_OF_TABLE_ARC_COUNT ERROR; do
          _ffv=$(printf '%s\n' "$STA_FSM_DIGEST" | sed -n "s/^STA_FSM_${_ff}=//p" | tail -1)
          if [[ -z "$_ffv" || "$_ffv" == "-" ]]; then _ffv="未测得"; fi
          printf -v "STA_FSM_${_ff}" '%s' "$_ffv"
        done
        # Every per-instance locator row (bounded to 8 by the Tcl block), joined
        # into one field so the planner sees the instance paths and ref_names
        # that the locator actually resolved.
        STA_FSM_CELLS=$(printf '%s\n' "$STA_FSM_DIGEST" | awk '/^STA_FSM_CELL=/{sub(/^STA_FSM_CELL=/,""); s = (n++ > 0) ? s " | " $0 : $0} END { if (n > 0) print s }')
        [[ -n "$STA_FSM_CELLS" ]] || STA_FSM_CELLS="未测得"
      fi
    fi
    else
      if [[ "${CLOSURE_SCHEDULED:-0}" == "1" ]]; then
        # Round-86 (harness repair): the closure partition passed the budget
        # check and ran, but did NOT exit 0 (typically rc=124 tool timeout).
        # The measure-loop prologue rm -f'd build/ppa_closure_top.v and the
        # truncated partition never wrote a fresh one, so a post-partition STA
        # step would report STA_NO_COMBINED_NETLIST and the audit would
        # hard-gate it as closure_export_empty even though no closure synthesis
        # failed.  A truncated closure is resumable incomplete evidence, not a
        # closure export defect: report not_attempted so
        # closure_repair_required=false and the supervisor treats the round as
        # a measurement timeout.
        STA_STATUS="not_attempted"
        STA_ERROR="closure partition (top=nano_golden_decoder_top) ran but exited ${CLOSURE_PART_RC:-unknown}; no fresh combined STA netlist produced this round (timeout/truncation), not a closure export defect"
      else
        # Round-79: no closure partition ran this round, so there is no combined
        # netlist by design -- not a defect.  Report not_attempted so the audit
        # classifies it as no-closure (closure_repair_required=false) instead of
        # a spurious closure_export_empty from STA_NO_COMBINED_NETLIST.
        STA_STATUS="not_attempted"
        STA_ERROR="closure partition (top=nano_golden_decoder_top) not scheduled in this round; no combined STA netlist by design"
      fi
    fi
    fi
  fi
else
  ssh_with_wall_timeout "$((TIMEOUT_SECONDS + SSH_PARTITION_GRACE_SECONDS))" \
    ssh -n -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
    "$REMOTE" \
    "docker exec -w '$CONTAINER_WORKDIR' '$CONTAINER' sh -lc 'run=runs/$RUN_ID; mkdir -p \"\$run\" build; rm -f \"\$run/exit_code\" \"\$run/container.pid\"; timeout -k 10s ${TIMEOUT_SECONDS}s sh -lc '\"'\"'READFILES=\$(tr \"\\n\" \" \" < rtl/filelist.f); sed -e \"s|TOP|nano_golden_decoder_top|g\" -e \"s|LIB|/opt/OpenROAD-flow-scripts/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib|g\" -e \"s|@READFILES@|\$READFILES|g\" harness/ppa_${MODE}.ys > build/ppa_${MODE}.ys; exec yosys -s build/ppa_${MODE}.ys'\"'\"' > \"\$run/yosys.log\" 2>&1 & pid=\$!; printf \"%s\\n\" \"\$pid\" > \"\$run/container.pid\"; wait \"\$pid\"; rc=\$?; rm -f \"\$run/container.pid\"; printf \"%s\\n\" \"\$rc\" > \"\$run/exit_code\"; exit \"\$rc\"'"
  RC=$?
  PREFLIGHT_TIMED_OUT=$SSH_LAST_TIMED_OUT
fi
set -e

# The full log stays on the server. Recover the authoritative container exit
# code and only download a bounded tail, including after an SSH disconnect.
REMOTE_RC=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" "cat '$RROOT/runs/$RUN_ID/exit_code'" 2>/dev/null || true)
if [[ ( "$RC" == 255 || "${PREFLIGHT_TIMED_OUT:-0}" == 1 ) && ! "$REMOTE_RC" =~ ^[0-9]+$ ]]; then
  if [[ "$RC" == 255 ]]; then
    RECOVERY_DEADLINE=$((SECONDS + TIMEOUT_SECONDS + 20))
  else
    RECOVERY_DEADLINE=$((SECONDS + 90))
  fi
  while (( SECONDS < RECOVERY_DEADLINE )); do
    REMOTE_RC=$(remote_ssh 20 "cat '$RROOT/runs/$RUN_ID/exit_code' 2>/dev/null" 2>/dev/null || true)
    [[ "$REMOTE_RC" =~ ^[0-9]+$ ]] && break
    sleep 10
  done
fi
if [[ "$REMOTE_RC" =~ ^[0-9]+$ ]]; then
  RC=$REMOTE_RC
elif [[ "$RC" == 255 || "${PREFLIGHT_TIMED_OUT:-0}" == 1 ]]; then
  RC=$INFRA_EXIT
fi
retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "tail -n 400 '$RROOT/runs/$RUN_ID/yosys.log' 2>/dev/null" >"$LOCAL_RUN/yosys.log" 2>&1 || true
# ---- Round-80/85 (measurement 口径 fix, zero RTL): un-buffered-fanout audit --
# The artifact arcs were attributed to "un-buffered high-fanout nets" from a
# cell-count estimate, so the decisive property of that hypothesis was never
# measured.  This step measures it on the netlist OpenSTA actually read
# (build/sta_norm/ppa_sta_all.v): the driver instances named by this round's STA
# report are located in that netlist, the pins connected to the net each one
# drives are counted, the Nangate45 Liberty input-pin capacitance of those pins
# is summed, and that load is compared against the capacitance axis of the
# driver cell's own delay tables.  A load beyond the axis maximum means
# report_checks extrapolated the table -- an un-buffered-fanout mapping
# artifact, not logic depth; a load inside the table refutes the hypothesis.
# Round-80 fed it the artifact report's drivers (worst-arc pin + worst-path
# startpoint); Round-85 adds the worst clk-group path's largest-increment driver
# (sta_fsm_path_max_arc_pin) as a third, separately-labelled source so the same
# measurement can decide whether the 29.821 ns clk-group path is logic depth or
# a high-fanout artifact.  The artifact fields keep selecting only artifact
# sources and the clk-group result goes to its own sta_fsm_path_fanout_* fields,
# so a clk-group driver is never reported as an artifact driver.  Zero RTL
# change, no write to the design, measurement scope unchanged (assembled
# PRE-LAYOUT PARTIAL_HIERARCHICAL closure netlist), and every new field stays
# 未测得 unless the audit really resolved -- it can never restate an existing
# STA field.
FANOUT_ART_ENABLED=0
if [[ "$STA_ARTIFACT_PATH_COUNT" =~ ^[0-9]+$ ]] && (( STA_ARTIFACT_PATH_COUNT > 0 )); then FANOUT_ART_ENABLED=1; fi
FANOUT_FSM_WANTED=0
if [[ "$STA_FSM_PATH_MAX_ARC_PIN" != "未测得" && -n "$STA_FSM_PATH_MAX_ARC_PIN" ]]; then FANOUT_FSM_WANTED=1; fi
if [[ "$STA_TIMING_MEASURED" == "yes" ]] && (( FANOUT_ART_ENABLED == 1 || FANOUT_FSM_WANTED == 1 )); then
  if (( FANOUT_ART_ENABLED == 1 )); then STA_ARTIFACT_FANOUT_STATUS="not_run"; fi
  if (( FANOUT_FSM_WANTED == 1 )); then STA_FSM_PATH_FANOUT_STATUS="not_run"; fi
  # The driver is resolved through the real hierarchy, so the audit needs the
  # closure top name the STA actually linked (read from the same netlist).
  # Same allow-list rule as the source names: only characters the local shell
  # can hand to the container unchanged.
  FANOUT_TOP_NAME=""
  if [[ "$STA_CLOSURE_TOP_NAME" =~ ^[A-Za-z0-9_.]+$ ]]; then FANOUT_TOP_NAME=$STA_CLOSURE_TOP_NAME; fi
  FANOUT_SRC_ARGS=""
  FANOUT_SRC_SLOT=0
  FANOUT_ART_LABELS=""
  FANOUT_FSM_LABEL=""
  for _fanout_src in "$STA_ARTIFACT_WORST_ARC_PIN" "$STA_ARTIFACT_MAX_ARRIVAL_START" "$STA_FSM_PATH_MAX_ARC_PIN"; do
    if [[ "$_fanout_src" == "未测得" || -z "$_fanout_src" ]]; then continue; fi
    # Only a name the local shell can hand to the container unchanged is
    # accepted; a name with `$`, a quote or whitespace would be re-parsed by the
    # remote sh and could turn a read-only audit into an arbitrary command.
    if [[ ! "$_fanout_src" =~ ^[A-Za-z0-9_./]+$ ]]; then continue; fi
    # One slot per distinct name: the same driver can be named by both the
    # artifact report and the clk-group path, and auditing it twice would only
    # duplicate the row (its label then serves both purposes).
    case " $FANOUT_ART_LABELS $FANOUT_FSM_LABEL " in *" $_fanout_src "*) continue ;; esac
    if (( FANOUT_SRC_SLOT >= 3 )); then break; fi
    FANOUT_SRC_SLOT=$((FANOUT_SRC_SLOT + 1))
    FANOUT_SRC_ARGS="$FANOUT_SRC_ARGS -v src${FANOUT_SRC_SLOT}=${_fanout_src}"
    if (( FANOUT_FSM_WANTED == 1 )) && [[ "$_fanout_src" == "$STA_FSM_PATH_MAX_ARC_PIN" ]]; then
      FANOUT_FSM_LABEL=$_fanout_src
    else
      FANOUT_ART_LABELS="${FANOUT_ART_LABELS}${FANOUT_ART_LABELS:+ }${_fanout_src}"
    fi
  done
  if (( FANOUT_FSM_WANTED == 1 )) && [[ -z "$FANOUT_FSM_LABEL" ]]; then
    # The measured pin exists but its name cannot be handed to the container
    # unchanged: say so instead of leaving an unexplained 未测得.
    STA_FSM_PATH_FANOUT_STATUS="source_name_unsupported"
  fi
  if [[ -z "$FANOUT_TOP_NAME" || -z "$FANOUT_SRC_ARGS" ]]; then
    # Nothing to resolve: state why instead of leaving an unexplained 未测得.
    if (( FANOUT_ART_ENABLED == 1 )); then
      STA_ARTIFACT_FANOUT_STATUS="source_unavailable"
      STA_ARTIFACT_FANOUT_SOURCES="closure_top_name_or_artifact_source_name_unsupported"
    fi
    if (( FANOUT_FSM_WANTED == 1 )) && [[ "$STA_FSM_PATH_FANOUT_STATUS" == "not_run" ]]; then
      STA_FSM_PATH_FANOUT_STATUS="source_unavailable"
    fi
  else
    cat > "$LOCAL_RUN/sta_fanout_audit.awk" <<'STA_FANOUT_AUDIT_EOF'
# Round-80 (measurement 口径 fix, zero RTL): fanout/load audit of the artifact
# driver nets, on the SAME netlist OpenSTA read.  Read-only: nothing here writes
# to the netlist or the design.
#
#   -v netfile : assembled closure netlist OpenSTA read (scanned per hierarchy
#                level, so it is streamed several times, only one module body at
#                a time is ever parsed in detail)
#   -v capfile : Nangate45 typical Liberty (table axes + per-pin capacitance)
#   -v topname : the closure top the STA linked, as resolved from this netlist
#   -v src1/-v src2 : driver sources named by the artifact report, written as
#                     "instance/.../pin" relative to the closure top, e.g.
#                     "writer/_37622_/ZN" (an arc pin) or "writer/_75389_"
#                     (a path startpoint; the pin with the largest MEASURED
#                     fanout among that instance's connections is then chosen
#                     from the data instead of being assumed).
#   -v src3        : Round-85, the worst clk-group path's largest-increment
#                    driver pin (sta_fsm_path_max_arc_pin).  Same name form and
#                    same resolution as src1/src2; an absent src3 simply leaves
#                    that slot inactive, so a two-source run is unchanged.
#
# stdout, one record per line:
#   STAFANOUT       per source: status, driver cell, pin, net, fanout, load
#   STAFANOUT_LIB   per source: the capacitance axis of the driver cell's own
#                   delay tables and that axis' maximum value
#   STAFANOUT       once, with the module-block count of the netlist
# Unresolved pieces are reported as a non-ok status; the shell then keeps its
# own fields 未测得.

function stripbs(s) { if (substr(s, 1, 1) == "\\") s = substr(s, 2); return s }

function num(s) { return (s ~ /^-?[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)?$/) ? s + 0 : "" }

function base_of(s,   i) { i = index(s, "["); return (i > 1) ? substr(s, 1, i - 1) : s }

# first token of a netlist line, stopping at whitespace or "("
function tok(s,   i, c) {
  sub(/^[ \t]+/, "", s)
  for (i = 1; i <= length(s); i++) {
    c = substr(s, i, 1)
    if (c == " " || c == "\t" || c == "(") break
  }
  return substr(s, 1, i - 1)
}

# the remainder after that first token (keeps the "(" when it follows directly)
function rest_after_tok(s,   i, c) {
  sub(/^[ \t]+/, "", s)
  for (i = 1; i <= length(s); i++) {
    c = substr(s, i, 1)
    if (c == " " || c == "\t" || c == "(") break
  }
  return substr(s, i)
}

function inside_parens(l,   s) {
  if (index(l, "(") == 0) return ""
  s = l
  sub(/^[^(]*\(/, "", s)
  sub(/\).*$/, "", s)
  gsub(/[ \t"]/, "", s)
  return s
}

# module name of a `module <name> (` declaration line, unescaped
function mod_name(l,   s) {
  s = l
  sub(/^[ \t]*module[ \t]+/, "", s)
  sub(/[ \t(;].*$/, "", s)
  return stripbs(s)
}

# every ".<pin>(<net>)" connection of one cell line -> pin_name[]/pin_net[]
function note_pins(l,   rest, dot, paren, close, pin, net) {
  npin = 0
  rest = l
  while ((dot = index(rest, ".")) > 0) {
    rest = substr(rest, dot + 1)
    paren = index(rest, "(")
    if (paren == 0) break
    pin = substr(rest, 1, paren - 1)
    gsub(/[ \t]/, "", pin)
    rest = substr(rest, paren + 1)
    close = index(rest, ")")
    if (close == 0) break
    net = substr(rest, 1, close - 1)
    gsub(/[ \t]/, "", net)
    if (pin != "" && net != "") {
      npin++
      pin_name[npin] = pin
      pin_net[npin] = stripbs(net)
    }
    rest = substr(rest, close + 1)
  }
  return npin
}

# occurrences of one needle as a connection argument: "(n)", "(n," or "(n["
function count_needle(l, needle,   rest, pos, after, cnt, ln) {
  cnt = 0
  ln = length(needle)
  if (ln == 0) return 0
  rest = l
  while ((pos = index(rest, "(" needle)) > 0) {
    after = substr(rest, pos + 1 + ln, 1)
    if (after == ")" || after == "," || after == "[") cnt++
    rest = substr(rest, pos + 1)
  }
  return cnt
}

# connections to one net on a line: "(net)", "(net," or a bus bit "(net[".
# Both the plain and the Verilog-escaped spelling are counted (a netlist may
# write `.ZN(\net)`), and a positional connection list counts too, so an
# unmapped/positional cell cannot silently deflate the number.
function count_conn(l, net) {
  if (net == "") return 0
  return count_needle(l, net) + count_needle(l, "\\" net)
}

function maxnum(list,   n, arr, i, m, v) {
  n = split(list, arr, /[, \t]+/)
  m = ""
  for (i = 1; i <= n; i++) {
    v = num(arr[i])
    if (v != "" && (m == "" || v > m)) m = v
  }
  return m
}

BEGIN {
  # ---- pass 0: Liberty - per-pin capacitance, cell templates, cap axis ----
  lib_lines = 0
  cap_unit = ""
  cur_cell = ""; cur_pin = ""
  in_tpl = 0; tpl_name = ""; tpl_v1 = ""; tpl_v2 = ""; tpl_i1 = ""; tpl_i2 = ""
  while ((getline l < capfile) > 0) {
    lib_lines++
    if (in_tpl) {
      if (l ~ /^[ \t]*variable_1[ \t]*:/) {
        s = l; sub(/^[^:]*:[ \t]*/, "", s); sub(/[; \t].*$/, "", s); tpl_v1 = s
      } else if (l ~ /^[ \t]*variable_2[ \t]*:/) {
        s = l; sub(/^[^:]*:[ \t]*/, "", s); sub(/[; \t].*$/, "", s); tpl_v2 = s
      } else if (l ~ /^[ \t]*index_1[ \t]*\(/) {
        s = l; sub(/^[^"]*"/, "", s); sub(/".*$/, "", s); tpl_i1 = maxnum(s)
      } else if (l ~ /^[ \t]*index_2[ \t]*\(/) {
        s = l; sub(/^[^"]*"/, "", s); sub(/".*$/, "", s); tpl_i2 = maxnum(s)
      } else if (l ~ /^[ \t]*}/) {
        if (tpl_name != "") {
          if (tpl_v1 ~ /capacitance/) { tpl_cap[tpl_name] = tpl_i1; tpl_axis[tpl_name] = tpl_v1 }
          else if (tpl_v2 ~ /capacitance/) { tpl_cap[tpl_name] = tpl_i2; tpl_axis[tpl_name] = tpl_v2 }
        }
        in_tpl = 0; tpl_name = ""; tpl_v1 = ""; tpl_v2 = ""; tpl_i1 = ""; tpl_i2 = ""
      }
      continue
    }
    if (cap_unit == "" && index(l, "capacitive_load_unit") > 0) {
      u = inside_parens(l)
      n = split(u, ua, ",")
      cap_unit = (n >= 2) ? ((num(ua[1]) != "") ? ua[2] : ua[1]) : u
      continue
    }
    if (l ~ /^[ \t]*lu_table_template[ \t]*\(/) {
      tpl_name = inside_parens(l)
      in_tpl = 1; tpl_v1 = ""; tpl_v2 = ""; tpl_i1 = ""; tpl_i2 = ""
      continue
    }
    if (l ~ /^[ \t]*cell[ \t]*\(/) { cur_cell = inside_parens(l); cur_pin = ""; continue }
    if (l ~ /^[ \t]*pin[ \t]*\(/) { cur_pin = inside_parens(l); continue }
    if (cur_cell != "" && cur_pin != "") {
      # `capacitance` and `max_capacitance` are pin-scope attributes; the
      # rise_/fall_ variants have their own names and never match here.
      if (l ~ /^[ \t]*capacitance[ \t]*:/) {
        s = l; sub(/^[^:]*:[ \t]*/, "", s); sub(/[; \t].*$/, "", s)
        if (num(s) != "") cap[cur_cell SUBSEP cur_pin] = num(s)
        continue
      }
      if (l ~ /^[ \t]*max_capacitance[ \t]*:/) {
        s = l; sub(/^[^:]*:[ \t]*/, "", s); sub(/[; \t].*$/, "", s)
        if (num(s) != "") maxcap[cur_cell SUBSEP cur_pin] = num(s)
        continue
      }
    }
    if (cur_cell != "" && l ~ /^[ \t]*(cell_rise|cell_fall|rise_transition|fall_transition|rise_power|fall_power)[ \t]*\(/) {
      t = inside_parens(l)
      if (t != "" && t != cur_cell && index(" " cell_tpls[cur_cell] " ", " " t " ") == 0) {
        cell_tpls[cur_cell] = cell_tpls[cur_cell] " " t
      }
      continue
    }
  }

  # ---- sources: resolve each STA name through the netlist hierarchy ----
  # An STA name is a hierarchy path rooted at the closure top
  # ("writer/_37622_/ZN" = instance `writer` of the top, cell `_37622_` inside
  # that instance's module, pin ZN).  This netlist is an assembly of separately
  # mapped partition bodies, so Yosys' auto names (`_NNNNN_`) repeat across
  # modules: a bare instance-token search could match an unrelated cell in
  # another module and report a meaningless fanout as evidence.  Every
  # component is therefore resolved through the real hierarchy -- the top
  # module by name, then each instance's TYPE, then only that module body is
  # searched for the driver cell.
  ns = 0
  nsrc = 3
  for (k = 1; k <= nsrc; k++) {
    raw = (k == 1) ? src1 : ((k == 2) ? src2 : src3)
    srclabel[k] = raw
    active[k] = 0
    if (raw == "") continue
    s = stripbs(raw)
    nf = split(s, parts, "/")
    # Round-85: the deepest component is the pin when the name has at least
    # three components (an STA pin path); a shorter name is an instance path
    # whose pin is chosen from the data by measured fanout.  Every component
    # above the pin is one instance level, and the hierarchy walk below resolves
    # them one at a time through the real module types, so a driver inside an
    # expanded blackbox (deeper than the previous fixed two-level walk) now
    # resolves instead of failing.  Names with three components or fewer keep
    # exactly the previous interpretation.
    spin[k] = (nf >= 3) ? parts[nf] : ""
    nchain[k] = (nf >= 3) ? (nf - 1) : nf
    for (lv = 1; lv <= nchain[k]; lv++) chain[k, lv] = parts[lv]
    if (chain[k, nchain[k]] == "") continue
    active[k] = 1
    ns++
  }
  if (ns == 0) { print "STAFANOUT src=none status=no_source"; exit }

  # ---- hierarchy resolution (one scan per component, cached across sources) ----
  for (k = 1; k <= nsrc; k++) {
    if (!active[k]) continue
    want = stripbs(topname)
    if (want == "") { chain_err[k] = "no_top_name"; continue }
    for (lv = 1; lv <= nchain[k]; lv++) {
      ckey = want SUBSEP chain[k, lv]
      if (ckey in res_type) {
        if (lv == nchain[k]) {
          drvline[k] = res_line[ckey]; drvcell[k] = res_type[ckey]
          drvblock[k] = res_block[ckey]; drv_matches[k] = res_matches[ckey]; drvmod[k] = want
        } else {
          want = res_type[ckey]
        }
        continue
      }
      first_line = ""; first_type = ""; first_block = 0; nmatch = 0
      nblocks = 0; curmod = ""
      while ((getline l < netfile) > 0) {
        # The `index` test rejects almost every line before the regexes run:
        # this netlist is multi-million-line, and the hierarchy walk scans it
        # once per STA-name component.
        if (index(l, "module") > 0) {
          if (l ~ /^[ \t]*module[ \t]/) { nblocks++; curmod = mod_name(l); continue }
          if (l ~ /^[ \t]*endmodule/) continue
        }
        if (curmod != want) continue
        if (index(l, "(") == 0) continue
        t1 = stripbs(tok(l))
        t2 = stripbs(tok(rest_after_tok(l)))
        if (t1 == "" || t2 == "" || t2 != chain[k, lv]) continue
        if (t1 ~ /^(assign|wire|reg|input|output|inout|parameter|localparam|integer|initial|always|generate|genvar|defparam|specify|function|task)$/) continue
        nmatch++
        if (first_line == "") { first_line = l; first_type = t1; first_block = nblocks }
      }
      close(netfile)
      if (first_line == "") { chain_err[k] = "level" lv; chain_err_mod[k] = want; break }
      res_type[ckey] = first_type; res_line[ckey] = first_line
      res_block[ckey] = first_block; res_matches[ckey] = nmatch
      if (lv == nchain[k]) {
        drvline[k] = first_line; drvcell[k] = first_type
        drvblock[k] = first_block; drv_matches[k] = nmatch; drvmod[k] = want
      } else {
        want = first_type
      }
    }
  }

  # ---- candidate driver nets of each located instance ----
  for (k = 1; k <= nsrc; k++) {
    ncand[k] = 0
    if (!active[k] || drvblock[k] == 0) continue
    n = note_pins(drvline[k])
    for (i = 1; i <= n; i++) {
      if (spin[k] != "") {
        if (pin_name[i] != spin[k]) continue
      } else if (pin_name[i] ~ /^(CK|CLK|CKN|CLKN|CLKIN|GCLK)$/) {
        # A clock pin drives the clock net (fanout = every flop in the
        # module), which is not the artifact's data net.  An unnamed source
        # (a report startpoint) is asked about its DATA net, so clock pins are
        # skipped; nothing is hidden -- the skipped count and every candidate
        # are printed, and an explicitly named pin is always honoured.
        clk_cand[k]++
        continue
      }
      ncand[k]++
      candnet[k, ncand[k]] = pin_net[i]
      candbase[k, ncand[k]] = base_of(pin_net[i])
      candpin[k, ncand[k]] = pin_name[i]
    }
  }

  # ---- pass 2: connections and summed pin capacitance, per candidate net ----
  nblocks = 0
  while ((getline l < netfile) > 0) {
    if (index(l, "module") > 0) {
      if (l ~ /^[ \t]*module[ \t]/) { nblocks++; continue }
      if (l ~ /^[ \t]*endmodule/) continue
    }
    hit = 0
    for (k = 1; k <= nsrc; k++) { if (active[k] && drvblock[k] == nblocks) hit = 1 }
    if (!hit) continue
    ctype = stripbs(tok(l))
    if (ctype == "" || index(l, "(") == 0) continue
    n = note_pins(l)
    for (i = 1; i <= n; i++) {
      pn = pin_name[i]
      cappin = cap[ctype SUBSEP pn]
      if (cappin == "") cappin = cap[ctype SUBSEP base_of(pn)]
      for (k = 1; k <= nsrc; k++) {
        if (!active[k] || drvblock[k] == 0) continue
        for (c = 1; c <= ncand[k]; c++) {
          if (candnet[k, c] == "") continue
          if (pin_net[i] == candnet[k, c] || substr(pin_net[i], 1, length(candbase[k, c]) + 1) == candbase[k, c] "[") {
            if (cappin != "") { load[k, c] += cappin; rpins[k, c]++ } else { upins[k, c]++ }
          }
        }
      }
    }
    for (k = 1; k <= nsrc; k++) {
      if (!active[k] || drvblock[k] == 0) continue
      for (c = 1; c <= ncand[k]; c++) {
        if (candnet[k, c] != "") conn[k, c] += count_conn(l, candnet[k, c])
      }
    }
  }

  # ---- report ----
  print "STAFANOUT status=ok netlist_module_blocks=" nblocks " lib_lines=" lib_lines
  for (k = 1; k <= nsrc; k++) {
    if (!active[k]) continue
    if (drvblock[k] == 0) {
      printf "STAFANOUT src=%s status=chain_unresolved reason=%s missing_in=%s\n", srclabel[k], (chain_err[k] == "" ? "source_not_found" : chain_err[k]), (chain_err_mod[k] == "" ? "-" : chain_err_mod[k])
      continue
    }
    if (ncand[k] == 0) {
      printf "STAFANOUT src=%s status=%s module=%s blocks=%d cell=%s clock_pins_skipped=%d\n", srclabel[k], (clk_cand[k] > 0 ? "clock_pins_only" : "pin_not_found"), drvmod[k], drv_matches[k] + 0, drvcell[k], clk_cand[k] + 0
      continue
    }
    best = 0
    for (c = 1; c <= ncand[k]; c++) {
      if (candnet[k, c] == "") continue
      if (best == 0 || conn[k, c] > conn[k, best]) best = c
    }
    if (best == 0) { printf "STAFANOUT src=%s status=no_candidate_net\n", srclabel[k]; continue }
    fan = conn[k, best] - 1
    if (fan < 0) fan = 0
    printf "STAFANOUT src=%s status=ok module=%s blocks=%d cell=%s pin=%s net=%s candidates=%d clock_pins_skipped=%d connections=%d fanout=%d load=%.6f load_resolved_pins=%d load_unresolved_pins=%d\n", srclabel[k], drvmod[k], drv_matches[k] + 0, drvcell[k], candpin[k, best], candnet[k, best], ncand[k], clk_cand[k] + 0, conn[k, best] + 0, fan, load[k, best] + 0, rpins[k, best] + 0, upins[k, best] + 0
    details = ""
    for (c = 1; c <= ncand[k]; c++) {
      if (candnet[k, c] == "") continue
      if (c > 12) { details = details ",..."; break }
      details = details ((details == "") ? "" : ",") candpin[k, c] ":" candnet[k, c] ":" (conn[k, c] + 0)
    }
    printf "STAFANOUT_CAND src=%s cell=%s pin_net_connections=%s\n", srclabel[k], drvcell[k], details
    t = ""
    m = split(cell_tpls[drvcell[k]], tl, /[ \t]+/)
    for (i = 1; i <= m; i++) {
      if (tl[i] != "" && tpl_cap[tl[i]] != "") { t = tl[i]; break }
    }
    pmax = maxcap[drvcell[k] SUBSEP candpin[k, best]]
    printf "STAFANOUT_LIB src=%s status=%s cell=%s template=%s cap_axis=%s max_cap=%s cap_unit=%s pin=%s pin_max_cap=%s\n", srclabel[k], (t == "" ? "unresolved" : "ok"), drvcell[k], (t == "" ? "-" : t), (t == "" ? "-" : tpl_axis[t]), (t == "" ? "-" : tpl_cap[t]), (cap_unit == "" ? "-" : cap_unit), candpin[k, best], (pmax == "" ? "-" : pmax)
  }
  exit
}
STA_FANOUT_AUDIT_EOF
    retry_transport rsync -az "$LOCAL_RUN/sta_fanout_audit.awk" "$REMOTE:$RROOT/runs/$RUN_ID/" >/dev/null 2>&1 || true
    FANOUT_OUT=$(remote_ssh "$FANOUT_AUDIT_TIMEOUT_SECONDS" \
      "docker exec -w '$CONTAINER_WORKDIR' '$CONTAINER' sh -lc 'net=build/sta_norm/ppa_sta_all.v; lib=/opt/OpenROAD-flow-scripts/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib; if [ -f \"\$net\" ]; then awk -v netfile=\"\$net\" -v capfile=\"\$lib\" -v topname=$FANOUT_TOP_NAME$FANOUT_SRC_ARGS -f runs/$RUN_ID/sta_fanout_audit.awk /dev/null; else printf \"STAFANOUT src=none status=no_netlist\\n\"; fi'" 2>/dev/null || true)
    FANOUT_LINES=$(printf '%s\n' "$FANOUT_OUT" | grep '^STAFANOUT' || true)
    # Round-85: the artifact field carries ONLY the artifact-eligible sources'
    # rows (the report header row has no src= and is kept), so the clk-group
    # source added this round cannot be read as an artifact source.  When no
    # artifact source exists the field keeps its 未测得 default.
    if (( FANOUT_ART_ENABLED == 1 )); then
      FANOUT_LINES_ART=$(printf '%s\n' "$FANOUT_LINES" | awk -v keep="$FANOUT_ART_LABELS" 'BEGIN{n=split(keep,K," ")} {s=""; for(i=1;i<=NF;i++) if (substr($i,1,4)=="src=") s=substr($i,5); if (s=="") {print; next} for(j=1;j<=n;j++) if (K[j]!="" && s==K[j]) {print; next}}')
      if [[ -n "$FANOUT_LINES_ART" ]]; then
        STA_ARTIFACT_FANOUT_SOURCES=$(printf '%s\n' "$FANOUT_LINES_ART" | tr '\n' '|' | sed -e 's/|$//')
      fi
    fi
    FANOUT_OK=$(printf '%s\n' "$FANOUT_LINES" | grep -E '^STAFANOUT src=[^ ]+ status=ok ' || true)
    # Field of one STAFANOUT/STAFANOUT_LIB row: that row's `key=value` pair.
    fanout_row_field() { printf '%s\n' "$1" | tr ' ' '\n' | sed -n "s/^$2=//p" | tail -1; }
    # src= label of one row (pin paths are whitespace-free, so this is exact).
    fanout_src_of() { fanout_row_field "$1" src; }
    # Largest measured fanout among a set of ok rows.
    fanout_pick_max() { awk '{f=-1; for(i=1;i<=NF;i++){e=index($i,"="); if(e>1 && substr($i,1,e-1)=="fanout") f=substr($i,e+1)+0} if(f>best){best=f; line=$0}} END{if(line!="") print line}'; }
    # ---- artifact drivers: only artifact sources are eligible, so the clk-group
    # source added this round can never be reported as an artifact driver. ----
    if (( FANOUT_ART_ENABLED == 1 )); then
      FANOUT_OK_ART=$(printf '%s\n' "$FANOUT_OK" | awk -v skip="$FANOUT_FSM_LABEL" '{s=""; for(i=1;i<=NF;i++) if (substr($i,1,4)=="src=") s=substr($i,5); if (skip != "" && s == skip) next; print}')
      FANOUT_MAIN=$(printf '%s\n' "$FANOUT_OK_ART" | fanout_pick_max)
      if [[ -z "$FANOUT_MAIN" ]]; then
        STA_ARTIFACT_FANOUT_STATUS="unresolved"
      else
        STA_ARTIFACT_FANOUT_STATUS="ok"
        FANOUT_SRC_LABEL=$(fanout_src_of "$FANOUT_MAIN")
        STA_ARTIFACT_FANOUT_NET=$(fanout_row_field "$FANOUT_MAIN" net)
        STA_ARTIFACT_FANOUT_DRIVER_CELL=$(fanout_row_field "$FANOUT_MAIN" cell)
        _fanout_val=$(fanout_row_field "$FANOUT_MAIN" fanout)
        if [[ "$_fanout_val" =~ ^[0-9]+$ ]]; then STA_ARTIFACT_FANOUT_PINS=$_fanout_val; fi
        _fanout_val=$(fanout_row_field "$FANOUT_MAIN" load)
        if [[ "$_fanout_val" =~ ^[0-9.]+$ ]]; then STA_ARTIFACT_FANOUT_LOAD=$_fanout_val; fi
        _fanout_val=$(fanout_row_field "$FANOUT_MAIN" load_resolved_pins)
        if [[ "$_fanout_val" =~ ^[0-9]+$ ]]; then STA_ARTIFACT_FANOUT_LOAD_RESOLVED_PINS=$_fanout_val; fi
        _fanout_val=$(fanout_row_field "$FANOUT_MAIN" load_unresolved_pins)
        if [[ "$_fanout_val" =~ ^[0-9]+$ ]]; then STA_ARTIFACT_FANOUT_LOAD_UNRESOLVED_PINS=$_fanout_val; fi
        FANOUT_LIB_LINE=""
        if [[ -n "$FANOUT_SRC_LABEL" ]]; then
          FANOUT_LIB_LINE=$(printf '%s\n' "$FANOUT_OUT" | grep -F "STAFANOUT_LIB src=$FANOUT_SRC_LABEL " || true)
        fi
        if [[ -n "$FANOUT_LIB_LINE" ]]; then
          _fanout_val=$(fanout_row_field "$FANOUT_LIB_LINE" max_cap)
          if [[ "$_fanout_val" =~ ^[0-9.]+$ ]]; then STA_ARTIFACT_LIB_MAX_CAP=$_fanout_val; fi
          _fanout_val=$(fanout_row_field "$FANOUT_LIB_LINE" cap_axis)
          if [[ -n "$_fanout_val" && "$_fanout_val" != "-" ]]; then STA_ARTIFACT_LIB_CAP_AXIS=$_fanout_val; fi
          _fanout_val=$(fanout_row_field "$FANOUT_LIB_LINE" cap_unit)
          if [[ -n "$_fanout_val" && "$_fanout_val" != "-" ]]; then STA_ARTIFACT_FANOUT_LOAD_UNIT=$_fanout_val; fi
        fi
        # The verdict only ever compares measured numbers.  A lower bound above
        # the table maximum is already proof of extrapolation (the true load is at
        # least the measured one); refuting the hypothesis needs every connected
        # pin's Liberty capacitance, so an unresolved pin leaves the verdict
        # undecided instead of silently claiming in_table.
        if [[ "$STA_ARTIFACT_FANOUT_LOAD" =~ ^[0-9.]+$ && "$STA_ARTIFACT_LIB_MAX_CAP" =~ ^[0-9.]+$ ]]; then
          _fanout_val=$(awk -v l="$STA_ARTIFACT_FANOUT_LOAD" -v m="$STA_ARTIFACT_LIB_MAX_CAP" 'BEGIN{ if (m > 0) printf "%.2f", l/m; else print "na" }')
          if [[ "$_fanout_val" =~ ^[0-9.]+$ ]]; then STA_ARTIFACT_FANOUT_LOAD_OVER_TABLE=$_fanout_val; fi
          if awk -v l="$STA_ARTIFACT_FANOUT_LOAD" -v m="$STA_ARTIFACT_LIB_MAX_CAP" 'BEGIN{exit !(m > 0 && l > m)}'; then
            STA_ARTIFACT_FANOUT_VERDICT="out_of_table_extrapolation"
          elif [[ "$STA_ARTIFACT_FANOUT_LOAD_UNRESOLVED_PINS" == "0" && "$STA_ARTIFACT_FANOUT_LOAD_RESOLVED_PINS" =~ ^[1-9][0-9]*$ ]]; then
            STA_ARTIFACT_FANOUT_VERDICT="in_table"
          else
            STA_ARTIFACT_FANOUT_VERDICT="load_partially_resolved"
          fi
        elif [[ "$STA_ARTIFACT_FANOUT_PINS" =~ ^[0-9]+$ ]]; then
          STA_ARTIFACT_FANOUT_VERDICT="fanout_measured_library_limit_unavailable"
        fi
      fi
    fi
    # ---- Round-85: worst clk-group path's largest-increment driver ----
    # Same measurement and same load-vs-cap-axis rule, on its own fields.  The
    # row and the library axis are carried verbatim so the planner can re-check
    # the arithmetic; the verdict stays undecided unless both numbers measured.
    if (( FANOUT_FSM_WANTED == 1 )) && [[ -n "$FANOUT_FSM_LABEL" ]]; then
      FANOUT_FSM_ROW=$(printf '%s\n' "$FANOUT_OK" | awk -v want="$FANOUT_FSM_LABEL" '{s=""; for(i=1;i<=NF;i++) if (substr($i,1,4)=="src=") s=substr($i,5); if (s == want) {print; exit}}')
      if [[ -z "$FANOUT_FSM_ROW" ]]; then
        STA_FSM_PATH_FANOUT_STATUS="unresolved"
      else
        STA_FSM_PATH_FANOUT_STATUS="ok"
        STA_FSM_PATH_FANOUT_ROW=$FANOUT_FSM_ROW
        FANOUT_FSM_LIB=$(printf '%s\n' "$FANOUT_OUT" | grep -F "STAFANOUT_LIB src=$FANOUT_FSM_LABEL " || true)
        if [[ -n "$FANOUT_FSM_LIB" ]]; then STA_FSM_PATH_FANOUT_LIB=$FANOUT_FSM_LIB; fi
        _fsm_load=$(fanout_row_field "$FANOUT_FSM_ROW" load)
        _fsm_upins=$(fanout_row_field "$FANOUT_FSM_ROW" load_unresolved_pins)
        _fsm_rpins=$(fanout_row_field "$FANOUT_FSM_ROW" load_resolved_pins)
        _fsm_pins=$(fanout_row_field "$FANOUT_FSM_ROW" fanout)
        _fsm_maxcap=$(fanout_row_field "$FANOUT_FSM_LIB" max_cap)
        if [[ "$_fsm_load" =~ ^[0-9.]+$ && "$_fsm_maxcap" =~ ^[0-9.]+$ ]]; then
          if awk -v l="$_fsm_load" -v m="$_fsm_maxcap" 'BEGIN{exit !(m > 0 && l > m)}'; then
            STA_FSM_PATH_FANOUT_VERDICT="out_of_table_extrapolation"
          elif [[ "$_fsm_upins" == "0" && "$_fsm_rpins" =~ ^[1-9][0-9]*$ ]]; then
            STA_FSM_PATH_FANOUT_VERDICT="in_table"
          else
            STA_FSM_PATH_FANOUT_VERDICT="load_partially_resolved"
          fi
        elif [[ "$_fsm_pins" =~ ^[0-9]+$ ]]; then
          STA_FSM_PATH_FANOUT_VERDICT="fanout_measured_library_limit_unavailable"
        fi
      fi
    fi
  fi
fi
# Round-80: one explicit verdict for this closure report, combining the existing
# credible/artifact split with the measured audit above (and its unresolved
# variants).  It stays a PRE-LAYOUT PARTIAL_HIERARCHICAL closure statement --
# never a chip Fmax.
if [[ "$STA_TIMING_MEASURED" == "yes" ]]; then
  if [[ "$STA_ARTIFACT_PATH_COUNT" =~ ^[0-9]+$ ]] && (( STA_ARTIFACT_PATH_COUNT > 0 )); then
    if [[ "$STA_ARTIFACT_FANOUT_VERDICT" == "out_of_table_extrapolation" ]]; then
      STA_TIMING_VERDICT="credible_in_table_path_with_measured_out_of_table_artifact"
    else
      STA_TIMING_VERDICT="credible_in_table_path_with_unresolved_out_of_table_artifact"
    fi
  elif [[ "$STA_CREDIBLE_PATH_COUNT" =~ ^[0-9]+$ ]] && (( STA_CREDIBLE_PATH_COUNT > 0 )); then
    STA_TIMING_VERDICT="credible_in_table_path_only"
  fi
fi
# The full log stays on the server.  Fetch every per-stage area/memory/timing
# line from it so a partial baseline that predates the 400-line tail is kept
# (the layered measure flow emits one stat per mapped module).
AREA=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "grep 'Chip area for module' '$RROOT/runs/$RUN_ID/yosys.log' 2>/dev/null || true" || true)
# Round-37 (measurement completion, harness fix): every ABC wire-load delay
# line is now attributed to the partition that produced it
# (HARNESS_PARTITION_BEGIN=<name>) and the module ABC was extracting
# (`Extracting gate netlist of module <name>`), so the report can name the
# source of the critical path and separate non-physical artifacts instead of
# surfacing an anonymous global max.  The module name is decoded inside awk
# without literal backtick/single-quote characters (built via sprintf("%c",96)
# / sprintf("%c",39)) so the double-quoted remote command stays free of shell
# command substitution and quote-escaping conflicts.
DELAY=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "awk 'BEGIN{bt=sprintf(\"%c\",96); q=sprintf(\"%c\",39)} /^HARNESS_PARTITION_BEGIN=/{p=\$0; sub(/^HARNESS_PARTITION_BEGIN=/,\"\",p); m=\"\"; next} /Extracting gate netlist of module /{line=\$0; sub(/^.*module /,\"\",line); i=index(line,bt); if(i>0) line=substr(line,i+1); j=index(line,q); m=(j>0?substr(line,1,j-1):line); next} /WireLoad|Delay =|critical path|Critical path/{printf \"partition=%s module=%s %s\n\", p, m, \$0}' '$RROOT/runs/$RUN_ID/yosys.log' 2>/dev/null | tail -60 || true" || true)
# Round-45 (measurement completion, harness fix): the critical-path source
# modules are now classified by their `stat -liberty` sequential share before
# any 10 ns / est_freq comparison is made.  ABC `stime` reports the longest
# combinational-cone delay of the module it is extracting; a module whose cells
# are (near-)all combinational (descriptor_lookup / matrix_qsz_buffer async-read
# mux trees, l2norm_q's isqrt64 + 64-bit $div, the loader tops) has no
# register-to-register path, so its large delay is a boundary combinational /
# wire-load pathology, not a clock critical path.  `stat -liberty` prints, per
# module, a "Chip area for module '\<name>': ..." line immediately followed by
# "of which used for sequential elements: <area> (<pct>%)"; the awk below
# tracks the partition (HARNESS_PARTITION_BEGIN=<name>) and the module being
# stat'd and emits the sequential-share percentage.  Only a delay from a module
# whose sequential share meets SEQ_RATIO_THRESHOLD_PCT is a candidate
# registered_path for the 10 ns / est_freq comparison; everything else is a
# pre-layout partition combinational estimate (not STA).
SEQ=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "awk 'BEGIN{q=sprintf(\"%c\",39)} /^HARNESS_PARTITION_BEGIN=/{p=\$0; sub(/^HARNESS_PARTITION_BEGIN=/,\"\",p); m=\"\"; next} /Chip area for module /{line=\$0; j=index(line,q); if(j>0){tail=substr(line,j+1); k=index(tail,q); m=(k>0?substr(tail,1,k-1):tail)} next} /of which used for sequential elements/{seq_line=\$0; i=index(seq_line,\"(\"); j=index(seq_line,\")\"); if(i>0 && j>i){s=substr(seq_line,i+1,j-i-1); sub(/%\$/,\"\",s); printf \"partition=%s module=%s seq_pct=%s\n\", p, m, s}}' '$RROOT/runs/$RUN_ID/yosys.log' 2>/dev/null | tail -500 || true" || true)
# Round-47 (measurement completion, end-to-end convergence): Round-45's SEQ
# attribution relies on the HARNESS_PARTITION_BEGIN=<name> markers in the main
# log.  When a partition's log section is truncated before the sequential line
# (exit 124 mid-stat) or a marker is lost, the row never reaches the table and
# the module is classified `seq_unobserved` (Round-46 evidence:
# p16_tensor_loader 430027 ps).  The per-partition tee'd stat files
# (build/ppa_measure.<tag>.<stage>.stat, written by ppa_measure.ys' `tee -o`)
# carry the SAME `stat -liberty` output with the partition name in the
# FILENAME, so parsing them is a marker-independent fallback that recovers the
# seq_pct rows the main-log parse may have cut off.  `sort -u` dedups rows the
# main log already captured.
SEQ_FILES=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "awk 'BEGIN{q=sprintf(\"%c\",39)} FNR==1{i=index(FILENAME,\"ppa_measure.\"); rest=substr(FILENAME,i+12); j=index(rest,\".\"); pn=(j>0?substr(rest,1,j-1):rest)} /Chip area for module /{line=\$0; j=index(line,q); if(j>0){tail=substr(line,j+1); k=index(tail,q); m=(k>0?substr(tail,1,k-1):tail)} next} /of which used for sequential elements/{seq_line=\$0; i=index(seq_line,\"(\"); j=index(seq_line,\")\"); if(i>0 && j>i){s=substr(seq_line,i+1,j-i-1); sub(/%\$/,\"\",s); printf \"partition=%s module=%s seq_pct=%s\n\", pn, m, s}}' '$RROOT'/build/ppa_measure.*.area.stat 2>/dev/null | sort -u | tail -500 || true" || true)
if [[ -n "$SEQ_FILES" ]]; then
  if [[ -n "$SEQ" ]]; then SEQ+=$'\n'"$SEQ_FILES"; else SEQ=$SEQ_FILES; fi
fi
# Round-33: partitions declared `macro_blackbox = true` in ppa.toml are SRAM/ROM
# macros -- their log records MACRO_BLACKBOX_DONE=<name> right before the script
# exits (no techmap/ABC ran for them).  Surface it so a wrapper-only area row is
# never mistaken for the flattened memory's logic area, and so the report can
# state explicitly that those partitions were measured as macros (~1 Mbit/mm2)
# rather than as mapped standard cells.
MACRO_LINES=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "grep 'MACRO_BLACKBOX_DONE=' '$RROOT/runs/$RUN_ID/yosys.log' 2>/dev/null || true" || true)
MEM=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "awk -f '$RROOT/harness/parse_partition_memory.awk' '$RROOT/runs/$RUN_ID/yosys.log' 2>/dev/null || true" || true)
# A partition that timed out (exit=124) must not be reported as zero SRAM/ROM
# bits even if its log reached the early memory-stat checkpoint before dying in
# techmap/abc: rewrite such lines from 0 to `unobserved`.  (Partitions that
# never reached the checkpoint are already emitted as `unobserved` by
# parse_partition_memory.awk; budget-skipped partitions open no log section at
# all, so they produce no line here.)  `unobserved` stays non-numeric, so the
# MEM_BITS/SRAM_MIB aggregates are unchanged -- 0 already summed to nothing --
# while the report no longer implies a measured zero for a partition that
# never completed its mapping.
if [[ -n "$MEM" && ${#PART_RESULTS[@]} -gt 0 ]]; then
  _to_names=""
  for _pr in "${PART_RESULTS[@]}"; do
    if [[ "$_pr" =~ ^partition[[:space:]]+([^[:space:]]+)[[:space:]]+exit=(124|skipped\(budget\))$ ]]; then
      _to_names+=" ${BASH_REMATCH[1]}"
    fi
  done
  # Round-46 (measurement completion, end-to-end convergence): the top_level_sta
  # closure partition re-derives the SAME real control-plane modules (mla_kv_cache
  # 245,760 bits) that their dedicated partitions already counted (cp_fsm), so its
  # memory-stat row is a redundant re-measurement, not a new partition -- drop it
  # from the aggregate so SRAM/ROM is counted exactly once per memory across the
  # round (no cross-partition double count).  It stays in scheduler_audit (29/29);
  # only its memory row is excluded.  The 124/skipped-to-unobserved rewrite below
  # is unchanged and applies to every other partition.
  _to_names+=" top_level_sta"
  _new_mem=""
  while IFS=$'\t' read -r _mn _mb; do
    [[ -n "$_mn" ]] || continue
    if [[ "$_mn" == "top_level_sta" ]]; then
      continue
    fi
    if [[ "$_mb" == "0" && " $_to_names " == *" $_mn "* ]]; then
      _mb="unobserved"
    fi
    # $(...) strips the trailing newline, so re-emitting through printf here
    # would concatenate every partition's row onto one line and break the
    # tab-delimited `name<TAB>value` aggregation below (attempt-13 showed
    # sram_total_bits=未测得 despite real 245760/524288-bit rows).  Build the
    # row with explicit TAB/NL so each partition stays its own line.
    _new_mem+="${_mn}"$'\t'"${_mb}"$'\n'
  done <<< "$MEM"
  MEM=$(printf '%s' "$_new_mem")
fi
# Round-41 (measurement completion, end-to-end convergence, harness fix):
# Round-40's `check` diagnostics were tee'd per partition to
# build/ppa_measure.<tag>.pre_abc.check, but the feedback assembler never
# fetched them, so the decisive "RTL combinational loop vs acyclic deep comb
# chain (wire-load artifact)" verdict never reached the planner.  Fetch the
# loop-hit lines from those per-partition check files AND the LOOP_CHECK_DONE=
# attribution from this run's yosys.log, then classify every partition in the
# schedule: reached check + hit  -> combinational_loop (real RTL loop, next
# round's targeted RTL fix); reached check + no hit -> acyclic (delay is a
# wire-load artifact, est_freq stays derived from the clean max); never reached
# check this round (timeout before check / macro partition / budget-skip) ->
# unobserved, never guessed from a stale check file of an earlier run.
# Round-42 (measurement completion, harness fix): ppa_measure.ys now runs the
# check as a single `tee -o <file> check -loop`, so the pre_abc.check file
# carries the real verdict.  The classification gains tolerance for a lost
# LOOP_CHECK_DONE= attribution: a partition that exited 0 this round
# necessarily passed the pre-ABC check stage (check precedes techmap/ABC/stat
# in ppa_measure.ys), so its check file is this-round evidence, never a stale
# earlier-run file -- such a partition is classified from the file with an
# explicit acyclic(marker_missing) / combinational_loop(marker_missing)
# suffix instead of a silent unobserved.  Macro partitions
# (MACRO_BLACKBOX_DONE=) never run check and stay unobserved; partitions that
# timed out (124) or were budget-skipped also stay unobserved, because for
# them a pre_abc.check file would only be stale from an earlier run.
# Round-44 (measurement completion, end-to-end convergence, harness fix):
# Round-43's classification returned unobserved for ALL 28 partitions despite
# every partition exiting 0, because BOTH the marker and the fallback branches
# were unreachable:
#   (1) Yosys `log` echoes its message WITH surrounding double quotes
#       ("LOOP_CHECK_DONE=silu_pwl" in the yosys.log), so the sed extraction of
#       LOOP_CHECK_NAMES captured a trailing `"` and `grep -Fxq "$_pn"` (bare
#       partition name) never matched -> the marker branch never fired.
#   (2) the fallback's macro-partition test `grep -qF "MACRO_BLACKBOX_DONE=
#       ${_pn}"` matched the `SRAM_MACRO_BLACKBOX_DONE=<name>` line that
#       ppa_measure.ys logs for EVERY partition (the SRAM macro blackbox
#       declaration), so every exit-0 partition looked like a macro and the
#       fallback never fired either.
# The rebuilt classification associates per-partition exit records BY NAME
# (capturing the exit value instead of an end-anchored `exit=0$` substring),
# detects macro partitions ONLY by the bare `MACRO_BLACKBOX_DONE=` line (which
# only ppa_macro.ys emits), strips quotes from the log-marker names, and uses
# check-file existence as an extra marker source.  Loop-hit evidence is read
# from the per-partition check files (build/ppa_measure.<tag>.pre_abc.check --
# the exact tee target in ppa_measure.ys) with the main yosys.log as a
# secondary source, because `tee -o <file> check` also passes check output
# through to the log, so a cleaned build/ dir cannot silently drop the hits.
LOOP_CHECK_NAMES=""
LOOP_CHECK_LINES=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "grep 'LOOP_CHECK_DONE=' '$RROOT/runs/$RUN_ID/yosys.log' 2>/dev/null || true" || true)
if [[ -n "$LOOP_CHECK_LINES" ]]; then
  # Yosys `log` echoes the message quoted; strip the quotes so the extracted
  # name matches the bare partition name exactly.
  LOOP_CHECK_NAMES=$(printf '%s\n' "$LOOP_CHECK_LINES" | sed -n 's/.*LOOP_CHECK_DONE=\([^[:space:]]*\).*/\1/p' | tr -d '"')
fi
# Macro partitions are exactly the ppa_macro.ys runs: they emit a BARE
# `MACRO_BLACKBOX_DONE=<name>` line.  ppa_measure.ys emits the SRAM-prefixed
# `SRAM_MACRO_BLACKBOX_DONE=<name>` line for every partition, which must NOT
# count as macro evidence.
MACRO_NAMES=""
if [[ -n "$MACRO_LINES" ]]; then
  # Round-57 (harness repair): a `grep -v` that selects nothing returns 1.
  # Under `set -e -o pipefail` that used to abort the whole round before
  # feedback.txt/summary.json were written whenever NO bare
  # `MACRO_BLACKBOX_DONE=` partition existed this round -- i.e. every
  # completed partition emitted only the SRAM-prefixed marker, which is the
  # normal case for a pure ppa_measure.ys round.  `|| true` makes the
  # extraction a no-op in that case so the STA terminal state and the rest of
  # the report still reach feedback.txt.
  MACRO_NAMES=$(printf '%s\n' "$MACRO_LINES" \
    | grep 'MACRO_BLACKBOX_DONE=' \
    | grep -v 'SRAM_MACRO_BLACKBOX_DONE=' \
    | sed -n 's/.*MACRO_BLACKBOX_DONE=\([^[:space:]]*\).*/\1/p' | tr -d '"' || true)
fi
# Check-file existence as a second marker source: an exit-0 partition rewrote
# build/ppa_measure.<tag>.pre_abc.check this round (check precedes techmap/ABC/
# stat and the partition completed its script); a 124 partition may have reached
# check before dying in techmap/ABC, but its file could also be stale from an
# earlier run, so 124-via-file classifications are flagged (marker_missing).
# Budget-skipped partitions never ran this round, so a lingering file must not
# classify them.
LOOP_CHECK_FILES=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "ls '$RROOT'/build/ppa_measure.*.pre_abc.check 2>/dev/null | sed -e 's|.*/ppa_measure\.||' -e 's|\.pre_abc\.check$||' || true" || true)
# Loop-hit evidence: primary = the per-partition check files (verbatim check
# verdicts); secondary = the main yosys.log attributed per partition via the
# HARNESS_PARTITION_BEGIN=<name> markers.
LOOP_HIT_LINES=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "grep -H 'Found a combinational loop' '$RROOT'/build/ppa_measure.*.pre_abc.check 2>/dev/null || true" || true)
LOOP_HIT_MAIN=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "awk '/^HARNESS_PARTITION_BEGIN=/{p=\$0; sub(/^HARNESS_PARTITION_BEGIN=/,\"\",p); next} /Found a combinational loop/{print \"partition=\" p \" :: \" \$0}' '$RROOT/runs/$RUN_ID/yosys.log' 2>/dev/null || true" || true)
LOOP_CLASSIFICATION=""
for _pr in "${PART_RESULTS[@]:-}"; do
  # Per-partition exit records are associated BY NAME; the exit value is
  # captured so the exit-0 fallback does not depend on a fragile end-anchor.
  [[ "$_pr" =~ ^partition[[:space:]]+([^[:space:]]+)[[:space:]]+exit=(skipped\(budget\)|[0-9]+)$ ]] || continue
  _pn=${BASH_REMATCH[1]}
  _pexit=${BASH_REMATCH[2]}
  _cls="unobserved"
  _cnt=""
  _src=""   # marker | marker_file | fallback | macro | (empty = never reached check this round)
  if printf '%s\n' "$MACRO_NAMES" | grep -Fxq "$_pn"; then
    _src="macro"
  elif [[ -n "$LOOP_CHECK_NAMES" ]] && printf '%s\n' "$LOOP_CHECK_NAMES" | grep -Fxq "$_pn"; then
    _src="marker"
  elif [[ ( "$_pexit" == "0" || "$_pexit" == "124" ) ]] && printf '%s\n' "$LOOP_CHECK_FILES" | grep -Fxq "$_pn"; then
    _src="marker_file"
  elif [[ "$_pexit" == "0" ]]; then
    # exit-0 non-macro partition: it completed the whole script this round, so
    # it necessarily passed the pre-ABC check stage (check precedes techmap/
    # ABC/stat); its verdict is this-round evidence even if the LOOP_CHECK_DONE=
    # marker or the check file was lost.
    _src="fallback"
  fi
  if [[ -n "$_src" && "$_src" != "macro" ]]; then
    _cls="acyclic"
    if printf '%s\n' "$LOOP_HIT_LINES" | grep -qF "ppa_measure.${_pn}.pre_abc.check" \
       || printf '%s\n' "$LOOP_HIT_MAIN" | grep -qF "partition=${_pn} ::"; then
      _cls="combinational_loop"
      _cnt=$( { printf '%s\n' "$LOOP_HIT_LINES" | grep -cF "ppa_measure.${_pn}.pre_abc.check" || true; \
                printf '%s\n' "$LOOP_HIT_MAIN" | grep -cF "partition=${_pn} ::" || true; } \
              | awk '{s+=$1} END { print s+0 }' )
    fi
    [[ "$_src" == "marker" ]] || _cls="${_cls}(marker_missing)"
  fi
  LOOP_CLASSIFICATION+="partition=$_pn loop_classification=$_cls${_cnt:+ loop_count=$_cnt}"$'\n'
done
LOOP_CLASSIFICATION=${LOOP_CLASSIFICATION%$'\n'}
# Per-partition Yosys ERROR extraction for rc!=0 partitions.  The full server
# log is typically >1 MiB (over the ssh read limit) and the local 400-line tail
# only shows the LAST partition, so rc=1 partitions like kda_recurrence_contract
# and decay_from_sigmoid_mod have been failing with no readable ERROR text.
# A server-side awk splits the log on HARNESS_PARTITION_BEGIN=<name> and returns
# error-pattern lines (with a bounded after-context) only for partitions whose
# real tool exit was non-zero, so the next planning round sees the actual Yosys
# ERROR message instead of blindly editing RTL.  Zero-exit and budget-skipped
# partitions (which open no log section) emit nothing.
FAILED_NAMES=""
if [[ "$MODE" == "measure" ]]; then
  for _pr in "${PART_RESULTS[@]:-}"; do
    if [[ "$_pr" =~ ^partition[[:space:]]+([^[:space:]]+)[[:space:]]+exit=[1-9][0-9]*$ ]]; then
      FAILED_NAMES+=" ${BASH_REMATCH[1]}"
    fi
  done
  FAILED_NAMES=${FAILED_NAMES# }
fi
PARTITION_ERRORS=""
if [[ -n "$FAILED_NAMES" ]]; then
  PARTITION_ERRORS=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
    "awk -v F='$FAILED_NAMES' 'BEGIN{n=split(F,A,\" \"); for(i=1;i<=n;i++) W[A[i]]=1} /^HARNESS_PARTITION_BEGIN=/{name=\$0; sub(/^HARNESS_PARTITION_BEGIN=/,\"\",name); keep=(name in W); ctx=0; cnt=0; next} !keep{next} cnt>=60{next} ctx>0{print name \" :: \" \$0; ctx--; cnt++; next} /ERROR|Error|Latch inferred|syntax error/{print name \" :: \" \$0; ctx=5; cnt++; next}' '$RROOT/runs/$RUN_ID/yosys.log' 2>/dev/null || true" || true)
fi
# The closure audit is intentionally derived from the exact run's generated
# artifacts, not inferred from a Yosys exit code.  These tiny scalar queries
# keep the full netlist on the server while giving the controller enough facts
# to distinguish an empty export, missing top, parser failure and link failure.
if [[ "$MODE" == "measure" ]]; then
  STA_CLOSURE_BYTES=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
    "if test -f '$RROOT/build/ppa_closure_top.v'; then wc -c < '$RROOT/build/ppa_closure_top.v'; else echo 0; fi" || true)
  STA_CLOSURE_MODULE_COUNT=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
    "if test -f '$RROOT/build/ppa_closure_top.v'; then grep -Ec '^[[:space:]]*module[[:space:]]+' '$RROOT/build/ppa_closure_top.v' || true; else echo 0; fi" || true)
  STA_BLACKBOX_EXPORT_STATUS=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
    "if test -s '$RROOT/build/sta_norm/ppa_sta_blackboxes.v'; then echo ok; else echo missing; fi" || true)
  [[ "$STA_CLOSURE_BYTES" =~ ^[0-9]+$ ]] || STA_CLOSURE_BYTES="未测得"
  [[ "$STA_CLOSURE_MODULE_COUNT" =~ ^[0-9]+$ ]] || STA_CLOSURE_MODULE_COUNT="未测得"
  [[ "$STA_BLACKBOX_EXPORT_STATUS" == "ok" || "$STA_BLACKBOX_EXPORT_STATUS" == "missing" ]] || STA_BLACKBOX_EXPORT_STATUS="未测得"
fi
SCOPE=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "grep 'MEASUREMENT_SCOPE=' '$RROOT/runs/$RUN_ID/yosys.log' 2>/dev/null | tail -1 || true" || true)
SCOPE=$(printf '%s\n' "$SCOPE" | sed -n 's/.*MEASUREMENT_SCOPE=\([A-Z_][A-Z_]*\).*/\1/p' | tail -1)
# Round-34 (synthesis-usability repair): fall back to PARTIAL_HIERARCHICAL when
# no MEASUREMENT_SCOPE= line reached the log but the log still proves real
# per-partition measurement -- any MACRO_BLACKBOX_DONE= row (a macro partition
# that ran and exited before ABC) or "Chip area for module" row (a
# standard-cell partition that reached stat -liberty) is a measured partial
# result, never a full no-blackbox top-level closure.  This keeps attempt-3's
# scope=UNKNOWN (marker line missing for rc=1 macro partitions) from hiding
# otherwise-valid partial measurements.
if [[ -z "$SCOPE" && ( -n "$MACRO_LINES" || -n "$AREA" ) ]]; then
  SCOPE=PARTIAL_HIERARCHICAL
fi
[[ -n "$SCOPE" ]] || SCOPE=UNKNOWN
# Round-46 (measurement completion, end-to-end convergence): a
# TOP_LEVEL_COMPLETE scope is only honest if the OpenSTA closure link succeeded
# and produced a real critical path with no unlinked compute cells; otherwise
# the top-level closure is not proven, so downgrade to PARTIAL_HIERARCHICAL
# (the per-partition measurements are still valid) and the controller cannot
# accept a partial closure as a full no-blackbox top-level closure.
if [[ "$SCOPE" == "TOP_LEVEL_COMPLETE" && "$STA_STATUS" != "ok" ]]; then
  SCOPE=PARTIAL_HIERARCHICAL
fi

# ---- derived report fields (all from real controller logs; else 未测得) ----
# SRAM/ROM macro accounting uses one maximum hierarchy-inclusive stat value per
# completed partition.  This accepts both Yosys' table form (`245760 memory
# bits`) and legacy `Number of memory bits`, without double-counting a leaf's
# local row and the top module's inclusive row.
MEM_BITS="未测得"; SRAM_MIB="未测得"; MACRO_AREA_MM2="未测得"; MEM_DISPLAY="$MEM"
if [[ -n "$MEM" ]]; then
  BITS_VAL=$(printf '%s\n' "$MEM" | awk -F '\t' '$2 ~ /^[0-9]+$/ {s += $2; found=1} END {if (found) print s; else print ""}')
  if [[ "$BITS_VAL" =~ ^[0-9]+$ ]]; then
    MEM_BITS=$BITS_VAL
  fi
fi
# Round-84 (measurement completion, harness fix): the storage-macro geometry
# read from this round's blackbox declaration (export first, then the staged
# stub the export was generated from -- see the staging block above). It is a
# fallback ONLY: a per-partition early-stat row is a real mapped $mem
# measurement and always wins, so mla_kv_cache is counted exactly once and is
# never summed with the cp_fsm row.  With no numeric partition row (a targeted
# smoke that ran only the closure partition, whose netlist has the macro as a
# timing blackbox), the declaration value fills the field instead of 未测得.
MEM_EXPORT_BITS=""; MEM_EXPORT_SOURCE=""
if [[ "$STA_BLACKBOX_EXPORT_STATUS" == "ok" ]]; then
  MEM_EXPORT_SOURCE="export_decl"
  MEM_EXPORT_LINE=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
    "awk -f '$RROOT/runs/$RUN_ID/sta_storage_macro.awk' '$RROOT/build/sta_norm/ppa_sta_blackboxes.v' 2>/dev/null || true" || true)
  if [[ -z "$MEM_EXPORT_LINE" ]]; then
    MEM_EXPORT_SOURCE="stub_decl"
    MEM_EXPORT_LINE=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
      "awk -f '$RROOT/runs/$RUN_ID/sta_storage_macro.awk' '$RROOT/build/ppa_bb_top_level_sta.v' 2>/dev/null || true" || true)
  fi
  MEM_EXPORT_BITS=$(printf '%s\n' "$MEM_EXPORT_LINE" | awk -F '\t' '$2 ~ /^[0-9]+$/ {print $2; exit}')
  if [[ ! "$MEM_EXPORT_BITS" =~ ^[0-9]+$ ]]; then
    MEM_EXPORT_BITS=""
    MEM_EXPORT_SOURCE=""
  fi
fi
if [[ "$MEM_BITS" =~ ^[0-9]+$ ]]; then
  # Both are real numbers of different scope (partition rows vs the
  # blackboxed macro), so a difference is recorded as context, never as a
  # silent overwrite and never as a new aggregate.
  if [[ -n "$MEM_EXPORT_BITS" && "$MEM_EXPORT_BITS" != "$MEM_BITS" ]]; then
    MEM_DISPLAY+=$'\n'"# blackbox_decl($MEM_EXPORT_SOURCE) storage_macro_bits=$MEM_EXPORT_BITS (partition aggregate differs; scopes differ)"
  fi
elif [[ -n "$MEM_EXPORT_BITS" ]]; then
  MEM_BITS="$MEM_EXPORT_BITS"
  MEM_DISPLAY="mla_kv_cache($MEM_EXPORT_SOURCE)"$'\t'"$MEM_EXPORT_BITS"
fi
if [[ "$MEM_BITS" =~ ^[0-9]+$ ]]; then
  SRAM_MIB=$(awk -v b="$MEM_BITS" 'BEGIN { printf "%.4f", b/(8*1024*1024) }')
  MACRO_AREA_MM2=$(awk -v b="$MEM_BITS" 'BEGIN { printf "%.4f", b/1000000 }')
fi

# Round-37 (measurement completion, harness fix): standard-cell logic area is
# now reported as a deduplicated per-module sum.  The layered measure flow
# emits "Chip area for module" exactly once per mapped module (leaves in pass 1,
# the partition top last in pass 2), so each partition's rows are its logic
# area; a module shared by two partitions (e.g. descriptor_lookup in both the
# p16 and qsz loaders) is counted once by taking the per-name max before the
# sum.  A $paramod$<hash> instance is its own module identity (its own width/
# cache parameterization) and is kept separate.  Macro partitions (ppa_macro.ys)
# emit no "Chip area" row, so they are naturally excluded here and their area is
# accounted only by the ~1 Mbit/mm2 macro model.
LOGIC_AREA_UM2=""; LOGIC_AREA_MM2="未测得"; LOGIC_AREA_MODULES="0"; LOGIC_AREA_BUDGET_MM2="4.0"; LOGIC_AREA_BUDGET_OK="未测得"; TOTAL_AREA_MM2="未测得"
if [[ -n "$AREA" ]]; then
  LOGIC_AREA_UM2=$(printf '%s\n' "$AREA" | sed -n "s/.*Chip area for module '\([^']*\)': *\([0-9][0-9.]*\).*/\1 \2/p" | awk '
    { name = $1; val = $2; if (val + 0 > max[name] + 0) max[name] = val }
    END { if (length(max) == 0) exit 1; s = 0; for (n in max) s += max[n]; printf "%.3f", s }
  ') || true
fi
if [[ "$LOGIC_AREA_UM2" =~ ^[0-9.]+$ && "$LOGIC_AREA_UM2" != "0.000" ]]; then
  LOGIC_AREA_MM2=$(awk -v a="$LOGIC_AREA_UM2" 'BEGIN { printf "%.4f", a/1000000 }')
  LOGIC_AREA_MODULES=$(printf '%s\n' "$AREA" | sed -n "s/.*Chip area for module '\([^']*\)': *\([0-9][0-9.]*\).*/\1/p" | sort -u | wc -l | tr -d ' ')
  if awk -v a="$LOGIC_AREA_UM2" 'BEGIN { exit (a/1000000 > 4.0) }'; then LOGIC_AREA_BUDGET_OK=yes; else LOGIC_AREA_BUDGET_OK=no; fi
fi
if [[ "$LOGIC_AREA_MM2" =~ ^[0-9.]+$ && "$MACRO_AREA_MM2" =~ ^[0-9.]+$ ]]; then
  TOTAL_AREA_MM2=$(awk -v l="$LOGIC_AREA_MM2" -v m="$MACRO_AREA_MM2" 'BEGIN { printf "%.4f", l+m }')
fi

# Round-37 (measurement completion, harness fix): the critical-path estimate now
# excludes non-physical ABC artifacts.  A delay >= 1e6 ps (>= 1 ms) from a
# few-hundred-k gate acyclic netlist is an ABC wire-load artifact, not a real
# path (evidence: a 266,276-gate descriptor_lookup combinational cache-read
# reported 3.26e9 ps; a 792,671-gate fsm 26e6 ps; an isqrt64 l2norm_q 9.4e6 ps),
# so such lines are listed separately as artifacts and est_freq / meets_10ns
# are derived only from the largest sub-1 ms measured delay.  The partition/
# module attribution is the prefix added when $DELAY was fetched.  If every
# delay is an artifact, est_freq stays 未测得 (no verdict).
#
# Round-45 (measurement completion, harness fix): even a sub-1 ms delay is only
# compared against 10 ns / converted to est_freq when its source module is
# classified `registered_path` -- i.e. its `stat -liberty` sequential share
# meets SEQ_RATIO_THRESHOLD_PCT.  The 430 ns p16_tensor_loader max and the five
# >=1e6 ps artifacts all come from near-zero-sequential combinational modules
# (boundary combinational cones / wire-load pathologies), so est_freq / meets_10ns
# stay 未测得 for them (see the CRIT_PATH_* classification below).
RAW_MAX_DELAY_PS=""; MAX_DELAY_PS=""; MAX_DELAY_SOURCE=""; DELAY_ARTIFACT_COUNT="0"; DELAY_ARTIFACTS="none"
DELAY_STATS=""
if [[ -n "$DELAY" ]]; then
  DELAY_STATS=$(printf '%s\n' "$DELAY" | awk '
    BEGIN { art = 1000000; raw = ""; real = ""; cnt = 0; lst = "" }
    {
      p = $1; sub(/^partition=/, "", p)
      m = $2; sub(/^module=/, "", m)
      if (match($0, /Delay = *[0-9.]+ *ps/) == 0) next
      d = substr($0, RSTART, RLENGTH); sub(/^Delay = */, "", d); sub(/ *ps$/, "", d)
      dv = d + 0
      if (dv > raw + 0) raw = d
      if (dv >= art) {
        if (cnt > 0) lst = lst ","
        lst = lst "partition=" p " module=" m " delay=" d
        cnt++
      } else if (dv > real + 0) {
        real = d
        real_src = "partition=" p " module=" m
      }
    }
    END {
      printf "RAW=%s\nREAL=%s\nREAL_SRC=%s\nART_COUNT=%d\nART_LIST=%s\n", raw, real, real_src, cnt, lst
    }
  ')
fi
if [[ -n "$DELAY_STATS" ]]; then
  while IFS='=' read -r _dk _dv2; do
    case "$_dk" in
      RAW) RAW_MAX_DELAY_PS=$_dv2 ;;
      REAL) MAX_DELAY_PS=$_dv2 ;;
      REAL_SRC) MAX_DELAY_SOURCE=$_dv2 ;;
      ART_COUNT) DELAY_ARTIFACT_COUNT=$_dv2 ;;
      ART_LIST) if [[ -n "$_dv2" ]]; then DELAY_ARTIFACTS=$_dv2; fi ;;
    esac
  done <<< "$DELAY_STATS"
fi
# Round-45: classify every max-delay source module by its `stat -liberty`
# sequential share before comparing against 10 ns.  ABC `stime` reports the
# longest combinational-cone delay of the module it is extracting; a module
# with (near-)no sequential cells (descriptor_lookup / matrix_qsz_buffer
# async-read mux trees, l2norm_q's isqrt64+$div, the loader tops) has no
# register-to-register path, so its delay is a boundary combinational /
# wire-load pathology, not a clock critical path.  Classification:
#   non_physical_artifact : delay >= 1e6 ps (Round-37 wire-load bar)
#   registered_path       : delay < 1e6 ps AND sequential share >= threshold
#   combinational_block   : delay < 1e6 ps AND sequential share < threshold
#                           (or share not measured -> seq_unobserved suffix)
# est_freq / meets_10ns are derived ONLY from a registered_path; everything
# else is reported as a pre-layout partition combinational estimate (not STA).
SEQ_RATIO_THRESHOLD_PCT=5.0
CRIT_PATH_CLASS="unobserved"; CRIT_PATH_SEQ_PCT="unobserved"; CRIT_PATH_SOURCES=""
seq_pct_of() {
  # $1 = source line ("partition=<p> module=<m> ..."); look up its sequential share
  local _src="$1" _pm
  _pm=$(printf '%s\n' "$_src" | sed -n 's/\(partition=[^ ]* module=[^ ]*\).*/\1/p')
  [[ -n "$_pm" ]] || return 0
  printf '%s\n' "$SEQ" | grep -F "$_pm" | sed -n 's/.*seq_pct=\([0-9.]*\).*/\1/p' | head -1 || true
}
classify_delay_source() {
  # $1 = source line ("partition=... module=..." [+ " delay=..."]) ; $2 = delay ps
  local _src="$1" _dps="$2" _spct _cls
  _spct=$(seq_pct_of "$_src")
  if awk -v d="$_dps" 'BEGIN { exit (d < 1000000) }'; then
    _cls="non_physical_artifact"
  elif [[ -n "$_spct" ]]; then
    if awk -v s="$_spct" -v t="$SEQ_RATIO_THRESHOLD_PCT" 'BEGIN { exit (s < t) }'; then
      _cls="registered_path"
    else
      _cls="combinational_block"
    fi
  else
    _cls="combinational_block(seq_unobserved)"
  fi
  printf '%s seq_pct=%s class=%s' "$_src" "${_spct:-unobserved}" "$_cls"
}
# Round-47 (measurement completion, end-to-end convergence): Round-45/46
# classified ONLY the single largest sub-1e6 delay ($MAX_DELAY_SOURCE).  When
# that one module is a near-combinational block (Round-46 evidence:
# p16_tensor_loader 430027 ps -> combinational_block(seq_unobserved)),
# est_freq/meets_10ns stayed 未测得 even though real registered modules elsewhere
# (FSM / loader counters / microop controller / writer) had decidable sub-1e6
# delays.  The scan below classifies EVERY sub-1e6 delay line and derives the
# pre-layout critical-path estimate from the largest one whose `stat -liberty`
# sequential share marks it `registered_path`; the largest combinational_block
# and the >=1e6 ps artifacts are still reported for context.  Everything
# remains a pre-layout partition estimate (not STA); RTL semantics unchanged.
REGISTERED_CRIT_PS=""; REGISTERED_CRIT_SRC=""
COMB_MAX_PS=""; COMB_MAX_SRC=""
DELAY_CANDIDATE_LINES=""
if [[ -n "$DELAY" ]]; then
  while IFS= read -r _dline; do
    [[ -n "$_dline" ]] || continue
    _dp=$(printf '%s\n' "$_dline" | sed -n 's/^partition=\([^ ]*\).*/\1/p')
    _dm=$(printf '%s\n' "$_dline" | sed -n 's/.*module=\([^ ]*\).*/\1/p')
    _dd=$(printf '%s\n' "$_dline" | sed -n 's/.*Delay = *\([0-9][0-9.]*\) *ps.*/\1/p')
    [[ -n "$_dp" && -n "$_dm" && -n "$_dd" ]] || continue
    # Round-82 (measurement 口径 fix, zero RTL): keep the ABC `stime` delay of
    # every module whose name mentions the fsm (including the escaped
    # `$paramod$…\nano_golden_datapath_fsm` form) so the closure STA path can be
    # compared against the exact number ABC reported for it.  Recorded before
    # the >=1e6 ps artifact filter on purpose: this field is the ABC side of the
    # reconciliation and must never be silently dropped by that filter.
    if printf '%s\n' "$_dm" | grep -qi 'fsm'; then
      if [[ ! "$STA_FSM_ABC_DELAY_PS" =~ ^[0-9.]+$ ]] || awk -v a="$_dd" -v b="$STA_FSM_ABC_DELAY_PS" 'BEGIN{exit !(a+0 > b+0)}'; then
        STA_FSM_ABC_DELAY_PS="$_dd"
        STA_FSM_ABC_DELAY_SOURCE="partition=$_dp module=$_dm delay=$_dd"
      fi
    fi
    # Non-physical wire-load artifacts (>=1e6 ps) are not clock-path candidates.
    if awk -v d="$_dd" 'BEGIN{exit !(d+0 >= 1000000)}'; then continue; fi
    _clsline=$(classify_delay_source "partition=$_dp module=$_dm delay=$_dd" "$_dd")
    DELAY_CANDIDATE_LINES+="${DELAY_CANDIDATE_LINES:+$'\n'}$_clsline"
    _cls=$(printf '%s\n' "$_clsline" | sed -n 's/.*class=\([^ ]*\).*/\1/p')
    if [[ "$_cls" == "registered_path" ]]; then
      if [[ -z "$REGISTERED_CRIT_PS" ]] || awk -v a="$_dd" -v b="$REGISTERED_CRIT_PS" 'BEGIN{exit !(a+0 > b+0)}'; then
        REGISTERED_CRIT_PS="$_dd"; REGISTERED_CRIT_SRC="partition=$_dp module=$_dm delay=$_dd"
      fi
    elif [[ -z "$COMB_MAX_PS" ]] || awk -v a="$_dd" -v b="$COMB_MAX_PS" 'BEGIN{exit !(a+0 > b+0)}'; then
      COMB_MAX_PS="$_dd"; COMB_MAX_SRC="partition=$_dp module=$_dm delay=$_dd"
    fi
  done <<< "$DELAY"
fi
# Critical-path basis: the largest registered_path wins; otherwise fall back to
# the largest sub-1e6 delay (previous behaviour, est stays 未测得 for a
# combinational_block).
CRIT_BASIS_PS=""; CRIT_BASIS_SRC=""
if [[ -n "$REGISTERED_CRIT_PS" ]]; then
  CRIT_BASIS_PS=$REGISTERED_CRIT_PS
  CRIT_BASIS_SRC=$REGISTERED_CRIT_SRC
  CRIT_PATH_CLASS="registered_path"
  CRIT_PATH_SEQ_PCT=$(seq_pct_of "$REGISTERED_CRIT_SRC")
  [[ -n "$CRIT_PATH_SEQ_PCT" ]] || CRIT_PATH_SEQ_PCT="unobserved"
elif [[ -n "$MAX_DELAY_SOURCE" ]]; then
  CRIT_BASIS_PS=$MAX_DELAY_PS
  CRIT_BASIS_SRC=$MAX_DELAY_SOURCE
  _clsline=$(classify_delay_source "delay=$MAX_DELAY_PS $MAX_DELAY_SOURCE" "$MAX_DELAY_PS")
  CRIT_PATH_CLASS=$(printf '%s\n' "$_clsline" | sed -n 's/.*class=\([^ ]*\).*/\1/p' | head -1)
  CRIT_PATH_SEQ_PCT=$(printf '%s\n' "$_clsline" | sed -n 's/.*seq_pct=\([^ ]*\).*/\1/p' | head -1)
fi
# Report every classified sub-1e6 candidate plus the non-physical artifacts so
# the planner sees each evaluated path, not just the winner.
CRIT_PATH_SOURCES=""
if [[ -n "$DELAY_CANDIDATE_LINES" ]]; then
  CRIT_PATH_SOURCES=$DELAY_CANDIDATE_LINES
fi
if [[ -n "$DELAY_ARTIFACTS" && "$DELAY_ARTIFACTS" != "none" ]]; then
  IFS=',' read -r -a _art_arr <<< "$DELAY_ARTIFACTS"
  for _art in "${_art_arr[@]:-}"; do
    _ad=$(printf '%s\n' "$_art" | sed -n 's/.*delay=\([0-9.]*\).*/\1/p')
    CRIT_PATH_SOURCES+=$'\n'"$(classify_delay_source "$_art" "$_ad")"
  done
fi
# max_abc_delay_ps/ns/source keep reporting the raw largest sub-1e6 ABC delay.
MAX_DELAY_NS="未测得"; CRIT_PATH_PS="未测得"; CRIT_PATH_NS="未测得"; CRIT_PATH_SOURCE=""
if [[ "$MAX_DELAY_PS" =~ ^[0-9.]+$ ]]; then
  MAX_DELAY_NS=$(awk -v d="$MAX_DELAY_PS" 'BEGIN { printf "%.4f", d/1000 }')
fi
if [[ "$CRIT_BASIS_PS" =~ ^[0-9.]+$ ]]; then
  CRIT_PATH_PS=$CRIT_BASIS_PS
  CRIT_PATH_NS=$(awk -v d="$CRIT_BASIS_PS" 'BEGIN { printf "%.4f", d/1000 }')
  CRIT_PATH_SOURCE=$CRIT_BASIS_SRC
fi
# Only a registered_path (module sequential share >= threshold) is compared
# against 10 ns / converted to est_freq.  A combinational_block delay is a
# pre-layout boundary-cone estimate, not a clock critical path.
EST_FREQ_MHZ="未测得"; MEETS_10NS="未测得"
if [[ "$CRIT_PATH_CLASS" == "registered_path" && "$CRIT_BASIS_PS" =~ ^[0-9.]+$ ]]; then
  EST_FREQ_MHZ=$(awk -v d="$CRIT_BASIS_PS" 'BEGIN { printf "%.1f", 1000000/d }')
  if awk -v d="$CRIT_BASIS_PS" 'BEGIN { exit (d > 10000) }'; then MEETS_10NS=yes; else MEETS_10NS=no; fi
fi

# Round-82 (measurement 口径 fix, zero RTL): reconcile ABC `stime` with the real
# in-table closure STA for the fsm.  The ABC delay and the STA path come from
# the SAME round, so the ratio answers a falsifiable question: if the fsm's own
# in-table worst path (fsm-endpoint-restricted report_checks, else the
# whole-design clk group) is tens of times shorter than ABC's stime number, then
# ABC's Delay on a wire-load-less mapped net is an estimate, not a Liberty-table
# path.  The clocked-register gate comes first: a fsm instance whose registers
# are not clocked by clk has no register-to-register path to measure, so the
# verdict is `sta_fsm_path_unobserved` rather than any ratio claim.
STA_FSM_STA_PATH_NS="未测得"
STA_FSM_PATH_BASIS="未测得"
if [[ "$STA_FSM_OWN_PATH_ARRIVAL_NS" =~ ^[0-9.]+$ ]]; then
  STA_FSM_STA_PATH_NS=$STA_FSM_OWN_PATH_ARRIVAL_NS
  STA_FSM_PATH_BASIS="fsm_endpoint_path"
elif [[ "$STA_FSM_PATH_ARRIVAL_NS" =~ ^[0-9.]+$ ]]; then
  STA_FSM_STA_PATH_NS=$STA_FSM_PATH_ARRIVAL_NS
  # Honour the group the report actually returned: only a `clk` path is a
  # register-to-register clock path, so a fallback report from another group
  # keeps that group in the basis instead of claiming a clk-group measurement.
  if [[ "$STA_FSM_PATH_GROUP" == "clk" ]]; then
    STA_FSM_PATH_BASIS="clk_group_path"
  else
    STA_FSM_PATH_BASIS="whole_design_path_group=${STA_FSM_PATH_GROUP}"
  fi
fi
if [[ "$STA_FSM_STA_PATH_NS" =~ ^[0-9.]+$ && "$STA_FSM_ABC_DELAY_PS" =~ ^[0-9.]+$ ]]; then
  STA_FSM_VS_ABC_RATIO=$(awk -v s="$STA_FSM_STA_PATH_NS" -v a="$STA_FSM_ABC_DELAY_PS" 'BEGIN { printf "%.4f", s/(a/1000) }')
fi
if [[ "$STA_FSM_CELL_COUNT" == "0" ]]; then
  STA_FSM_VERDICT="fsm_instance_not_found"
elif [[ "$STA_FSM_CELL_CLK_REG_TOTAL" =~ ^[0-9]+$ ]] && (( STA_FSM_CELL_CLK_REG_TOTAL == 0 )); then
  STA_FSM_VERDICT="sta_fsm_path_unobserved_no_clk_registers"
elif [[ "$STA_FSM_VS_ABC_RATIO" =~ ^[0-9.]+$ ]]; then
  if awk -v r="$STA_FSM_VS_ABC_RATIO" 'BEGIN { exit !(r < 0.1) }'; then
    STA_FSM_VERDICT="abc_delay_not_reproduced_in_liberty_tables"
  elif awk -v r="$STA_FSM_VS_ABC_RATIO" 'BEGIN { exit !(r > 10) }'; then
    STA_FSM_VERDICT="sta_path_slower_than_abc_delay"
  else
    STA_FSM_VERDICT="abc_delay_consistent_with_sta"
  fi
elif [[ "$STA_FSM_STA_PATH_NS" == "未测得" ]]; then
  STA_FSM_VERDICT="fsm_path_unmeasured"
else
  STA_FSM_VERDICT="abc_delay_unmeasured"
fi

# Throughput from the real functional log: the controller runs the functional
# gate right before preflight/PPA, so the newest 02_yosys_ppa_functional run
# dir on the server is this round's functional verdict.  Cycles come from the
# simulation "PASS cycles=N" line, token count from summary.json seq_len.
FUNCT_DIR=""; FUNCT_CYCLES=""; FUNCT_SEQ=""
CYCLES_PER_TOKEN="未测得"; TOKENS_PER_SEC="未测得"
FUNCT_DIR=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "ls -td '$RROOT'/runs/02_yosys_ppa_functional--* 2>/dev/null | head -1 || true" || true)
# Remote fetch: newest functional run dir on the server.  PASS cycles= is
# searched across every log filename the harness writes (simulation.log,
# feedback.txt, remote_console.log) so a renamed log cannot drop the number.
if [[ -n "$FUNCT_DIR" ]]; then
  FUNCT_CYCLES=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
    "grep -h 'PASS cycles=' '$FUNCT_DIR'/simulation.log '$FUNCT_DIR'/feedback.txt '$FUNCT_DIR'/remote_console.log 2>/dev/null | sed -n 's/.*PASS cycles=\([0-9][0-9]*\).*/\1/p' | tail -1 || true" || true)
  FUNCT_SEQ=$(retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
    "grep -hoE '\"(requested_seq_len|seq_len|tokens|quick_seq_len|medium_seq_len)\"[^,}]*' '$FUNCT_DIR'/summary.json '$FUNCT_DIR'/meta.json '$FUNCT_DIR'/result.json '$FUNCT_DIR'/feedback.txt 2>/dev/null | grep -oE '[0-9]+' | head -1 || true" || true)
fi
# Local fallback: when the remote fetch came back empty -- e.g. the transport
# dropped at the tail of the PPA round (exit 75) after the controller already
# downloaded the functional verdict -- derive cycles/token from the newest
# locally-downloaded functional artifacts instead of reporting 未测得.
# agent-02_yosys_ppa--*/functional_quick.log records feedback=<path> to the
# controller-downloaded functional feedback.txt, which embeds the simulation
# log (PASS cycles=) and the JSON head (requested_seq_len).  We also try the
# newest 02_yosys_ppa_functional--*/feedback.txt and the attempt's
# result.json/summary.json/meta.json (seq_len keys requested_seq_len/seq_len/
# tokens/quick_seq_len/medium_seq_len).  Numbers are only ever taken from real
# controller-produced logs.
if [[ ! ( "$FUNCT_CYCLES" =~ ^[0-9]+$ && "$FUNCT_SEQ" =~ ^[0-9]+$ && "$FUNCT_SEQ" -gt 0 ) ]]; then
  LOCAL_FUNCT_LOG=$(ls -td "$ROOT"/runs/agent-02_yosys_ppa--*/functional_quick.log 2>/dev/null | head -1 || true)
  LOCAL_FEEDBACK=""
  if [[ -n "$LOCAL_FUNCT_LOG" ]]; then
    LOCAL_FEEDBACK=$(sed -n 's/^feedback=\(.*\)$/\1/p' "$LOCAL_FUNCT_LOG" 2>/dev/null | tail -1 || true)
  fi
  NEWEST_FUNCT_FB=$(ls -td "$ROOT"/runs/02_yosys_ppa_functional--*/feedback.txt 2>/dev/null | head -1 || true)
  for _cand in "$LOCAL_FEEDBACK" "$NEWEST_FUNCT_FB"; do
    if [[ -n "$_cand" && -f "$_cand" ]]; then
      FUNCT_CYCLES=$(grep -h 'PASS cycles=' "$_cand" 2>/dev/null \
        | sed -n 's/.*PASS cycles=\([0-9][0-9]*\).*/\1/p' | tail -1 || true)
      if [[ "$FUNCT_CYCLES" =~ ^[0-9]+$ ]]; then break; fi
    fi
  done
  for _cand in "$LOCAL_FEEDBACK" "$NEWEST_FUNCT_FB" \
      "$(dirname "$LOCAL_FUNCT_LOG")/result.json" \
      "$(dirname "$LOCAL_FUNCT_LOG")/summary.json" \
      "$(dirname "$LOCAL_FUNCT_LOG")/meta.json"; do
    if [[ -n "$_cand" && -f "$_cand" ]]; then
      FUNCT_SEQ=$(grep -hoE '"(requested_seq_len|seq_len|tokens|quick_seq_len|medium_seq_len)"[^,}]*' "$_cand" 2>/dev/null \
        | grep -oE '[0-9]+' | head -1 || true)
      if [[ "$FUNCT_SEQ" =~ ^[0-9]+$ && "$FUNCT_SEQ" -gt 0 ]]; then break; fi
    fi
  done
  if [[ -z "$FUNCT_DIR" && -n "$LOCAL_FEEDBACK" && -f "$LOCAL_FEEDBACK" ]]; then
    FUNCT_DIR=$(dirname "$LOCAL_FEEDBACK")
  fi
fi
# Single derive point: both numbers must be real and positive.
if [[ "$FUNCT_CYCLES" =~ ^[0-9]+$ && "$FUNCT_SEQ" =~ ^[0-9]+$ && "$FUNCT_SEQ" -gt 0 ]]; then
  CYCLES_PER_TOKEN=$(awk -v c="$FUNCT_CYCLES" -v s="$FUNCT_SEQ" 'BEGIN { printf "%.0f", c/s }')
  TOKENS_PER_SEC=$(awk -v c="$FUNCT_CYCLES" -v s="$FUNCT_SEQ" 'BEGIN { printf "%.1f", s*100000000/c }')
fi

# ---- machine-readable summary (same fields as feedback.txt).  Aggregate
# memory/macro sums and max delay are safe scalars; raw per-partition
# area/memory/timing lines are preserved verbatim so the controller can weight
# each partition without re-parsing the log. ----
json_escape() {
  printf '%s' "$1" | tr '\n' ' ' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}
{
  printf '{\n'
  printf '  "run_id": "%s",\n' "$(json_escape "$RUN_ID")"
  printf '  "task": "%s",\n' "$(json_escape "$TASK")"
  printf '  "mode": "%s",\n' "$(json_escape "$MODE")"
  printf '  "timeout_seconds": %s,\n' "$TIMEOUT_SECONDS"
  printf '  "exit_code": %s,\n' "$RC"
  printf '  "measurement_scope": "%s",\n' "$(json_escape "$SCOPE")"
  printf '  "measurement_partition_selection": "%s",\n' "$(json_escape "${REQUESTED_PARTITIONS:-all}")"
  printf '  "pre_layout_estimate": true,\n'
  printf '  "measurement_type": "pre-layout per-partition ABC estimate + best-effort top-level OpenSTA closure (Round-46)",\n'
  printf '  "scheduler_audit": "%s",\n' "$(json_escape "$SCHEDULER_AUDIT")"
  printf '  "expected_partitions": %s,\n' "$EXPECTED_PARTITIONS"
  printf '  "observed_partition_terminals": %s,\n' "$OBSERVED_PARTITIONS"
  printf '  "partitions": ['
  if (( ${#PART_RESULTS[@]} > 0 )); then
    printf ' "%s"' "$(json_escape "${PART_RESULTS[0]}")"
    for _p in "${PART_RESULTS[@]:1}"; do printf ', "%s"' "$(json_escape "$_p")"; done
  fi
  printf ' ],\n'
  printf '  "logic_area_lines": "%s",\n' "$(json_escape "$AREA")"
  printf '  "memory_lines": "%s",\n' "$(json_escape "$MEM_DISPLAY")"
  printf '  "macro_blackbox_lines": "%s",\n' "$(json_escape "$MACRO_LINES")"
  printf '  "sram_total_bits": "%s",\n' "$(json_escape "$MEM_BITS")"
  printf '  "sram_total_mib": "%s",\n' "$(json_escape "$SRAM_MIB")"
  printf '  "macro_area_mm2": "%s",\n' "$(json_escape "$MACRO_AREA_MM2")"
  printf '  "logic_area_um2": "%s",\n' "$(json_escape "$LOGIC_AREA_UM2")"
  printf '  "logic_area_mm2": "%s",\n' "$(json_escape "$LOGIC_AREA_MM2")"
  printf '  "logic_area_unique_modules": "%s",\n' "$(json_escape "$LOGIC_AREA_MODULES")"
  printf '  "logic_area_budget_mm2": "%s",\n' "$(json_escape "$LOGIC_AREA_BUDGET_MM2")"
  printf '  "logic_area_budget_ok": "%s",\n' "$(json_escape "$LOGIC_AREA_BUDGET_OK")"
  printf '  "total_area_mm2_est": "%s",\n' "$(json_escape "$TOTAL_AREA_MM2")"
  printf '  "timing_lines": "%s",\n' "$(json_escape "$DELAY")"
  printf '  "partition_errors": "%s",\n' "$(json_escape "$PARTITION_ERRORS")"
  printf '  "max_abc_delay_ps": "%s",\n' "$(json_escape "$MAX_DELAY_PS")"
  printf '  "max_abc_delay_ns": "%s",\n' "$(json_escape "$MAX_DELAY_NS")"
  printf '  "est_freq_mhz": "%s",\n' "$(json_escape "$EST_FREQ_MHZ")"
  printf '  "meets_10ns": "%s",\n' "$(json_escape "$MEETS_10NS")"
  printf '  "max_abc_delay_source": "%s",\n' "$(json_escape "$MAX_DELAY_SOURCE")"
  printf '  "raw_max_abc_delay_ps": "%s",\n' "$(json_escape "$RAW_MAX_DELAY_PS")"
  printf '  "delay_artifact_count": "%s",\n' "$(json_escape "$DELAY_ARTIFACT_COUNT")"
  printf '  "delay_artifacts": "%s",\n' "$(json_escape "$DELAY_ARTIFACTS")"
  printf '  "critical_path_class": "%s",\n' "$(json_escape "$CRIT_PATH_CLASS")"
  printf '  "critical_path_seq_pct": "%s",\n' "$(json_escape "$CRIT_PATH_SEQ_PCT")"
  printf '  "seq_ratio_threshold_pct": "%s",\n' "$(json_escape "$SEQ_RATIO_THRESHOLD_PCT")"
  printf '  "critical_path_sources": "%s",\n' "$(json_escape "$CRIT_PATH_SOURCES")"
  printf '  "critical_path_ps": "%s",\n' "$(json_escape "$CRIT_PATH_PS")"
  printf '  "critical_path_ns": "%s",\n' "$(json_escape "$CRIT_PATH_NS")"
  printf '  "critical_path_source": "%s",\n' "$(json_escape "$CRIT_PATH_SOURCE")"
  printf '  "largest_registered_path_ps": "%s",\n' "$(json_escape "$REGISTERED_CRIT_PS")"
  printf '  "largest_combinational_block_ps": "%s",\n' "$(json_escape "$COMB_MAX_PS")"
  printf '  "largest_combinational_block_source": "%s",\n' "$(json_escape "$COMB_MAX_SRC")"
  printf '  "sta_status": "%s",\n' "$(json_escape "$STA_STATUS")"
  printf '  "sta_crit_path_ps": "%s",\n' "$(json_escape "$STA_CRIT_PATH_PS")"
  printf '  "sta_est_freq_mhz": "%s",\n' "$(json_escape "$STA_EST_FREQ_MHZ")"
  printf '  "sta_meets_10ns": "%s",\n' "$(json_escape "$STA_MEETS_10NS")"
  printf '  "sta_timing_measured": "%s",\n' "$(json_escape "$STA_TIMING_MEASURED")"
  printf '  "sta_timing_path_count": "%s",\n' "$(json_escape "$STA_TIMING_PATH_COUNT")"
  printf '  "sta_raw_max_arrival_ps": "%s",\n' "$(json_escape "$STA_RAW_MAX_ARRIVAL_PS")"
  printf '  "sta_raw_min_slack_ns": "%s",\n' "$(json_escape "$STA_RAW_MIN_SLACK_NS")"
  printf '  "sta_crit_path_ns": "%s",\n' "$(json_escape "$STA_CRIT_PATH_NS")"
  printf '  "sta_crit_path_start": "%s",\n' "$(json_escape "$STA_CRIT_PATH_START")"
  printf '  "sta_crit_path_end": "%s",\n' "$(json_escape "$STA_CRIT_PATH_END")"
  printf '  "sta_crit_path_group": "%s",\n' "$(json_escape "$STA_CRIT_PATH_GROUP")"
  printf '  "sta_crit_path_slack_ns": "%s",\n' "$(json_escape "$STA_CRIT_PATH_SLACK_NS")"
  printf '  "sta_crit_path_class": "%s",\n' "$(json_escape "$STA_CRIT_PATH_CLASS")"
  printf '  "sta_credible_path_count": "%s",\n' "$(json_escape "$STA_CREDIBLE_PATH_COUNT")"
  printf '  "sta_credible_meets_10ns": "%s",\n' "$(json_escape "$STA_CREDIBLE_MEETS_10NS")"
  printf '  "sta_unbuffered_fanout_artifact": "%s",\n' "$(json_escape "$STA_UNBUFFERED_FANOUT_ARTIFACT")"
  printf '  "sta_artifact_path_count": "%s",\n' "$(json_escape "$STA_ARTIFACT_PATH_COUNT")"
  printf '  "sta_artifact_max_arrival_ps": "%s",\n' "$(json_escape "$STA_ARTIFACT_MAX_ARRIVAL_PS")"
  printf '  "sta_artifact_max_arrival_start": "%s",\n' "$(json_escape "$STA_ARTIFACT_MAX_ARRIVAL_START")"
  printf '  "sta_artifact_max_arrival_end": "%s",\n' "$(json_escape "$STA_ARTIFACT_MAX_ARRIVAL_END")"
  printf '  "sta_artifact_max_arrival_group": "%s",\n' "$(json_escape "$STA_ARTIFACT_MAX_ARRIVAL_GROUP")"
  printf '  "sta_artifact_setup_ns": "%s",\n' "$(json_escape "$STA_ARTIFACT_SETUP_NS")"
  printf '  "sta_artifact_worst_arc_ps": "%s",\n' "$(json_escape "$STA_ARTIFACT_WORST_ARC_PS")"
  printf '  "sta_artifact_worst_arc_pin": "%s",\n' "$(json_escape "$STA_ARTIFACT_WORST_ARC_PIN")"
  printf '  "sta_artifact_negative_arc_count": "%s",\n' "$(json_escape "$STA_ARTIFACT_NEG_ARC_COUNT")"
  printf '  "sta_artifact_fanout_status": "%s",\n' "$(json_escape "$STA_ARTIFACT_FANOUT_STATUS")"
  printf '  "sta_artifact_fanout_verdict": "%s",\n' "$(json_escape "$STA_ARTIFACT_FANOUT_VERDICT")"
  printf '  "sta_artifact_fanout_net": "%s",\n' "$(json_escape "$STA_ARTIFACT_FANOUT_NET")"
  printf '  "sta_artifact_fanout_driver_cell": "%s",\n' "$(json_escape "$STA_ARTIFACT_FANOUT_DRIVER_CELL")"
  printf '  "sta_artifact_fanout_pins": "%s",\n' "$(json_escape "$STA_ARTIFACT_FANOUT_PINS")"
  printf '  "sta_artifact_fanout_load": "%s",\n' "$(json_escape "$STA_ARTIFACT_FANOUT_LOAD")"
  printf '  "sta_artifact_fanout_load_unit": "%s",\n' "$(json_escape "$STA_ARTIFACT_FANOUT_LOAD_UNIT")"
  printf '  "sta_artifact_fanout_load_resolved_pins": "%s",\n' "$(json_escape "$STA_ARTIFACT_FANOUT_LOAD_RESOLVED_PINS")"
  printf '  "sta_artifact_fanout_load_unresolved_pins": "%s",\n' "$(json_escape "$STA_ARTIFACT_FANOUT_LOAD_UNRESOLVED_PINS")"
  printf '  "sta_artifact_fanout_load_over_table": "%s",\n' "$(json_escape "$STA_ARTIFACT_FANOUT_LOAD_OVER_TABLE")"
  printf '  "sta_artifact_lib_cap_axis": "%s",\n' "$(json_escape "$STA_ARTIFACT_LIB_CAP_AXIS")"
  printf '  "sta_artifact_lib_max_cap": "%s",\n' "$(json_escape "$STA_ARTIFACT_LIB_MAX_CAP")"
  printf '  "sta_artifact_fanout_sources": "%s",\n' "$(json_escape "$STA_ARTIFACT_FANOUT_SOURCES")"
  printf '  "sta_timing_verdict": "%s",\n' "$(json_escape "$STA_TIMING_VERDICT")"
  printf '  "sta_fsm_status": "%s",\n' "$(json_escape "$STA_FSM_STATUS")"
  printf '  "sta_fsm_locator": "%s",\n' "$(json_escape "$STA_FSM_LOCATOR")"
  printf '  "sta_fsm_cell_count": "%s",\n' "$(json_escape "$STA_FSM_CELL_COUNT")"
  printf '  "sta_fsm_cells": "%s",\n' "$(json_escape "$STA_FSM_CELLS")"
  printf '  "sta_fsm_cell_reg_total": "%s",\n' "$(json_escape "$STA_FSM_CELL_REG_TOTAL")"
  printf '  "sta_fsm_cell_clk_reg_total": "%s",\n' "$(json_escape "$STA_FSM_CELL_CLK_REG_TOTAL")"
  printf '  "sta_fsm_closure_register_count": "%s",\n' "$(json_escape "$STA_FSM_CLOSURE_REGISTER_COUNT")"
  printf '  "sta_fsm_clk_register_count": "%s",\n' "$(json_escape "$STA_FSM_CLK_REGISTER_COUNT")"
  printf '  "sta_fsm_report_clocks": "%s",\n' "$(json_escape "$STA_FSM_REPORT_CLOCKS")"
  printf '  "sta_fsm_path_status": "%s",\n' "$(json_escape "$STA_FSM_PATH_STATUS")"
  printf '  "sta_fsm_path_block_complete": "%s",\n' "$(json_escape "$STA_FSM_PATH_BLOCK_COMPLETE")"
  printf '  "sta_fsm_path_arrival_ns": "%s",\n' "$(json_escape "$STA_FSM_PATH_ARRIVAL_NS")"
  printf '  "sta_fsm_path_slack_ns": "%s",\n' "$(json_escape "$STA_FSM_PATH_SLACK_NS")"
  printf '  "sta_fsm_path_start": "%s",\n' "$(json_escape "$STA_FSM_PATH_START")"
  printf '  "sta_fsm_path_end": "%s",\n' "$(json_escape "$STA_FSM_PATH_END")"
  printf '  "sta_fsm_path_group": "%s",\n' "$(json_escape "$STA_FSM_PATH_GROUP")"
  printf '  "sta_fsm_path_stage_count": "%s",\n' "$(json_escape "$STA_FSM_PATH_STAGE_COUNT")"
  printf '  "sta_fsm_path_cell_types": "%s",\n' "$(json_escape "$STA_FSM_PATH_CELL_TYPES")"
  printf '  "sta_fsm_path_cell_histo": "%s",\n' "$(json_escape "$STA_FSM_PATH_CELL_HISTO")"
  printf '  "sta_fsm_path_stage_head": "%s",\n' "$(json_escape "$STA_FSM_PATH_STAGE_HEAD")"
  printf '  "sta_fsm_path_stage_head_truncated": "%s",\n' "$(json_escape "$STA_FSM_PATH_STAGE_HEAD_TRUNCATED")"
  printf '  "sta_fsm_path_max_arc_ns": "%s",\n' "$(json_escape "$STA_FSM_PATH_MAX_ARC_NS")"
  printf '  "sta_fsm_path_max_arc_pin": "%s",\n' "$(json_escape "$STA_FSM_PATH_MAX_ARC_PIN")"
  printf '  "sta_fsm_path_neg_arc_count": "%s",\n' "$(json_escape "$STA_FSM_PATH_NEG_ARC_COUNT")"
  printf '  "sta_fsm_path_out_of_table_arc_count": "%s",\n' "$(json_escape "$STA_FSM_PATH_OUT_OF_TABLE_ARC_COUNT")"
  printf '  "sta_fsm_own_path_status": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_STATUS")"
  printf '  "sta_fsm_own_path_block_complete": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_BLOCK_COMPLETE")"
  printf '  "sta_fsm_own_path_arrival_ns": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_ARRIVAL_NS")"
  printf '  "sta_fsm_own_path_slack_ns": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_SLACK_NS")"
  printf '  "sta_fsm_own_path_start": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_START")"
  printf '  "sta_fsm_own_path_end": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_END")"
  printf '  "sta_fsm_own_path_group": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_GROUP")"
  printf '  "sta_fsm_own_path_stage_count": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_STAGE_COUNT")"
  printf '  "sta_fsm_own_path_cell_types": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_CELL_TYPES")"
  printf '  "sta_fsm_own_path_cell_histo": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_CELL_HISTO")"
  printf '  "sta_fsm_own_path_stage_head": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_STAGE_HEAD")"
  printf '  "sta_fsm_own_path_stage_head_truncated": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_STAGE_HEAD_TRUNCATED")"
  printf '  "sta_fsm_own_path_max_arc_ns": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_MAX_ARC_NS")"
  printf '  "sta_fsm_own_path_max_arc_pin": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_MAX_ARC_PIN")"
  printf '  "sta_fsm_own_path_neg_arc_count": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_NEG_ARC_COUNT")"
  printf '  "sta_fsm_own_path_out_of_table_arc_count": "%s",\n' "$(json_escape "$STA_FSM_OWN_PATH_OUT_OF_TABLE_ARC_COUNT")"
  printf '  "sta_fsm_path_fanout_status": "%s",\n' "$(json_escape "$STA_FSM_PATH_FANOUT_STATUS")"
  printf '  "sta_fsm_path_fanout_row": "%s",\n' "$(json_escape "$STA_FSM_PATH_FANOUT_ROW")"
  printf '  "sta_fsm_path_fanout_lib": "%s",\n' "$(json_escape "$STA_FSM_PATH_FANOUT_LIB")"
  printf '  "sta_fsm_path_fanout_verdict": "%s",\n' "$(json_escape "$STA_FSM_PATH_FANOUT_VERDICT")"
  printf '  "sta_fsm_sta_path_ns": "%s",\n' "$(json_escape "$STA_FSM_STA_PATH_NS")"
  printf '  "sta_fsm_path_basis": "%s",\n' "$(json_escape "$STA_FSM_PATH_BASIS")"
  printf '  "sta_fsm_vs_abc_ratio": "%s",\n' "$(json_escape "$STA_FSM_VS_ABC_RATIO")"
  printf '  "sta_fsm_abc_delay_ps": "%s",\n' "$(json_escape "$STA_FSM_ABC_DELAY_PS")"
  printf '  "sta_fsm_abc_delay_source": "%s",\n' "$(json_escape "$STA_FSM_ABC_DELAY_SOURCE")"
  printf '  "sta_fsm_verdict": "%s",\n' "$(json_escape "$STA_FSM_VERDICT")"
  printf '  "sta_fsm_error": "%s",\n' "$(json_escape "$STA_FSM_ERROR")"
  printf '  "sta_netlist_port_mismatch_count": "%s",\n' "$(json_escape "$STA_NETLIST_PORT_MISMATCH_COUNT")"
  printf '  "sta_netlist_port_mismatch_sample": "%s",\n' "$(json_escape "$STA_NETLIST_PORT_MISMATCH_SAMPLE")"
  printf '  "sta_error": "%s",\n' "$(json_escape "$STA_ERROR")"
  printf '  "sta_link_result": "%s",\n' "$(json_escape "$STA_LINK_RESULT")"
  printf '  "sta_diag_status": "%s",\n' "$(json_escape "$STA_DIAG_STATUS")"
  printf '  "sta_diag_clock_port_count": "%s",\n' "$(json_escape "$STA_DIAG_CLOCK_PORT_COUNT")"
  printf '  "sta_diag_clock_count": "%s",\n' "$(json_escape "$STA_DIAG_CLOCK_COUNT")"
  printf '  "sta_diag_register_count": "%s",\n' "$(json_escape "$STA_DIAG_REGISTER_COUNT")"
  printf '  "sta_diag_data_input_count": "%s",\n' "$(json_escape "$STA_DIAG_DATA_INPUT_COUNT")"
  printf '  "sta_closure_top_name": "%s",\n' "$(json_escape "$STA_CLOSURE_TOP_NAME")"
  printf '  "sta_closure_top_found": "%s",\n' "$(json_escape "$STA_CLOSURE_TOP_FOUND")"
  printf '  "sta_read_ok_count": "%s",\n' "$(json_escape "$STA_READ_OK_COUNT")"
  printf '  "sta_read_skip_count": "%s",\n' "$(json_escape "$STA_READ_SKIP_COUNT")"
  printf '  "sta_closure_read_skip_count": "%s",\n' "$(json_escape "$STA_CLOSURE_READ_SKIP_COUNT")"
  printf '  "sta_body_check": "%s",\n' "$(json_escape "$STA_BODY_CHECK")"
  printf '  "sta_closure_bytes": "%s",\n' "$(json_escape "$STA_CLOSURE_BYTES")"
  printf '  "sta_closure_module_count": "%s",\n' "$(json_escape "$STA_CLOSURE_MODULE_COUNT")"
  printf '  "sta_blackbox_export_status": "%s",\n' "$(json_escape "$STA_BLACKBOX_EXPORT_STATUS")"
  printf '  "loop_classification": "%s",\n' "$(json_escape "$LOOP_CLASSIFICATION")"
  printf '  "functional_run": "%s",\n' "$(json_escape "$FUNCT_DIR")"
  printf '  "functional_seq_len": "%s",\n' "$(json_escape "$FUNCT_SEQ")"
  printf '  "functional_total_cycles": "%s",\n' "$(json_escape "$FUNCT_CYCLES")"
  printf '  "cycles_per_token": "%s",\n' "$(json_escape "$CYCLES_PER_TOKEN")"
  printf '  "tokens_per_sec_at_100mhz": "%s"\n' "$(json_escape "$TOKENS_PER_SEC")"
  printf '}\n'
} >"$LOCAL_RUN/summary.json"

{
  echo "task=$TASK"; echo "mode=$MODE"; echo "timeout_seconds=$TIMEOUT_SECONDS"; echo "run_id=$RUN_ID"; echo "exit_code=$RC";
  echo "measurement_scope=$SCOPE";
  echo "measurement_partition_selection=${REQUESTED_PARTITIONS:-all}";
  echo "measurement_type=pre-layout per-partition ABC estimate + best-effort top-level OpenSTA closure (Round-46)";
  echo "scheduler_audit=$SCHEDULER_AUDIT";
  echo "expected_partitions=$EXPECTED_PARTITIONS";
  echo "observed_partition_terminals=$OBSERVED_PARTITIONS";
  echo "--- per-partition exits ---";
  if (( ${#PART_RESULTS[@]} > 0 )); then printf '%s\n' "${PART_RESULTS[@]}"; else echo "(no partitions ran)"; fi
  echo "--- partition yosys errors (rc!=0 partitions) ---"; echo "$PARTITION_ERRORS";
  echo "top=nano_golden_decoder_top"; echo "liberty=Nangate45 typical";
  echo "--- area ---"; echo "$AREA";
  echo "logic_area_um2=$LOGIC_AREA_UM2";
  echo "logic_area_mm2=$LOGIC_AREA_MM2 (deduped per-module standard-cell sum, pre-layout)";
  echo "logic_area_unique_modules=$LOGIC_AREA_MODULES";
  echo "logic_area_budget_mm2=$LOGIC_AREA_BUDGET_MM2";
  echo "logic_area_budget_ok=$LOGIC_AREA_BUDGET_OK";
  echo "total_area_mm2_est=$TOTAL_AREA_MM2 (logic_area_mm2 + macro_area_mm2)";
  echo "--- SRAM/ROM macro blackbox partitions (no ABC) ---"; echo "$MACRO_LINES";
  echo "--- timing (per-partition ABC wire-load delay) ---"; echo "$DELAY";
  echo "--- critical path vs 10 ns (pre-layout partition estimate) ---";
  echo "max_abc_delay_ps=$MAX_DELAY_PS"; echo "max_abc_delay_ns=$MAX_DELAY_NS";
  echo "max_abc_delay_source=$MAX_DELAY_SOURCE";
  echo "raw_max_abc_delay_ps=$RAW_MAX_DELAY_PS (incl. non-physical >=1e6 ps artifacts)";
  echo "delay_artifact_count=$DELAY_ARTIFACT_COUNT";
  echo "delay_artifacts=$DELAY_ARTIFACTS";
  echo "--- critical-path source classification (stat -liberty sequential share; only registered_path vs 10 ns) ---";
  echo "$CRIT_PATH_SOURCES";
  echo "critical_path_class=$CRIT_PATH_CLASS";
  echo "critical_path_seq_pct=$CRIT_PATH_SEQ_PCT";
  echo "critical_path_ps=$CRIT_PATH_PS (critical-path basis: largest registered_path sub-1e6, else largest sub-1e6 delay)";
  echo "critical_path_ns=$CRIT_PATH_NS";
  echo "critical_path_source=$CRIT_PATH_SOURCE";
  echo "largest_registered_path_ps=$REGISTERED_CRIT_PS";
  echo "largest_combinational_block_ps=$COMB_MAX_PS";
  echo "largest_combinational_block_source=$COMB_MAX_SRC";
  echo "seq_ratio_threshold_pct=$SEQ_RATIO_THRESHOLD_PCT";
  echo "est_freq_mhz=$EST_FREQ_MHZ (derived only from a registered_path; else 未测得)";
  echo "meets_10ns=$MEETS_10NS (only meaningful for a registered_path)";
  echo "--- combinational loop classification (check verdict; unobserved = partition never reached check this round) ---";
  echo "$LOOP_CLASSIFICATION";
  echo "--- top-level closure STA (OpenSTA pre-layout, Round-46; best-effort, does not change round exit) ---";
  echo "sta_status=$STA_STATUS";
  echo "sta_crit_path_ps=$STA_CRIT_PATH_PS (largest in-table, physically credible path data-arrival delay; out-of-table extrapolation artifacts are excluded -- see sta_artifact_*)";
  echo "sta_crit_path_ns=$STA_CRIT_PATH_NS (same credible path in ns)";
  echo "sta_crit_path_start=$STA_CRIT_PATH_START (startpoint of the reported credible path)";
  echo "sta_crit_path_end=$STA_CRIT_PATH_END (endpoint of the reported credible path)";
  echo "sta_crit_path_group=$STA_CRIT_PATH_GROUP (path group: clk = registered, asynchronous = recovery/removal)";
  echo "sta_crit_path_class=$STA_CRIT_PATH_CLASS (registered = the credible worst path's own group is clk; non_registered = its group is boundary/async, so sta_est_freq_mhz can still come from a separate credible clk-group path)";
  echo "sta_crit_path_slack_ns=$STA_CRIT_PATH_SLACK_NS (slack of the credible path at the 10 ns clock)";
  echo "sta_raw_max_arrival_ps=$STA_RAW_MAX_ARRIVAL_PS (unfiltered maximum arrival over every reported path; this is the pre-Round-76 sta_crit_path_ps)";
  echo "sta_raw_min_slack_ns=$STA_RAW_MIN_SLACK_NS (unfiltered worst slack; negative = the raw netlist does not meet 10 ns)";
  echo "sta_credible_path_count=$STA_CREDIBLE_PATH_COUNT (reported paths inside the Nangate45 Liberty tables)";
  echo "sta_credible_meets_10ns=$STA_CREDIBLE_MEETS_10NS (verdict of the credible path only; NEVER a full-chip Fmax)";
  echo "sta_unbuffered_fanout_artifact=$STA_UNBUFFERED_FANOUT_ARTIFACT (yes = report_checks saw out-of-table load/slew extrapolation on un-buffered high-fanout nets)";
  echo "sta_artifact_path_count=$STA_ARTIFACT_PATH_COUNT (reported paths excluded as out-of-table artifacts: arrival>=1e6 ps or a cell arc>=1000 ps or a negative arc)";
  echo "sta_artifact_max_arrival_ps=$STA_ARTIFACT_MAX_ARRIVAL_PS (raw arrival of the worst excluded path)";
  echo "sta_artifact_max_arrival_start=$STA_ARTIFACT_MAX_ARRIVAL_START (startpoint of the worst excluded path)";
  echo "sta_artifact_max_arrival_end=$STA_ARTIFACT_MAX_ARRIVAL_END (endpoint of the worst excluded path)";
  echo "sta_artifact_max_arrival_group=$STA_ARTIFACT_MAX_ARRIVAL_GROUP (path group of the worst excluded path)";
  echo "sta_artifact_worst_arc_pin=$STA_ARTIFACT_WORST_ARC_PIN (largest single-arc pin of the worst excluded path; the un-buffered fanout site)";
  echo "sta_artifact_worst_arc_ps=$STA_ARTIFACT_WORST_ARC_PS (that arc's increment; Nangate45 Liberty cannot express it)";
  echo "sta_artifact_setup_ns=$STA_ARTIFACT_SETUP_NS (library setup/recovery time of the worst excluded path; negative beyond -1 ns = setup table extrapolated)";
  echo "sta_artifact_negative_arc_count=$STA_ARTIFACT_NEG_ARC_COUNT (negative cell arcs in the worst excluded path; a standard cell cannot have one)";
  echo "sta_artifact_fanout_verdict=$STA_ARTIFACT_FANOUT_VERDICT (out_of_table_extrapolation = the MEASURED load of the artifact driver net exceeds the driver cell's own capacitance table axis, so report_checks extrapolated and the arc is an un-buffered-fanout mapping artifact, not logic depth; in_table = the measurement refutes that hypothesis; anything else = undecided)";
  echo "sta_artifact_fanout_status=$STA_ARTIFACT_FANOUT_STATUS (ok = the audit resolved on the exact netlist OpenSTA read; otherwise the fanout fields stay 未测得)";
  echo "sta_artifact_fanout_net=$STA_ARTIFACT_FANOUT_NET (driver net actually measured in that netlist)";
  echo "sta_artifact_fanout_driver_cell=$STA_ARTIFACT_FANOUT_DRIVER_CELL (standard cell driving it)";
  echo "sta_artifact_fanout_pins=$STA_ARTIFACT_FANOUT_PINS (connections counted on that net in its declaring module, minus the driver's own output)";
  echo "sta_artifact_fanout_load=$STA_ARTIFACT_FANOUT_LOAD (summed Liberty capacitance of those connected pins; pre-layout, wire load 0)";
  echo "sta_artifact_fanout_load_unit=$STA_ARTIFACT_FANOUT_LOAD_UNIT (capacitance unit declared by the Liberty)";
  echo "sta_artifact_fanout_load_resolved_pins=$STA_ARTIFACT_FANOUT_LOAD_RESOLVED_PINS (connected pins with a Liberty capacitance)";
  echo "sta_artifact_fanout_load_unresolved_pins=$STA_ARTIFACT_FANOUT_LOAD_UNRESOLVED_PINS (connected pins without one; non-zero makes the load a lower bound)";
  echo "sta_artifact_lib_cap_axis=$STA_ARTIFACT_LIB_CAP_AXIS (Liberty table axis the load was compared against)";
  echo "sta_artifact_lib_max_cap=$STA_ARTIFACT_LIB_MAX_CAP (largest value of that axis in the driver cell's own delay tables)";
  echo "sta_artifact_fanout_load_over_table=$STA_ARTIFACT_FANOUT_LOAD_OVER_TABLE (measured load / that axis maximum; above 1 = out-of-table)";
  echo "sta_artifact_fanout_sources=$STA_ARTIFACT_FANOUT_SOURCES (raw per-source audit records; assembled pre-layout partial closure netlist)";
  echo "sta_timing_verdict=$STA_TIMING_VERDICT (credible_in_table_path_* = sta_crit_path_ps is this report's largest in-table path, with the out-of-table artifact measured or unresolved; still PRE-LAYOUT PARTIAL_HIERARCHICAL, never a chip Fmax)";
  echo "sta_fsm_status=$STA_FSM_STATUS (ok = the FSM reconciliation block ran in the linked closure STA process; error/unavailable means every sta_fsm_* field below stays 未测得)";
  echo "sta_fsm_locator=$STA_FSM_LOCATOR (ref_name_filter = get_cells -hier -filter ref_name =~ *fsm*; cell_name_glob / instance_name_dp = fallbacks; none = no fsm instance found)";
  echo "sta_fsm_cell_count=$STA_FSM_CELL_COUNT (fsm instances resolved in the linked closure design)";
  echo "sta_fsm_cells=$STA_FSM_CELLS (per-instance index/path/ref/regs/clk_regs, up to 8; clk_regs is all_registers -cells <inst> -clock clk)";
  echo "sta_fsm_cell_reg_total=$STA_FSM_CELL_REG_TOTAL (sum of the located fsm instances' registers)";
  echo "sta_fsm_cell_clk_reg_total=$STA_FSM_CELL_CLK_REG_TOTAL (of those, the ones clocked by clk; 0 => no register-to-register fsm path exists to measure)";
  echo "sta_fsm_closure_register_count=$STA_FSM_CLOSURE_REGISTER_COUNT (all_registers of the whole linked closure netlist)";
  echo "sta_fsm_clk_register_count=$STA_FSM_CLK_REGISTER_COUNT (all_registers -clock clk of the whole linked closure netlist)";
  echo "sta_fsm_report_clocks=$STA_FSM_REPORT_CLOCKS (report_clocks of the linked design, bounded)";
  echo "sta_fsm_path_status=$STA_FSM_PATH_STATUS (attempted|error for the whole-design clk-group report)";
  echo "sta_fsm_path_block_complete=$STA_FSM_PATH_BLOCK_COMPLETE (1 = the clk-group path block reached its END marker; 0 = truncated, e.g. by a STA timeout)";
  echo "sta_fsm_path_arrival_ns=$STA_FSM_PATH_ARRIVAL_NS (whole-design clk-group worst path arrival, in-table-or-not as reported)";
  echo "sta_fsm_path_slack_ns=$STA_FSM_PATH_SLACK_NS (same path's slack vs the 10 ns clock)";
  echo "sta_fsm_path_start=$STA_FSM_PATH_START (startpoint of that clk-group worst path)";
  echo "sta_fsm_path_end=$STA_FSM_PATH_END (endpoint of that clk-group worst path)";
  echo "sta_fsm_path_group=$STA_FSM_PATH_GROUP (path group of that path)";
  echo "sta_fsm_path_stage_count=$STA_FSM_PATH_STAGE_COUNT (per-stage arcs parsed in that path; each is one cell/pin delay)";
  echo "sta_fsm_path_cell_types=$STA_FSM_PATH_CELL_TYPES (distinct Nangate45 cell types on that path)";
  echo "sta_fsm_path_cell_histo=$STA_FSM_PATH_CELL_HISTO (cell:count histogram of that path, first-seen order)";
  echo "sta_fsm_path_stage_head=$STA_FSM_PATH_STAGE_HEAD (the COMPLETE per-stage arc chain of that path, verbatim: incr delay, cumulative time, edge, pin, cell -- every arc up to 1024 lines)";
  echo "sta_fsm_path_stage_head_truncated=$STA_FSM_PATH_STAGE_HEAD_TRUNCATED (1 = the chain exceeded the 1024-arc bound, so the last stages are missing; 0 = the chain above is complete)";
  echo "sta_fsm_path_max_arc_ns=$STA_FSM_PATH_MAX_ARC_NS (largest single cell arc in that path)";
  echo "sta_fsm_path_max_arc_pin=$STA_FSM_PATH_MAX_ARC_PIN (pin of that largest arc)";
  echo "sta_fsm_path_neg_arc_count=$STA_FSM_PATH_NEG_ARC_COUNT (negative arcs in that path; a standard cell cannot have one)";
  echo "sta_fsm_path_out_of_table_arc_count=$STA_FSM_PATH_OUT_OF_TABLE_ARC_COUNT (arcs >= 1000 ns in that path: outside the Nangate45 tables)";
  echo "sta_fsm_own_path_status=$STA_FSM_OWN_PATH_STATUS (attempted|error|no_fsm_registers for the fsm-endpoint-restricted report)";
  echo "sta_fsm_own_path_block_complete=$STA_FSM_OWN_PATH_BLOCK_COMPLETE (1 = the fsm-restricted path block reached its END marker)";
  echo "sta_fsm_own_path_arrival_ns=$STA_FSM_OWN_PATH_ARRIVAL_NS (worst path ending at an fsm register data pin)";
  echo "sta_fsm_own_path_slack_ns=$STA_FSM_OWN_PATH_SLACK_NS";
  echo "sta_fsm_own_path_start=$STA_FSM_OWN_PATH_START";
  echo "sta_fsm_own_path_end=$STA_FSM_OWN_PATH_END";
  echo "sta_fsm_own_path_group=$STA_FSM_OWN_PATH_GROUP";
  echo "sta_fsm_own_path_stage_count=$STA_FSM_OWN_PATH_STAGE_COUNT";
  echo "sta_fsm_own_path_cell_types=$STA_FSM_OWN_PATH_CELL_TYPES";
  echo "sta_fsm_own_path_cell_histo=$STA_FSM_OWN_PATH_CELL_HISTO";
  echo "sta_fsm_own_path_stage_head=$STA_FSM_OWN_PATH_STAGE_HEAD";
  echo "sta_fsm_own_path_stage_head_truncated=$STA_FSM_OWN_PATH_STAGE_HEAD_TRUNCATED (1 = the chain exceeded the 1024-arc bound; 0 = complete)";
  echo "sta_fsm_own_path_max_arc_ns=$STA_FSM_OWN_PATH_MAX_ARC_NS";
  echo "sta_fsm_own_path_max_arc_pin=$STA_FSM_OWN_PATH_MAX_ARC_PIN";
  echo "sta_fsm_own_path_neg_arc_count=$STA_FSM_OWN_PATH_NEG_ARC_COUNT";
  echo "sta_fsm_own_path_out_of_table_arc_count=$STA_FSM_OWN_PATH_OUT_OF_TABLE_ARC_COUNT";
  echo "sta_fsm_path_fanout_status=$STA_FSM_PATH_FANOUT_STATUS (ok = the Round-80/85 fanout+load audit resolved sta_fsm_path_max_arc_pin's driver net in the exact netlist OpenSTA read; source_name_unsupported/source_unavailable/unresolved otherwise, and then the fields below stay 未测得)";
  echo "sta_fsm_path_fanout_row=$STA_FSM_PATH_FANOUT_ROW (verbatim STAFANOUT row for that driver: cell, pin, net, fanout, load, resolved/unresolved pins)";
  echo "sta_fsm_path_fanout_lib=$STA_FSM_PATH_FANOUT_LIB (verbatim STAFANOUT_LIB row: the driver cell's own delay-table capacitance axis and its maximum)";
  echo "sta_fsm_path_fanout_verdict=$STA_FSM_PATH_FANOUT_VERDICT (out_of_table_extrapolation = the MEASURED load of that clk-group-path driver exceeds its cell's capacitance axis, so report_checks extrapolated the table and the arc is an un-buffered-fanout mapping artifact, not logic depth; in_table = the measurement refutes that for this driver; load_partially_resolved / fanout_measured_library_limit_unavailable = undecided)";
  echo "sta_fsm_sta_path_ns=$STA_FSM_STA_PATH_NS (the in-table STA number this round compares against ABC: fsm-restricted path when available, else the clk group)";
  echo "sta_fsm_path_basis=$STA_FSM_PATH_BASIS (which path sta_fsm_sta_path_ns came from)";
  echo "sta_fsm_abc_delay_ps=$STA_FSM_ABC_DELAY_PS (largest ABC stime Delay among modules whose name mentions fsm, this round)";
  echo "sta_fsm_abc_delay_source=$STA_FSM_ABC_DELAY_SOURCE (partition/module/delay that ABC number belongs to)";
  echo "sta_fsm_vs_abc_ratio=$STA_FSM_VS_ABC_RATIO (sta_fsm_sta_path_ns / (abc_delay_ps/1000); <<1 = the ABC delay is not reproducible as an in-table Liberty path, ~1 = consistent)";
  echo "sta_fsm_verdict=$STA_FSM_VERDICT (abc_delay_not_reproduced_in_liberty_tables | abc_delay_consistent_with_sta | sta_path_slower_than_abc_delay | sta_fsm_path_unobserved_no_clk_registers | fsm_instance_not_found | fsm_path_unmeasured | abc_delay_unmeasured; all statements about the assembled PRE-LAYOUT PARTIAL_HIERARCHICAL closure netlist, never a chip Fmax)";
  echo "sta_fsm_error=$STA_FSM_ERROR";
  echo "sta_netlist_port_mismatch_count=$STA_NETLIST_PORT_MISMATCH_COUNT (STA-0200 port/net width mismatches in the read set; non-zero means per-net loads may be aliased)";
  echo "sta_netlist_port_mismatch_sample=$STA_NETLIST_PORT_MISMATCH_SAMPLE (first mismatch: instance/port/port_bits/net_bits)";
  echo "sta_est_freq_mhz=$STA_EST_FREQ_MHZ (1000/credible_crit_path_ns; only from a credible clk-group registered path, else 未测得)";
  echo "sta_meets_10ns=$STA_MEETS_10NS (raw unfiltered verdict: the worst slack of the whole report vs the 10 ns clock; filtering never fixes un-buffered fanout)";
  echo "sta_timing_measured=$STA_TIMING_MEASURED (yes = sta_crit_path_ps/sta_meets_10ns derived from a real report_checks run)";
  echo "sta_timing_path_count=$STA_TIMING_PATH_COUNT (report_checks paths parsed between STA_TIMING_BEGIN/END)";
  echo "sta_error=$STA_ERROR";
  echo "sta_link_result=$STA_LINK_RESULT (ok|error; link failure is never masked as ok_no_path)";
  echo "sta_diag_status=$STA_DIAG_STATUS (ok = clock/register/input structural probe completed in the real linked STA design)";
  echo "sta_diag_clock_port_count=$STA_DIAG_CLOCK_PORT_COUNT (ports named clk before create_clock)";
  echo "sta_diag_clock_count=$STA_DIAG_CLOCK_COUNT (created/analyzed clocks)";
  echo "sta_diag_register_count=$STA_DIAG_REGISTER_COUNT (all_registers after link; 0 suggests sequential logic is absent/abstracted)";
  echo "sta_diag_data_input_count=$STA_DIAG_DATA_INPUT_COUNT (all_inputs -no_clocks)";
  echo "sta_closure_top_name=$STA_CLOSURE_TOP_NAME (link_design target resolved from the normalized closure netlist)";
  echo "sta_closure_top_found=$STA_CLOSURE_TOP_FOUND (1 = top declared in normalized closure netlist; 0 = link target missing)";
  echo "sta_read_ok_count=$STA_READ_OK_COUNT";
  echo "sta_read_skip_count=$STA_READ_SKIP_COUNT";
  echo "sta_closure_read_skip_count=$STA_CLOSURE_READ_SKIP_COUNT";
  echo "sta_body_check=$STA_BODY_CHECK (control-plane body declared in ppa_sta_all.v; missing = ORD-2013 candidate)";
  echo "sta_closure_bytes=$STA_CLOSURE_BYTES (exact generated build/ppa_closure_top.v size; 0 = empty/missing export)";
  echo "sta_closure_module_count=$STA_CLOSURE_MODULE_COUNT (module declarations in generated closure export)";
  echo "sta_blackbox_export_status=$STA_BLACKBOX_EXPORT_STATUS (Yosys-normalized blackbox declaration export)";
  echo "target_period_ns=10.0"; echo "meets_10ns=$MEETS_10NS"; echo "est_freq_mhz=$EST_FREQ_MHZ";
  echo "--- memory (SRAM/ROM bits) ---"; echo "$MEM_DISPLAY";
  echo "sram_total_bits=$MEM_BITS"; echo "sram_total_mib=$SRAM_MIB";
  echo "macro_area_mm2=$MACRO_AREA_MM2 (model ~1 Mbit/mm2)";
  echo "--- throughput (derived from real functional log) ---";
  echo "functional_run=$FUNCT_DIR"; echo "functional_seq_len=$FUNCT_SEQ"; echo "functional_total_cycles=$FUNCT_CYCLES";
  echo "cycles_per_token=$CYCLES_PER_TOKEN"; echo "tokens_per_sec_at_100mhz=$TOKENS_PER_SEC (target 20000)";
  echo "--- yosys log tail ---"; tail -80 "$LOCAL_RUN/yosys.log";
} >"$LOCAL_RUN/feedback.txt"
STATE=passed; [ "$RC" -eq 0 ] || STATE=failed
retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" "printf '%s\n' 'state=$STATE' 'task=$TASK' 'run_id=$RUN_ID' 'exit_code=$RC' 'container=$CONTAINER' > '$RROOT/status/current.status'" || true
echo "run_id=$RUN_ID"
echo "feedback=$LOCAL_RUN/feedback.txt"
exit "$RC"
