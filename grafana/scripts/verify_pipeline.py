#!/usr/bin/env python3
"""Verify the pipeline so Grafana can show data. Run from totem-mm with venv active."""
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
POLLING_FILE = os.path.join(DATA_DIR, "live_odds_polling.txt")

def main():
    print("1. Polling file")
    if not os.path.isfile(POLLING_FILE):
        print(f"   MISSING: {POLLING_FILE}")
        print("   → Start the writer: python3 scripts/live_feed_polling.py")
        return 1
    with open(POLLING_FILE) as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith("-") and "Betfair (back" not in l]
    if not lines:
        print(f"   EMPTY (no data rows): {POLLING_FILE}")
        print("   → Start the writer: python3 scripts/live_feed_polling.py")
        return 1
    print(f"   OK: {len(lines)} data rows, last line: {lines[-1][:80]}...")

    print("\n2. Exporter (parse last row)")
    from prometheus_exporter import get_last_row, metrics_text
    data = get_last_row(POLLING_FILE)
    if not data or all(v is None for v in data.values()):
        print("   FAIL: could not parse last row or all values None")
        return 1
    non_null = sum(1 for v in data.values() if v is not None)
    print(f"   OK: {non_null} gauges from last row")
    body = metrics_text(data)
    if "live_odds_percent" not in body or "} " not in body:
        print("   FAIL: metrics text has no gauge lines")
        return 1
    print("   Sample metrics (first 3 gauge lines):")
    for line in body.splitlines():
        if line.startswith("live_odds_"):
            print(f"      {line[:75]}...")
            if "live_odds_sum" in line:
                break

    print("\n3. Next steps (you must do these)")
    print("   • Exporter:  python3 scripts/prometheus_exporter.py   (keep running)")
    print("   • In browser:  http://localhost:9091/metrics   → should show gauge lines")
    print("   • Prometheus:  http://localhost:9090/targets   → job 'live_odds' must be UP")
    print("   • Grafana datasource:  must be Prometheus with URL  http://prometheus:9090  (if Grafana in Docker)")
    print("   • If targets are DOWN:  start exporter on host; on Linux you may need to fix host.docker.internal")
    return 0

if __name__ == "__main__":
    sys.exit(main())
