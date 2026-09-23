#!/usr/bin/env bash
# Static policy checks for bounded Task-02 functional transport; no remote calls.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SCRIPT="$ROOT/scripts/remote_task02_functional.sh"
RECONCILER="$ROOT/scripts/remote_reconcile.sh"
CONTROLLER="$ROOT/harness/ppa_agent_loop.py"
ENVIRONMENT="$ROOT/harness/environment.py"

contains_fixed() {
  if command -v rg >/dev/null 2>&1; then
    rg -Fq "$1" "$2"
  else
    grep -Fq -- "$1" "$2"
  fi
}

bash -n "$SCRIPT"
bash -n "$RECONCILER"
if contains_fixed 'from __future__ import annotations' "$ENVIRONMENT"; then
  echo 'environment.py must remain compatible with the server host Python 3.6' >&2
  exit 1
fi
for required in \
  'ENV_EXPORT=' \
  'environment export failed before remote functional run' \
  'environment.py did not export the complete harness environment' \
  'FUNCTIONAL_TIMEOUT_SECONDS=3600' \
  'SSH_FUNCTIONAL_GRACE_SECONDS=90' \
  'functional.exit_code' \
  'remote_functional_liveness' \
  'controller must not start a replacement run' \
  'INFRA_UNCERTAIN_EXIT=76' \
  'recovered terminal success from durable feedback' \
  'ServerAliveInterval=15' \
  'timeout -k 30s'; do
  contains_fixed "$required" "$SCRIPT"
done
for required in \
  'WALL_SECONDS=120' \
  'state=cancelled' \
  'reconcile=cancelled' \
  'kill -TERM $pids' \
  'kill -KILL $pids' \
  'intentionally keeps stdin' \
  'invalid_state=' \
  'ServerAliveInterval=15'; do
  contains_fixed "$required" "$RECONCILER"
done
contains_fixed 'set_stage("cancelled", CURRENT_ATTEMPT' "$CONTROLLER"
contains_fixed 'INFRA_UNCERTAIN_EXIT = 76' "$CONTROLLER"
contains_fixed 'scripts/remote_reconcile.sh' "$CONTROLLER"
contains_fixed 'set_stage("remote_reconcile", "startup"' "$CONTROLLER"
echo 'Remote functional transport policy tests: PASS'
