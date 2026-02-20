"""
Polymarket RFQ Polling Listener (HTTP-based).

Polls Polymarket RFQ API every N seconds to check for new requests.
Use this when streaming is not available or for testing.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import settings
from connectors.polymarket.client import PolymarketClient
from models.rfq import QuoteSubmission
from pricing.quote_engine import QuoteEngine

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL: float = 5.0  # seconds between request polls
DEFAULT_QUOTE_TTL: float = 300.0  # seconds before a quote is considered stale

_SEEN_CACHE_LIMIT: int = 10_000  # max request_ids to remember before clearing
_ORDER_EXPIRATION_BUFFER: int = 3600  # seconds of validity when approving an order


class AsyncPolymarketPollListener:
    """
    Async loop that polls Polymarket RFQ API and manages quote lifecycle.

    Polling-based implementation:
    1. Poll Polymarket for pending RFQ requests (every poll_interval seconds)
    2. Price each new request via QuoteEngine
    3. Submit quotes and track their state
    4. Monitor active quotes — approve orders when accepted
    5. Cancel stale quotes to free exposure
    """

    def __init__(
        self,
        client: PolymarketClient,
        quote_engine: QuoteEngine,
        snapshot_fn: Callable[[], dict[int, dict[str, Any]]],
        markets: list[str] | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        quote_ttl: float = DEFAULT_QUOTE_TTL,
    ):
        """
        Initialize polling RFQ listener.

        Args:
            client: PolymarketClient instance
            quote_engine: QuoteEngine for pricing RFQs
            snapshot_fn: Function that returns current Betfair odds snapshot
            markets: Optional list of Polymarket market IDs to filter RFQs
            poll_interval: Seconds between RFQ polls
            quote_ttl: Seconds before a quote is considered stale
        """
        self._client = client
        self._engine = quote_engine
        self._snapshot_fn = snapshot_fn
        self._markets = markets
        self._poll_interval = poll_interval
        self._quote_ttl = quote_ttl

        exec_cfg = settings.RFQ_EXECUTION_CONFIG
        self._dry_run = exec_cfg["DRY_RUN"]
        self._approve_orders = exec_cfg["APPROVE_ORDERS"]

        self._is_running: bool = False
        self._task: asyncio.Task | None = None

        # dedupe: request_ids we have already acted on
        self._seen_requests: set[str] = set()
        # lifecycle: quote_id → submission record
        self._active_quotes: dict[str, QuoteSubmission] = {}

    # ── public lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        """Start the polling listener."""
        if self._is_running:
            logger.warning("Poll listener already running")
            return

        logger.info(
            "[POLLING] Starting Polymarket RFQ listener (poll_interval=%.1fs, quote_ttl=%.0fs, markets=%s, dry_run=%s, approve_orders=%s)",
            self._poll_interval,
            self._quote_ttl,
            self._markets,
            self._dry_run,
            self._approve_orders,
        )
        self._is_running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the polling listener."""
        self._is_running = False
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                logger.warning("Force-cancelling poll listener task")
                self._task.cancel()
        logger.info("[POLLING] Poll listener stopped (active_quotes=%d)", len(self._active_quotes))

    @property
    def active_quotes(self) -> dict[str, QuoteSubmission]:
        """Return active quotes dictionary."""
        return dict(self._active_quotes)

    # ── main loop ─────────────────────────────────────────────────

    async def _run(self) -> None:
        """Main polling loop."""
        while self._is_running:
            try:
                await self._poll_requests()
                await self._check_active_quotes()
                await self._cancel_stale_quotes()
            except Exception:
                logger.exception("[POLLING] Unhandled error in poll listener loop")
            await asyncio.sleep(self._poll_interval)

    # ── request polling & quote submission ────────────────────────

    async def _poll_requests(self) -> None:
        """Poll Polymarket for new RFQ requests."""
        response = await asyncio.to_thread(
            self._client.get_pending_requests,
            markets=self._markets,
        )
        requests: list[dict] = response.get("data", [])
        if not requests:
            logger.debug("[POLLING] No pending RFQ requests")
            return

        logger.info("[POLLING] Fetched %d pending RFQ request(s)", len(requests))

        snapshot = self._snapshot_fn()
        if not snapshot:
            logger.warning("[POLLING] Betfair snapshot is empty — skipping quote round")
            return

        for req in requests:
            request_id = req.get("request_id")
            if not request_id or request_id in self._seen_requests:
                continue
            self._seen_requests.add(request_id)
            await self._handle_request(req, snapshot)

        # Bound the seen-requests cache to prevent unbounded memory growth.
        # Re-seen request_ids after a clear will be rejected by the API as duplicates.
        if len(self._seen_requests) > _SEEN_CACHE_LIMIT:
            logger.debug(
                "Clearing seen-requests cache (was %d entries)", len(self._seen_requests)
            )
            self._seen_requests.clear()

    async def _handle_request(self, req: dict[str, Any], snapshot: dict) -> None:
        """Handle a single RFQ request: price it and submit quote if valid."""
        request_id = req["request_id"]

        price_quote = self._engine.price(req, snapshot)
        if price_quote is None:
            logger.debug("[POLLING] QuoteEngine declined request %s", request_id)
            return

        submission = QuoteSubmission(
            request_id=request_id,
            token_id=price_quote.token_id,
            price=price_quote.price,
            side=price_quote.side,
            size=price_quote.size,
        )

        if self._dry_run:
            logger.info(
                "[POLLING] DRY_RUN: would submit quote request_id=%s token=%s side=%s price=%.6f size=%.6f notional=%.4f",
                request_id,
                price_quote.token_id,
                price_quote.side,
                price_quote.price,
                price_quote.size,
                price_quote.notional_usdc,
            )
            return

        try:
            result = await asyncio.to_thread(
                self._client.submit_quote,
                request_id=request_id,
                token_id=price_quote.token_id,
                price=price_quote.price,
                side=price_quote.side,
                size=price_quote.size,
            )

            quote_id = result.get("quote_id")
            if not quote_id:
                submission.status = "failed"
                submission.error = result.get("error", "no quote_id in response")
                logger.error(
                    "[POLLING] Quote submission failed for request %s: %s",
                    request_id,
                    submission.error,
                )
                return

            submission.quote_id = quote_id
            submission.status = "active"
            self._active_quotes[quote_id] = submission
            self._engine.track_quote(price_quote.notional_usdc)

            logger.info(
                "[POLLING] Quote submitted — quote_id=%s request_id=%s price=%.6f size=%.4f side=%s",
                quote_id,
                request_id,
                price_quote.price,
                price_quote.size,
                price_quote.side,
            )

        except Exception:
            submission.status = "failed"
            logger.exception("[POLLING] Error submitting quote for request %s", request_id)

    # ── active-quote monitoring ───────────────────────────────────

    async def _check_active_quotes(self) -> None:
        """Poll Polymarket for state changes on our active quotes and react."""
        active = {qid: q for qid, q in self._active_quotes.items() if q.status == "active"}
        if not active:
            return

        request_ids = list({q.request_id for q in active.values()})
        try:
            response = await asyncio.to_thread(
                self._client.get_my_quotes,
                request_ids=request_ids,
            )
        except Exception:
            logger.exception("[POLLING] Error fetching active quote states")
            return

        remote_quotes: dict[str, dict] = {q["quote_id"]: q for q in response.get("data", [])}

        for quote_id, submission in list(active.items()):
            remote = remote_quotes.get(quote_id)
            if not remote:
                continue
            remote_state = (remote.get("state") or "").upper()
            await self._transition_quote(quote_id, submission, remote_state)

    async def _transition_quote(
        self, quote_id: str, submission: QuoteSubmission, remote_state: str
    ) -> None:
        """Handle quote state transitions."""
        if remote_state == "ACTIVE":
            return

        if remote_state == "ACCEPTED":
            await self._approve_quote(quote_id, submission)
        elif remote_state in ("FILLED", "SETTLED"):
            submission.status = "filled"
            self._engine.release_quote(submission.notional_usdc)
            logger.info("[POLLING] Quote %s filled", quote_id)
        elif remote_state in ("CANCELLED", "EXPIRED"):
            submission.status = "cancelled"
            self._engine.release_quote(submission.notional_usdc)
            del self._active_quotes[quote_id]
            logger.info("[POLLING] Quote %s cancelled/expired remotely", quote_id)
        else:
            logger.debug("[POLLING] Quote %s has unhandled remote state '%s'", quote_id, remote_state)

    async def _approve_quote(self, quote_id: str, submission: QuoteSubmission) -> None:
        """Approve an accepted quote (execute the trade)."""
        if self._dry_run or not self._approve_orders:
            logger.info(
                "[POLLING] Skipping approve (dry_run=%s approve_orders=%s) quote_id=%s request_id=%s",
                self._dry_run,
                self._approve_orders,
                quote_id,
                submission.request_id,
            )
            return

        expiration = int(time.time()) + _ORDER_EXPIRATION_BUFFER
        try:
            await asyncio.to_thread(
                self._client.approve_order,
                request_id=submission.request_id,
                quote_id=quote_id,
                expiration=expiration,
            )
            submission.status = "filled"
            self._engine.release_quote(submission.notional_usdc)
            logger.info(
                "[POLLING] Order approved — quote_id=%s request_id=%s",
                quote_id,
                submission.request_id,
            )
        except Exception:
            logger.exception("[POLLING] Error approving order for quote %s", quote_id)

    # ── stale-quote housekeeping ──────────────────────────────────

    async def _cancel_stale_quotes(self) -> None:
        """Cancel quotes that have exceeded their TTL."""
        now = time.time()
        stale = [
            (qid, q)
            for qid, q in self._active_quotes.items()
            if q.status == "active" and (now - q.created_at.timestamp()) > self._quote_ttl
        ]
        for quote_id, submission in stale:
            try:
                await asyncio.to_thread(self._client.cancel_quote, quote_id)
                submission.status = "cancelled"
                self._engine.release_quote(submission.notional_usdc)
                del self._active_quotes[quote_id]
                logger.info(
                    "[POLLING] Cancelled stale quote %s (age=%.0fs)",
                    quote_id,
                    now - submission.created_at.timestamp(),
                )
            except Exception:
                logger.exception("[POLLING] Error cancelling stale quote %s", quote_id)
