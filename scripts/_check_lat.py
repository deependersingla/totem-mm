import json
from datetime import datetime, timedelta
from collections import defaultdict

# Load price timeline
price_by_sec = defaultdict(list)
with open('../data/crint-nzl-zaf-2026-03-22/20260322_115551_crint-nzl-zaf-2026-03-22.jsonl') as f:
    for line in f:
        d = json.loads(line)
        typ = d.get('type','')
        if typ not in ('trade','rest_trade','pure_fill','snipe_mix'): continue
        outcome = d.get('outcome','')
        price = d.get('price')
        ist = d.get('ist','')
        if price is None or not ist: continue
        clean = ist.replace(' IST','').strip()
        try: dt = datetime.strptime(clean[:19], '%Y-%m-%d %H:%M:%S')
        except: continue
        if outcome == 'New Zealand': nz_price = float(price)
        elif outcome == 'South Africa': nz_price = 1.0 - float(price)
        else: continue
        price_by_sec[dt].append(nz_price)
timeline = sorted([(dt, prices[-1]) for dt, prices in price_by_sec.items()])

with open('../polymarket-simulator/captures/20260322_115713_scores_1491738_events.json') as f:
    events = json.load(f)

# Check events with latency < 20s or negative
print(f"{'#':>3} {'SCOREFEED':>10} {'MKT_START':>10} {'MKT_END':>10} {'LAT':>5} {'EVT':>6} {'TICKS':>5} {'SCORE':>12}")
print("=" * 80)

suspect = []
for i, e in enumerate(events):
    mm = e['market_movement']
    if not isinstance(mm, dict): continue
    lat = mm['latency_sec']
    if lat < 20:
        suspect.append((i, e))
        marker = " <<<" if lat < 0 else " <"
        score_str = f"{e['score_before']}→{e['score_after']}"
        print(f"{i+1:>3} {e['ist'][11:19]:>10} {mm['market_move_start']:>10} {mm['market_move_end']:>10} "
              f"{lat:>5} {e['event']:>6} {mm['ticks']:>5} {score_str:>12}{marker}")

print(f"\n{'='*80}")
print(f"Found {len(suspect)} events with latency < 20s\n")

# Deep dive each
for i, e in suspect:
    mm = e['market_movement']
    clean = e['ist'].replace(' IST','').strip()
    evt_time = datetime.strptime(clean[:19], '%Y-%m-%d %H:%M:%S')
    expected_dir = "down" if e['side'] != "NZ" else "up"
    
    print(f"\n--- Event #{i+1}: {e['event']} at {e['ist'][:19]} | {e['over']} ov | side={e['side']} ---")
    print(f"  Current match: anchor={mm['market_move_start']} peak={mm['market_move_end']} "
          f"{mm['price_before']}→{mm['price_after']} {mm['ticks']}t latency={mm['latency_sec']}s")
    
    # Show price second by second in +-60s window
    window = [(dt, p) for dt, p in timeline if abs((dt - evt_time).total_seconds()) <= 60]
    print(f"\n  Price around event (+-60s):")
    for dt, p in window:
        gap = int((dt - evt_time).total_seconds())
        marker = " <-- ESPN" if gap == 0 else ""
        if dt.strftime('%H:%M:%S') == mm['market_move_start']:
            marker = " <-- MKT_START (anchor)"
        if dt.strftime('%H:%M:%S') == mm['market_move_end']:
            marker += " <-- MKT_END (peak)"
        print(f"    {dt.strftime('%H:%M:%S')} NZ={p:.4f} ({gap:+4d}s){marker}")
