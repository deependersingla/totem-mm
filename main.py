import asyncio
import logging
import os
import signal

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


async def main() -> None:
    logger.info("Starting totem-mm")

    # ── Betfair client and price feed ──────────────────────────────
    if not BETFAIR_MARKET_IDS:
        logger.error("BETFAIR_MARKET_IDS not configured. Set it in .env (comma-separated market IDs)")
        return

    betfair_client = BetfairClient()

    logger.info("Configured Betfair market IDs: %s", BETFAIR_MARKET_IDS)

    price_feed = AsyncBetfairPriceFeed(
        client=betfair_client,
        market_ids=BETFAIR_MARKET_IDS,
        poll_interval=10,
    )
    await price_feed.start()

    # ── graceful shutdown setup ────────────────────────────────────
    stop_event = asyncio.Event()

    def _shutdown():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    # ── Betfair session keep-alive ─────────────────────────────────
    keep_alive_task = asyncio.create_task(
        betfair_keep_alive_loop(betfair_client, stop_event)
    )

    # ── Polymarket RFQ system ─────────────────────────────────────
    rfq_listener = None
    if settings.TOKEN_MAP:
        polymarket_client = PolymarketClient()
        quote_engine = QuoteEngine(
            token_map=settings.TOKEN_MAP,
            spread=settings.RFQ_CONFIG["SPREAD"],
            max_quote_size_usdc=settings.RFQ_CONFIG["MAX_QUOTE_SIZE_USDC"],
            max_exposure_usdc=settings.RFQ_CONFIG["MAX_EXPOSURE_USDC"],
        )
        rfq_listener = AsyncRFQListener(
            client=polymarket_client,
            quote_engine=quote_engine,
            snapshot_fn=price_feed.get_snapshot,
            poll_interval=settings.RFQ_CONFIG["POLL_INTERVAL"],
            quote_ttl=settings.RFQ_CONFIG["QUOTE_TTL"],
        )
        await rfq_listener.start()
    else:
        logger.warning(
            "TOKEN_MAP not configured — RFQ system disabled. Set TOKEN_MAP env var to enable."
        )

    # ── wait for shutdown ──────────────────────────────────────────
    await stop_event.wait()

    # ── cleanup ────────────────────────────────────────────────────
    if rfq_listener:
        await rfq_listener.stop()
    await price_feed.stop()

    # Wait for keep-alive task to finish
    try:
        await asyncio.wait_for(keep_alive_task, timeout=5)
    except asyncio.TimeoutError:
        keep_alive_task.cancel()

    logger.info("totem-mm stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
