#!/usr/bin/env bash
# Kill running capture/dls/dp scripts — either all of them, or only for one slug.
#
# Usage:
#   scripts/schedule/stop_today.sh            # list what's running
#   scripts/schedule/stop_today.sh list       # same
#   scripts/schedule/stop_today.sh all        # kill EVERYTHING (with confirmation)
#   scripts/schedule/stop_today.sh <slug>     # kill only the 3 scripts for this slug

set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." &>/dev/null && pwd)"
RUNNING_DIR="$PROJECT_ROOT/captures/.running"

PATTERN_BASE='scripts/(live_capture|dls_monitor|dp_monitor)\.py'

list_running() {
  echo "── running capture processes ─────────────────────────────"
  # BSD ps on macOS — grep for our script patterns
  ps -Ao pid,etime,command | grep -E "$PATTERN_BASE" | grep -v grep || echo "  (none)"
  echo
  echo "── tracking files in $RUNNING_DIR ────────────────────────"
  if [[ -d "$RUNNING_DIR" ]]; then
    shopt -s nullglob
    local any=0
    for f in "$RUNNING_DIR"/*.json; do
      any=1
      slug=$(basename "$f" .json)
      launched=$(python3 -c "import json;print(json.load(open('$f')).get('launched_at_ist','?'))" 2>/dev/null || echo "?")
      echo "  $slug  launched_at=$launched"
    done
    shopt -u nullglob
    if [[ $any -eq 0 ]]; then echo "  (none)"; fi
  fi
}

kill_by_slug() {
  local slug="$1"
  local pat="$PATTERN_BASE --slug $slug(\b|$)"
  local pids
  pids=$(pgrep -f "$pat" || true)
  if [[ -z "$pids" ]]; then
    echo "no running processes for slug=$slug"
  else
    echo "sending SIGINT to: $pids"
    kill -INT $pids || true
    sleep 2
    pids=$(pgrep -f "$pat" || true)
    if [[ -n "$pids" ]]; then
      echo "still running, sending SIGTERM: $pids"
      kill -TERM $pids || true
    fi
  fi
  rm -f "$RUNNING_DIR/$slug.json"
  echo "cleaned tracking file for $slug"
}

kill_all() {
  echo "This will SIGINT every live_capture / dls_monitor / dp_monitor."
  read -r -p "Really kill all? [y/N] " yn
  if [[ "$yn" != "y" && "$yn" != "Y" ]]; then echo "aborted."; return; fi
  local pids
  pids=$(pgrep -f "$PATTERN_BASE" || true)
  if [[ -z "$pids" ]]; then
    echo "nothing running."
  else
    echo "SIGINT: $pids"
    kill -INT $pids || true
    sleep 2
    pids=$(pgrep -f "$PATTERN_BASE" || true)
    if [[ -n "$pids" ]]; then
      echo "still running, SIGTERM: $pids"
      kill -TERM $pids || true
    fi
  fi
  if [[ -d "$RUNNING_DIR" ]]; then
    rm -f "$RUNNING_DIR"/*.json 2>/dev/null || true
  fi
  echo "done."
}

cmd="${1:-list}"
case "$cmd" in
  list|"") list_running ;;
  all)     kill_all ;;
  cricipl-*) kill_by_slug "$cmd" ;;
  *) echo "usage: $0 [list|all|<slug>]" >&2; exit 2 ;;
esac
