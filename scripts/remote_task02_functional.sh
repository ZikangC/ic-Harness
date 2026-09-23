#!/usr/bin/env bash
# Run Task-02 functional smoke/full checks from the current RTLharness tree only.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
INFRA_EXIT=75
INFRA_UNCERTAIN_EXIT=76
ENV_EXPORT=""
if ENV_EXPORT=$(python3 "$ROOT/harness/environment.py" --shell); then
  :
else
  ENV_RC=$?
  printf 'environment export failed before remote functional run (exit=%d)\n' "$ENV_RC" >&2
  exit "$INFRA_EXIT"
fi
if ! eval "$ENV_EXPORT"; then
  printf 'environment export evaluation failed before remote functional run\n' >&2
  exit "$INFRA_EXIT"
fi
if [[ -z "${HARNESS_REMOTE_HOST:-}" || -z "${HARNESS_REMOTE_PROJECT:-}" ||
      -z "${HARNESS_CONTAINER:-}" || -z "${HARNESS_CONTAINER_WORKDIR:-}" ]]; then
  printf 'environment.py did not export the complete harness environment\n' >&2
  exit "$INFRA_EXIT"
fi
REMOTE=$HARNESS_REMOTE_HOST
RROOT=$HARNESS_REMOTE_PROJECT
CONTAINER=$HARNESS_CONTAINER
CONTAINER_WORKDIR=$HARNESS_CONTAINER_WORKDIR
TRANSPORT_RETRIES=3
REMOTE_SSH_TIMEOUT_SECONDS=30
FUNCTIONAL_TIMEOUT_SECONDS=3600
SSH_FUNCTIONAL_GRACE_SECONDS=90
SSH_RECOVERY_TIMEOUT_SECONDS=180
PROMPT=1
SEQ_LEN=8

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prompt) PROMPT=$2; shift 2 ;;
    --seq-len) SEQ_LEN=$2; shift 2 ;;
    --run-id) RUN_ID=$2; shift 2 ;;
    --timeout-seconds) FUNCTIONAL_TIMEOUT_SECONDS=$2; shift 2 ;;
    *) echo "usage: $0 [--prompt 0|1] [--seq-len N] [--run-id ID] [--timeout-seconds N]" >&2; exit 2 ;;
  esac
done
[[ -n "${RUN_ID:-}" ]] || RUN_ID="02_yosys_ppa_functional--$(date +%Y%m%d-%H%M%S)--p${PROMPT}--prefix${SEQ_LEN}--$$"
[[ "$PROMPT" == 0 || "$PROMPT" == 1 ]] || { echo "--prompt must be 0 or 1" >&2; exit 2; }
[[ "$SEQ_LEN" =~ ^[1-9][0-9]*$ ]] || { echo "--seq-len must be positive" >&2; exit 2; }
[[ "$FUNCTIONAL_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || { echo "--timeout-seconds must be positive" >&2; exit 2; }
[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "--run-id contains unsafe characters" >&2; exit 2; }

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

# Direct callers get the same startup takeover safety as ppa_agent_loop.py.
bash "$ROOT/scripts/remote_reconcile.sh" || exit $?

# ConnectTimeout covers only TCP setup.  This local watchdog also bounds an
# established-but-silent SSH/docker-exec stream.  The container command below
# has its own timeout and persistent exit-code; after a watchdog fire we read
# that evidence with a fresh SSH connection instead of resending the run.
SSH_LAST_TIMED_OUT=0
ssh_with_wall_timeout() {
  local wall_seconds=$1 marker command_pid watchdog rc=0
  shift
  marker=$(mktemp "${TMPDIR:-/tmp}/rtl-harness-functional-ssh.XXXXXX") || return 1
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
  [[ -f "$marker" ]] && SSH_LAST_TIMED_OUT=1
  rm -f "$marker"
  return "$rc"
}

remote_ssh() {
  local wall_seconds=$1
  shift
  ssh_with_wall_timeout "$wall_seconds" \
    ssh -n -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
    "$REMOTE" "$@"
}

RECOVERED_RC=""
recover_functional_exit() {
  local attempt value
  RECOVERED_RC=""
  for ((attempt = 1; attempt <= TRANSPORT_RETRIES; attempt++)); do
    value=$(remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
      "run='$RROOT/runs/$RUN_ID'; if [ -f \"\$run/functional.exit_code\" ]; then printf 'exit:'; cat \"\$run/functional.exit_code\"; elif [ -f \"\$run/summary.json\" ]; then python3 -c \"import json; print('exit:0' if json.load(open('$RROOT/runs/$RUN_ID/summary.json')).get('pass') else 'exit:1')\"; else echo pending; fi" \
      2>/dev/null || true)
    value=$(printf '%s' "$value" | tr -d '[:space:]')
    if [[ "$value" =~ ^exit:([0-9]+)$ ]]; then
      RECOVERED_RC=${BASH_REMATCH[1]}
      printf 'recovered remote functional exit=%s\n' "$RECOVERED_RC" >&2
      return 0
    fi
    (( attempt == TRANSPORT_RETRIES )) || sleep 3
  done
  return 1
}

remote_functional_liveness() {
  local probe_script probe_command value
  probe_script='run="$HARNESS_CONTAINER_WORKDIR/runs/$HARNESS_RUN_ID"
if [ -f "$run/container.pid" ]; then
  pid=$(sed -n "1p" "$run/container.pid")
  case "$pid" in ""|*[!0-9]*) ;; *) if kill -0 "$pid" 2>/dev/null; then echo live; exit 0; fi ;; esac
fi
if ps -eo args= | grep -F -- "--run-id $HARNESS_RUN_ID" | grep -v grep >/dev/null; then echo live; else echo absent; fi'
  probe_command=$(printf 'docker exec -e HARNESS_RUN_ID=%q -e HARNESS_CONTAINER_WORKDIR=%q %q sh -lc %q' \
    "$RUN_ID" "$CONTAINER_WORKDIR" "$CONTAINER" "$probe_script")
  value=$(remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" "$probe_command" 2>/dev/null || true)
  value=$(printf '%s' "$value" | tr -d '[:space:]')
  case "$value" in live|absent) printf '%s\n' "$value" ;; *) printf '%s\n' unknown ;; esac
}

retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "mkdir -p '$RROOT/harness' '$RROOT/rtl' '$RROOT/tb' '$RROOT/tools/standalone_rtl' '$RROOT/runs' '$RROOT/status'" || exit "$INFRA_EXIT"

# No task-1 project path is used. The current workspace is the only source tree.
RSYNC_SSH='ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2'
retry_transport rsync -az -e "$RSYNC_SSH" --delete "$ROOT/reference.py" "$REMOTE:$RROOT/reference.py" || exit "$INFRA_EXIT"
retry_transport rsync -az -e "$RSYNC_SSH" --delete "$ROOT/tools/standalone_rtl/" "$REMOTE:$RROOT/tools/standalone_rtl/" || exit "$INFRA_EXIT"
retry_transport rsync -az -e "$RSYNC_SSH" --delete "$ROOT/harness/__init__.py" "$ROOT/harness/run.py" "$REMOTE:$RROOT/harness/" || exit "$INFRA_EXIT"
retry_transport rsync -az -e "$RSYNC_SSH" "$ROOT/harness.toml" "$REMOTE:$RROOT/harness.toml" || exit "$INFRA_EXIT"
retry_transport rsync -az -e "$RSYNC_SSH" --delete "$ROOT/rtl/" "$REMOTE:$RROOT/rtl/" || exit "$INFRA_EXIT"
retry_transport rsync -az -e "$RSYNC_SSH" --delete "$ROOT/tb/" "$REMOTE:$RROOT/tb/" || exit "$INFRA_EXIT"

retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "printf '%s\n' 'state=running' 'task=02_yosys_ppa_functional' 'run_id=$RUN_ID' 'container=$CONTAINER' > '$RROOT/status/current.status'"

CONTAINER_SCRIPT='set -u
run="$HARNESS_CONTAINER_WORKDIR/runs/$HARNESS_RUN_ID"
timeout -k 30s "${HARNESS_FUNCTIONAL_TIMEOUT_SECONDS}s" python3 harness/run.py --task 02_yosys_ppa_functional --seed 20260706 --prompt "$HARNESS_PROMPT" --group-size 128 --seq-len "$HARNESS_SEQ_LEN" --run-id "$HARNESS_RUN_ID" &
child=$!
for _ in $(seq 1 50); do
  if [ -d "$run" ]; then printf "%s\\n" "$child" > "$run/container.pid"; break; fi
  kill -0 "$child" 2>/dev/null || break
  sleep 0.1
done
wait "$child"
rc=$?
if [ -d "$run" ]; then
  printf "%s\\n" "$rc" > "$run/functional.exit_code"
  rm -f "$run/container.pid"
fi
exit "$rc"'
REMOTE_DOCKER_COMMAND=$(printf 'docker exec -w %q -e HARNESS_RUN_ID=%q -e HARNESS_CONTAINER_WORKDIR=%q -e HARNESS_FUNCTIONAL_TIMEOUT_SECONDS=%q -e HARNESS_PROMPT=%q -e HARNESS_SEQ_LEN=%q %q sh -lc %q' \
  "$CONTAINER_WORKDIR" "$RUN_ID" "$CONTAINER_WORKDIR" "$FUNCTIONAL_TIMEOUT_SECONDS" "$PROMPT" "$SEQ_LEN" "$CONTAINER" "$CONTAINER_SCRIPT")
set +e
remote_ssh "$((FUNCTIONAL_TIMEOUT_SECONDS + SSH_FUNCTIONAL_GRACE_SECONDS))" "$REMOTE_DOCKER_COMMAND" \
  >"$LOCAL_RUN/remote_console.log" 2>&1
RC=$?
set -e

# An SSH watchdog/disconnect does not prove that simulation failed.  Recover a
# durable exit code using a fresh connection; never resend this run-id because
# the original docker exec may have completed after its output stream wedged.
if [[ "$SSH_LAST_TIMED_OUT" == 1 || "$RC" == 255 ]]; then
  RECOVERY_DEADLINE=$((SECONDS + SSH_RECOVERY_TIMEOUT_SECONDS))
  while (( SECONDS < RECOVERY_DEADLINE )); do
    if recover_functional_exit; then
      RC=$RECOVERED_RC
      echo "recovered remote functional exit=$RC after SSH interruption" >>"$LOCAL_RUN/remote_console.log"
      break
    fi
    sleep 5
  done
  if [[ -z "$RECOVERED_RC" ]]; then
    LIVENESS=$(remote_functional_liveness)
    if [[ "$LIVENESS" == absent ]]; then
      echo "transport failure: remote run is absent and exit code unavailable; controller may retry" >>"$LOCAL_RUN/remote_console.log"
      RC=$INFRA_EXIT
    else
      echo "transport state uncertain (remote=$LIVENESS); controller must not start a replacement run" >>"$LOCAL_RUN/remote_console.log"
      RC=$INFRA_UNCERTAIN_EXIT
    fi
  fi
fi
if [[ "$RC" == 124 || "$RC" == 137 || "$RC" == 143 ]]; then RC=$INFRA_EXIT; fi

# Fetch durable artifacts BEFORE finalizing the verdict.  The recovery loop
# above can classify a finished run as transport-uncertain purely because the
# SSH link was briefly down during its window, even though the container
# already wrote summary.json/functional.exit_code.  A terminal summary.json
# with pass=true and simulation_exit=0 is written only after the remote runner
# completed successfully; when that durable evidence is now readable it must
# win over a transient transport classification, otherwise the functional
# counting point loses its C(n)/m line and the round becomes a false 76.
retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "run='$RROOT/runs/$RUN_ID'; if [ -f \"\$run/summary.json\" ]; then cat \"\$run/summary.json\"; fi; for f in compile.log simulation.log score.log; do [ -f \"\$run/\$f\" ] && { echo \"=== \$f ===\"; tail -n 120 \"\$run/\$f\"; }; done" \
  >"$LOCAL_RUN/feedback.txt" 2>&1 || true
if [[ "$RC" == "$INFRA_EXIT" || "$RC" == "$INFRA_UNCERTAIN_EXIT" ]] \
   && grep -q '"pass"[[:space:]]*:[[:space:]]*true' "$LOCAL_RUN/feedback.txt" \
   && grep -q '"simulation_exit"[[:space:]]*:[[:space:]]*0' "$LOCAL_RUN/feedback.txt"; then
  echo "recovered terminal success from durable feedback; overriding transport exit=$RC to 0" >>"$LOCAL_RUN/remote_console.log"
  RC=0
fi

STATE=passed; [[ "$RC" == 0 ]] || STATE=failed
[[ "$RC" == "$INFRA_EXIT" ]] && STATE=infra_failed
[[ "$RC" == "$INFRA_UNCERTAIN_EXIT" ]] && STATE=infra_uncertain
retry_transport remote_ssh "$REMOTE_SSH_TIMEOUT_SECONDS" \
  "printf '%s\n' 'state=$STATE' 'task=02_yosys_ppa_functional' 'run_id=$RUN_ID' 'exit_code=$RC' 'container=$CONTAINER' > '$RROOT/status/current.status'" || true
printf 'run_id=%s\nfeedback=%s/feedback.txt\n' "$RUN_ID" "$LOCAL_RUN"
exit "$RC"
