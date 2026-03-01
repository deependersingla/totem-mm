#!/bin/bash
# Run from totem-mm. Diagnoses why Grafana shows "No data".
set -e
cd "$(dirname "$0")/../.."

echo "=== 1. Data file ==="
if [ -f data/live_odds_polling.txt ]; then
  lines=$(wc -l < data/live_odds_polling.txt)
  echo "OK: data/live_odds_polling.txt exists ($lines lines)"
  echo "Last line:"
  tail -1 data/live_odds_polling.txt
else
  echo "FAIL: data/live_odds_polling.txt missing. Run live_feed_polling.py first."
  exit 1
fi

echo ""
echo "=== 2. Docker stack ==="
if ! command -v docker &>/dev/null; then
  echo "FAIL: docker not found. Install Docker Desktop."
  exit 1
fi
docker compose -f grafana/docker-compose.yml ps

echo ""
echo "=== 3. Exporter (must be running in Docker) ==="
if curl -s -m 3 http://localhost:9091/metrics 2>/dev/null | grep -q live_odds_percent; then
  echo "OK: Exporter returns live_odds metrics"
else
  echo "FAIL: Exporter not reachable or no metrics. Run:"
  echo "  docker compose -f grafana/docker-compose.yml up -d"
  echo "Then check: docker compose -f grafana/docker-compose.yml logs exporter"
fi

echo ""
echo "=== 4. Prometheus ==="
n=$(curl -s -m 3 "http://localhost:9090/api/v1/query?query=live_odds_percent" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',{}).get('result',[])))" 2>/dev/null || echo "0")
if [ "$n" -gt 0 ] 2>/dev/null; then
  echo "OK: Prometheus has $n series"
else
  echo "FAIL: Prometheus has 0 series. Open http://localhost:9090/targets — live_odds_poll must be UP"
fi

echo ""
echo "=== 5. Grafana ==="
echo "Open http://localhost:3000 (admin/admin) → Dashboards → Live Odds"
echo "Time range: Last 15 minutes, Refresh: 5s"
