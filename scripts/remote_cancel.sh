#!/usr/bin/env bash
# Cancel only the harness run named by the remote status file.  It never stops
# the k3demo-eda container or any unrelated server project.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
eval "$(python3 "$ROOT/harness/environment.py" --shell)"
REMOTE_HOST=$HARNESS_REMOTE_HOST
REMOTE_PROJECT=$HARNESS_REMOTE_PROJECT
CONTAINER=$HARNESS_CONTAINER
CONTAINER_WORKDIR=$HARNESS_CONTAINER_WORKDIR
DRY_RUN=0
EXPECTED_TASK=""

usage() {
  cat <<'EOF'
Usage: bash scripts/remote_cancel.sh [--dry-run] [--task TASK]

Reads the current run-id from the remote harness status file and sends TERM,
then (only if necessary) KILL to container processes belonging to exactly that
run.  It does not stop the Docker container or unrelated projects.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --task) EXPECTED_TASK="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -z "$EXPECTED_TASK" || "$EXPECTED_TASK" =~ ^[A-Za-z0-9._-]+$ ]] || {
  echo "--task may contain only letters, numbers, dot, underscore, and hyphen" >&2
  exit 2
}

ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_HOST" \
  "REMOTE_PROJECT='$REMOTE_PROJECT' CONTAINER='$CONTAINER' CONTAINER_WORKDIR='$CONTAINER_WORKDIR' EXPECTED_TASK='$EXPECTED_TASK' DRY_RUN='$DRY_RUN' sh -s" <<'REMOTE_SCRIPT'
set -eu

status="$REMOTE_PROJECT/status/current.status"
[ -f "$status" ] || { echo "no harness status file: $status"; exit 1; }
run_id=$(sed -n 's/^run_id=//p' "$status" | head -n 1)
task=$(sed -n 's/^task=//p' "$status" | head -n 1)
state=$(sed -n 's/^state=//p' "$status" | head -n 1)

case "$run_id" in
  ''|*[!A-Za-z0-9._-]*) echo "refusing unsafe run_id from status: $run_id" >&2; exit 2 ;;
esac
case "$task" in
  ''|*[!A-Za-z0-9._-]*) echo "refusing unsafe task from status: $task" >&2; exit 2 ;;
esac
[ -z "$EXPECTED_TASK" ] || [ "$EXPECTED_TASK" = "$task" ] || {
  echo "refusing: status task is $task, not requested $EXPECTED_TASK" >&2
  exit 2
}

echo "target task=$task run_id=$run_id previous_state=$state"
if [ "$DRY_RUN" = 1 ]; then
  docker exec -e HARNESS_RUN_ID="$run_id" -e HARNESS_CONTAINER_WORKDIR="$CONTAINER_WORKDIR" "$CONTAINER" sh -lc '
    ps -eo pid=,etime=,args= | awk -v run="$HARNESS_RUN_ID" \
      "index(\$0, \"/runs/\" run \"/\") || index(\$0, \"runs/\" run) || (index(\$0, \"harness/run.py\") && index(\$0, \"--run-id \" run))"
  ' || true
  exit 0
fi

# A finished run has no cancellable work.  Do not overwrite its historical
# failed/passed/cancelled state merely because someone invokes this script late.
case "$state" in
  running|diagnosing) ;;
  *) echo "status is $state; no active run will be cancelled"; exit 0 ;;
esac

# The script text contains no expanded run-id, so the inspection shell cannot
# accidentally match and kill itself.  Only the target process receives it via
# HARNESS_RUN_ID.
docker exec -e HARNESS_RUN_ID="$run_id" -e HARNESS_CONTAINER_WORKDIR="$CONTAINER_WORKDIR" "$CONTAINER" sh -lc '
  find_pids() {
    pid_file="$HARNESS_CONTAINER_WORKDIR/runs/$HARNESS_RUN_ID/container.pid"
    if [ -f "$pid_file" ]; then
      pid=$(sed -n "1p" "$pid_file")
      case "$pid" in
        ""|*[!0-9]*) ;;
        *) kill -0 "$pid" 2>/dev/null && echo "$pid" ;;
      esac
    fi
    ps -eo pid=,stat=,args= | awk -v run="$HARNESS_RUN_ID" \
      "\$2 !~ /^Z/ && (index(\$0, \"/runs/\" run \"/\") || index(\$0, \"runs/\" run) || (index(\$0, \"harness/run.py\") && index(\$0, \"--run-id \" run))) {print \$1}"
  }
  pids=$(find_pids | sort -u || true)
  if [ -z "$pids" ]; then
    echo "no active container process belongs to run $HARNESS_RUN_ID"
    exit 0
  fi
  echo "sending TERM to: $pids"
  kill -TERM $pids 2>/dev/null || true
  sleep 3
  pids=$(find_pids | sort -u || true)
  if [ -n "$pids" ]; then
    echo "sending KILL to remaining: $pids"
    kill -KILL $pids 2>/dev/null || true
  fi
'

updated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
mkdir -p "$REMOTE_PROJECT/status"
printf '%s\n' \
  'state=cancelled' \
  "task=$task" \
  "run_id=$run_id" \
  "updated_at=$updated_at" \
  'exit_code=130' \
  "container=$CONTAINER" > "$status"
echo "marked cancelled: $run_id"
REMOTE_SCRIPT
