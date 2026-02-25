#!/usr/bin/env python3
"""Quick check: can Grafana get data? Run from totem-mm. Needs network."""
import urllib.request
import sys

def get(url, timeout=3):
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode()
    except Exception as e:
        return None, str(e)

def main():
    ok = True
    # 1. Exporter (9091) — may be in Docker or on host
    try:
        with urllib.request.urlopen("http://127.0.0.1:9091/metrics", timeout=2) as r:
            body = r.read().decode()
            if "live_odds_percent" in body:
                print("[OK] Exporter (9091) returns live_odds metrics")
            else:
                print("[??] Exporter (9091) up but no live_odds_percent (file empty?)")
    except Exception as e:
        print("[SKIP] Exporter (9091):", e)
        print("       → With Docker-only setup the exporter runs in Docker; ensure docker compose is up and polling script writes data/live_odds_polling.txt")

    # 2. Exporter stream (9092)
    try:
        with urllib.request.urlopen("http://127.0.0.1:9092/metrics", timeout=2) as r:
            body = r.read().decode()
            if "live_odds_percent" in body:
                print("[OK] Exporter stream (9092) returns live_odds metrics")
            else:
                print("[??] Exporter stream (9092) up but no live_odds_percent")
    except Exception as e:
        print("[SKIP] Exporter stream (9092):", e)
        print("       → Optional: DATA_SOURCE=stream POLLING_FILE=data/live_odds_streaming.txt EXPORTER_PORT=9092 python3 scripts/prometheus_exporter.py")

    # 3. Prometheus has series
    try:
        with urllib.request.urlopen("http://127.0.0.1:9090/api/v1/query?query=live_odds_percent", timeout=2) as r:
            import json
            data = json.loads(r.read().decode())
            n = len(data.get("data", {}).get("result", []))
            if n > 0:
                print(f"[OK] Prometheus has {n} series for live_odds_percent")
            else:
                print("[FAIL] Prometheus returns 0 series for live_odds_percent")
                print("       → Open http://localhost:9090/targets — if live_odds_poll is DOWN, Docker Prometheus cannot reach the host.")
                print("       → Fix: run Prometheus on the host instead (see grafana/README.md 'Prometheus on host').")
                ok = False
    except Exception as e:
        print("[FAIL] Prometheus (9090):", e)
        print("       → Start: docker compose -f grafana/docker-compose.yml up -d")
        ok = False

    if not ok:
        sys.exit(1)
    print("\nIf Grafana still shows No data: re-import dashboard (Live Odds Poll) and set time range Last 15 min, refresh 5s.")

if __name__ == "__main__":
    main()
