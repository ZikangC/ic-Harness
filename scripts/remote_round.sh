#!/usr/bin/env bash
# Run one deterministic verification round in the remote EDA container.
# The local tree is the source of truth; the remote tree is disposable execution state.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K3DEMO_DIR="$(cd "$PROJECT_DIR/../k3demo" && pwd)"
REMOTE_HOST="txjg7-2@192.168.0.13"
REMOTE_PROJECT="/home/txjg7-2/ic-project/k3harness"
CONTAINER="k3demo-eda"

TASK=""
SEED="20260706"
PROMPT="1"
GROUP_SIZE="128"
SYNC_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --task) TASK="$2"; shift 2 ;;
    --seed) SEED="$2"; shift 2 ;;
    --prompt) PROMPT="$2"; shift 2 ;;
    --group-size) GROUP_SIZE="$2"; shift 2 ;;
    --sync-only) SYNC_ONLY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$TASK" ]] || { echo "--task is required" >&2; exit 2; }
[[ "$TASK" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "--task may contain only letters, numbers, dot, underscore, and hyphen" >&2; exit 2; }
[[ "$PROMPT" == "0" || "$PROMPT" == "1" ]] || { echo "--prompt must be 0 or 1" >&2; exit 2; }
[[ "$GROUP_SIZE" == "64" || "$GROUP_SIZE" == "128" ]] || { echo "--group-size must be 64 or 128" >&2; exit 2; }

# The PID prevents collisions when an agent starts more than one round in a second.
RUN_ID="${TASK}-$(date +%Y%m%d-%H%M%S)-$$"
LOCAL_RUN="$PROJECT_DIR/runs/$RUN_ID"

write_remote_status() {
  local state="$1"
  local exit_code="${2:-}"
  local updated_at
  updated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_HOST" \
    "mkdir -p '$REMOTE_PROJECT/status'; printf '%s\\n' \
      'state=$state' \
      'task=$TASK' \
      'run_id=$RUN_ID' \
      'updated_at=$updated_at' \
      'exit_code=$exit_code' \
      'container=$CONTAINER' > '$REMOTE_PROJECT/status/current.status'"
}

ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_HOST" \
  "mkdir -p '$REMOTE_PROJECT/harness' '$REMOTE_PROJECT/rtl' '$REMOTE_PROJECT/tb' '$REMOTE_PROJECT/standalone_rtl/tools' '$REMOTE_PROJECT/runs'"

# Deliberately sync only the runtime minimum: reference, two authoritative tools,
# remote runner/config, and candidate RTL/testbench. Agent prompts and local history stay local.
rsync -az --delete "$K3DEMO_DIR/reference.py" "$REMOTE_HOST:$REMOTE_PROJECT/reference.py"
rsync -az --delete "$K3DEMO_DIR/standalone_rtl/tools/build_golden_image.py" \
  "$K3DEMO_DIR/standalone_rtl/tools/score_against_golden.py" \
  "$REMOTE_HOST:$REMOTE_PROJECT/standalone_rtl/tools/"
rsync -az --delete "$PROJECT_DIR/harness/__init__.py" "$PROJECT_DIR/harness/run.py" \
  "$REMOTE_HOST:$REMOTE_PROJECT/harness/"
rsync -az "$PROJECT_DIR/harness.toml" "$REMOTE_HOST:$REMOTE_PROJECT/harness.toml"
rsync -az --delete "$PROJECT_DIR/rtl/" "$REMOTE_HOST:$REMOTE_PROJECT/rtl/"
rsync -az --delete "$PROJECT_DIR/tb/" "$REMOTE_HOST:$REMOTE_PROJECT/tb/"

if [[ "$SYNC_ONLY" == "1" ]]; then
  printf 'synchronized baseline to %s:%s\n' "$REMOTE_HOST" "$REMOTE_PROJECT"
  exit 0
fi

mkdir -p "$LOCAL_RUN"
write_remote_status "running"
set +e
ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_HOST" \
  "docker exec -w /work/k3harness '$CONTAINER' python3 harness/run.py --task '$TASK' --seed '$SEED' --prompt '$PROMPT' --group-size '$GROUP_SIZE' --run-id '$RUN_ID'" \
  >"$LOCAL_RUN/remote_console.log" 2>&1
REMOTE_RC=$?
set -e
if [[ "$REMOTE_RC" == "0" ]]; then
  write_remote_status "passed" "$REMOTE_RC"
else
  write_remote_status "failed" "$REMOTE_RC"
fi

# Full image/dump/obj_dir/log artifacts remain on the server.  Only a bounded
# textual packet crosses back to the local OMP controller, which is enough for
# agent_loop.py to diagnose the next repair without copying simulation outputs.
ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_HOST" \
  "run='$REMOTE_PROJECT/runs/$RUN_ID'; \
   printf '%s\\n' '__HARNESS_SUMMARY_BEGIN__'; \
   if [ -f \"\$run/summary.json\" ]; then cat \"\$run/summary.json\"; else echo '{\"pass\": false, \"reason\": \"remote summary is unavailable\"}'; fi; \
   printf '%s\\n' '__HARNESS_SUMMARY_END__'; \
   for log in simulation.log score.log reference.log image.log; do \
     if [ -f \"\$run/\$log\" ]; then printf '\\n=== %s (last 120 lines) ===\\n' \"\$log\"; tail -n 120 \"\$run/\$log\"; fi; \
   done" \
  >"$LOCAL_RUN/feedback.txt" 2>&1 || \
  printf '%s\n' 'Unable to retrieve compact remote feedback; inspect the remote run path manually.' >>"$LOCAL_RUN/feedback.txt"

printf 'remote run: %s\nremote artifacts retained at: %s/runs/%s\ncompact feedback: %s\n' \
  "$RUN_ID" "$REMOTE_PROJECT" "$RUN_ID" "$LOCAL_RUN/feedback.txt"
exit "$REMOTE_RC"
