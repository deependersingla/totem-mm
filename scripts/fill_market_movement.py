#!/usr/bin/env python3
"""
Fill market_movement field in events JSON.

Simple approach based on empirical data:
- ESPN delay is consistently 60-110s
- For each event, look 120s back from ESPN time
- Find the FIRST significant price jump in expected direction
- Track it to its peak
- That's the movement. Latency = ESPN_time - first_jump_time.

Usage:
    python fill_market_movement.py <events.json> <capture.jsonl> <nz_team> [--match-date 2026-03-22]
"""

import argparse
import json
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

LOOKBACK_SEC = 120
LOOKBACK_EXTENDED = 480  # fallback for DRS reviews, breaks
MIN_LATENCY_SEC = 5
BOUNDARY_JUMP = 1   # min cents for first jump (boundary)
WICKET_JUMP = 2     # min cents for first jump (wicket)


def parse_ist(ist_str):
    clean = ist_str.replace(" IST", "").strip()
    try:
        return datetime.strptime(clean[:26], "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        try:
            return datetime.strptime(clean[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def load_price_timeline(jsonl_path, nz_team, match_date=None):
    price_by_sec = defaultdict(list)
    nz_outcome = None
    sa_outcome = None

    with open(jsonl_path) as f:
        for line in f:
            d = json.loads(line)
            typ = d.get("type", "")
            if typ == "capture_start":
                for name in d.get("outcome_names", []):
                    if nz_team.lower() in name.lower():
                        nz_outcome = name
                    else:
                        sa_outcome = name
                continue
            if typ not in ("trade", "rest_trade", "pure_fill", "snipe_mix"):
                continue
            outcome = d.get("outcome", "")
            price = d.get("price")
            ist = d.get("ist", "")
            if price is None or not outcome or not ist:
                continue
            dt = parse_ist(ist)
            if dt is None:
                continue
            if match_date and dt.strftime("%Y-%m-%d") != match_date:
                continue
            if outcome == nz_outcome:
                nz_price = float(price)
            elif outcome == sa_outcome:
                nz_price = 1.0 - float(price)
            else:
                continue
            price_by_sec[dt.replace(microsecond=0)].append(nz_price)

    def median(vals):
        s = sorted(vals)
        return s[len(s) // 2]

    timeline = sorted(
        [(dt, median(prices)) for dt, prices in price_by_sec.items()],
        key=lambda x: x[0]
    )
    print(f"Loaded {len(timeline)} price points" + (f" on {match_date}" if match_date else ""))
    if nz_outcome:
        print(f"  NZ outcome: '{nz_outcome}', SA outcome: '{sa_outcome}'")
    return timeline


def smooth(timeline, idx, window=5):
    dt_c = timeline[idx][0]
    nearby = sorted([p for t, p in timeline
                     if abs((t - dt_c).total_seconds()) <= window])
    return nearby[len(nearby) // 2] if nearby else timeline[idx][1]


def get_smoothed_prices(timeline, t_start, t_end):
    """Get smoothed prices in a time window, only at points where price changes."""
    result = []
    prev_p = None
    for i, (dt, _) in enumerate(timeline):
        if dt < t_start:
            continue
        if dt > t_end:
            break
        sp = smooth(timeline, i)
        if sp != prev_p:
            result.append((dt, sp))
            prev_p = sp
    return result


def _scan_for_jump(timeline, t_start, t_end, expected_dir, jump_threshold):
    """Scan second by second, find FIRST jump >= threshold in expected direction.
    Uses raw per-second prices (no smoothing) to avoid hiding sharp moves."""
    pts = []
    for i, (dt, _) in enumerate(timeline):
        if dt < t_start:
            continue
        if dt > t_end:
            break
        pts.append((dt, smooth(timeline, i, window=3)))

    if len(pts) < 2:
        return None, None, None

    for i in range(1, len(pts)):
        prev_dt, prev_p = pts[i - 1]
        curr_dt, curr_p = pts[i]
        diff_c = round((curr_p - prev_p) * 100)

        if expected_dir == "down" and diff_c <= -jump_threshold:
            return prev_dt, prev_p, pts
        elif expected_dir == "up" and diff_c >= jump_threshold:
            return prev_dt, prev_p, pts

    return None, None, pts


def find_event_movement(timeline, espn_time, expected_dir, is_wicket, prev_price=None):
    """
    For each event, scan from 120s before ESPN time.
    Find the FIRST significant price jump in expected direction.
    If not found, extend lookback to 480s (DRS, breaks).
    At extreme prices (<0.15 or >0.85), use 1c threshold even for wickets.
    """
    # Adaptive threshold: at extreme prices, 1c is significant
    if prev_price is not None and (prev_price < 0.15 or prev_price > 0.85):
        jump_threshold = 1
    else:
        jump_threshold = WICKET_JUMP if is_wicket else BOUNDARY_JUMP

    # Try normal lookback first
    t_end = espn_time
    t_start = espn_time - timedelta(seconds=LOOKBACK_SEC)
    anchor_dt, anchor_p, pts = _scan_for_jump(timeline, t_start, t_end,
                                               expected_dir, jump_threshold)

    # If not found, try extended lookback (DRS, breaks, drinks)
    if anchor_dt is None:
        t_start = espn_time - timedelta(seconds=LOOKBACK_EXTENDED)
        anchor_dt, anchor_p, pts = _scan_for_jump(timeline, t_start, t_end,
                                                   expected_dir, jump_threshold)

    if anchor_dt is None or pts is None:
        return None

    latency = int((espn_time - anchor_dt).total_seconds())
    if latency < MIN_LATENCY_SEC:
        return None

    # Track to peak from anchor to ESPN time
    peak_p = anchor_p
    peak_dt = anchor_dt

    for dt, p in pts:
        if dt < anchor_dt:
            continue
        if expected_dir == "up" and p > peak_p:
            peak_p = p
            peak_dt = dt
        elif expected_dir == "down" and p < peak_p:
            peak_p = p
            peak_dt = dt

    ticks = abs(round((peak_p - anchor_p) * 100))
    if ticks < 1:
        return None

    return {
        "anchor_time": anchor_dt,
        "anchor_price": anchor_p,
        "peak_time": peak_dt,
        "peak_price": peak_p,
        "ticks": ticks,
        "latency": latency,
    }


def fill_events(events, timeline, nz_team):
    matched = 0
    missed = 0
    last_known_price = None

    for e in events:
        espn_time = parse_ist(e["ist"])
        if espn_time is None:
            e["market_movement"] = "no_data"
            missed += 1
            continue

        expected_dir = "up" if e["side"] == nz_team else "down"
        is_wicket = e["event"] == "WICKET"

        move = find_event_movement(timeline, espn_time, expected_dir, is_wicket,
                                    prev_price=last_known_price)
        if move:
            direction_label = f"{nz_team}_UP" if expected_dir == "up" else f"{nz_team}_DOWN"
            e["market_movement"] = {
                "market_move_start": move["anchor_time"].strftime("%H:%M:%S"),
                "market_move_end": move["peak_time"].strftime("%H:%M:%S"),
                "price_before": round(move["anchor_price"], 4),
                "price_after": round(move["peak_price"], 4),
                "ticks": move["ticks"],
                "latency_sec": move["latency"],
                "direction": direction_label,
            }
            last_known_price = move["peak_price"]
            matched += 1
        else:
            e["market_movement"] = "no_significant_movement"
            missed += 1

    return matched, missed


def main():
    parser = argparse.ArgumentParser(description="Fill market_movement")
    parser.add_argument("events_json", help="Path to events JSON file")
    parser.add_argument("capture_jsonl", help="Path to capture JSONL file")
    parser.add_argument("nz_team", help="Team abbreviation for first outcome")
    parser.add_argument("--match-date", help="Filter trades to this date (YYYY-MM-DD)")
    args = parser.parse_args()

    events_path = Path(args.events_json)
    capture_path = Path(args.capture_jsonl)

    timeline = load_price_timeline(capture_path, args.nz_team, args.match_date)

    with open(events_path) as f:
        events = json.load(f)

    matched, missed = fill_events(events, timeline, args.nz_team)

    with open(events_path, "w") as f:
        json.dump(events, f, indent=2)

    print(f"\n{'='*100}")
    print(f"EVENT REPORT — {len(events)} events | {matched} matched | {missed} missed")
    print(f"{'='*100}")
    print(f"{'#':>3} {'MKTMOVE':>10} {'ESPN':>10} {'LAT':>5} {'OV':>6} {'EVT':>6} "
          f"{'SIDE':>4} {'TICKS':>5} {'NZ BEF':>7} {'NZ AFT':>7} {'SA BEF':>7} {'SA AFT':>7}")
    print("-" * 100)
    for idx, e in enumerate(events):
        mm = e["market_movement"]
        cb_time = e["ist"][:19].split(" ")[-1]

        if isinstance(mm, dict):
            sa_bef = round(1.0 - mm['price_before'], 4)
            sa_aft = round(1.0 - mm['price_after'], 4)
            print(f"{idx+1:>3} {mm['market_move_start']:>10} {cb_time:>10} "
                  f"{mm['latency_sec']:>4}s {e['over']:>6} {e['event']:>6} "
                  f"{e['side']:>4} {mm['ticks']:>5} "
                  f"{mm['price_before']:>7.2f} {mm['price_after']:>7.2f} "
                  f"{sa_bef:>7.2f} {sa_aft:>7.2f}")
        else:
            print(f"{idx+1:>3} {'—':>10} {cb_time:>10} {'—':>5} {e['over']:>6} "
                  f"{e['event']:>6} {e['side']:>4}   {mm}")


if __name__ == "__main__":
    main()
