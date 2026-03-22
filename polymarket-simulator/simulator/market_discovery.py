"""Market search and discovery via Gamma API."""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from .config import GAMMA_API
from .models import MarketInfo, MarketSearchResult

log = logging.getLogger(__name__)


async def search_markets(query: str, limit: int = 20) -> list[MarketSearchResult]:
    """Search for markets by text query."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{GAMMA_API}/markets", params={
            "_q": query, "closed": "false", "limit": limit,
        })
        resp.raise_for_status()
        markets = resp.json()

    results = []
    for m in markets:
        outcomes = m.get("outcomes", "[]")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                outcomes = []
        results.append(MarketSearchResult(
            slug=m.get("slug", ""),
            question=m.get("question", ""),
            active=not m.get("closed", False),
            volume=float(m.get("volume", 0) or 0),
            liquidity=float(m.get("liquidity", 0) or 0),
            outcomes=outcomes,
        ))
    return results


async def fetch_market_by_slug(slug: str) -> Optional[MarketInfo]:
    """Fetch full market info by slug."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{GAMMA_API}/markets", params={"slug": slug})
        resp.raise_for_status()
        markets = resp.json()

    if not markets:
        return None

    m = markets[0]
    tokens = m.get("clobTokenIds", "")
    outcomes = m.get("outcomes", "[]")

    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except json.JSONDecodeError:
            tokens = tokens.split(",") if tokens else []
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = []

    return MarketInfo(
        condition_id=m.get("conditionId", ""),
        question=m.get("question", ""),
        slug=slug,
        token_ids=tokens,
        outcome_names=outcomes,
        active=not m.get("closed", False),
        image=m.get("image", ""),
    )
