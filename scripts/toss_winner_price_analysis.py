#!/usr/bin/env python3
"""Same pre/post toss-price analysis but on the TOSS-WINNER market.

For each match we pull CLOB prices-history at fidelity=1 (minute-bars)
around gameStart - 30 min, plus a finer look at the react shape.
"""
import json, subprocess, csv, statistics, sys
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

TEAM_NAME = {
    'che':'Chennai','kol':'Kolkata','del':'Delhi','mum':'Mumbai',
    'pun':'Punjab','raj':'Rajasthan','roy':'Royal','sun':'Sunrisers',
    'guj':'Gujarat','luc':'Lucknow',
}

def curl_json(url):
    out = subprocess.run(['curl','-s','-A','Mozilla/5.0',url], capture_output=True, text=True, timeout=30)
    try: return json.loads(out.stdout) if out.stdout else None
    except: return None

def parse_gamestart(s):
    if not s: return None
    s = s.strip().replace(' ','T')
    if s.endswith('+00'): s = s[:-3] + '+00:00'
    if s.endswith('Z'):   s = s[:-1] + '+00:00'
    try: return datetime.fromisoformat(s)
    except: return None

def outcome_index(outcomes, team_code):
    needle = TEAM_NAME.get(team_code, team_code).lower()
    for i, o in enumerate(outcomes):
        if needle in o.lower(): return i
    return None

def fetch_prices(token, t_start, t_end):
    url = f"https://clob.polymarket.com/prices-history?market={token}&startTs={t_start}&endTs={t_end}&fidelity=1"
    d = curl_json(url)
    return d.get('history', []) if isinstance(d, dict) else (d or [])

def median_in(hist, t_lo, t_hi):
    vals = [h['p'] for h in hist if t_lo <= h['t'] <= t_hi]
    return statistics.median(vals) if vals else None

def find_first_cross(hist, t_start, threshold, stay_for=60):
    """First t where price ≥ threshold and stays for stay_for seconds."""
    above_from = None
    for h in hist:
        if h['t'] < t_start: continue
        if h['p'] >= threshold:
            if above_from is None:
                above_from = h['t']
            elif h['t'] - above_from >= stay_for:
                return above_from
        else:
            above_from = None
    return None

def main():
    # Join moneyline toss metadata with toss-winner tokens
    tw = {x['slug']:x for x in json.load(open('/tmp/ipl_tw_meta.json'))}
    rows_in = list(csv.DictReader(open('/tmp/ipl2026_toss.csv')))
    enriched_main = {x['slug']:x for x in json.load(open('/tmp/ipl_enriched.json'))}

    out = []
    for r in rows_in:
        slug = r['slug']
        tw_meta = tw.get(slug)
        main_meta = enriched_main.get(slug)
        if not tw_meta or not main_meta:
            continue
        gs = parse_gamestart(main_meta.get('gameStart') or '')
        if gs is None:
            continue
        toss_unix = int(gs.timestamp()) - 30*60

        winner_code = r['toss_winner'].lower() if r.get('toss_winner') else None
        w_idx = outcome_index(tw_meta['tw_outcomes'], winner_code)
        if w_idx is None: continue

        tokens = tw_meta['tw_tokens']
        t0, t1 = toss_unix - 15*60, toss_unix + 25*60
        h_w = fetch_prices(tokens[w_idx], t0, t1)
        h_l = fetch_prices(tokens[1-w_idx], t0, t1)

        pre_lo, pre_hi   = toss_unix - 8*60, toss_unix - 2*60
        post_lo, post_hi = toss_unix + 2*60, toss_unix + 10*60

        pre_w  = median_in(h_w, pre_lo, pre_hi)
        post_w = median_in(h_w, post_lo, post_hi)

        # Finer metrics: latest pre-toss (T-1min), peak post, first-cross time
        t_latest_pre = toss_unix - 30  # 30s before scheduled toss
        latest_pre = next((h['p'] for h in reversed(h_w) if h['t'] <= t_latest_pre), None)

        cross_70 = find_first_cross(h_w, toss_unix - 60, 0.70, stay_for=30)
        cross_80 = find_first_cross(h_w, toss_unix - 60, 0.80, stay_for=30)
        cross_90 = find_first_cross(h_w, toss_unix - 60, 0.90, stay_for=30)

        # Seconds from scheduled toss to first stable cross
        def secs(t): return (t - toss_unix) if t else None

        # Peak and timing
        peak_p, peak_t = (None, None)
        for h in h_w:
            if h['t'] < toss_unix - 60: continue
            if peak_p is None or h['p'] > peak_p:
                peak_p, peak_t = h['p'], h['t']

        row = {
            'slug': slug,
            'date': r['date'],
            'start_ist': r['start_ist'],
            'toss_winner': winner_code,
            'toss_decision': r['toss_decision'],
            'pre_px': round(pre_w, 4) if pre_w is not None else None,
            'latest_pre_px': round(latest_pre, 4) if latest_pre is not None else None,
            'post_px': round(post_w, 4) if post_w is not None else None,
            'delta': round(post_w - pre_w, 4) if (pre_w is not None and post_w is not None) else None,
            'peak_px': round(peak_p, 4) if peak_p is not None else None,
            'secs_to_0.70': secs(cross_70),
            'secs_to_0.80': secs(cross_80),
            'secs_to_0.90': secs(cross_90),
            'n_minute_bars': len(h_w),
        }
        out.append(row)
        sys.stderr.write(f"{slug}: pre={row['pre_px']} →post={row['post_px']} peak={row['peak_px']} "
                         f"t70={row['secs_to_0.70']}s t80={row['secs_to_0.80']}s t90={row['secs_to_0.90']}s\n")

    # Write CSV
    keys = ['slug','date','start_ist','toss_winner','toss_decision',
            'pre_px','latest_pre_px','post_px','delta','peak_px',
            'secs_to_0.70','secs_to_0.80','secs_to_0.90','n_minute_bars']
    with open('/tmp/ipl_toss_winner_prices.csv','w',newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
        w.writeheader()
        for r in out: w.writerow(r)
    json.dump(out, open('/tmp/ipl_toss_winner_prices.json','w'), indent=2, default=str)

    # Summary
    deltas = [r['delta'] for r in out if r['delta'] is not None]
    peaks = [r['peak_px'] for r in out if r['peak_px'] is not None]
    pres  = [r['pre_px'] for r in out if r['pre_px'] is not None]
    t70s = [r['secs_to_0.70'] for r in out if r['secs_to_0.70'] is not None]
    t80s = [r['secs_to_0.80'] for r in out if r['secs_to_0.80'] is not None]
    t90s = [r['secs_to_0.90'] for r in out if r['secs_to_0.90'] is not None]

    print(f"\nToss-winner market: {len(out)} matches analyzed")
    print(f"  pre-toss median     : {statistics.median(pres):.3f}")
    print(f"  post-toss (5-min) median Δ : +{statistics.median(deltas):.3f}")
    print(f"  post-toss mean Δ    : +{statistics.mean(deltas):.3f}")
    print(f"  peak-px median      : {statistics.median(peaks):.3f}")
    print(f"\nTime for toss winner to reach & hold (sec from scheduled toss):")
    if t70s: print(f"  0.70: n={len(t70s):2d}  median={statistics.median(t70s):+.0f}s  mean={statistics.mean(t70s):+.0f}s")
    if t80s: print(f"  0.80: n={len(t80s):2d}  median={statistics.median(t80s):+.0f}s  mean={statistics.mean(t80s):+.0f}s")
    if t90s: print(f"  0.90: n={len(t90s):2d}  median={statistics.median(t90s):+.0f}s  mean={statistics.mean(t90s):+.0f}s")

if __name__ == '__main__':
    main()
