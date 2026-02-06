import asyncio
import logging
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


async def main() -> None:
    logger.info("Starting totem-mm")

    # ── Betfair price feed ────────────────────────────────────────
    betfair_client = BetfairClient()
    market_id = "1.253606950"

    price_feed = AsyncBetfairPriceFeed(
        client=betfair_client,
        market_id=market_id,
        poll_interval=300.0,
    )
    await price_feed.start()

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

    # ── graceful shutdown ─────────────────────────────────────────
    stop_event = asyncio.Event()

    def _shutdown():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    await stop_event.wait()

    if rfq_listener:
        await rfq_listener.stop()
    await price_feed.stop()
    logger.info("totem-mm stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
