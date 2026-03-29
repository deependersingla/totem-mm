#!/usr/bin/env python3
"""
Match event → market reaction latency analysis.

Reads special cricket events from a scores xlsx and trade data from a capture
JSONL, then finds the exact instant the market reacted (sharp 1-3s price jump
in expected direction) before each event timestamp.

Outputs a 2-sheet Excel:
  Sheet 1 "Events"           — cricket events with market_lead_sec + reaction ticks
  Sheet 2 "Market Movements" — detailed per-event reaction data

Usage:
    python event_market_lead.py <scores_xlsx> <capture_jsonl> <team_a> <team_b> <winner> [--match-date YYYY-MM-DD] [-o output.xlsx]

Example:
    python event_market_lead.py \
        data/_scores/20260325_NZvSA_definitive.xlsx \
        data/crint-nzl-zaf-2026-03-25/20260325_111048_crint-nzl-zaf-2026-03-25.jsonl \
        NZ SA SA --match-date 2026-03-25
"""

import argparse
import json
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

import pandas as pd

# ── Config ────────────────────────────────────────────────
LOOKBACK_SEC = 120       # first scan window before event
LOOKBACK_EXTENDED = 480  # fallback for DRS reviews, breaks
MIN_LATENCY_SEC = 5      # ignore if reaction is too close to event
REACTION_WINDOW = 3      # seconds — reaction must complete within this
BOUNDARY_JUMP = 1        # min cents for boundary reaction
WICKET_JUMP = 2          # min cents for wicket reaction


# ── Helpers ───────────────────────────────────────────────

def parse_ist(ist_str):
    """Parse IST time string to datetime."""
    clean = str(ist_str).replace(" IST", "").strip()
    # Truncate nanoseconds to microseconds (6 digits after dot)
    if "." in clean:
        base, frac = clean.split(".", 1)
        frac = frac[:6]
        clean = f"{base}.{frac}"
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%H:%M:%S.%f", "%H:%M:%S"):
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue
    return None


def parse_time_only(time_str, match_date=None):
    """Parse HH:MM:SS.fff into datetime, optionally adding match_date."""
    clean = str(time_str).strip()
    # Truncate nanoseconds
    if "." in clean:
        base, frac = clean.split(".", 1)
        frac = frac[:6]
        clean = f"{base}.{frac}"
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            dt = datetime.strptime(clean, fmt)
            if match_date:
                md = datetime.strptime(match_date, "%Y-%m-%d")
                dt = dt.replace(year=md.year, month=md.month, day=md.day)
            return dt
        except ValueError:
            continue
    # Try full datetime parse
    return parse_ist(clean)


def load_price_timeline(jsonl_path, team_a, match_date=None):
    """Build per-second median price timeline for team_a from capture JSONL."""
    price_by_sec = defaultdict(list)
    a_outcome = None
    b_outcome = None

    abbrev_map = {
        "nz": "new zealand", "sa": "south africa",
        "ind": "india", "aus": "australia", "eng": "england",
        "pak": "pakistan", "sl": "sri lanka", "wi": "west indies",
        "ban": "bangladesh", "afg": "afghanistan", "zim": "zimbabwe",
        "ire": "ireland", "sco": "scotland", "ned": "netherlands",
    }
    ta_full = abbrev_map.get(team_a.lower(), team_a.lower())

    with open(jsonl_path) as f:
        for line in f:
            d = json.loads(line)
            typ = d.get("type", "")

            if typ == "capture_start":
                for name in d.get("outcome_names", []):
                    if ta_full in name.lower() or team_a.lower() in name.lower():
                        a_outcome = name
                    else:
                        b_outcome = name
                if not a_outcome:
                    outcome_names = d.get("outcome_names", [])
                    if len(outcome_names) >= 2:
                        a_outcome = outcome_names[0]
                        b_outcome = outcome_names[1]
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

            if outcome == a_outcome:
                a_price = float(price)
            elif outcome == b_outcome:
                a_price = 1.0 - float(price)
            else:
                continue

            price_by_sec[dt.replace(microsecond=0)].append(a_price)

    def median(vals):
        s = sorted(vals)
        return s[len(s) // 2]

    timeline = sorted(
        [(dt, median(prices)) for dt, prices in price_by_sec.items()],
        key=lambda x: x[0]
    )
    print(f"Loaded {len(timeline)} price points"
          + (f" on {match_date}" if match_date else ""))
    if a_outcome:
        print(f"  Team A outcome: '{a_outcome}', Team B outcome: '{b_outcome}'")
    return timeline


# ── Reaction Detection ────────────────────────────────────

def find_event_reaction(timeline, event_time, expected_dir, is_wicket,
                        prev_price=None):
    """Find the exact instant the market reacted to an event.

    Scans backwards from event_time. A "reaction" is a sharp price jump
    within REACTION_WINDOW seconds (1-3s) in the expected direction,
    >= threshold cents. Returns the first (earliest) such reaction.
    """
    if prev_price is not None and (prev_price < 0.15 or prev_price > 0.85):
        jump_threshold = 1
    else:
        jump_threshold = WICKET_JUMP if is_wicket else BOUNDARY_JUMP

    for lookback in (LOOKBACK_SEC, LOOKBACK_EXTENDED):
        t_start = event_time - timedelta(seconds=lookback)
        t_end = event_time

        pts = [(dt, p) for dt, p in timeline if t_start <= dt <= t_end]
        if len(pts) < 2:
            continue

        # Scan for the first sharp jump in expected direction
        for i in range(len(pts) - 1):
            dt_i, p_i = pts[i]

            # Look ahead up to REACTION_WINDOW seconds from this point
            for j in range(i + 1, len(pts)):
                dt_j, p_j = pts[j]
                gap = (dt_j - dt_i).total_seconds()
                if gap > REACTION_WINDOW:
                    break

                diff_c = round((p_j - p_i) * 100)
                hit = False
                if expected_dir == "down" and diff_c <= -jump_threshold:
                    hit = True
                    ticks = abs(diff_c)
                elif expected_dir == "up" and diff_c >= jump_threshold:
                    hit = True
                    ticks = diff_c

                if hit:
                    latency = int((event_time - dt_i).total_seconds())
                    if latency < MIN_LATENCY_SEC:
                        continue
                    return {
                        "reaction_time": dt_i,
                        "price_before": p_i,
                        "price_after": p_j,
                        "ticks": ticks,
                        "latency": latency,
                        "reaction_secs": round(gap, 1),
                    }

    return None


# ── Main ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Cricket event → market reaction latency analysis")
    parser.add_argument("scores_xlsx", help="Scores Excel with 'Special Events' sheet")
    parser.add_argument("capture_jsonl", help="Capture JSONL with trade data")
    parser.add_argument("team_a", help="First team short name (e.g. NZ)")
    parser.add_argument("team_b", help="Second team short name (e.g. SA)")
    parser.add_argument("winner", help="Winning team short name")
    parser.add_argument("--match-date", help="Filter trades to this date (YYYY-MM-DD)")
    parser.add_argument("-o", "--output", help="Output xlsx path")
    args = parser.parse_args()

    match_date = args.match_date
    team_a = args.team_a
    team_b = args.team_b

    # 1. Load special events
    events_df = pd.read_excel(args.scores_xlsx, sheet_name="Special Events")
    print(f"Loaded {len(events_df)} special events")

    # 2. Build price timeline
    timeline = load_price_timeline(args.capture_jsonl, team_a, match_date)
    if not timeline:
        print("ERROR: No price data found")
        return

    # 3. Process each event
    last_known_price = None
    event_rows = []
    movement_rows = []

    for idx, row in events_df.iterrows():
        inn = int(row["Inn"])
        over = row["Over"]
        batting_team = str(row["Team"]).strip()
        score = str(row["Score"])
        runs = row["Runs"]
        event_type = str(row["Event"]).strip()
        summary = str(row.get("Summary", ""))
        fastest_ist = str(row.get("Fastest IST", ""))

        # Parse event time
        event_time = parse_time_only(fastest_ist, match_date)
        if event_time is None:
            event_rows.append({
                **{c: row[c] for c in events_df.columns},
                "expected_dir": "",
                "market_reaction_ist": "",
                "market_lead_sec": None,
                "reaction_ticks": None,
                "reaction_secs": None,
            })
            movement_rows.append({"event_idx": idx + 1, "status": "no_timestamp"})
            continue

        # Determine who benefits
        is_wicket = "W" in str(event_type) or "WICKET" in summary.upper()
        is_boundary = event_type in ("4", "6", "FOUR", "SIX")

        # Map batting team if needed
        if batting_team not in (team_a, team_b):
            if team_a.lower() in batting_team.lower():
                batting_team = team_a
            elif team_b.lower() in batting_team.lower():
                batting_team = team_b

        if is_wicket or "RUN OUT" in summary.upper():
            beneficiary = team_b if batting_team == team_a else team_a
        elif is_boundary:
            beneficiary = team_a if batting_team == team_a else team_b
        else:
            beneficiary = team_a if batting_team == team_a else team_b

        expected_dir = "up" if beneficiary == team_a else "down"

        # Find reaction
        move = find_event_reaction(
            timeline, event_time, expected_dir, is_wicket,
            prev_price=last_known_price)

        ev_row = {c: row[c] for c in events_df.columns}
        dir_label = f"{team_a}_UP" if expected_dir == "up" else f"{team_a}_DOWN"
        ev_row["expected_dir"] = dir_label

        if move:
            last_known_price = move["price_after"]
            ev_row["market_reaction_ist"] = move["reaction_time"].strftime("%H:%M:%S")
            ev_row["market_lead_sec"] = move["latency"]
            ev_row["reaction_ticks"] = move["ticks"]
            ev_row["reaction_secs"] = move["reaction_secs"]

            b_before = round(1.0 - move["price_before"], 4)
            b_after = round(1.0 - move["price_after"], 4)

            movement_rows.append({
                "event_idx": idx + 1,
                "inn": inn,
                "over": over,
                "event": event_type,
                "summary": summary,
                "score": score,
                "event_time_ist": event_time.strftime("%H:%M:%S.%f")[:12],
                "expected_dir": dir_label,
                "market_reaction_ist": move["reaction_time"].strftime("%H:%M:%S"),
                f"{team_a}_before": round(move["price_before"], 4),
                f"{team_a}_after": round(move["price_after"], 4),
                f"{team_b}_before": b_before,
                f"{team_b}_after": b_after,
                "reaction_ticks": move["ticks"],
                "reaction_secs": move["reaction_secs"],
                "market_lead_sec": move["latency"],
                "status": "matched",
            })
        else:
            ev_row["market_reaction_ist"] = ""
            ev_row["market_lead_sec"] = None
            ev_row["reaction_ticks"] = None
            ev_row["reaction_secs"] = None

            movement_rows.append({
                "event_idx": idx + 1,
                "inn": inn,
                "over": over,
                "event": event_type,
                "summary": summary,
                "score": score,
                "event_time_ist": event_time.strftime("%H:%M:%S.%f")[:12],
                "expected_dir": dir_label,
                "market_reaction_ist": "",
                f"{team_a}_before": None,
                f"{team_a}_after": None,
                f"{team_b}_before": None,
                f"{team_b}_after": None,
                "reaction_ticks": None,
                "reaction_secs": None,
                "market_lead_sec": None,
                "status": "no_reaction",
            })

        event_rows.append(ev_row)

    # Build output DataFrames
    events_out = pd.DataFrame(event_rows)
    movements_out = pd.DataFrame(movement_rows)

    # Output path
    if args.output:
        out_path = Path(args.output)
    else:
        out_path = Path(args.scores_xlsx).parent.parent / "data" / \
            f"event_market_lead_{match_date or 'unknown'}.xlsx"
        if not out_path.parent.exists():
            out_path = Path(args.scores_xlsx).with_name(
                f"event_market_lead_{match_date or 'unknown'}.xlsx")

    # Write Excel
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        events_out.to_excel(writer, sheet_name="Events", index=False)
        movements_out.to_excel(writer, sheet_name="Market Movements", index=False)

        for sheet_name in ["Events", "Market Movements"]:
            ws = writer.sheets[sheet_name]
            for col in ws.columns:
                max_len = max(len(str(cell.value or "")) for cell in col)
                header_len = len(str(col[0].value or ""))
                ws.column_dimensions[col[0].column_letter].width = \
                    min(max(max_len, header_len) + 2, 40)

    # Console report
    matched = sum(1 for m in movement_rows if m.get("status") == "matched")
    missed = len(movement_rows) - matched
    print(f"\n{'='*100}")
    print(f"  {len(events_out)} events | {matched} matched | {missed} missed")
    print(f"{'='*100}")
    print(f"{'#':>3} {'REACTION':>9} {'EVENT_T':>12} {'LEAD':>5} {'OV':>5} "
          f"{'EVT':>6} {'DIR':>8} {'TICKS':>5} {'~SEC':>4} {'SCORE':>7}")
    print("-" * 100)

    for i, m in enumerate(movement_rows):
        evt_t = m.get("event_time_ist", "—")
        mkt_t = m.get("market_reaction_ist", "")
        lead = m.get("market_lead_sec")
        lead_s = f"{lead:>4}s" if lead is not None else "   —"
        ticks = m.get("reaction_ticks")
        ticks_s = f"{ticks:>5}" if ticks is not None else "    —"
        rsec = m.get("reaction_secs")
        rsec_s = f"{rsec:>4}" if rsec is not None else "   —"
        edir = m.get("expected_dir", "")
        print(f"{i+1:>3} {mkt_t or '—':>9} {evt_t:>12} {lead_s} "
              f"{m.get('over', ''):>5} {m.get('event', ''):>6} "
              f"{edir:>8} {ticks_s} {rsec_s} {m.get('score', ''):>7}")

    if matched > 0:
        leads = [m["market_lead_sec"] for m in movement_rows
                 if m.get("status") == "matched" and m.get("market_lead_sec")]
        if leads:
            print(f"\n  Avg market lead: {sum(leads)/len(leads):.0f}s | "
                  f"Median: {sorted(leads)[len(leads)//2]}s | "
                  f"Min: {min(leads)}s | Max: {max(leads)}s")

    print(f"\n  Output → {out_path}")


if __name__ == "__main__":
    main()
