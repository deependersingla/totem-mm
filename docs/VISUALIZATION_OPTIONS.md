# Live odds visualization – options (Grafana, Prometheus, Python GUIs)

## What you want
- **1% ticks** on percentage charts (0–100%)
- **0.01 ticks** on Arb/MM chart
- **Working hover** with exact data points
- **Good GUI** – clear, responsive, “pro” feel

---

## 1. Grafana + Prometheus (or InfluxDB)

**Idea:** Your Python script exposes metrics (or writes to a DB). Grafana connects and builds dashboards in the browser.

| Pros | Cons |
|------|------|
| Very good time-series UX: zoom, pan, hover, annotations | Need to run Prometheus + Grafana (or InfluxDB + Grafana) |
| 1% / 0.01 ticks and decimals are easy in Grafana | One-time setup and config |
| Browser-based, no matplotlib window | Script must push or expose metrics |

**Ways to get data in:**

- **Prometheus:** Script runs an HTTP server; Prometheus scrapes it every few seconds. You expose gauges like `live_odds_bf_back_team_a`, `live_odds_arb_sum`, etc. Grafana datasource = Prometheus.  
  - Ticks: in panel → Axis → Unit “percent (0–100)” or “none”, then set “Step” / “Min step” for 1 or 0.01 as needed.

- **InfluxDB:** Script writes time-series points (timestamp + fields) to InfluxDB. Grafana datasource = InfluxDB.  
  - Same idea: configure axis step 1 for %, 0.01 for arb.

- **Grafana Infinity / JSON API:** A small Flask/FastAPI app returns current (or recent) time-series as JSON. Grafana Infinity plugin can query that URL. Less common but no Prometheus/InfluxDB needed.

**Summary:** “Prometheus + Grafana” (or InfluxDB + Grafana) fits “pro” dashboards and fine ticks; the cost is running two extra services and wiring your script to them.

---

## 2. Python-only: Plotly Dash or Streamlit

**Idea:** One Python app that reads your polling file (or in-memory data), builds Plotly charts with exact tick control and built-in hover. Opens in browser.

| Pros | Cons |
|------|------|
| Single process, no DB or Prometheus | You keep the app running (like current script) |
| 1% and 0.01 ticks trivial in Plotly | Slightly more code than “tail file + matplotlib” |
| Hover “just works” (Plotly default) | |
| Good-looking, responsive UI | |

**Ticks in Plotly:**  
- Percent: `layout.yaxis.dtick = 1`, range `[0, 100]`.  
- Arb/MM: `layout.yaxis.dtick = 0.01`, range e.g. `[0.7, 1.3]`.

**Dash:** More structure (callbacks, layout). Best if you want dropdowns, checkboxes, multiple pages.  
**Streamlit:** Simpler (script = UI). Good for a single live dashboard that auto-refreshes.

---

## 3. Bokeh server

**Idea:** Bokeh can serve an app over HTTP; you get interactive plots (zoom, hover, tooltips) in the browser. Your script updates a Bokeh document in memory; the server pushes updates to the client.

- Good hover and tooltips.
- Ticks: set `yaxis.ticker = FixedTicker(ticks=list(range(0,101))` for 1%, or numpy.arange(0.7, 1.31, 0.01) for 0.01.
- One Python process; no Prometheus/InfluxDB.

---

## 4. Current matplotlib script (what we fixed)

- **Hover:** Fixed by using a **figure-level** annotation (`fig.annotate`) and updating `xy` + `xycoords` to the hovered axis’s `transData` each time, so the tooltip survives `ax.clear()` on refresh.
- **Ticks:**  
  - **Percent:** major 10%, minor **1%**.  
  - **Arb/MM:** major 0.10, minor **0.01**.

So you get 1% and 0.01 ticks and working hover in the current window while you try Grafana or a Python dashboard.

---

## Recommendation (brainstorm)

- **Short term:** Use the updated matplotlib script (hover + 1% / 0.01 ticks) as the daily driver.
- **If you want “Grafana-like” in the browser without extra infra:** Add a **Plotly Dash** or **Streamlit** app that reads the same polling file and plots with 1% / 0.01; you get pro hover and control with one process.
- **If you want real Grafana:** Add **Prometheus** (or InfluxDB) and have your polling script expose/write metrics, then point **Grafana** at it and set axis steps to 1% and 0.01.

If you say which path you prefer (Grafana vs Dash/Streamlit vs Bokeh), we can sketch the exact steps and code changes next.
