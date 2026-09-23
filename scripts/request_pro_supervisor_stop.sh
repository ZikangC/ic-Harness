#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ $# -eq 1 ]] || { echo "usage: $0 SUPERVISOR_SESSION" >&2; exit 2; }
exec python3 "$ROOT/harness/pro_supervisor.py" --request-stop "$1"
