import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from connectors.betfair.client import BetfairClient
from utils.data_persistence import PriceDataWriter

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 10

class AsyncBetfairPriceFeed: 
    def __init__(
        self, 
        client: BetfairClient,
        market_id: str,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        data_dir: str = "data",
        enable_data_persistence: bool = True,
    ):
        self.client = client
        self.market_id = market_id
        self.poll_interval = poll_interval
        self.enable_data_persistence = enable_data_persistence

        self._odds: Dict[int, Dict[str, Any]] = {}
        self._last_update: Optional[datetime] = None
        self._is_running: bool = False
        self._task: Optional[asyncio.Task] = None
        
        if self.enable_data_persistence:
            self._data_writer = PriceDataWriter(
                data_dir=data_dir,
                market_id=market_id,
            )
        else:
            self._data_writer = None

    async def start(self) -> None:
        """Start the price feed polling task."""
        if self._is_running:
            logger.warning("Async price feed already running for %s", self.market_id)
            return
        
        logger.info("Starting async Betfair price feed for %s", self.market_id)
        self._is_running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._is_running = False

        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                logger.warning("Force-cancelling price feed task")
                self._task.cancel()
        
        if self._data_writer and self._odds:
            try:
                self._data_writer.write_snapshot(
                    market_id=self.market_id,
                    odds=self._odds,
                )
                logger.info("Final snapshot written on shutdown")
            except Exception as e:
                logger.error("Error writing final snapshot: %s", e)


    def get_snapshot(self) -> Dict[int, Dict[str, Any]]:
        """Return latest odds snapshot."""
        return dict(self._odds) or {}




    async def _run(self) -> None:
        while self._is_running:
            try:
                await self._poll_once()
            except Exception:
                logger.exception(
                    "Unhandled error in async price polling for %s",
                    self.market_id,
                )
            await asyncio.sleep(self.poll_interval)


    async def _poll_once(self) -> None:
        """Poll once and update odds."""
        

        params = {
            "marketIds": [self.market_id],
            "priceProjection": {
                "priceData": ["EX_BEST_OFFERS", "EX_TRADED"]
            },
        }
        try:
            response = await asyncio.to_thread(
                self.client.call,
                "SportsAPING/v1.0/listMarketBook",
                params,
            )

            logger.info(
                "Betfair marketBook response market=%s response=%s",
                self.market_id,
                response,
            )

            market_book = response.get("result", [])
            if not market_book:
                logger.warning("Market book empty for %s", self.market_id)
                return

            market_book = market_book[0]
            if not market_book:
                logger.warning("Market book empty for %s", self.market_id)
                return

            market_id = market_book.get("marketId")
            if not market_id:
                logger.warning("Market ID not found in market book for %s", self.market_id)
                return

            self._update_odds(market_book)
            self._last_update = datetime.now()
            
            if self._data_writer:
                self._data_writer.write_price_update(
                    market_id=self.market_id,
                    odds=self._odds,
                    timestamp=self._last_update,
                )

            logger.info(
                "Updated odds for market %s with %d runners",
                self.market_id,
                len(market_book.get("runners", [])),
            )
                
        except Exception:
            logger.exception("Error polling Betfair for %s", self.market_id)

    def _update_odds(self, market_book: Dict[str, Any]) -> None:
        timestamp = datetime.now()
        for runner in market_book.get("runners", []):
            selection_id = runner["selectionId"]

            ex = runner.get("ex", {})
            back = self._best_price(ex.get("availableToBack", []))
            lay = self._best_price(ex.get("availableToLay", []))
            last_traded = runner.get("lastPriceTraded")

            self._odds[selection_id] = {
                "back": back,
                "lay": lay,
                "last_traded": last_traded,
                "timestamp": timestamp,
            }

    @staticmethod
    def _best_price(prices: list) -> Optional[float]:
        if not prices:
            return None
        return prices[0].get("price")