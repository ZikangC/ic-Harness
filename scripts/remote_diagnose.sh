#!/usr/bin/env bash
# Invoke a named server-side diagnostic without transferring any run artifacts.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
eval "$(python3 "$ROOT/harness/environment.py" --shell)"
REMOTE_HOST=$HARNESS_REMOTE_HOST
REMOTE_PROJECT=$HARNESS_REMOTE_PROJECT
CONTAINER=$HARNESS_CONTAINER
CONTAINER_WORKDIR=$HARNESS_CONTAINER_WORKDIR
ACTION="auto"
RUN_ID=""
SEED="20260706"
PROMPT="1"
GROUP_SIZE="128"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --action) ACTION="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --seed) SEED="$2"; shift 2 ;;
    --prompt) PROMPT="$2"; shift 2 ;;
    --group-size) GROUP_SIZE="$2"; shift 2 ;;
    *) echo "usage: bash scripts/remote_diagnose.sh [--action auto|routes|snapshots|one-token] [--run-id ID] [--seed N] [--prompt 0|1] [--group-size 64|128]" >&2; exit 2 ;;
  esac
done
case "$ACTION" in auto|routes|snapshots|one-token) ;; *) echo "invalid --action: $ACTION" >&2; exit 2 ;; esac
if [[ -z "$RUN_ID" ]]; then
  RUN_ID="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_HOST" "sed -n 's/^run_id=//p' '$REMOTE_PROJECT/status/current.status' 2>/dev/null || true")"
fi
[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "No valid run-id found; provide --run-id" >&2; exit 2; }

ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_HOST" \
  "docker exec -w '$CONTAINER_WORKDIR' '$CONTAINER' sh harness/diagnose.sh '$ACTION' '$RUN_ID' '$SEED' '$PROMPT' '$GROUP_SIZE'"
