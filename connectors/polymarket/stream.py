"""
Polymarket RFQ Streaming Listener (WebSocket-based).

Uses Polymarket's RTDS websocket (`wss://ws-live-data.polymarket.com`) to subscribe to the `rfq`
topic and react to RFQ events in near real-time.

Docs / references:
- RTDS client (topic/type schema + subscribe message shape): https://github.com/Polymarket/real-time-data-client
- RFQ REST API objects (fields/state names): https://docs.polymarket.com/developers/market-makers/rfq/api-reference
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

import settings
from connectors.polymarket.client import PolymarketClient
from models.rfq import QuoteSubmission
from pricing.quote_engine import QuoteEngine

logger = logging.getLogger(__name__)


class AsyncPolymarketStreamListener:
    """
    WebSocket-based RFQ listener for real-time Polymarket RFQ events.

    It subscribes to RTDS topic `rfq` and handles:
    - request_* events: price + (optionally) submit quotes
    - quote_* events: logs lifecycle signals (and can optionally approve orders if enabled)

    Safety:
    - Controlled by `settings.RFQ_EXECUTION_CONFIG` (DRY_RUN / APPROVE_ORDERS).
    """

    def __init__(
        self,
        client: PolymarketClient,
        quote_engine: QuoteEngine,
        snapshot_fn: Callable[[], dict[int, dict[str, Any]]],
        markets: list[str] | None = None,
        quote_ttl: float = 300.0,
        ws_url: Optional[str] = None,
        ping_interval_seconds: Optional[float] = None,
        reconnect_delay_seconds: Optional[float] = None,
        log_raw_messages: Optional[bool] = None,
    ):
        self._client = client
        self._engine = quote_engine
        self._snapshot_fn = snapshot_fn
        self._markets = markets
        self._quote_ttl = quote_ttl

        rtds_cfg = settings.POLYMARKET_RTDS_CONFIG
        self._ws_url = ws_url or rtds_cfg["WS_URL"]
        self._ping_interval_seconds = (
            ping_interval_seconds
            if ping_interval_seconds is not None
            else rtds_cfg["PING_INTERVAL_SECONDS"]
        )
        self._reconnect_delay_seconds = (
            reconnect_delay_seconds
            if reconnect_delay_seconds is not None
            else rtds_cfg["RECONNECT_DELAY_SECONDS"]
        )
        self._log_raw_messages = (
            log_raw_messages if log_raw_messages is not None else rtds_cfg["LOG_RAW_MESSAGES"]
        )

        exec_cfg = settings.RFQ_EXECUTION_CONFIG
        self._dry_run = exec_cfg["DRY_RUN"]
        self._approve_orders = exec_cfg["APPROVE_ORDERS"]

        self._is_running: bool = False
        self._task: asyncio.Task | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._ping_task: asyncio.Task | None = None

        # lifecycle: quote_id → submission record
        self._active_quotes: dict[str, QuoteSubmission] = {}
        self._seen_requests: set[str] = set()

    async def start(self) -> None:
        if self._is_running:
            logger.warning("[STREAMING] Stream listener already running")
            return

        logger.info(
            "[STREAMING] Starting Polymarket RFQ listener (ws_url=%s, ping=%.1fs, dry_run=%s, approve_orders=%s)",
            self._ws_url,
            self._ping_interval_seconds,
            self._dry_run,
            self._approve_orders,
        )

        self._is_running = True
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._is_running = False

        if self._ping_task:
            self._ping_task.cancel()

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                logger.exception("[STREAMING] Error closing RTDS websocket")

        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                logger.warning("[STREAMING] Force-cancelling stream listener task")
                self._task.cancel()

        logger.info("[STREAMING] Stream listener stopped (active_quotes=%d)", len(self._active_quotes))

    @property
    def active_quotes(self) -> dict[str, QuoteSubmission]:
        return dict(self._active_quotes)

    async def _run_forever(self) -> None:
        attempt = 0
        while self._is_running:
            attempt += 1
            # Brief delay before first connect to reduce HTTP 429 from RTDS on cold start
            if attempt == 1:
                await asyncio.sleep(1)
            try:
                await self._connect_and_consume()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[STREAMING] Unhandled error in RTDS stream loop (attempt=%d)", attempt)

            if not self._is_running:
                break

            await asyncio.sleep(self._reconnect_delay_seconds)

    async def _connect_and_consume(self) -> None:
        logger.info("[STREAMING] Connecting to RTDS websocket %s", self._ws_url)

        # RTDS can return HTTP 429 on first connect; use short close + retry (handled by _run_forever)
        async with websockets.connect(
            self._ws_url,
            ping_interval=None,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            logger.info("[STREAMING] RTDS websocket connected")

            await self._subscribe_rfq()
            self._start_ping_loop()

            async for raw_msg in ws:
                await self._handle_raw_message(raw_msg)

    def _start_ping_loop(self) -> None:
        if self._ping_task and not self._ping_task.done():
            return

        async def _pinger() -> None:
            while self._is_running and self._ws:
                try:
                    await asyncio.sleep(self._ping_interval_seconds)
                    # Check if connection is closed (close_code is None when open)
                    if self._ws.close_code is not None:
                        return
                    await self._ws.send("ping")
                except asyncio.CancelledError:
                    raise
                except ConnectionClosed:
                    logger.debug("[STREAMING] RTDS ping: connection closed")
                    return
                except Exception:
                    logger.exception("[STREAMING] RTDS ping loop error")
                    return

        self._ping_task = asyncio.create_task(_pinger())

    async def _subscribe_rfq(self) -> None:
        if not self._ws:
            return

        # RTDS subscription format (see Polymarket real-time-data-client):
        # { "action": "subscribe", "subscriptions": [ { "topic": "rfq", "type": "*" } ] }
        msg = {"action": "subscribe", "subscriptions": [{"topic": "rfq", "type": "*"}]}
        await self._ws.send(json.dumps(msg))
        logger.info("[STREAMING] RTDS subscribe sent (topic=rfq, type=*)")

    async def _handle_raw_message(self, raw_msg: Any) -> None:
        if raw_msg == "pong":
            logger.info("[STREAMING] RTDS pong received (connection alive)")
            return
        if raw_msg == "ping":
            logger.debug("[STREAMING] RTDS ping received")
            return

        if not isinstance(raw_msg, str):
            logger.debug("[STREAMING] RTDS non-text message: %r", raw_msg)
            return

        # Empty/whitespace frames are often keepalives; skip without warning
        if not raw_msg or not raw_msg.strip():
            return

        if self._log_raw_messages:
            logger.debug("[STREAMING] RTDS raw message: %s", raw_msg)

        try:
            msg = json.loads(raw_msg)
        except json.JSONDecodeError:
            logger.warning("[STREAMING] RTDS message not JSON (len=%d): %r", len(raw_msg), raw_msg[:200])
            return

        topic = msg.get("topic")
        typ = msg.get("type")
        payload = msg.get("payload")

        if not topic or not typ:
            logger.debug("[STREAMING] RTDS message missing topic/type: %s", msg)
            return

        if topic != "rfq":
            return

        # Log that we received an RFQ topic message (confirms stream is alive)
        logger.info("[STREAMING] RTDS rfq message received (type=%s)", typ)

        if not isinstance(payload, dict):
            logger.debug("[STREAMING] RTDS rfq message payload not dict (type=%s): %r", typ, payload)
            return

        # request lifecycle events
        if typ.startswith("request_"):
            await self._handle_request_event(typ, payload)
            return

        # quote lifecycle events
        if typ.startswith("quote_"):
            await self._handle_quote_event(typ, payload)
            return

        logger.debug("[STREAMING] RTDS rfq unhandled type=%s payload_keys=%s", typ, list(payload.keys()))

    @staticmethod
    def _normalize_rfq_request(payload: dict[str, Any]) -> dict[str, Any]:
        """
        Normalize RTDS RFQ Request payload into the shape expected by QuoteEngine.

        RTDS docs use camelCase (requestId, sizeIn, sizeOut). QuoteEngine currently
        expects `token`, `side`, and `size_in`/`size_out` (either base units or tokens).
        """
        return {
            "request_id": payload.get("requestId") or payload.get("request_id"),
            "token": payload.get("token"),
            "side": payload.get("side"),
            "size_in": payload.get("sizeIn") if "sizeIn" in payload else payload.get("size_in"),
            "size_out": payload.get("sizeOut") if "sizeOut" in payload else payload.get("size_out"),
            "market": payload.get("market") or payload.get("condition"),
            "expiry": payload.get("expiry"),
            "raw": payload,
        }

    async def _handle_request_event(self, typ: str, payload: dict[str, Any]) -> None:
        req = self._normalize_rfq_request(payload)
        request_id = req.get("request_id")
        token = req.get("token")

        if not request_id:
            logger.debug("[STREAMING] RTDS rfq %s missing request_id", typ)
            return

        if request_id in self._seen_requests:
            logger.debug("[STREAMING] Already processed request_id=%s", request_id)
            return
        self._seen_requests.add(request_id)

        logger.info("[STREAMING] RTDS rfq event=%s request_id=%s token=%s side=%s", typ, request_id, token, req.get("side"))

        snapshot = self._snapshot_fn()
        if not snapshot:
            logger.warning("[STREAMING] Betfair snapshot empty — cannot price request_id=%s", request_id)
            return

        price_quote = self._engine.price(req, snapshot)
        if price_quote is None:
            logger.info("[STREAMING] Declined RFQ request_id=%s (no quote produced)", request_id)
            return

        if self._dry_run:
            logger.info(
                "[STREAMING] DRY_RUN: would submit quote request_id=%s token=%s side=%s price=%.6f size=%.6f notional=%.4f",
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
        except Exception:
            logger.exception("[STREAMING] Error submitting quote for request_id=%s", request_id)
            return

        quote_id = result.get("quote_id") or result.get("quoteId")
        if not quote_id:
            logger.error("[STREAMING] Quote submission failed request_id=%s response=%s", request_id, result)
            return

        submission = QuoteSubmission(
            request_id=request_id,
            token_id=price_quote.token_id,
            price=price_quote.price,
            side=price_quote.side,
            size=price_quote.size,
            quote_id=quote_id,
            status="active",
        )
        self._active_quotes[quote_id] = submission
        self._engine.track_quote(price_quote.notional_usdc)

        logger.info(
            "[STREAMING] Quote submitted — quote_id=%s request_id=%s price=%.6f size=%.6f side=%s",
            quote_id,
            request_id,
            price_quote.price,
            price_quote.size,
            price_quote.side,
        )

    async def _handle_quote_event(self, typ: str, payload: dict[str, Any]) -> None:
        quote_id = payload.get("quoteId") or payload.get("quote_id")
        request_id = payload.get("requestId") or payload.get("request_id")
        state = payload.get("state")

        logger.info(
            "[STREAMING] RTDS rfq quote_event=%s quote_id=%s request_id=%s state=%s",
            typ,
            quote_id,
            request_id,
            state,
        )

        # Optional: if the websocket is emitting a state indicating we should approve, do so.
        # This is gated behind RFQ_APPROVE_ORDERS=true because it can execute real trades.
        if not self._approve_orders or self._dry_run:
            return

        # Only attempt approval if this quote is one we submitted.
        if quote_id and quote_id in self._active_quotes and state:
            # We don't know exact state strings from RTDS for "accepted" across all flows,
            # so we conservatively look for obvious substrings.
            normalized = str(state).upper()
            if "ACCEPT" in normalized:
                submission = self._active_quotes[quote_id]
                expiration = int(time.time()) + 3600
                try:
                    await asyncio.to_thread(
                        self._client.approve_order,
                        request_id=submission.request_id,
                        quote_id=quote_id,
                        expiration=expiration,
                    )
                    submission.status = "filled"
                    self._engine.release_quote(submission.notional_usdc)
                    logger.info("[STREAMING] Order approved — quote_id=%s request_id=%s", quote_id, submission.request_id)
                except Exception:
                    logger.exception("[STREAMING] Error approving order quote_id=%s", quote_id)
