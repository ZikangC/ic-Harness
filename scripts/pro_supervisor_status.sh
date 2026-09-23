#!/usr/bin/env bash
# Human-facing, bounded status view for a Pro/Flash supervisor session.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
STATUS="$ROOT/status/pro_supervisor.status"
PENDING="$ROOT/status/task02_pending_verification.json"
SESSION=""
LINES=80
VERBOSE=false
HISTORY=false

usage() {
  cat <<'EOF'
Usage: bash scripts/pro_supervisor_status.sh [--session SESSION] [--verbose] [--history] [--tail N]

With no --session, reads the active/most recent session from
status/pro_supervisor.status. The default prints only the selected session's
current state and latest decision/result. --verbose adds only that session's
latest raw logs. --history explicitly prints this selected session's entries
from the cumulative Pro report. --tail N controls verbose/history output.
It never follows logs indefinitely.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session) SESSION=$2; shift 2 ;;
    --verbose) VERBOSE=true; shift ;;
    --history) HISTORY=true; shift ;;
    --tail) LINES=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ "$LINES" =~ ^[1-9][0-9]*$ ]] || { echo "--tail must be positive" >&2; exit 2; }
if [[ -z "$SESSION" && -f "$STATUS" ]]; then
  SESSION=$(sed -n 's/^session=//p' "$STATUS" | head -1)
fi
[[ "$SESSION" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "no valid supervisor session; pass --session" >&2; exit 1; }
RUN="$ROOT/runs/pro_supervisor--$SESSION"
[[ -d "$RUN" ]] || { echo "session directory not found: $RUN" >&2; exit 1; }

echo '--- supervisor status ---'
STATUS_SESSION=$(sed -n 's/^session=//p' "$STATUS" 2>/dev/null | head -1 || true)
if [[ "$STATUS_SESSION" == "$SESSION" ]]; then
  grep -E '^(session|stage|updated_at|detail)=' "$STATUS" || true
else
  printf 'session=%s\n' "$SESSION"
  printf 'status=historical session (global status belongs to %s)\n' "${STATUS_SESSION:-unknown}"
fi
echo '--- governance summary ---'
if [[ -f "$RUN/session_state.json" ]]; then
  grep -E '"(started_at|flash_batches|flash_attempts|ppa_budget_seconds|stagnant_batches|pending_sta_smoke|last_sta_phase)"[[:space:]]*:' "$RUN/session_state.json" || true
else
  echo 'no session_state.json yet'
fi
if [[ -f "$PENDING" ]]; then
  echo '--- pending verification ---'
  sed -n '/"paths"[[:space:]]*:/,/][[:space:]]*,\{0,1\}$/p' "$PENDING" || true
  grep -E '"(summary|expected_phase|partition|timeout_seconds)"[[:space:]]*:' "$PENDING" || true
fi
echo '--- latest Pro decision summary ---'
LATEST_DECISION=$(ls -t "$RUN"/decision_*.json 2>/dev/null | head -1 || true)
if [[ -n "$LATEST_DECISION" ]]; then
  printf 'file=%s\n' "${LATEST_DECISION#$ROOT/}"
  grep -E '"(action|flash_attempts|reusable_knowledge)"[[:space:]]*:' "$LATEST_DECISION" || true
else
  echo 'no parsed Pro decision yet'
fi
echo '--- latest Flash batch summary ---'
LATEST_FLASH=$(ls -t "$RUN"/flash_result_*.json 2>/dev/null | head -1 || true)
if [[ -n "$LATEST_FLASH" ]]; then cat "$LATEST_FLASH"; else echo 'no completed Flash batch yet'; fi
echo '--- current Task-02 controller stage ---'
if [[ -f "$ROOT/status/task02_agent.status" ]]; then cat "$ROOT/status/task02_agent.status"; else echo 'no controller stage published yet'; fi

if [[ "$VERBOSE" != true && "$HISTORY" != true ]]; then
  echo 'More: --verbose (latest raw logs), --history (this session only).'
  exit 0
fi

if [[ "$HISTORY" == true ]]; then
  echo '--- selected session history ---'
  if [[ -f "$ROOT/reports/02_yosys_ppa/pro_progress.md" ]]; then
    awk -v wanted="$SESSION" '/^## / { show = index($0, wanted) > 0 } show { print }' \
      "$ROOT/reports/02_yosys_ppa/pro_progress.md" | tail -n "$LINES"
  else
    echo 'no Pro progress report yet'
  fi
fi

if [[ "$VERBOSE" == true ]]; then
  echo '--- latest raw Pro decision ---'
  LATEST_PRO=$(ls -t "$RUN"/pro_decision_*.log 2>/dev/null | head -1 || true)
  if [[ -n "$LATEST_PRO" ]]; then tail -n "$LINES" "$LATEST_PRO"; else echo 'no Pro decision log yet'; fi
  echo '--- latest raw Pro action ---'
  LATEST_ACTION=$(ls -t "$RUN"/action_*/pro_action.log 2>/dev/null | head -1 || true)
  if [[ -n "$LATEST_ACTION" ]]; then tail -n "$LINES" "$LATEST_ACTION"; else echo 'no completed Pro action yet'; fi
  echo '--- latest raw Flash controller log ---'
  LATEST_FLASH_LOG=$(ls -t "$RUN"/flash_controller_*.log 2>/dev/null | head -1 || true)
  if [[ -n "$LATEST_FLASH_LOG" ]]; then tail -n "$LINES" "$LATEST_FLASH_LOG"; else echo 'no Flash batch log yet'; fi
fi
