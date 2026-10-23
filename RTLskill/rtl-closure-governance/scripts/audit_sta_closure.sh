#!/usr/bin/env bash
# Emit a compact, deterministic top-level STA closure audit from feedback.txt.
# This is deliberately read-only: it never runs synthesis, OpenSTA, or PPA.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

usage() {
  echo "usage: $0 <feedback.txt> [--artifact <artifact.json>] [--output <audit.json>]" >&2
  exit 2
}

[[ $# -ge 1 ]] || usage
[[ -f "$1" ]] || { echo "feedback file not found: $1" >&2; exit 2; }

exec python3 "$SCRIPT_DIR/sta_closure_audit.py" "$@"
