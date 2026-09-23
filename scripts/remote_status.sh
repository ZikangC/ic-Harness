#!/usr/bin/env bash
# View the currently active server-side harness run without downloading artifacts.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
eval "$(python3 "$ROOT/harness/environment.py" --shell)"
REMOTE_HOST=$HARNESS_REMOTE_HOST
REMOTE_PROJECT=$HARNESS_REMOTE_PROJECT
CONTAINER=$HARNESS_CONTAINER
FOLLOW=0
if [[ "${1:-}" == "--follow" ]]; then FOLLOW=1; fi
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--follow" ) ]]; then
  echo "usage: bash scripts/remote_status.sh [--follow]" >&2
  exit 2
fi

if [[ "$FOLLOW" == "0" ]]; then
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_HOST" \
    "printf '%s\\n' '--- task-02 controller status ---'; \
     if [ -f '$REMOTE_PROJECT/status/task02_agent.status' ]; then cat '$REMOTE_PROJECT/status/task02_agent.status'; else echo 'no controller state published yet'; fi; \
     printf '%s\\n' '--- current harness status ---'; \
     if [ -f '$REMOTE_PROJECT/status/current.status' ]; then cat '$REMOTE_PROJECT/status/current.status'; else echo 'no run has started yet'; fi; \
     printf '%s\\n' '--- container ---'; docker ps --filter name='$CONTAINER' --format '{{.Names}} {{.Status}}'"
  exit 0
fi

# The full log stays on the server; this streams it live to the terminal only.
ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_HOST" \
  "status='$REMOTE_PROJECT/status/current.status'; \
   if [ ! -f \"\$status\" ]; then echo 'no run has started yet'; exit 1; fi; \
   cat \"\$status\"; \
   state=\$(sed -n 's/^state=//p' \"\$status\"); \
   run=\$(sed -n 's/^run_id=//p' \"\$status\"); \
   if [ \"\$state\" != running ]; then \
     echo \"--- no live simulation to follow (state=\$state, run_id=\$run) ---\"; \
     echo 'The server is idle or has finished this run. To monitor local AI reasoning, follow runs/agent-*/agent.log instead.'; \
     exit 0; \
   fi; \
   log='$REMOTE_PROJECT/runs/'\"\$run\"'/simulation.log'; \
   printf '%s\\n' '--- following remote simulation.log; Ctrl-C stops viewing only ---'; \
   if [ -f \"\$log\" ]; then tail -n 80 -f \"\$log\"; else echo \"log is not available yet: \$log\"; fi"
