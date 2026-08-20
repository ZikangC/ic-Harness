#!/usr/bin/env bash
# View the currently active server-side harness run without downloading artifacts.
set -euo pipefail

REMOTE_HOST="txjg7-2@192.168.0.13"
REMOTE_PROJECT="/home/txjg7-2/ic-project/k3harness"
FOLLOW=0
if [[ "${1:-}" == "--follow" ]]; then FOLLOW=1; fi
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--follow" ) ]]; then
  echo "usage: bash scripts/remote_status.sh [--follow]" >&2
  exit 2
fi

if [[ "$FOLLOW" == "0" ]]; then
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_HOST" \
    "printf '%s\\n' '--- current harness status ---'; \
     if [ -f '$REMOTE_PROJECT/status/current.status' ]; then cat '$REMOTE_PROJECT/status/current.status'; else echo 'no run has started yet'; fi; \
     printf '%s\\n' '--- container ---'; docker ps --filter name=k3demo-eda --format '{{.Names}} {{.Status}}'"
  exit 0
fi

# The full log stays on the server; this streams it live to the terminal only.
ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_HOST" \
  "status='$REMOTE_PROJECT/status/current.status'; \
   if [ ! -f \"\$status\" ]; then echo 'no run has started yet'; exit 1; fi; \
   cat \"\$status\"; \
   run=\$(sed -n 's/^run_id=//p' \"\$status\"); \
   log='$REMOTE_PROJECT/runs/'\"\$run\"'/simulation.log'; \
   printf '%s\\n' '--- following remote simulation.log; Ctrl-C stops viewing only ---'; \
   if [ -f \"\$log\" ]; then tail -n 80 -f \"\$log\"; else echo \"log is not available yet: \$log\"; fi"
