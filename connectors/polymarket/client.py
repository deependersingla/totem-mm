import logging

from py_clob_client.client import ClobClient
from py_clob_client.rfq import (
    ApproveOrderParams,
    CancelRfqQuoteParams,
    GetRfqQuotesParams,
    GetRfqRequestsParams,
    RfqUserQuote,
)

import settings

logger = logging.getLogger(__name__)


class PolymarketClientError(Exception):
    """Raised when the Polymarket client encounters an initialisation or transport error."""


class PolymarketClient:
    """Thin wrapper around py_clob_client for Polymarket CLOB RFQ operations.

    All methods are synchronous — callers (typically AsyncRFQListener) should
    offload them to a thread via ``asyncio.to_thread``.
    """

    DEFAULT_HOST = "https://clob.polymarket.com"
    DEFAULT_CHAIN_ID = 137  # Polygon mainnet

    def __init__(
        self,
        host: str | None = None,
        chain_id: int | None = None,
        private_key: str | None = None,
        signature_type: int | None = None,
        funder: str | None = None,
    ):
        config = settings.POLYMARKET_CONFIG

        self._host = host or self.DEFAULT_HOST
        self._chain_id = chain_id or self.DEFAULT_CHAIN_ID
        self._private_key = private_key or config.get("PRIVATE_KEY")
        self._signature_type = (
            signature_type if signature_type is not None else config.get("SIGNATURE_TYPE", 1)
        )
        self._funder = funder or config.get("ADDRESS")

        self._validate_config()
        self._client = self._initialise_client()

        logger.info(
            "PolymarketClient initialised (host=%s, chain_id=%d)",
            self._host,
            self._chain_id,
        )

    # ── initialisation ────────────────────────────────────────────

    def _validate_config(self) -> None:
        missing = [
            k
            for k, v in {"PRIVATE_KEY": self._private_key, "ADDRESS": self._funder}.items()
            if not v
        ]
        if missing:
            raise PolymarketClientError(f"Missing Polymarket config: {missing}")

    def _initialise_client(self) -> ClobClient:
        client = ClobClient(
            host=self._host,
            chain_id=self._chain_id,
            key=self._private_key,
            signature_type=self._signature_type,
            funder=self._funder,
        )

        creds = client.create_or_derive_api_creds()
        if not creds:
            raise PolymarketClientError("Failed to create or derive API credentials")
        client.set_api_creds(creds)

        if not client.get_ok():
            raise PolymarketClientError("Polymarket CLOB health check failed")

        return client

    # ── RFQ request polling ───────────────────────────────────────

    def get_pending_requests(
        self,
        markets: list[str] | None = None,
        size_min: float | None = None,
        size_max: float | None = None,
        limit: int | None = None,
    ) -> dict:
        """Fetch currently active RFQ requests, optionally filtered by market / size."""
        params = GetRfqRequestsParams(
            state="active",
            markets=markets,
            size_min=size_min,
            size_max=size_max,
            limit=limit,
        )
        return self._client.rfq.get_rfq_requests(params)

    # ── quote lifecycle ───────────────────────────────────────────

    def submit_quote(
        self,
        request_id: str,
        token_id: str,
        price: float,
        side: str,
        size: float,
    ) -> dict:
        """Submit a quote in response to an RFQ request.

        Returns the raw API response; check for ``quote_id`` on success or
        ``error`` on failure.
        """
        quote = RfqUserQuote(
            request_id=request_id,
            token_id=token_id,
            price=price,
            side=side,
            size=size,
        )
        return self._client.rfq.create_rfq_quote(quote)

    def cancel_quote(self, quote_id: str) -> str:
        """Cancel a previously submitted quote."""
        return self._client.rfq.cancel_rfq_quote(CancelRfqQuoteParams(quote_id=quote_id))

    def approve_order(self, request_id: str, quote_id: str, expiration: int) -> str:
        """Approve an order after the requester has accepted our quote."""
        return self._client.rfq.approve_rfq_order(
            ApproveOrderParams(
                request_id=request_id,
                quote_id=quote_id,
                expiration=expiration,
            )
        )

    # ── quote monitoring ──────────────────────────────────────────

    def get_my_quotes(
        self,
        state: str | None = None,
        request_ids: list[str] | None = None,
    ) -> dict:
        """Retrieve quotes we have submitted, optionally filtered by state / request."""
        params = GetRfqQuotesParams(state=state, request_ids=request_ids)
        return self._client.rfq.get_rfq_quoter_quotes(params)
