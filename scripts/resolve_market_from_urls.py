#!/usr/bin/env python3
"""
Resolve Betfair market ID and Polymarket token map from the two URLs.
Set the URLs and UPD_ENV flag at the top, then run the script.

  python scripts/resolve_market_from_urls.py

If UPD_ENV is True, BETFAIR_MARKET_IDS, TOKEN_MAP, TEAM_A, and TEAM_B in .env are updated.

Team A/B mapping:
  - Order is by Betfair selection_id (ascending): lower selection_id = team A, higher = team B.
  - TOKEN_MAP links Polymarket token_id -> Betfair selection_id.
  - So: the Polymarket token that maps to the lower selection_id is team A; the other is team B.
  - TEAM_A and TEAM_B are display labels (from Betfair runner names); they do not change the mapping.
"""

import json
import os
import re
import sys

# =============================================================================
# CONFIG — set these, then run the script
# =============================================================================

BETFAIR_URL = "https://www.betfair.com/exchange/plus/en/cricket/icc-men-s-t20-world-cup/zimbabwe-v-west-indies-betting-35284652"
POLYMARKET_URL = "https://polymarket.com/sports/crint/crint-zwe-wst-2026-02-23"

# If True, update .env with BETFAIR_MARKET_IDS, TOKEN_MAP, TEAM_A, TEAM_B
UPD_ENV = True

# Optional: override auto-detected team names (e.g. TEAM_A_OVERRIDE="ZIM", TEAM_B_OVERRIDE="WI")
TEAM_A_OVERRIDE = "WI"
TEAM_B_OVERRIDE = "ZIM"

# =============================================================================
# Script (no need to edit below)
# =============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")


def extract_betfair_event_id(url: str) -> str | None:
    """Get event ID from Betfair URL (e.g. ...-betting-35284617 -> 35284617)."""
    if not url or not url.strip():
        return None
    # Last path segment often ends with event id
    path = url.split("?")[0].rstrip("/")
    segment = path.split("/")[-1] if "/" in path else path
    match = re.search(r"(\d{6,})$", segment)
    return match.group(1) if match else None


def extract_polymarket_slug(url: str) -> str | None:
    """Get event slug from Polymarket URL (e.g. .../crint-nzl-pak-2026-02-21)."""
    if not url or not url.strip():
        return None
    path = url.split("?")[0].rstrip("/")
    return path.split("/")[-1] if "/" in path else None


def fetch_polymarket_event(slug: str) -> dict | None:
    """Fetch event + markets from Gamma API. Returns first event or None."""
    import urllib.request

    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print("Polymarket fetch failed:", e, file=sys.stderr)
        return None
    events = data if isinstance(data, list) else (data.get("data") or [data])
    return events[0] if events else None


def fetch_betfair_market(event_id: str, market_type: str = "MATCH_ODDS"):
    """Get market ID and runners (selection_id -> name) from Betfair API."""
    sys.path.insert(0, PROJECT_ROOT)
    import dotenv
    dotenv.load_dotenv(ENV_PATH)
    from connectors.betfair.client import BetfairClient

    client = BetfairClient()
    params = {
        "filter": {
            "eventIds": [event_id],
            "marketTypeCodes": [market_type],
        },
        "marketProjection": ["RUNNER_METADATA", "MARKET_DESCRIPTION"],
        "maxResults": 20,
    }
    response = client.call("SportsAPING/v1.0/listMarketCatalogue", params)
    result = response.get("result") or []
    if not result:
        return None, []
    cat = result[0]
    market_id = cat.get("marketId")
    runners = cat.get("runners") or []
    return market_id, runners


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")


def _runner_norm_keys(name: str) -> list[str]:
    """Normalized keys for matching: full norm and with 'women' / ' w' stripped."""
    n = _norm(name)
    if not n:
        return []
    keys = [n]
    stripped = re.sub(r"women\'?s?|\bw\b", "", name, flags=re.I).strip()
    if stripped:
        keys.append(_norm(stripped))
    return keys


def _moneyline_market(markets: list, event_slug: str) -> dict | None:
    """Pick the Moneyline (match winner) market, not Completed match or Toss."""
    for m in markets:
        slug = (m.get("slug") or "").strip()
        mtype = (m.get("sportsMarketType") or "").strip().lower()
        if mtype == "moneyline":
            return m
        if slug == event_slug and mtype != "cricket_completed_match" and "toss" not in slug:
            return m
    return markets[0] if markets else None


def build_token_map(polymarket_event: dict, betfair_runners: list) -> dict[str, int]:
    """
    Build TOKEN_MAP: Polymarket token_id (str) -> Betfair selectionId (int).
    Uses the Moneyline (match winner) market; matches outcomes to runner names.
    """
    markets = polymarket_event.get("markets") or []
    if not markets:
        return {}
    event_slug = (polymarket_event.get("slug") or "").strip()
    market = _moneyline_market(markets, event_slug) or markets[0]
    outcomes_raw = market.get("outcomes") or "[]"
    clob_raw = market.get("clobTokenIds") or "[]"
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        token_ids = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
    except json.JSONDecodeError:
        return {}
    if len(outcomes) != len(token_ids):
        return {}
    outcome_to_token = {_norm(out): str(tid) for out, tid in zip(outcomes, token_ids)}
    runner_norm_to_sel = {}
    for r in betfair_runners:
        name = ((r.get("metadata") or {}).get("runnerName") or r.get("runnerName") or "").strip()
        if name:
            for k in _runner_norm_keys(name):
                runner_norm_to_sel[k] = r.get("selectionId")
    token_map = {}
    for out_norm, token_id in outcome_to_token.items():
        if out_norm in runner_norm_to_sel:
            token_map[token_id] = runner_norm_to_sel[out_norm]
        else:
            for run_norm, sel_id in runner_norm_to_sel.items():
                if out_norm in run_norm or run_norm in out_norm:
                    token_map[token_id] = sel_id
                    break
    return token_map


# Short codes for common cricket teams (Betfair runner name -> display label)
TEAM_SHORT = {
    "south africa": "SA", "sri lanka": "SL", "england": "ENG", "india": "IND",
    "australia": "AUS", "pakistan": "PAK", "new zealand": "NZ", "west indies": "WI",
    "zimbabwe": "ZIM", "bangladesh": "BAN", "afghanistan": "AFG", "ireland": "IRE",
}


def _shorten_team(name: str) -> str:
    """Use short code if known, else first 3 chars of last word or full name."""
    n = (name or "").strip().lower()
    if n in TEAM_SHORT:
        return TEAM_SHORT[n]
    words = n.split()
    if words:
        return words[-1][:3].upper() if len(words[-1]) >= 3 else n[:4].upper()
    return name[:4].upper() if name else "A"


def get_team_names_from_map(token_map: dict[str, int], betfair_runners: list) -> tuple[str, str]:
    """
    Derive TEAM_A and TEAM_B from token_map and Betfair runners.
    Order: by selection_id (asc). Lower selection_id = team A.
    """
    sel_to_name = {}
    for r in betfair_runners:
        name = ((r.get("metadata") or {}).get("runnerName") or r.get("runnerName") or "").strip()
        sid = r.get("selectionId")
        if name and sid is not None:
            sel_to_name[sid] = name
    sel_ids_ordered = sorted(set(token_map.values()))
    if len(sel_ids_ordered) < 2:
        return "team_a", "team_b"
    a_name = sel_to_name.get(sel_ids_ordered[0], "team_a")
    b_name = sel_to_name.get(sel_ids_ordered[1], "team_b")
    return _shorten_team(a_name), _shorten_team(b_name)


def update_env_only(betfair_market_ids: str, token_map_json: str, team_a: str = "", team_b: str = "") -> None:
    """Update BETFAIR_MARKET_IDS, TOKEN_MAP, TEAM_A, TEAM_B in .env; leave rest unchanged."""
    env_vars = [
        ("BETFAIR_MARKET_IDS", betfair_market_ids),
        ("TOKEN_MAP", token_map_json),
        ("TEAM_A", team_a or "team_a"),
        ("TEAM_B", team_b or "team_b"),
    ]
    if not os.path.exists(ENV_PATH):
        with open(ENV_PATH, "w") as f:
            for k, v in env_vars:
                f.write(f"{k}={v}\n")
        print("Created .env with BETFAIR_MARKET_IDS, TOKEN_MAP, TEAM_A, TEAM_B.")
        return

    with open(ENV_PATH, "r") as f:
        lines = f.readlines()

    seen = {k: False for k, _ in env_vars}
    new_lines = []
    for line in lines:
        matched = False
        for k, v in env_vars:
            if line.strip().startswith(f"{k}="):
                new_lines.append(f"{k}={v}\n")
                seen[k] = True
                matched = True
                break
        if not matched:
            new_lines.append(line)

    for k, v in env_vars:
        if not seen[k]:
            new_lines.append(f"{k}={v}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(new_lines)
    print("Updated .env (BETFAIR_MARKET_IDS, TOKEN_MAP, TEAM_A, TEAM_B).")


def main():
    betfair_url = BETFAIR_URL.strip() or None
    polymarket_url = POLYMARKET_URL.strip() or None

    if not betfair_url:
        print("Set BETFAIR_URL at the top of the script.", file=sys.stderr)
        sys.exit(1)
    if not polymarket_url:
        print("Set POLYMARKET_URL at the top of the script.", file=sys.stderr)
        sys.exit(1)

    event_id = extract_betfair_event_id(betfair_url)
    slug = extract_polymarket_slug(polymarket_url)

    if not event_id:
        print("Could not extract Betfair event ID from URL.", file=sys.stderr)
        sys.exit(1)
    if not slug:
        print("Could not extract Polymarket slug from URL.", file=sys.stderr)
        sys.exit(1)

    print("Betfair event ID (from URL, used to query API):", event_id)
    print("Polymarket slug:", slug)
    print()

    # Polymarket
    event = fetch_polymarket_event(slug)
    if not event:
        print("Could not fetch Polymarket event.", file=sys.stderr)
        sys.exit(1)
    markets = event.get("markets") or []
    if not markets:
        print("No markets in Polymarket event.", file=sys.stderr)
        sys.exit(1)

    # Betfair (needs .env with BETFAIR_APP_KEY and BETFAIR_SESSION_TOKEN)
    try:
        market_id, runners = fetch_betfair_market(event_id)
    except Exception as e:
        print("Betfair API failed. Ensure .env has BETFAIR_APP_KEY and BETFAIR_SESSION_TOKEN.", file=sys.stderr)
        print(e, file=sys.stderr)
        sys.exit(1)

    if not market_id or not runners:
        print("No Betfair market or runners for this event.", file=sys.stderr)
        sys.exit(1)

    token_map = build_token_map(event, runners)
    if not token_map:
        print("Could not build token map (outcome names may not match).", file=sys.stderr)
        sys.exit(1)

    team_a, team_b = get_team_names_from_map(token_map, runners)
    if (ovr_a := (TEAM_A_OVERRIDE or "").strip()):
        team_a = ovr_a
    if (ovr_b := (TEAM_B_OVERRIDE or "").strip()):
        team_b = ovr_b

    # Output (market_id is from Betfair API listMarketCatalogue — format 1.xxxxx, not the event ID)
    token_map_json = json.dumps(token_map)
    print("--- Market & token map ---")
    print("Betfair market ID (from API, use this in .env):", market_id)
    print("BETFAIR_MARKET_IDS=" + str(market_id))
    print("TOKEN_MAP=" + token_map_json)
    print("TEAM_A (lower selection_id) =", team_a)
    print("TEAM_B (higher selection_id) =", team_b)
    print()

    if UPD_ENV:
        update_env_only(str(market_id), token_map_json, team_a, team_b)
    else:
        print("(Set UPD_ENV = True at the top to update .env)")


if __name__ == "__main__":
    main()
