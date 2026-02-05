import asyncio
import logging
import signal

from connectors.betfair.client import BetfairClient
from connectors.betfair.prices import AsyncBetfairPriceFeed
from utils.logging_config import setup_logging

setup_logging(log_level=logging.INFO)

logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("Starting totem-mm")

    client = BetfairClient()

    
    market_id = "1.253606950"

    price_feed = AsyncBetfairPriceFeed(
        client=client,
        market_id=market_id,
        poll_interval=300.0,
    )

    await price_feed.start()

    stop_event = asyncio.Event()

    def _shutdown():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    await stop_event.wait()

    await price_feed.stop()
    logger.info("totem-mm stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
