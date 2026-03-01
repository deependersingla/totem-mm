#!/usr/bin/env python3
"""
Prometheus exporter for live odds. Reads data/live_odds_polling.txt (last row),
exposes gauges at GET /metrics for Prometheus. Run with live_feed_polling.py writing the file.

Usage:
  python scripts/prometheus_exporter.py
  # Metrics at http://localhost:9091/metrics

Grafana uses Prometheus as datasource; set panel axis to 1% step for % charts, 0.01 for Arb/MM.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, SCRIPT_DIR)

try:
    import dotenv
    dotenv.load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except Exception:
    pass

DATA_DIR = os.path.join(PROJECT_ROOT, "data")
POLLING_FILE = os.path.join(DATA_DIR, "live_odds_polling.txt")
DEFAULT_PORT = 9091


def _team_labels():
    """TEAM_A / TEAM_B from env; safe for Prometheus label (alphanumeric + underscore)."""
    a = (os.environ.get("TEAM_A") or "").strip() or "team_a"
    b = (os.environ.get("TEAM_B") or "").strip() or "team_b"
    def safe(s: str) -> str:
        return "".join(c if c.isalnum() or c in "_-" else "_" for c in s)[:32] or "team"
    return safe(a), safe(b)


def _data_source() -> str:
    """DATA_SOURCE env: poll or stream (default poll)."""
    s = (os.environ.get("DATA_SOURCE") or "poll").strip().lower()
    return s if s in ("poll", "stream") else "poll"


def _parse_float(s: str) -> float | None:
    if not s or (s := s.strip()) == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def get_last_row(filepath: str) -> dict | None:
    """Parse last valid data row from polling file. Returns dict of gauge names -> float or None."""
    if not os.path.isfile(filepath):
        return None
    out = None
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("-") or "Betfair (back" in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5:
                continue
            # BF: back/lay/last for team A and B
            def bf_nums(i):
                after = parts[i].split(":", 1)[-1].strip() if ":" in parts[i] else ""
                return [_parse_float(x) for x in after.replace(",", ".").split("/")]

            def poly_nums(i):
                after = parts[i].split(":", 1)[-1].strip() if ":" in parts[i] else ""
                return [_parse_float(x) for x in after.replace(",", ".").split("/")]

            na = bf_nums(1)
            nb = bf_nums(2)
            pa = poly_nums(3)
            pb = poly_nums(4)
            out = {
                "bf_back_a": na[0] if len(na) > 0 else None,
                "bf_lay_a": na[1] if len(na) > 1 else None,
                "bf_last_a": na[2] if len(na) > 2 else None,
                "bf_back_b": nb[0] if len(nb) > 0 else None,
                "bf_lay_b": nb[1] if len(nb) > 1 else None,
                "bf_last_b": nb[2] if len(nb) > 2 else None,
                "poly_bid_a": pa[0] if len(pa) > 0 else None,
                "poly_ask_a": pa[1] if len(pa) > 1 else None,
                "poly_lt_a": pa[2] if len(pa) > 2 else None,
                "poly_price_a": pa[3] if len(pa) > 3 else None,
                "poly_bid_b": pb[0] if len(pb) > 0 else None,
                "poly_ask_b": pb[1] if len(pb) > 1 else None,
                "poly_lt_b": pb[2] if len(pb) > 2 else None,
                "poly_price_b": pb[3] if len(pb) > 3 else None,
            }
    return out


def metrics_text(data: dict | None, team_a: str = "team_a", team_b: str = "team_b", data_source: str = "poll") -> str:
    """Prometheus exposition format. team_a/team_b = legend names, data_source = poll or stream."""
    ds = data_source if data_source in ("poll", "stream") else "poll"
    lines = [
        "# HELP live_odds_percent Live odds probability (0-100)",
        "# TYPE live_odds_percent gauge",
    ]
    if not data:
        return "\n".join(lines) + "\n"

    for key, val in data.items():
        if val is None:
            continue
        team = team_a if key.endswith("_a") else team_b
        if key.startswith("bf_"):
            metric = key.replace("bf_", "").replace("_a", "").replace("_b", "")
            lines.append(f'live_odds_percent{{datasource="{ds}",source="betfair",metric="{metric}",team="{team}"}} {val}')
        elif key.startswith("poly_"):
            metric = key.replace("poly_", "").replace("_a", "").replace("_b", "")
            lines.append(f'live_odds_percent{{datasource="{ds}",source="polymarket",metric="{metric}",team="{team}"}} {val}')

    lines.append("# HELP live_odds_sum Arb/MM sum (0-2, arb<1, mm>1)")
    lines.append("# TYPE live_odds_sum gauge")
    a_ask = data.get("poly_ask_a")
    b_ask = data.get("poly_ask_b")
    if a_ask is not None and b_ask is not None:
        lines.append(f'live_odds_sum{{datasource="{ds}",series="arb_ask"}} {(a_ask + b_ask) / 100.0:f}')
    a_bid = data.get("poly_bid_a")
    b_bid = data.get("poly_bid_b")
    if a_bid is not None and b_bid is not None:
        lines.append(f'live_odds_sum{{datasource="{ds}",series="mm_bid"}} {(a_bid + b_bid) / 100.0:f}')

    return "\n".join(lines) + "\n"


def main():
    try:
        from http.server import HTTPServer, BaseHTTPRequestHandler
    except ImportError:
        print("Python 3 required", file=sys.stderr)
        sys.exit(1)

    port = int(os.environ.get("EXPORTER_PORT", DEFAULT_PORT))
    polling_file = os.environ.get("POLLING_FILE", POLLING_FILE)
    if not os.path.isabs(polling_file):
        polling_file = os.path.join(PROJECT_ROOT, polling_file)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics" or self.path == "/metrics/":
                data = get_last_row(polling_file)
                team_a, team_b = _team_labels()
                ds = _data_source()
                body = metrics_text(data, team_a=team_a, team_b=team_b, data_source=ds).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/" or self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK\n")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            print(format % args)

    server = HTTPServer(("", port), Handler)
    print(f"Prometheus exporter on http://0.0.0.0:{port}/metrics (reading {polling_file})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
    server.server_close()


if __name__ == "__main__":
    main()
