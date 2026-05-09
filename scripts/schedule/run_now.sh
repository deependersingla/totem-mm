#!/usr/bin/env bash
# One-shot: refresh the schedule and launch the next match right now.
#
# Usage:
#   scripts/schedule/run_now.sh                       # refresh + launch next match
#   scripts/schedule/run_now.sh cricipl-roy-guj-...   # refresh + launch this specific slug
#   scripts/schedule/run_now.sh --skip-refresh        # just launch next, no refresh

set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." &>/dev/null && pwd)"
cd "$PROJECT_ROOT"

PY="$PROJECT_ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$PROJECT_ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

SKIP_REFRESH=0
SLUG_ARG=()

for arg in "$@"; do
  case "$arg" in
    --skip-refresh) SKIP_REFRESH=1 ;;
    cricipl-*)      SLUG_ARG=(--slug "$arg") ;;
    -h|--help)
      sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [[ $SKIP_REFRESH -eq 0 ]]; then
  echo "=== refreshing schedule ==="
  "$PY" scripts/schedule/refresh_upcoming.py
  echo
fi

echo "=== launching ==="
if [[ ${#SLUG_ARG[@]} -gt 0 ]]; then
  "$PY" scripts/schedule/launch_today.py "${SLUG_ARG[@]}"
else
  "$PY" scripts/schedule/launch_today.py --now
fi
