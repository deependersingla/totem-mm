import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException

import settings
from connectors.betfair.client import BetfairClient
from connectors.betfair.prices import AsyncBetfairPriceFeed
from connectors.polymarket.client import PolymarketClient
from connectors.polymarket.rfq_listener import AsyncRFQListener
from pricing.quote_engine import QuoteEngine
from utils.logging_config import setup_logging

setup_logging(log_level=logging.INFO)

logger = logging.getLogger(__name__)

# Betfair session keep-alive interval (default: 20 minutes)
KEEP_ALIVE_INTERVAL = float(os.environ.get("BETFAIR_KEEP_ALIVE_INTERVAL", "1200"))

# Betfair market IDs (comma-separated)
BETFAIR_MARKET_IDS = [
    mid.strip()
    for mid in os.environ.get("BETFAIR_MARKET_IDS", "").split(",")
    if mid.strip()
]


# =============================================================================
# Global State
# =============================================================================

class AppState:
    def __init__(self):
        self.betfair_client: Optional[BetfairClient] = None
        self.price_feed: Optional[AsyncBetfairPriceFeed] = None
        self.rfq_listener: Optional[AsyncRFQListener] = None
        self.keep_alive_task: Optional[asyncio.Task] = None
        self.stop_event: Optional[asyncio.Event] = None
        self.started_at: Optional[datetime] = None


state = AppState()


# =============================================================================
# Background Tasks
# =============================================================================

async def betfair_keep_alive_loop(client: BetfairClient, stop_event: asyncio.Event) -> None:
    """Periodically call Betfair keep-alive to prevent session expiry."""
    logger.info(
        "Starting Betfair session keep-alive (interval=%.0fs)",
        KEEP_ALIVE_INTERVAL,
    )

    while not stop_event.is_set():
        try:
            await asyncio.to_thread(client.keep_alive)
        except Exception:
            logger.exception("Error in Betfair keep-alive")

        # Wait for interval or until stop is signaled
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=KEEP_ALIVE_INTERVAL)
            break  # Stop event was set
        except asyncio.TimeoutError:
            pass  # Timeout reached, continue loop

    logger.info("Betfair keep-alive loop stopped")


# =============================================================================
# Lifecycle
# =============================================================================

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup and shutdown lifecycle."""
    logger.info("Starting totem-mm")

    # ── Betfair client and price feed ──────────────────────────────
    if not BETFAIR_MARKET_IDS:
        logger.error("BETFAIR_MARKET_IDS not configured. Set it in .env (comma-separated market IDs)")
    else:
        state.betfair_client = BetfairClient()
        logger.info("Configured Betfair market IDs: %s", BETFAIR_MARKET_IDS)

        state.price_feed = AsyncBetfairPriceFeed(
            client=state.betfair_client,
            market_ids=BETFAIR_MARKET_IDS,
            poll_interval=10,
        )
        await state.price_feed.start()

        # ── graceful shutdown setup ────────────────────────────────────
        state.stop_event = asyncio.Event()

        # ── Betfair session keep-alive ─────────────────────────────────
        state.keep_alive_task = asyncio.create_task(
            betfair_keep_alive_loop(state.betfair_client, state.stop_event)
        )

        # ── Polymarket RFQ system ─────────────────────────────────────
        if settings.TOKEN_MAP:
            polymarket_client = PolymarketClient()
            quote_engine = QuoteEngine(
                token_map=settings.TOKEN_MAP,
                spread=settings.RFQ_CONFIG["SPREAD"],
                max_quote_size_usdc=settings.RFQ_CONFIG["MAX_QUOTE_SIZE_USDC"],
                max_exposure_usdc=settings.RFQ_CONFIG["MAX_EXPOSURE_USDC"],
            )
            state.rfq_listener = AsyncRFQListener(
                client=polymarket_client,
                quote_engine=quote_engine,
                snapshot_fn=state.price_feed.get_snapshot,
                poll_interval=settings.RFQ_CONFIG["POLL_INTERVAL"],
                quote_ttl=settings.RFQ_CONFIG["QUOTE_TTL"],
            )
            await state.rfq_listener.start()
        else:
            logger.warning(
                "TOKEN_MAP not configured — RFQ system disabled. Set TOKEN_MAP env var to enable."
            )

        state.started_at = datetime.now()

    yield

    # ── cleanup ────────────────────────────────────────────────────
    logger.info("Shutting down totem-mm")

    if state.stop_event:
        state.stop_event.set()

    if state.rfq_listener:
        await state.rfq_listener.stop()

    if state.price_feed:
        await state.price_feed.stop()

    # Wait for keep-alive task to finish
    if state.keep_alive_task:
        try:
            await asyncio.wait_for(state.keep_alive_task, timeout=5)
        except asyncio.TimeoutError:
            state.keep_alive_task.cancel()

    logger.info("totem-mm stopped cleanly")


# =============================================================================
# FastAPI App
# =============================================================================

app = FastAPI(
    title="totem-mm",
    description="Market maker for Betfair/Polymarket",
    version="1.0.0",
    lifespan=lifespan,
)


# =============================================================================
# Endpoints
# =============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy" if state.price_feed else "degraded",
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/status")
async def get_status():
    """Get current system status."""
    uptime = None
    if state.started_at:
        uptime = (datetime.now() - state.started_at).total_seconds()

    return {
        "status": "running" if state.price_feed else "stopped",
        "started_at": state.started_at.isoformat() if state.started_at else None,
        "uptime_seconds": uptime,
        "betfair_market_ids": BETFAIR_MARKET_IDS,
        "rfq_enabled": state.rfq_listener is not None,
        "token_map_configured": bool(settings.TOKEN_MAP),
    }


@app.get("/prices")
async def get_prices(market_id: Optional[str] = None):
    """Get current Betfair prices."""
    if not state.price_feed:
        raise HTTPException(status_code=503, detail="Price feed not running")

    if market_id:
        prices = state.price_feed.get_snapshot(market_id)
    else:
        prices = state.price_feed.get_all_snapshots()

    return {
        "market_ids": BETFAIR_MARKET_IDS,
        "prices": prices,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/quotes")
async def get_active_quotes():
    """Get active RFQ quotes."""
    if not state.rfq_listener:
        raise HTTPException(status_code=404, detail="RFQ system not enabled")

    quotes = state.rfq_listener.active_quotes
    return {
        "active_quotes": len(quotes),
        "quotes": {
            qid: {
                "request_id": q.request_id,
                "token_id": q.token_id,
                "price": q.price,
                "side": q.side,
                "size": q.size,
                "status": q.status,
                "created_at": q.created_at.isoformat() if q.created_at else None,
            }
            for qid, q in quotes.items()
        },
    }


@app.get("/config")
async def get_config():
    """Get current configuration."""
    return {
        "betfair_market_ids": BETFAIR_MARKET_IDS,
        "keep_alive_interval": KEEP_ALIVE_INTERVAL,
        "rfq_config": {
            "poll_interval": settings.RFQ_CONFIG["POLL_INTERVAL"],
            "quote_ttl": settings.RFQ_CONFIG["QUOTE_TTL"],
            "spread": settings.RFQ_CONFIG["SPREAD"],
            "max_quote_size_usdc": settings.RFQ_CONFIG["MAX_QUOTE_SIZE_USDC"],
            "max_exposure_usdc": settings.RFQ_CONFIG["MAX_EXPOSURE_USDC"],
        },
        "token_map_entries": len(settings.TOKEN_MAP),
    }


# =============================================================================
# Main
# =============================================================================
# Run with the venv's Python so dependencies (e.g. py_clob_client) are found:
#   python main.py
# Or: python -m uvicorn main:app --host 0.0.0.0 --port 8000
# Avoid using the bare "uvicorn" command if your PATH points at system Python.

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))

    uvicorn.run(app, host=host, port=port)
