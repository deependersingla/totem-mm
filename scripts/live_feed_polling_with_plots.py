#!/usr/bin/env python3
"""
Live feed POLLING + live graphs. Writes to data/live_odds_polling.txt in a background
thread; main thread runs a matplotlib window and redraws from the file every 1s.
Run: python scripts/live_feed_polling_with_plots.py
"""

import os
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, SCRIPT_DIR)

import dotenv
dotenv.load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from live_feed_common import (
    betfair_book_to_probs,
    fetch_poly_book,
    fetch_poly_prices,
    get_market_ids,
    get_team_labels,
    get_token_map,
    ist_now,
    poly_book_to_probs,
    poly_prices_last,
)

DATA_DIR = os.path.join(PROJECT_ROOT, "data")
POLLING_FILE = os.path.join(DATA_DIR, "live_odds_polling.txt")
POLL_INTERVAL = 1.0
PLOT_REFRESH_INTERVAL = 1.0

# Optional: matplotlib may not be installed. Prefer backends that don't need tkinter (MacOSX on Mac).
try:
    import matplotlib
    _backend = None
    if sys.platform == "darwin":
        try:
            matplotlib.use("MacOSX")
            _backend = "MacOSX"
        except Exception:
            pass
    if _backend is None:
        try:
            matplotlib.use("TkAgg")
            _backend = "TkAgg"
        except Exception:
            try:
                matplotlib.use("Qt5Agg")
                _backend = "Qt5Agg"
            except Exception:
                matplotlib.use("Agg")
                _backend = "Agg"
    import matplotlib.pyplot as plt
    from matplotlib.gridspec import GridSpec
    from matplotlib.ticker import MultipleLocator, MaxNLocator
    from matplotlib.patches import Rectangle
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    GridSpec = None
    MultipleLocator = None
    MaxNLocator = None
    Rectangle = None


def _fmt(x: float | None) -> str:
    if x is None:
        return ""
    return f"{x:.4f}"


def _parse_float(s: str) -> float | None:
    if not s or (s := s.strip()) == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_polling_file(filepath: str):
    """Parse live_odds_polling.txt; return (times, data_dict) or ([], {})."""
    if not os.path.isfile(filepath):
        return [], {}
    times = []
    out = {
        "bf_back_a": [], "bf_lay_a": [], "bf_last_a": [],
        "bf_back_b": [], "bf_lay_b": [], "bf_last_b": [],
        "poly_bid_a": [], "poly_ask_a": [], "poly_lt_a": [], "poly_price_a": [],
        "poly_bid_b": [], "poly_ask_b": [], "poly_lt_b": [], "poly_price_b": [],
    }
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("-") or "Betfair (back" in line:
                continue
            # Format: "14:23:59 | AUS BF: 62.1/61.7/61.7 | IND BF: 38.4/37.8/38.4 | AUS Poly: 59/60/41/62.5 | IND Poly: 40/41/41/37.5"
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5:
                continue
            ist_str = parts[0]
            times.append(ist_str)
            # BF parts: "AUS BF: back/lay/last" and "IND BF: ..."
            for i, key_pref in [(1, "bf"), (2, "bf")]:
                pref = "a" if i == 1 else "b"
                after_colon = parts[i].split(":", 1)[-1].strip() if ":" in parts[i] else ""
                nums = [_parse_float(x) for x in after_colon.replace(",", ".").split("/")]
                out[f"{key_pref}_back_{pref}"].append(nums[0] if len(nums) > 0 else None)
                out[f"{key_pref}_lay_{pref}"].append(nums[1] if len(nums) > 1 else None)
                out[f"{key_pref}_last_{pref}"].append(nums[2] if len(nums) > 2 else None)
            # Poly: "AUS Poly: bid/ask/lt/price"
            for i, pref in [(3, "a"), (4, "b")]:
                after_colon = parts[i].split(":", 1)[-1].strip() if ":" in parts[i] else ""
                nums = [_parse_float(x) for x in after_colon.replace(",", ".").split("/")]
                out[f"poly_bid_{pref}"].append(nums[0] if len(nums) > 0 else None)
                out[f"poly_ask_{pref}"].append(nums[1] if len(nums) > 1 else None)
                out[f"poly_lt_{pref}"].append(nums[2] if len(nums) > 2 else None)
                out[f"poly_price_{pref}"].append(nums[3] if len(nums) > 3 else None)
    return times, out


def fetch_betfair_book():
    from connectors.betfair.client import BetfairClient
    market_ids = get_market_ids()
    if not market_ids:
        return None
    client = BetfairClient()
    params = {
        "marketIds": market_ids,
        "priceProjection": {"priceData": ["EX_BEST_OFFERS", "EX_TRADED"]},
    }
    resp = client.call("SportsAPING/v1.0/listMarketBook", params)
    result = resp.get("result") or []
    return result[0] if result else None


def run_writer(team_a: str, team_b: str, sel_to_token: dict, selection_ids_ordered: list, stop: threading.Event):
    """Background thread: fetch and append to POLLING_FILE."""
    os.makedirs(DATA_DIR, exist_ok=True)
    header_line = f"IST     | {team_a} Betfair (back/lay/last)       | {team_b} Betfair (back/lay/last)      | {team_a} Poly (bid/ask/lt/price)           | {team_b} Poly (bid/ask/lt/price)"
    sep_line = "-" * 130
    if not os.path.exists(POLLING_FILE) or os.path.getsize(POLLING_FILE) == 0:
        with open(POLLING_FILE, "a", encoding="utf-8") as f:
            f.write(header_line + "\n")
            f.write(sep_line + "\n")
    while not stop.is_set():
        t0 = time.perf_counter()
        row_parts = [ist_now()]
        bf = fetch_betfair_book()
        if bf:
            probs = betfair_book_to_probs(bf)
            for sid in selection_ids_ordered:
                p = probs.get(sid) or {}
                row_parts.append(_fmt(p.get("back_pct")))
                row_parts.append(_fmt(p.get("lay_pct")))
                row_parts.append(_fmt(p.get("last_pct")))
        else:
            for _ in selection_ids_ordered:
                row_parts.extend(["", "", ""])
        for tid in [sel_to_token[sid] for sid in selection_ids_ordered]:
            try:
                book = fetch_poly_book(tid)
                bid, ask, lt = poly_book_to_probs(book)
                row_parts.append(_fmt(bid * 100.0) if bid is not None else "")
                row_parts.append(_fmt(ask * 100.0) if ask is not None else "")
                row_parts.append(_fmt(lt * 100.0) if lt is not None else "")
                pr = poly_prices_last(fetch_poly_prices(tid))
                row_parts.append(_fmt(pr * 100.0) if pr is not None else "")
            except Exception:
                row_parts.extend(["", "", "", ""])
        if len(row_parts) >= 15:
            ist, ab, al, alt, bb, bl, blt, a_pb, a_pa, a_plt, a_pr, b_pb, b_pa, b_plt, b_pr = row_parts[:15]
            a_bf = f"{ab or '-'}/{al or '-'}/{alt or '-'}"
            b_bf = f"{bb or '-'}/{bl or '-'}/{blt or '-'}"
            a_poly = f"{a_pb or '-'}/{a_pa or '-'}/{a_plt or '-'}/{a_pr or '-'}"
            b_poly = f"{b_pb or '-'}/{b_pa or '-'}/{b_plt or '-'}/{b_pr or '-'}"
            visual = f"{ist} | {team_a} BF: {a_bf:28} | {team_b} BF: {b_bf:28} | {team_a} Poly: {a_poly:32} | {team_b} Poly: {b_poly}"
        else:
            visual = "\t".join(row_parts)
        with open(POLLING_FILE, "a", encoding="utf-8") as f:
            f.write(visual + "\n")
            f.flush()
        print(visual)
        elapsed = time.perf_counter() - t0
        sleep = max(0.0, POLL_INTERVAL - elapsed)
        if sleep > 0 and not stop.is_set():
            stop.wait(timeout=sleep)


def _set_xticks_labels(ax, n: int, x: list):
    xi = list(range(n))
    if n <= 20:
        ax.set_xticks(xi)
        ax.set_xticklabels(x, rotation=45, ha="right", fontsize=9)
    else:
        step = max(1, n // 15)
        ax.set_xticks(xi[::step])
        ax.set_xticklabels([x[i] for i in range(0, n, step)], rotation=45, ha="right", fontsize=9)
    ax.set_xlim(-0.5, n - 0.5)


def _plot_series(ax, x, ya, yb, team_a: str, team_b: str, title: str, ylabel: str = "%"):
    ax.clear()
    ax.set_title(title, fontsize=13)
    ax.set_ylabel(ylabel, fontsize=11)
    n = len(x)
    if n == 0:
        return
    xi = list(range(n))
    if ya:
        ax.plot(xi, ya, label=team_a, color="C0", alpha=0.9, linewidth=2)
    if yb:
        ax.plot(xi, yb, label=team_b, color="C1", alpha=0.9, linewidth=2)
    ax.legend(loc="upper right", fontsize=10)
    ax.set_ylim(0, 100)
    if MultipleLocator is not None:
        ax.yaxis.set_major_locator(MultipleLocator(10))
        ax.yaxis.set_minor_locator(MultipleLocator(1))
    ax.grid(True, alpha=0.3, which="both")
    ax.tick_params(axis="both", labelsize=10)
    _set_xticks_labels(ax, n, x)


def _plot_arb_mm(ax, x, sum_asks, sum_bids):
    """Single chart: Arb (ask sum) and MM (bid sum); Y from 0.70 to 1.30 with 0.01 grid."""
    ax.clear()
    ax.set_title("Arb & MM: ask sum (arb < 1) · bid sum (MM > 1)", fontsize=13)
    ax.set_ylabel("Sum", fontsize=11)
    n = len(x)
    if n == 0:
        return
    xi = list(range(n))
    ax.axhline(1.0, color="gray", linestyle="--", alpha=0.8, linewidth=1.5)
    ax.plot(xi, sum_asks, label="Arb (ask A+B)", color="C0", alpha=0.9, linewidth=2)
    ax.plot(xi, sum_bids, label="MM (bid A+B)", color="C1", alpha=0.9, linewidth=2)
    ax.legend(loc="upper right", fontsize=10)
    ax.set_ylim(0.70, 1.30)
    if MultipleLocator is not None:
        ax.yaxis.set_major_locator(MultipleLocator(0.10))
        ax.yaxis.set_minor_locator(MultipleLocator(0.01))
    ax.grid(True, alpha=0.3, which="both")
    ax.tick_params(axis="both", labelsize=10)
    _set_xticks_labels(ax, n, x)


CHART_TITLES = [
    "1. Betfair back %",
    "2. Betfair lay %",
    "3. Betfair last %",
    "4. Polymarket bid %",
    "5. Polymarket ask %",
    "6. Polymarket last trade %",
    "7. Polymarket price %",
    "8. Arb & MM",
]


def run_plots(team_a: str, team_b: str, stop: threading.Event):
    """Main thread: large 4×2 graphs, checkboxes 1–8 at top to show/hide charts."""
    if not HAS_MATPLOTLIB or GridSpec is None or Rectangle is None:
        print("matplotlib not installed; run: pip install matplotlib", file=sys.stderr)
        return
    plt.ion()
    fig = plt.figure(figsize=(20, 22))
    fig.suptitle(f"Live odds: {team_a} vs {team_b} — tick chart numbers to show/hide · hover for values", fontsize=14)
    ax_cb = fig.add_axes([0.08, 0.92, 0.84, 0.05])
    ax_cb.set_xlim(0, 8)
    ax_cb.set_ylim(0, 1)
    ax_cb.axis("off")
    chart_visible = [True] * 8
    box_rects = []
    for i in range(8):
        x = 0.25 + i
        r = ax_cb.add_patch(Rectangle((x, 0.15), 0.5, 0.7, facecolor="lightgreen", edgecolor="darkgreen", linewidth=1.5))
        ax_cb.text(x + 0.25, 0.5, str(i + 1), ha="center", va="center", fontsize=12, fontweight="bold")
        box_rects.append(r)

    def update_checkbox_display():
        for i in range(8):
            box_rects[i].set_facecolor("lightgreen" if chart_visible[i] else "lightgray")
            box_rects[i].set_edgecolor("darkgreen" if chart_visible[i] else "gray")
        fig.canvas.draw_idle()

    def on_click(event):
        if event.inaxes != ax_cb or event.ydata is None or event.xdata is None:
            return
        x = event.xdata
        for i in range(8):
            if 0.25 + i <= x < 0.75 + i:
                chart_visible[i] = not chart_visible[i]
                ax_flat[i].set_visible(chart_visible[i])
                update_checkbox_display()
                break

    gs = GridSpec(4, 2, figure=fig, left=0.06, right=0.96, bottom=0.04, top=0.86, hspace=0.52, wspace=0.42)
    ax_flat = [fig.add_subplot(gs[r, c]) for r in range(4) for c in range(2)]
    hover_data = [None, {}]
    ann = [None]

    def on_motion(event):
        if ann[0] is None:
            return
        if event.inaxes is None:
            ann[0].set_visible(False)
            fig.canvas.draw_idle()
            return
        times_ref, data_ref = hover_data[0], hover_data[1]
        if not times_ref or not data_ref:
            return
        ax = event.inaxes
        try:
            ax_idx = ax_flat.index(ax)
        except ValueError:
            return
        if ax_idx not in data_ref or not ax.lines:
            return
        xdata = event.xdata
        if xdata is None:
            return
        idx = int(round(xdata))
        n = len(times_ref)
        if idx < 0 or idx >= n:
            return
        time_str = times_ref[idx]
        # Find nearest line in y
        best_line_i = 0
        best_dist = float("inf")
        for i, line in enumerate(ax.lines):
            if not line.get_visible():
                continue
            yd = line.get_ydata()
            if idx >= len(yd):
                continue
            yval = yd[idx]
            if isinstance(yval, float) and (yval != yval):  # nan
                continue
            d = abs(event.ydata - yval) if event.ydata is not None else 0
            if d < best_dist:
                best_dist = d
                best_line_i = i
        if best_line_i >= len(ax.lines):
            return
        line = ax.lines[best_line_i]
        yd = line.get_ydata()
        if idx >= len(yd):
            return
        yval = yd[idx]
        label = line.get_label() if line.get_label() else f"line {best_line_i}"
        ann[0].xy = (idx, yval)
        ann[0].xycoords = ax.transData
        ann[0].set_text(f"{time_str}\n{label}: {yval:.4f}")
        ann[0].set_visible(True)
        fig.canvas.draw_idle()

    def on_motion_leave(event):
        if ann[0]:
            ann[0].set_visible(False)
            fig.canvas.draw_idle()

    leg_visibility = {}  # (ax_idx, line_idx) -> bool

    def on_pick(event):
        if not hasattr(event.artist, "_toggle_idx"):
            return
        i, j = event.artist._toggle_idx
        leg_visibility[(i, j)] = not leg_visibility.get((i, j), True)
        event.artist.set_visible(leg_visibility[(i, j)])
        if i < len(ax_flat) and j < len(ax_flat[i].lines):
            ax_flat[i].lines[j].set_visible(leg_visibility[(i, j)])
        fig.canvas.draw_idle()

    cid_click = fig.canvas.mpl_connect("button_press_event", on_click)
    cid_pick = fig.canvas.mpl_connect("pick_event", on_pick)
    # Annotation on figure so it survives ax.clear() each refresh
    if ax_flat:
        ann[0] = fig.annotate(
            "", xy=(0, 0), xycoords=ax_flat[0].transData,
            xytext=(12, 12), textcoords="offset points",
            fontsize=10, bbox=dict(boxstyle="round,pad=0.3", facecolor="wheat", alpha=0.9),
            visible=False,
        )
    cid_motion = fig.canvas.mpl_connect("motion_notify_event", on_motion)
    cid_leave = fig.canvas.mpl_connect("axes_leave_event", on_motion_leave)
    update_checkbox_display()

    plt.show(block=False)
    fig.canvas.draw()
    try:
        if hasattr(fig.canvas, "manager") and fig.canvas.manager.window:
            fig.canvas.manager.window.raise_()
    except Exception:
        pass

    while not stop.is_set():
        try:
            if not plt.fignum_exists(fig.number):
                break
        except Exception:
            break
        times, d = parse_polling_file(POLLING_FILE)
        n = len(times)
        if n > 0:
            _plot_series(ax_flat[0], times, d["bf_back_a"], d["bf_back_b"], team_a, team_b, CHART_TITLES[0])
            _plot_series(ax_flat[1], times, d["bf_lay_a"], d["bf_lay_b"], team_a, team_b, CHART_TITLES[1])
            _plot_series(ax_flat[2], times, d["bf_last_a"], d["bf_last_b"], team_a, team_b, CHART_TITLES[2])
            _plot_series(ax_flat[3], times, d["poly_bid_a"], d["poly_bid_b"], team_a, team_b, CHART_TITLES[3])
            _plot_series(ax_flat[4], times, d["poly_ask_a"], d["poly_ask_b"], team_a, team_b, CHART_TITLES[4])
            _plot_series(ax_flat[5], times, d["poly_lt_a"], d["poly_lt_b"], team_a, team_b, CHART_TITLES[5])
            _plot_series(ax_flat[6], times, d["poly_price_a"], d["poly_price_b"], team_a, team_b, CHART_TITLES[6])
            sum_asks = []
            for i in range(n):
                a = d["poly_ask_a"][i] if i < len(d["poly_ask_a"]) else None
                b = d["poly_ask_b"][i] if i < len(d["poly_ask_b"]) else None
                if a is not None and b is not None:
                    sum_asks.append((a + b) / 100.0)
                else:
                    sum_asks.append(float("nan"))
            sum_bids = []
            for i in range(n):
                a = d["poly_bid_a"][i] if i < len(d["poly_bid_a"]) else None
                b = d["poly_bid_b"][i] if i < len(d["poly_bid_b"]) else None
                if a is not None and b is not None:
                    sum_bids.append((a + b) / 100.0)
                else:
                    sum_bids.append(float("nan"))
            _plot_arb_mm(ax_flat[7], times, sum_asks, sum_bids)

            for i in range(8):
                ax_flat[i].set_visible(chart_visible[i])

            hover_data[0] = times
            hover_data[1] = {i: [(ln.get_label(), list(ln.get_ydata())) for ln in ax_flat[i].lines] for i in range(8)}

            for i, ax in enumerate(ax_flat):
                if ax.get_legend() is None:
                    continue
                leg = ax.get_legend()
                for j, leg_line in enumerate(leg.get_lines()):
                    vis = leg_visibility.get((i, j), True)
                    leg_line.set_visible(vis)
                    leg_line.set_picker(8)
                    leg_line._toggle_idx = (i, j)
                    if j < len(ax.lines):
                        ax.lines[j].set_visible(vis)

        fig.canvas.draw_idle()
        fig.canvas.flush_events()
        plt.pause(PLOT_REFRESH_INTERVAL)
    for cid in (cid_click, cid_pick, cid_motion, cid_leave):
        try:
            fig.canvas.mpl_disconnect(cid)
        except Exception:
            pass
    try:
        plt.close(fig)
    except Exception:
        pass


def main():
    token_map = get_token_map()
    market_ids = get_market_ids()
    if not token_map or not market_ids:
        print("Set BETFAIR_MARKET_IDS and TOKEN_MAP in .env", file=sys.stderr)
        sys.exit(1)
    team_a, team_b = get_team_labels()
    sel_to_token = {sel_id: tid for tid, sel_id in token_map.items()}
    selection_ids_ordered = sorted(sel_to_token.keys())
    stop = threading.Event()
    writer = threading.Thread(
        target=run_writer,
        args=(team_a, team_b, sel_to_token, selection_ids_ordered, stop),
        daemon=True,
    )
    writer.start()
    print("Polling (writer) started →", POLLING_FILE)
    if HAS_MATPLOTLIB:
        try:
            run_plots(team_a, team_b, stop)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
    else:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop.set()


if __name__ == "__main__":
    main()
