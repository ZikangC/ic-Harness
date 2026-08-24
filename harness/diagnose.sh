#!/usr/bin/env sh
# Server-side diagnostic dispatch.  Runs inside k3demo-eda with /work/k3harness
# as the working directory.  Full outputs remain below runs/<run-id>/diagnostics.
set -u

ACTION="${1:-auto}"
RUN_ID="${2:-}"
SEED="${3:-20260706}"
PROMPT="${4:-1}"
GROUP_SIZE="${5:-128}"
case "$ACTION" in auto|routes|snapshots|one-token) ;; *) echo "unknown diagnostic action: $ACTION"; exit 2 ;; esac
[ -n "$RUN_ID" ] || { echo "run-id is required"; exit 2; }

RUN_DIR="runs/$RUN_ID"
RTL_LOG="$RUN_DIR/simulation.log"
OUT_DIR="$RUN_DIR/diagnostics"
mkdir -p "$OUT_DIR"
[ -f "$RTL_LOG" ] || { echo "simulation log not found: $RTL_LOG"; exit 0; }
SEQ_LEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["seq_len"])' "$RUN_DIR/meta.json" 2>/dev/null || true)"

run_tool() {
  name="$1"
  script="$2"
  output="$OUT_DIR/$name.log"
  echo "== $name ==" | tee "$output"
  # The multi-token tools accept --seq-len so that reference data matches the
  # causal prefix under test.  The legacy single-token comparator intentionally
  # has no such argument.
  if [ "$name" = "one-token" ] || [ -z "$SEQ_LEN" ]; then
    python3 "$script" --rtl-log "$RTL_LOG" --seed "$SEED" --prompt "$PROMPT" --group-size "$GROUP_SIZE" >>"$output" 2>&1
  else
    python3 "$script" --rtl-log "$RTL_LOG" --seed "$SEED" --prompt "$PROMPT" --group-size "$GROUP_SIZE" --seq-len "$SEQ_LEN" >>"$output" 2>&1
  fi
  rc=$?
  echo "diagnostic_exit=$rc" >>"$output"
  tail -n 160 "$output"
  return 0
}

case "$ACTION" in
  routes)
    run_tool routes tools/standalone_rtl/audit_multitoken_routes.py
    ;;
  snapshots)
    run_tool snapshots tools/standalone_rtl/compare_multitoken_snapshots.py
    ;;
  one-token)
    run_tool one-token tools/standalone_rtl/compare_one_token_snapshots.py
    ;;
  auto)
    if ! grep -q 'TOKEN_AUDIT' "$RTL_LOG"; then
      echo "No TOKEN_AUDIT markers: compilation, reset, timeout, or pre-token failure; route diagnostics skipped."
      exit 0
    fi
    run_tool routes tools/standalone_rtl/audit_multitoken_routes.py
    run_tool snapshots tools/standalone_rtl/compare_multitoken_snapshots.py
    ;;
esac
