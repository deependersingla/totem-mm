#!/usr/bin/env python3
"""Analyze Polymarket IPL 2026 odds shift around toss time.

For each match, we fetch CLOB prices-history in a ±15 min window around
(gameStart - 30 min). We compute:
  - pre_px  = median price in [T-8, T-2] min
  - post_px = median price in [T+2, T+10] min
Applied to both outcome tokens, then reported per-team and per-toss-winner.
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
    try:
        return json.loads(out.stdout) if out.stdout else None
    except Exception:
        return None

def outcome_index(outcomes, team_code):
    needle = TEAM_NAME.get(team_code, team_code).lower()
    for i, o in enumerate(outcomes):
        if needle in o.lower():
            return i
    return None

def fetch_prices(token, t_start, t_end):
    url = f"https://clob.polymarket.com/prices-history?market={token}&startTs={t_start}&endTs={t_end}&fidelity=1"
    d = curl_json(url)
    return d.get('history', []) if isinstance(d, dict) else (d or [])

def median_in(hist, t_lo, t_hi):
    vals = [h['p'] for h in hist if t_lo <= h['t'] <= t_hi]
    return statistics.median(vals) if vals else None

def parse_gamestart(s):
    if not s: return None
    s = s.strip().replace(' ','T')
    # Polymarket often returns "+00" instead of "+00:00"
    if s.endswith('+00'): s = s[:-3] + '+00:00'
    if s.endswith('Z'):   s = s[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

def main():
    data = json.load(open('/tmp/ipl_enriched.json'))
    results = []
    for r in data:
        slug = r['slug']
        tokens = r.get('tokens') or []
        outcomes = r.get('outcomes') or []
        gs = parse_gamestart(r.get('gameStart') or '')
        if gs is None or len(tokens) != 2 or len(outcomes) != 2:
            results.append({'slug': slug, 'error':'missing_metadata'})
            continue
        toss_unix = int(gs.timestamp()) - 30*60  # toss 30 min before start
        winner_code = r['toss_winner'].lower() if r.get('toss_winner') else None
        if not winner_code:
            results.append({'slug': slug, 'error':'no_toss_winner'})
            continue
        w_idx = outcome_index(outcomes, winner_code)
        if w_idx is None:
            results.append({'slug': slug, 'error':f'cant_map_{winner_code}'})
            continue
        l_idx = 1 - w_idx
        # fetch prices ±15 min
        t0 = toss_unix - 15*60
        t1 = toss_unix + 20*60
        h_w = fetch_prices(tokens[w_idx], t0, t1)
        h_l = fetch_prices(tokens[l_idx], t0, t1)

        pre_lo,  pre_hi  = toss_unix - 8*60,  toss_unix - 2*60
        post_lo, post_hi = toss_unix + 2*60,  toss_unix + 10*60

        pre_w  = median_in(h_w, pre_lo, pre_hi)
        post_w = median_in(h_w, post_lo, post_hi)
        pre_l  = median_in(h_l, pre_lo, pre_hi)
        post_l = median_in(h_l, post_lo, post_hi)

        # implied-midpoint sanity: pre_w + pre_l should ≈ 1
        results.append({
            'slug': slug,
            'date': r['date'],
            'start_ist': r['start_ist'],
            'toss_ist': (gs - timedelta(minutes=30)).astimezone(IST).strftime('%H:%M'),
            'team_a': r['team_a'], 'team_b': r['team_b'],
            'toss_winner': winner_code,
            'toss_decision': r.get('toss_decision',''),
            'winner_side': outcomes[w_idx],
            'pre_winner_px':  round(pre_w,  4) if pre_w  is not None else None,
            'post_winner_px': round(post_w, 4) if post_w is not None else None,
            'delta_winner':   round(post_w - pre_w, 4) if (pre_w is not None and post_w is not None) else None,
            'pre_loser_px':   round(pre_l,  4) if pre_l  is not None else None,
            'post_loser_px':  round(post_l, 4) if post_l is not None else None,
            'delta_loser':    round(post_l - pre_l, 4) if (pre_l is not None and post_l is not None) else None,
            'match_result': r.get('winner','') or '',
        })
        sys.stderr.write(f"{slug}: toss_winner={winner_code} Δ={results[-1].get('delta_winner')}\n")

    # write CSV
    keys = ['slug','date','start_ist','toss_ist','team_a','team_b','toss_winner','toss_decision',
            'winner_side','pre_winner_px','post_winner_px','delta_winner',
            'pre_loser_px','post_loser_px','delta_loser','match_result']
    with open('/tmp/ipl_toss_prices.csv','w',newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
        w.writeheader()
        for r in results:
            if 'error' not in r:
                w.writerow(r)
    json.dump(results, open('/tmp/ipl_toss_prices.json','w'), indent=2, default=str)

    # Summary
    deltas = [r['delta_winner'] for r in results if r.get('delta_winner') is not None]
    if deltas:
        print(f"\nToss-winner Δprice across {len(deltas)} matches:")
        print(f"  mean  : {statistics.mean(deltas):+.4f}")
        print(f"  median: {statistics.median(deltas):+.4f}")
        print(f"  min / max: {min(deltas):+.4f} / {max(deltas):+.4f}")
        pos = sum(1 for d in deltas if d > 0.005)
        neg = sum(1 for d in deltas if d < -0.005)
        flat = len(deltas) - pos - neg
        print(f"  moves >+0.5¢: {pos}   <-0.5¢: {neg}   flat: {flat}")

if __name__ == '__main__':
    main()
