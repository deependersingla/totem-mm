#!/usr/bin/env python3
"""Build Live Odds Poll and Live Odds Stream dashboards (with datasource filter and {{team}} = real names)."""
import json
import os

DIR = os.path.dirname(os.path.abspath(__file__))
DASH_DIR = os.path.join(os.path.dirname(DIR), "dashboards")
PROV_DIR = os.path.join(os.path.dirname(DIR), "provisioning", "dashboards", "json")


def panel_percent(title, expr: str, grid_pos):
    return {
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {"axisCenteredZero": False, "axisLabel": "", "axisPlacement": "auto", "drawStyle": "line", "fillOpacity": 10, "gradientMode": "none", "hideFrom": {"legend": False, "tooltip": False, "viz": False}, "lineInterpolation": "smooth", "lineWidth": 2, "pointSize": 5, "scaleDistribution": {"type": "linear"}, "showPoints": "auto", "spanNulls": False, "stacking": {"group": "A", "mode": "none"}, "thresholdsStyle": {"mode": "off"}},
                "mappings": [],
                "max": 100,
                "min": 0,
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 80}]},
                "unit": "percent",
            },
            "overrides": [],
        },
        "gridPos": grid_pos,
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "tooltip": {"mode": "single", "sort": "none"}},
        "targets": [{"datasource": {"type": "prometheus", "uid": "prometheus"}, "expr": expr, "legendFormat": "{{team}}", "refId": "A"}],
        "title": title,
        "type": "timeseries",
    }


def panel_arb_mm_with_expr(sum_expr):
    return {
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {"axisCenteredZero": False, "axisLabel": "", "axisPlacement": "auto", "drawStyle": "line", "fillOpacity": 10, "gradientMode": "none", "hideFrom": {"legend": False, "tooltip": False, "viz": False}, "lineInterpolation": "smooth", "lineWidth": 2, "pointSize": 5, "scaleDistribution": {"type": "linear"}, "showPoints": "auto", "spanNulls": False, "stacking": {"group": "A", "mode": "none"}, "thresholdsStyle": {"mode": "off"}},
                "mappings": [],
                "max": 1.3,
                "min": 0.7,
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 1}]},
                "unit": "short",
            },
            "overrides": [],
        },
        "gridPos": {"h": 8, "w": 24, "x": 0, "y": 28},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "tooltip": {"mode": "single", "sort": "none"}},
        "targets": [
            {"datasource": {"type": "prometheus", "uid": "prometheus"}, "expr": sum_expr("arb_ask"), "legendFormat": "Arb (ask A+B)", "refId": "A"},
            {"datasource": {"type": "prometheus", "uid": "prometheus"}, "expr": sum_expr("mm_bid"), "legendFormat": "MM (bid A+B)", "refId": "B"},
        ],
        "title": "Arb & MM (arb < 1, MM > 1)",
        "type": "timeseries",
    }


def panel_data_status():
    """Shows count(live_odds_percent). 0 = exporter not running or Prometheus not scraping."""
    return {
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "description": "If 0: run 'python3 scripts/prometheus_exporter.py' (port 9091) and ensure Prometheus is scraping it. Then set time range Last 15 min.",
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "mappings": [],
                "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}]},
                "unit": "short",
            },
            "overrides": [],
        },
        "gridPos": {"h": 4, "w": 8, "x": 0, "y": 0},
        "options": {"colorMode": "value", "graphMode": "none", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "textMode": "auto"},
        "targets": [{"datasource": {"type": "prometheus", "uid": "prometheus"}, "expr": "count(live_odds_percent)", "legendFormat": "__auto", "refId": "A"}],
        "title": "Data status (series count)",
        "type": "stat",
    }


def build_dashboard(ds_filter: str, title: str, uid: str):
    # Poll: show series that are NOT from stream (so poll or no datasource label). Stream: only stream.
    def expr(source, metric):
        if ds_filter == "poll":
            # Match poll or legacy (no datasource); exclude stream
            return f'live_odds_percent{{source="{source}",metric="{metric}"}} unless live_odds_percent{{datasource="stream",source="{source}",metric="{metric}"}}'
        return f'live_odds_percent{{datasource="stream",source="{source}",metric="{metric}"}}'

    def sum_expr(series: str):
        if ds_filter == "poll":
            return f'live_odds_sum{{series="{series}"}} unless live_odds_sum{{datasource="stream",series="{series}"}}'
        return f'live_odds_sum{{datasource="stream",series="{series}"}}'

    # Status panel at (0,0); percent panels start at y=4 and use gridPos with y offset
    panels = [
        panel_data_status(),
        panel_percent("Betfair back %", expr("betfair", "back"), {"h": 8, "w": 12, "x": 0, "y": 4}),
        panel_percent("Betfair lay %", expr("betfair", "lay"), {"h": 8, "w": 12, "x": 12, "y": 4}),
        panel_percent("Betfair last %", expr("betfair", "last"), {"h": 8, "w": 12, "x": 0, "y": 12}),
        panel_percent("Polymarket bid %", expr("polymarket", "bid"), {"h": 8, "w": 12, "x": 12, "y": 12}),
        panel_percent("Polymarket ask %", expr("polymarket", "ask"), {"h": 8, "w": 12, "x": 0, "y": 20}),
        panel_percent("Polymarket price %", expr("polymarket", "price"), {"h": 8, "w": 12, "x": 12, "y": 20}),
        panel_arb_mm_with_expr(sum_expr),
    ]
    return {
        "annotations": {"list": []},
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "id": None,
        "links": [],
        "liveNow": True,
        "panels": panels,
        "refresh": "5s",
        "schemaVersion": 38,
        "style": "dark",
        "tags": ["live-odds", "totem", ds_filter],
        "templating": {"list": []},
        "time": {"from": "now-1h", "to": "now"},
        "timepicker": {"refresh_intervals": ["1s", "2s", "5s", "10s", "30s"]},
        "timezone": "browser",
        "title": title,
        "uid": uid,
        "version": 1,
    }


def main():
    os.makedirs(DASH_DIR, exist_ok=True)
    os.makedirs(PROV_DIR, exist_ok=True)
    dashboard = build_dashboard("poll", "Live Odds", "live-odds-poll")
    for name in ["live_odds_poll.json"]:
        for base in [DASH_DIR, PROV_DIR]:
            out_path = os.path.join(base, name)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(dashboard, f, indent=2, ensure_ascii=False)
            with open(out_path) as f:
                json.load(f)
            print("Wrote", out_path, "-> valid JSON")


if __name__ == "__main__":
    main()
