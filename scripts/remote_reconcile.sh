#!/usr/bin/env bash
# Reconcile the single remote current.status before a new local controller starts.
# Only the exact run-id recorded there is eligible for cancellation; server runs
# remain evidence and are never deleted.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
eval "$(python3 "$ROOT/harness/environment.py" --shell)"
REMOTE=$HARNESS_REMOTE_HOST
RROOT=$HARNESS_REMOTE_PROJECT
CONTAINER=$HARNESS_CONTAINER
CONTAINER_WORKDIR=$HARNESS_CONTAINER_WORKDIR
INFRA_UNCERTAIN_EXIT=76
WALL_SECONDS=120

marker=$(mktemp "${TMPDIR:-/tmp}/rtl-harness-reconcile.XXXXXX")
rm -f "$marker"
# This invocation intentionally keeps stdin: it receives REMOTE_SCRIPT below.
# Other one-line SSH probes may use -n, but here it would discard the script.
ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 "$REMOTE" \
  "REMOTE_PROJECT=$(printf %q "$RROOT") CONTAINER=$(printf %q "$CONTAINER") CONTAINER_WORKDIR=$(printf %q "$CONTAINER_WORKDIR") sh -s" <<'REMOTE_SCRIPT' &
set -eu
status="$REMOTE_PROJECT/status/current.status"
[ -f "$status" ] || { echo "reconcile=none"; exit 0; }
run_id=$(sed -n 's/^run_id=//p' "$status" | head -n 1)
task=$(sed -n 's/^task=//p' "$status" | head -n 1)
state=$(sed -n 's/^state=//p' "$status" | head -n 1)
case "$run_id" in ''|*[!A-Za-z0-9._-]*) echo "reconcile=uncertain invalid_run_id"; exit 76 ;; esac
case "$task" in ''|*[!A-Za-z0-9._-]*) echo "reconcile=uncertain invalid_task"; exit 76 ;; esac
# A cancellation request can race with the process actually exiting, so it is
# also reconciled.  Only known terminal results may bypass the PID check.
case "$state" in
  running|diagnosing|infra_uncertain|cancelled) ;;
  passed|failed) echo "reconcile=settled state=$state run_id=$run_id"; exit 0 ;;
  *) echo "reconcile=uncertain invalid_state=$state"; exit 76 ;;
esac

# A local SSH parent can disappear after the container has already committed
# its authoritative exit_code.  That is a completed measurement, not a live
# process to cancel.  Reconcile the exact status run-id first; never infer a
# terminal result from a directory name or scan another run.
terminal_rc=$(docker exec -e HARNESS_RUN_ID="$run_id" -e HARNESS_CONTAINER_WORKDIR="$CONTAINER_WORKDIR" "$CONTAINER" sh -lc '
  f="$HARNESS_CONTAINER_WORKDIR/runs/$HARNESS_RUN_ID/exit_code"
  test -f "$f" && sed -n "1p" "$f" || true
' 2>/dev/null || true)
case "$terminal_rc" in
  0|[1-9][0-9]*)
    updated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    terminal_state=failed
    [ "$terminal_rc" = 0 ] && terminal_state=passed
    printf '%s\n' "state=$terminal_state" "task=$task" "run_id=$run_id" "updated_at=$updated_at" \
      "exit_code=$terminal_rc" "container=$CONTAINER" > "$status"
    echo "reconcile=settled state=$terminal_state run_id=$run_id exit_code=$terminal_rc"
    exit 0
    ;;
  "") ;;
  *) echo "reconcile=uncertain invalid_exit_code=$terminal_rc"; exit 76 ;;
esac

docker exec -e HARNESS_RUN_ID="$run_id" -e HARNESS_CONTAINER_WORKDIR="$CONTAINER_WORKDIR" "$CONTAINER" sh -lc '
  find_pids() {
    pid_file="$HARNESS_CONTAINER_WORKDIR/runs/$HARNESS_RUN_ID/container.pid"
    if [ -f "$pid_file" ]; then
      pid=$(sed -n "1p" "$pid_file")
      case "$pid" in ""|*[!0-9]*) ;; *) kill -0 "$pid" 2>/dev/null && echo "$pid" ;; esac
    fi
    ps -eo pid=,stat=,args= | awk -v run="$HARNESS_RUN_ID" \
      "\$2 !~ /^Z/ && (index(\$0, \"/runs/\" run \"/\") || index(\$0, \"runs/\" run) || (index(\$0, \"harness/run.py\") && index(\$0, \"--run-id \" run))) {print \$1}"
  }
  pids=$(find_pids | sort -u || true)
  if [ -n "$pids" ]; then
    echo "reconcile=terminating pids=$pids"
    kill -TERM $pids 2>/dev/null || true
    sleep 3
    pids=$(find_pids | sort -u || true)
    if [ -n "$pids" ]; then kill -KILL $pids 2>/dev/null || true; sleep 1; fi
  fi
  pids=$(find_pids | sort -u || true)
  [ -z "$pids" ] || { echo "reconcile=uncertain live_pids=$pids"; exit 76; }
'
rc=$?
if [ "$rc" -ne 0 ]; then echo "reconcile=uncertain cancel_rc=$rc"; exit 76; fi
updated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\n' 'state=cancelled' "task=$task" "run_id=$run_id" "updated_at=$updated_at" 'exit_code=130' "container=$CONTAINER" > "$status"
echo "reconcile=cancelled run_id=$run_id task=$task"
REMOTE_SCRIPT
command_pid=$!
(
  sleep "$WALL_SECONDS"
  if kill -0 "$command_pid" 2>/dev/null; then : > "$marker"; kill -TERM "$command_pid" 2>/dev/null || true; sleep 5; kill -KILL "$command_pid" 2>/dev/null || true; fi
) &
watchdog=$!
if wait "$command_pid"; then rc=0; else rc=$?; fi
kill "$watchdog" 2>/dev/null || true
wait "$watchdog" 2>/dev/null || true
if [[ -f "$marker" ]]; then
  echo "reconcile=uncertain local_watchdog"
  rc=$INFRA_UNCERTAIN_EXIT
fi
rm -f "$marker"
[[ "$rc" == 0 ]] || exit "$INFRA_UNCERTAIN_EXIT"
