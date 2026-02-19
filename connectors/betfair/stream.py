"""
Betfair Streaming Price Feed using betfairlightweight.

Replaces the polling-based AsyncBetfairPriceFeed with a WebSocket streaming implementation
for real-time price updates (< 200ms latency vs ~2000ms polling).
"""

import logging
import queue
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

import betfairlightweight
from betfairlightweight import filters
from betfairlightweight.streaming import StreamListener

from utils.data_persistence import PriceDataWriter

logger = logging.getLogger(__name__)


def _market_book_to_odds_update(market: Any, update_time: datetime) -> Dict[int, Dict[str, Any]]:
    """Convert betfairlightweight MarketBook to our odds_update dict.

    Streaming API returns RunnerBook with prices under runner.ex (RunnerBookEX);
    REST listMarketBook can use same shape. We support both.
    """
    odds_update: Dict[int, Dict[str, Any]] = {}
    for runner in market.runners:
        selection_id = runner.selection_id
        # Streaming: RunnerBook has runner.ex.available_to_back / available_to_lay
        atb = getattr(runner, "available_to_back", None) or (
            getattr(runner.ex, "available_to_back", None) if getattr(runner, "ex", None) else None
        )
        atl = getattr(runner, "available_to_lay", None) or (
            getattr(runner.ex, "available_to_lay", None) if getattr(runner, "ex", None) else None
        )
        back_price = (atb[0].price if atb and len(atb) > 0 else None)
        lay_price = (atl[0].price if atl and len(atl) > 0 else None)
        odds_update[selection_id] = {
            "back": back_price,
            "lay": lay_price,
            "last_traded": getattr(runner, "last_price_traded", None),
            "timestamp": update_time,
        }
    return odds_update


class BetfairStreamListener(StreamListener):
    """
    StreamListener that forwards market book updates to AsyncBetfairStreamFeed.

    betfairlightweight expects a BaseListener/StreamListener with on_data(raw_data).
    We subclass StreamListener, run the default parsing, then snap market books
    and push odds into the price feed.
    """

    def __init__(self, price_feed: "AsyncBetfairStreamFeed", market_ids: List[str]):
        super().__init__(output_queue=queue.Queue(), max_latency=0.5, lightweight=False)
        self.price_feed = price_feed
        self.market_ids = market_ids

    def on_data(self, raw_data: str) -> Optional[bool]:
        result = super().on_data(raw_data)
        if result is False:
            return False
        if not self.stream or self.stream_type != "marketSubscription":
            return
        try:
            market_books = self.stream.snap(self.market_ids)
            if not market_books:
                return None
            update_time = datetime.utcnow()
            for market in market_books:
                if market is None:
                    continue
                market_id = str(market.market_id)
                odds_update = _market_book_to_odds_update(market, update_time)
                if odds_update:
                    self.price_feed._update_odds(market_id, odds_update, update_time)
                    logger.info(
                        "[BETFAIR-STREAMING] Updated odds for market %s (%d runners)",
                        market_id,
                        len(odds_update),
                    )
        except Exception:
            logger.exception("[BETFAIR-STREAMING] Error processing market update")
        return None

    def _on_status(self, data: dict, unique_id: int) -> None:
        """Log stream status with [BETFAIR-STREAMING] prefix."""
        status_code = data.get("statusCode")
        connections_available = data.get("connectionsAvailable")
        if connections_available is not None:
            self.connections_available = connections_available
        logger.info(
            "[BETFAIR-STREAMING] Stream status: %s (id=%s, connections_available=%s)",
            status_code,
            unique_id,
            getattr(self, "connections_available", None),
        )


class AsyncBetfairStreamFeed:
    """
    Async Betfair price feed using WebSocket streaming (betfairlightweight).

    This replaces AsyncBetfairPriceFeed with a streaming implementation that provides
    sub-200ms latency updates instead of polling every 10 seconds.

    Architecture:
    - Uses betfairlightweight library for WebSocket connection
    - Runs streaming client in a separate thread (betfairlightweight is sync)
    - Processes updates via StreamListener callbacks
    - Maintains same public API as AsyncBetfairPriceFeed for compatibility
    """

    def __init__(
        self,
        username: str,
        password: str,
        app_key: str,
        market_ids: List[str],
        data_dir: str = "data",
        enable_data_persistence: bool = True,
        certs: Optional[str] = None,
        cert_file: Optional[str] = None,
    ):
        """
        Initialize streaming price feed.

        Args:
            username: Betfair username (for streaming API)
            password: Betfair password
            app_key: Betfair application key
            market_ids: List of Betfair market IDs to subscribe to
            data_dir: Directory for data persistence
            enable_data_persistence: Whether to write price updates to disk
            certs: Directory containing Betfair SSL certs (.crt+.key or .pem). If None, APIClient uses /certs.
            cert_file: Path to single .pem file (alternative to certs dir). Takes precedence if set.
        """
        self.username = username
        self.password = password
        self.app_key = app_key
        self.market_ids = market_ids
        self.certs = certs
        self.cert_file = cert_file
        self.data_dir = data_dir
        self.enable_data_persistence = enable_data_persistence

        # Internal state (same structure as AsyncBetfairPriceFeed)
        self._odds: Dict[str, Dict[int, Dict[str, Any]]] = {}
        self._last_update: Optional[datetime] = None
        self._is_running: bool = False

        # Streaming components
        self._trading: Optional[betfairlightweight.APIClient] = None
        self._stream: Optional[betfairlightweight.Stream] = None
        self._stream_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Data writers
        self._data_writers: Dict[str, PriceDataWriter] = {}
        if self.enable_data_persistence:
            for market_id in market_ids:
                self._data_writers[market_id] = PriceDataWriter(
                    data_dir=data_dir,
                    market_id=market_id,
                )

        # Lock for thread-safe updates
        self._lock = threading.Lock()

    async def start(self) -> None:
        """Start the streaming price feed."""
        if self._is_running:
            logger.warning("Stream feed already running for %s", self.market_ids)
            return

        logger.info(
            "[BETFAIR-STREAMING] Starting price feed for %d market(s): %s",
            len(self.market_ids),
            self.market_ids,
        )

        # Initialize Betfair API client (cert login requires SSL client certs)
        client_kw: Dict[str, Any] = {
            "username": self.username,
            "password": self.password,
            "app_key": self.app_key,
        }
        if self.cert_file:
            client_kw["cert_files"] = self.cert_file
        elif self.certs:
            client_kw["certs"] = self.certs
        else:
            # Default to ./certs so dev machines don't hit missing /certs
            client_kw["certs"] = "certs"
        try:
            self._trading = betfairlightweight.APIClient(**client_kw)
            self._trading.login()
            logger.info("[BETFAIR-STREAMING] API client logged in successfully")

        except Exception as e:
            logger.error("[BETFAIR-STREAMING] Failed to login: %s", e, exc_info=True)
            raise

        # Create stream listener (must subclass StreamListener; receives on_data(raw_data))
        listener = BetfairStreamListener(self, market_ids=list(self.market_ids))

        # Market filter: which markets to subscribe to
        market_filter = filters.streaming_market_filter(market_ids=self.market_ids)
        # Market data filter: which fields (prices, ladder levels)
        market_data_filter = filters.streaming_market_data_filter(
            fields=[
                "EX_BEST_OFFERS",  # Top of book prices
                "EX_MARKET_DEF",  # Market definition
            ],
            ladder_levels=1,  # Only need top level for speed
        )

        # Create stream and subscribe (must be done before starting)
        try:
            self._stream = self._trading.streaming.create_stream(listener=listener)
            logger.info("[BETFAIR-STREAMING] Stream created")

            self._stream.subscribe_to_markets(
                market_filter=market_filter,
                market_data_filter=market_data_filter,
            )
            logger.info("[BETFAIR-STREAMING] Subscribed to markets: %s", self.market_ids)

        except Exception as e:
            logger.error("[BETFAIR-STREAMING] Failed to create/subscribe stream: %s", e, exc_info=True)
            raise

        # Start stream in a separate thread (betfairlightweight is synchronous)
        self._is_running = True
        self._stop_event.clear()

        def run_stream():
            """Run the stream in a blocking manner."""
            try:
                logger.info("[BETFAIR-STREAMING] Starting stream thread...")
                # Use dict unpacking to avoid 'async' keyword conflict (reserved in Python 3.7+)
                self._stream.start()  # Blocking call
            except Exception as e:
                logger.error("[BETFAIR-STREAMING] Stream thread error: %s", e, exc_info=True)
                self._is_running = False
            finally:
                logger.info("[BETFAIR-STREAMING] Stream thread stopped")

        self._stream_thread = threading.Thread(target=run_stream, daemon=True)
        self._stream_thread.start()

        logger.info("[BETFAIR-STREAMING] Streaming feed started")

    async def stop(self) -> None:
        """Stop the streaming price feed."""
        if not self._is_running:
            return

        logger.info("[BETFAIR-STREAMING] Stopping streaming feed...")
        self._is_running = False
        self._stop_event.set()

        # Stop stream
        if self._stream:
            try:
                self._stream.stop()
            except Exception:
                logger.exception("[BETFAIR-STREAMING] Error stopping stream")

        # Wait for stream thread to finish
        if self._stream_thread and self._stream_thread.is_alive():
            self._stream_thread.join(timeout=5)
            if self._stream_thread.is_alive():
                logger.warning("Stream thread did not stop within timeout")

        # Logout
        if self._trading:
            try:
                self._trading.logout()
            except Exception:
                logger.exception("[BETFAIR-STREAMING] Error logging out")

        # Write final snapshots
        with self._lock:
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

        logger.info("[BETFAIR-STREAMING] Streaming feed stopped")

    def _update_odds(
        self,
        market_id: str,
        odds_update: Dict[int, Dict[str, Any]],
        timestamp: datetime,
    ) -> None:
        """
        Update odds for a market (called by StreamListener).

        Thread-safe update of internal odds state.
        """
        with self._lock:
            if market_id not in self._odds:
                self._odds[market_id] = {}

            self._odds[market_id].update(odds_update)
            self._last_update = timestamp

            # Write to data persistence
            if market_id in self._data_writers:
                try:
                    self._data_writers[market_id].write_price_update(
                        market_id=market_id,
                        odds=self._odds[market_id],
                        timestamp=timestamp,
                    )
                except Exception as e:
                    logger.error("Error writing price update for %s: %s", market_id, e)

            logger.debug(
                "[BETFAIR-STREAMING] Updated odds for market %s (%d runners)",
                market_id,
                len(odds_update),
            )

    def get_snapshot(self, market_id: Optional[str] = None) -> Dict[int, Dict[str, Any]]:
        """
        Return latest odds snapshot (same API as AsyncBetfairPriceFeed).

        Args:
            market_id: If provided, return odds for that specific market.
                       If None and only one market, return that market's odds.
                       If None and multiple markets, return all markets' odds.

        Returns:
            Dictionary mapping selection_id -> odds data
        """
        with self._lock:
            if market_id:
                return dict(self._odds.get(market_id, {}))

            # Backwards compatibility: if only one market, return its odds directly
            if len(self.market_ids) == 1:
                return dict(self._odds.get(self.market_ids[0], {}))

            # Multiple markets: return all
            return {mid: dict(odds) for mid, odds in self._odds.items()}

    def get_all_snapshots(self) -> Dict[str, Dict[int, Dict[str, Any]]]:
        """Return odds snapshots for all markets, keyed by market_id."""
        with self._lock:
            return {mid: dict(odds) for mid, odds in self._odds.items()}
