# Grafana + Prometheus for Live Odds (Docker only)

Everything runs in Docker. You only run the **polling script** on your machine to write the data file.

## Steps (from project root `totem-mm`)

1. **Start the stack** (exporter + Prometheus + Grafana):
   ```bash
   docker compose -f grafana/docker-compose.yml up -d
   ```

2. **Run the polling script** so it writes `data/live_odds_polling.txt` (e.g. for ENG vs SL):
   ```bash
   python3 scripts/live_feed_polling.py
   ```
   Leave it running in a terminal.

3. **In Grafana** (http://localhost:3000, admin / admin):  
   **Dashboards** → **New** → **Import** → **Upload JSON file** → choose **`totem-mm/grafana/dashboards/live_odds_poll.json`** → **Load** → select **Prometheus** → **Import**.

4. Open **Live Odds**, set time range **Last 15 minutes**, refresh **5s**. You should see live graphs.

**No data?** Ensure the polling script is running and writing rows to `data/live_odds_polling.txt`. The exporter (in Docker) reads that file; Prometheus scrapes the exporter.

## Quick start

1. **Start the data writer** (from project root `totem-mm`):
   ```bash
   source .venv/bin/activate   # or your venv
   python3 scripts/live_feed_polling.py
   ```
   This writes to `data/live_odds_polling.txt` every second.

2. **Start the Prometheus exporter** (in another terminal, same directory):
   ```bash
   python3 scripts/prometheus_exporter.py
   ```
   Metrics at: http://localhost:9091/metrics

3. **Start Prometheus + Grafana**
   - **With Docker:** `docker compose -f grafana/docker-compose.yml up -d`  
     (If you see `command not found: docker`, install [Docker Desktop](https://www.docker.com/products/docker-desktop/) or run **without Docker** below.)
   - **Without Docker (macOS):** see [Run without Docker](#run-without-docker-macos) below.
   - Prometheus: http://localhost:9090  
   - Grafana: http://localhost:3000 (login: **admin** / **admin**)

4. Open Grafana → **Dashboards** → **Browse** → **Live Odds** (or import `grafana/dashboards/live_odds_poll.json` as above).

**If you don’t see the Live Odds dashboard:** import it manually: **Dashboards** → **New** → **Import** → **Upload JSON file** → choose `totem-mm/grafana/dashboards/live_odds_poll.json` → **Load** → **Import**. Then pick the **Prometheus** datasource and save.

### Run without Docker (macOS)

If Docker isn’t installed, use Homebrew:

```bash
brew install prometheus grafana
```

**Prometheus** (scrapes exporter at localhost:9091):

```bash
# From totem-mm directory
mkdir -p data/prometheus
prometheus --config.file=grafana/prometheus-native.yml --storage.tsdb.path=./data/prometheus
```

Keep this running in a terminal (or run in the background).

**Grafana:**

```bash
brew services start grafana
# Or run once: grafana server --config /opt/homebrew/etc/grafana/grafana.ini
```

Open http://localhost:3000. Add a **Prometheus** datasource: URL `http://localhost:9090`. Then import the dashboard: **Dashboards** → **New** → **Import** → upload or paste the JSON from `grafana/dashboards/live_odds.json`.

## No data in panels?

Grafana shows "No data" when **Prometheus has no series** for the query. Check in order:

1. Run `python3 grafana/scripts/check_data.py` and fix any step it reports.

2. **Exporter must be running** and reachable:
   ```bash
   python3 scripts/prometheus_exporter.py
   ```
   Then open http://localhost:9091/metrics — you should see lines like `live_odds_percent{source="betfair",...} 36.23`.

3. **Prometheus must scrape the exporter.** Open http://localhost:9090/targets and ensure **live_odds_poll** and **live_odds_stream** are **UP**. If it’s **DOWN**:
   If **live_odds_poll** is DOWN, use **Prometheus on host** (section below).

4. **Grafana datasource** must point at Prometheus. If Grafana runs in Docker, the URL must be **http://prometheus:9090** (not `http://localhost:9090`). Our provisioning sets this; if you added the datasource by hand, edit it to use **http://prometheus:9090**.

5. **Time range** — set to “Last 5 minutes” or “Last 1 hour” so recent scrapes are included.

### Prometheus on host (when Docker Prometheus has 0 series)

If the exporter is OK but Prometheus has 0 series, Prometheus in Docker may not reach the host. Run Prometheus on your Mac so it scrapes `localhost:9091`:

1. **Stop the Prometheus container** (keep Grafana):  
   `docker compose -f grafana/docker-compose.yml stop prometheus`

2. **Run Prometheus on your Mac** (from `totem-mm`, new terminal):  
   `mkdir -p data/prometheus`  
   `prometheus --config.file=grafana/prometheus-native.yml --storage.tsdb.path=./data/prometheus --web.listen-address=:9090`  
   (Install with `brew install prometheus` if needed.) Leave it running.

3. **Point Grafana at the host:** In Grafana go to **Connections** → **Data sources** → **Prometheus** → set **URL** to `http://host.docker.internal:9090` → **Save and test**.

4. Reload the **Live Odds** dashboard (time **Last 15 minutes**, refresh **5s**). Data should appear.

## Dashboard not refreshing?

1. **Refresh dropdown (top right)** – In the dashboard, open the **time range** picker (e.g. “Last 1 hour”). Next to it is the **refresh** dropdown. Set it to **5s**, **2s**, or **1s** (not “Off”).
2. **All three processes must be running:**
   - **Polling script** → writes `data/live_odds_polling.txt` every second.
   - **Exporter** → `python3 scripts/prometheus_exporter.py` (serves `/metrics` from that file).
   - **Prometheus** → scrapes the exporter (Docker or native).
3. **Check Prometheus targets** – Open http://localhost:9090/targets and ensure the `live_odds` job is **UP**. If it’s DOWN, the exporter isn’t reachable (e.g. start the exporter, or fix `host.docker.internal` on Linux).
4. **Re-import the dashboard** – If you imported an old JSON, re-import `grafana/dashboards/live_odds_poll.json` (or run `python3 grafana/scripts/build_dashboard.py` and re-import). The built dashboard has **liveNow** and refresh intervals (1s, 2s, 5s) set.

## Ticks (1% and 0.01)

- **Percentage panels:** Edit panel → **Axis** → set **Tick step** to `1` (for 1% grid). Optionally set **Min** = 0, **Max** = 100.
- **Arb & MM panel:** Edit panel → **Axis** → set **Tick step** to `0.01`. Min = 0.7, Max = 1.3 (already set in the dashboard).

## Ports and env

| Env / Port | Default | Description |
|------------|--------|-------------|
| `EXPORTER_PORT` | 9091 | Exporter HTTP port (use 9092 for stream) |
| `POLLING_FILE` | `data/live_odds_polling.txt` | Data file (relative to project root or absolute) |
| `DATA_SOURCE` | `poll` | Label: `poll` or `stream` |
| 9091 | Exporter (poll) | /metrics for polling data |
| 9092 | Exporter (stream) | /metrics for streaming data |
| 9090 | Prometheus | Scrape config and UI |
| 3000 | Grafana | Dashboards |

## Linux (no Docker host network)

If Prometheus in Docker cannot reach the host exporter at `host.docker.internal:9091`, either:

- Run the exporter with `--network host` (not typical), or  
- In `grafana/prometheus.yml` set the target to your host IP, e.g. `192.168.1.x:9091`, and ensure the exporter binds to `0.0.0.0` (it does by default).

## Metrics

- **live_odds_percent** – Gauges with labels `datasource` (poll | stream), `source` (betfair | polymarket), `metric` (back, lay, last, bid, ask, lt, price), `team` (e.g. SL, ENG from `.env` TEAM_A/TEAM_B). Value 0–100.
- **live_odds_sum** – Gauges with label `series`: `arb_ask`, `mm_bid`. Arb &lt; 1, MM &gt; 1.
