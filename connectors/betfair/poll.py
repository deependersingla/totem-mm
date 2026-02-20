"""
Betfair Polling Price Feed (HTTP-based).

Polls Betfair API every N seconds to fetch market book data.
Use this when streaming is not available or for testing.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from connectors.betfair.client import BetfairClient
from utils.data_persistence import PriceDataWriter

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 10


class AsyncBetfairPollFeed:
    """
    Async Betfair price feed using HTTP polling.

    Polls listMarketBook endpoint every poll_interval seconds.
    Suitable for testing or when streaming credentials are not available.
    """

    def __init__(
        self,
        client: BetfairClient,
        market_id: Optional[str] = None,
        market_ids: Optional[List[str]] = None,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        data_dir: str = "data",
        enable_data_persistence: bool = True,
    ):
        """
        Initialize polling price feed.

        Args:
            client: BetfairClient instance
            market_id: Single market ID (backwards compatible)
            market_ids: List of market IDs to poll
            poll_interval: Seconds between polls
            data_dir: Directory for data persistence
            enable_data_persistence: Whether to write price updates to disk
        """
        self.client = client
        self.poll_interval = poll_interval
        self.enable_data_persistence = enable_data_persistence

        # Support both single market_id (backwards compatible) and multiple market_ids
        if market_ids:
            self.market_ids = market_ids
        elif market_id:
            self.market_ids = [market_id]
        else:
            raise ValueError("Either market_id or market_ids must be provided")

        # For backwards compatibility
        self.market_id = self.market_ids[0]

        # Odds keyed by market_id -> selection_id -> odds data
        self._odds: Dict[str, Dict[int, Dict[str, Any]]] = {}
        self._last_update: Optional[datetime] = None
        self._is_running: bool = False
        self._task: Optional[asyncio.Task] = None

        # Data writers for each market
        self._data_writers: Dict[str, PriceDataWriter] = {}
        if self.enable_data_persistence:
            for mid in self.market_ids:
                self._data_writers[mid] = PriceDataWriter(
                    data_dir=data_dir,
                    market_id=mid,
                )

    async def start(self) -> None:
        """Start the price feed polling task."""
        if self._is_running:
            logger.warning("Poll feed already running for %s", self.market_ids)
            return

        logger.info(
            "[BETFAIR-POLLING] Starting price feed for %d market(s): %s (interval=%ds)",
            len(self.market_ids),
            self.market_ids,
            self.poll_interval,
        )
        self._is_running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the polling task."""
        self._is_running = False

        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                logger.warning("Force-cancelling poll feed task")
                self._task.cancel()

        # Write final snapshots for all markets
        for market_id, writer in self._data_writers.items():
            if writer and market_id in self._odds:
                try:
                    writer.write_snapshot(
                        market_id=market_id,
                        odds=self._odds[market_id],
                    )
                    logger.info("Final snapshot written for market %s", market_id)
                except Exception as e:
                    logger.error("Error writing final snapshot for %s: %s", market_id, e)

    def get_snapshot(self, market_id: Optional[str] = None) -> Dict[int, Dict[str, Any]]:
        """
        Return latest odds snapshot.

        Args:
            market_id: If provided, return odds for that specific market.
                       If None and only one market, return that market's odds (backwards compatible).
                       If None and multiple markets, return all markets' odds.

        Returns:
            Dictionary mapping selection_id -> odds data
        """
        if market_id:
            return dict(self._odds.get(market_id, {}))

        # Backwards compatibility: if only one market, return its odds directly
        if len(self.market_ids) == 1:
            return dict(self._odds.get(self.market_ids[0], {}))

        # Multiple markets: return all
        return {mid: dict(odds) for mid, odds in self._odds.items()}

    def get_all_snapshots(self) -> Dict[str, Dict[int, Dict[str, Any]]]:
        """Return odds snapshots for all markets, keyed by market_id."""
        return {mid: dict(odds) for mid, odds in self._odds.items()}

    async def _run(self) -> None:
        """Main polling loop."""
        while self._is_running:
            try:
                await self._poll_once()
            except Exception:
                logger.exception(
                    "Unhandled error in async price polling for %s",
                    self.market_ids,
                )
            await asyncio.sleep(self.poll_interval)

    async def _poll_once(self) -> None:
        """Poll all markets once and update odds."""
        params = {
            "marketIds": self.market_ids,
            "priceProjection": {
                "priceData": ["EX_BEST_OFFERS", "EX_TRADED"],
            },
        }
        try:
            response = await asyncio.to_thread(
                self.client.call,
                "SportsAPING/v1.0/listMarketBook",
                params,
            )

            logger.debug(
                "Betfair marketBook response markets=%s",
                self.market_ids,
            )

            market_books = response.get("result", [])
            if not market_books:
                logger.warning("[BETFAIR-POLLING] Market books empty for %s", self.market_ids)
                return

            self._last_update = datetime.now()

            for market_book in market_books:
                if not market_book:
                    continue

                market_id = market_book.get("marketId")
                if not market_id:
                    continue

                self._update_odds(market_id, market_book)

                # Write to data persistence
                if market_id in self._data_writers:
                    self._data_writers[market_id].write_price_update(
                        market_id=market_id,
                        odds=self._odds.get(market_id, {}),
                        timestamp=self._last_update,
                    )

                logger.info(
                    "[BETFAIR-POLLING] Updated odds for market %s with %d runners",
                    market_id,
                    len(market_book.get("runners", [])),
                )

        except Exception:
            logger.exception("[BETFAIR-POLLING] Error polling Betfair for %s", self.market_ids)

    def _update_odds(self, market_id: str, market_book: Dict[str, Any]) -> None:
        """Update odds for a market from market book response."""
        timestamp = datetime.now()

        if market_id not in self._odds:
            self._odds[market_id] = {}

        for runner in market_book.get("runners", []):
            selection_id = runner["selectionId"]

            ex = runner.get("ex", {})
            back = self._best_price(ex.get("availableToBack", []))
            lay = self._best_price(ex.get("availableToLay", []))
            last_traded = runner.get("lastPriceTraded")

            self._odds[market_id][selection_id] = {
                "back": back,
                "lay": lay,
                "last_traded": last_traded,
                "timestamp": timestamp,
            }

    @staticmethod
    def _best_price(prices: list) -> Optional[float]:
        """Extract best (first) price from price ladder."""
        if not prices:
            return None
        return prices[0].get("price")
