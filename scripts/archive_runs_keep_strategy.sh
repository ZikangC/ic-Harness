#!/usr/bin/env bash
# Compact historical run artifacts without losing AI plans/edits or the latest
# feedback needed to restart either controller.  This intentionally removes
# only known Task-01/Task-02 generated run directory patterns.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUNS="$ROOT/runs"
ARCHIVE="$RUNS/strategy_archive"
# Historical server runs are the immutable evidence store.  This maintenance
# helper is deliberately local-only, even though its source is synchronized
# for documentation/version consistency.
LOCAL_PROJECT="/Users/chenzikang/code/work/RTLharness"
[[ "$ROOT" == "$LOCAL_PROJECT" ]] || {
  echo "refusing cleanup: server runs are immutable; this script may run only in $LOCAL_PROJECT" >&2
  exit 4
}
[[ -d "$RUNS" ]] || { echo "missing runs directory: $RUNS" >&2; exit 2; }
# A controller owns mutable snapshots under runs/.  Refuse compaction while it
# is active; stop it through the normal cancellation path first.
if pgrep -f "python3 harness/(ppa_agent_loop|agent_loop)\\.py" >/dev/null 2>&1; then
  echo "refusing cleanup: an RTL harness controller is still running" >&2
  exit 3
fi
mkdir -p "$ARCHIVE"

copy_strategy_file() {
  local source=$1 destination=$2 filename
  for filename in agent.log planner.log executor.log result.json changes.diff planner_violation.diff; do
    [[ -f "$source/$filename" ]] && cp -p "$source/$filename" "$destination/$filename"
  done
  return 0
}

copy_latest_feedback() {
  local archive_name=$1
  shift
  local latest="" candidate
  for candidate in "$@"; do
    [[ -f "$candidate" ]] || continue
    if [[ -z "$latest" || "$candidate" -nt "$latest" ]]; then latest=$candidate; fi
  done
  [[ -n "$latest" ]] && cp -p "$latest" "$ARCHIVE/$archive_name"
  return 0
}

shopt -s nullglob
agent_dirs=("$RUNS"/agent-*)
for source in "${agent_dirs[@]}"; do
  [[ -d "$source" ]] || continue
  destination="$ARCHIVE/$(basename "$source")"
  mkdir -p "$destination"
  copy_strategy_file "$source" "$destination"
done

# Keep just the newest compact feedback for controller restart context.
copy_latest_feedback latest_01_repair_multitoken_golden_feedback.txt "$RUNS"/01_repair_multitoken_golden-*/feedback.txt
copy_latest_feedback latest_ppa_feedback.txt "$RUNS"/02_yosys_ppa--*/feedback.txt
copy_latest_feedback latest_functional_feedback.txt "$RUNS"/02_yosys_ppa_functional--*/feedback.txt

strategy_records=$(find "$ARCHIVE" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d '[:space:]')
printf 'archived_at=%s\nstrategy_record_directories=%s\n' "$(date -Iseconds)" "$strategy_records" \
  > "$ARCHIVE/README.txt"

# Delete only known, verified generated-run naming families; source RTL outside
# runs/ is never touched.  The archive itself is deliberately excluded.
remove_dirs=(
  "${agent_dirs[@]}"
  "$RUNS"/01_repair_multitoken_golden-*
  "$RUNS"/02_yosys_ppa--*
  "$RUNS"/02_yosys_ppa_functional--*
  "$RUNS"/02_yosys_preflight--*
  "$RUNS"/smoke-prepare
  "$RUNS"/remote-smoke-*
  "$RUNS"/verilator-contract-*
)
removed=0
for source in "${remove_dirs[@]}"; do
  [[ -d "$source" ]] || continue
  rm -rf "$source"
  removed=$((removed + 1))
done

echo "Archived $strategy_records AI strategy records; removed $removed generated run directories."
echo "Preserved archive: $ARCHIVE"
