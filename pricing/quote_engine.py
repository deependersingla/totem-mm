import logging
from typing import Any

from models.rfq import PriceQuote

logger = logging.getLogger(__name__)

# Polymarket conditional tokens and USDC on Polygon both use 6 decimal places.
_TOKEN_DECIMALS = 6


class QuoteEngineError(Exception):
    """Raised on misconfiguration or fatal pricing failures."""


class QuoteEngine:
    """Computes quotes for Polymarket RFQ requests using Betfair as the reference.

    Pricing model
    -------------
    1.  Mid-price from the Betfair book::

            mid = mean(1/back, 1/lay)   — falls back to whichever side is available.

    2.  Spread applied based on the side we are quoting::

            We SELL  (requester BUYs)  → price = mid + spread
            We BUY   (requester SELLs) → price = mid - spread

    3.  Size is the tightest of:
            * the size the requester asked for
            * ``max_quote_size_usdc``    (per-quote cap, converted to tokens)
            * ``available_exposure_usdc`` (portfolio-level cap, converted to tokens)

    Token mapping
    -------------
    ``token_map`` maps a Polymarket ``token_id`` → a Betfair ``selectionId``.
    Only tokens present in the map will be quoted; everything else is skipped.
    """

    DEFAULT_SPREAD: float = 0.03
    DEFAULT_MAX_QUOTE_SIZE_USDC: float = 100.0
    DEFAULT_MAX_EXPOSURE_USDC: float = 1_000.0

    def __init__(
        self,
        token_map: dict[str, int],
        spread: float | None = None,
        max_quote_size_usdc: float | None = None,
        max_exposure_usdc: float | None = None,
    ):
        if not token_map:
            raise QuoteEngineError("token_map must not be empty")

        self._token_map = token_map
        self._spread = spread if spread is not None else self.DEFAULT_SPREAD
        self._max_quote_size_usdc = (
            max_quote_size_usdc
            if max_quote_size_usdc is not None
            else self.DEFAULT_MAX_QUOTE_SIZE_USDC
        )
        self._max_exposure_usdc = (
            max_exposure_usdc
            if max_exposure_usdc is not None
            else self.DEFAULT_MAX_EXPOSURE_USDC
        )
        self._open_exposure_usdc: float = 0.0

    # ── exposure tracking ─────────────────────────────────────────

    @property
    def available_exposure_usdc(self) -> float:
        return max(0.0, self._max_exposure_usdc - self._open_exposure_usdc)

    def track_quote(self, notional_usdc: float) -> None:
        """Record open exposure when a quote is submitted."""
        self._open_exposure_usdc += notional_usdc

    def release_quote(self, notional_usdc: float) -> None:
        """Release exposure when a quote is filled or cancelled."""
        self._open_exposure_usdc = max(0.0, self._open_exposure_usdc - notional_usdc)

    # ── pricing ───────────────────────────────────────────────────

    def price(
        self,
        rfq_request: dict[str, Any],
        betfair_snapshot: dict[int, dict[str, Any]],
    ) -> PriceQuote | None:
        """Attempt to price an RFQ request.

        Returns ``None`` when the request cannot or should not be quoted
        (unknown token, stale Betfair data, no remaining capacity).
        """
        token_id = rfq_request.get("token")
        if not token_id:
            logger.debug("RFQ request missing 'token' field — skipping")
            return None

        selection_id = self._token_map.get(token_id)
        if selection_id is None:
            logger.debug("No Betfair mapping for token %s — skipping", token_id)
            return None

        odds = betfair_snapshot.get(selection_id)
        if not odds:
            logger.warning("No Betfair odds for selection %s (token=%s)", selection_id, token_id)
            return None

        mid = self._compute_mid(odds)
        if mid is None:
            logger.warning("Could not compute mid-price for selection %s", selection_id)
            return None

        requester_side = (rfq_request.get("side") or "").upper()
        if requester_side not in ("BUY", "SELL"):
            logger.warning("Unrecognised RFQ side '%s' — skipping", requester_side)
            return None

        our_side, quote_price = self._apply_spread(mid, requester_side)
        request_size_tokens = self._parse_request_size(rfq_request, requester_side)

        if request_size_tokens <= 0:
            logger.debug("Request size is zero for token %s", token_id)
            return None

        quote_size = self._cap_size(request_size_tokens, quote_price)
        if quote_size <= 0:
            logger.info(
                "No available capacity for token %s (exposure=%.2f / %.2f)",
                token_id,
                self._open_exposure_usdc,
                self._max_exposure_usdc,
            )
            return None

        logger.info(
            "Priced quote: token=%s our_side=%s price=%.6f size=%.4f "
            "(mid=%.6f betfair_back=%s betfair_lay=%s)",
            token_id,
            our_side,
            quote_price,
            quote_size,
            mid,
            odds.get("back"),
            odds.get("lay"),
        )

        return PriceQuote(
            token_id=token_id,
            price=quote_price,
            side=our_side,
            size=quote_size,
        )

    # ── internal helpers ──────────────────────────────────────────

    @staticmethod
    def _compute_mid(odds: dict[str, Any]) -> float | None:
        """Derive implied mid-probability from Betfair back/lay."""
        back = odds.get("back")
        lay = odds.get("lay")
        if back and lay:
            return (1.0 / back + 1.0 / lay) / 2.0
        if back:
            return 1.0 / back
        if lay:
            return 1.0 / lay
        return None

    def _apply_spread(self, mid: float, requester_side: str) -> tuple[str, float]:
        """Return *(our_side, quote_price)* with spread applied and price clamped to [0.01, 0.99]."""
        if requester_side == "BUY":
            our_side, raw = "SELL", mid + self._spread
        else:
            our_side, raw = "BUY", mid - self._spread
        return our_side, max(0.01, min(0.99, round(raw, 6)))

    @staticmethod
    def _parse_request_size(rfq_request: dict[str, Any], requester_side: str) -> float:
        """Extract the token quantity from the RFQ request.

        Polymarket sizes may arrive in either:
        - base units (6 decimals, e.g. "50000000" for 50.0 tokens), or
        - human token units (e.g. 50 or 50.0), depending on the API surface.

        BUY  → requester receives tokens → ``size_out`` is the token amount.
        SELL → requester sends tokens    → ``size_in``  is the token amount.
        """
        field = "size_out" if requester_side == "BUY" else "size_in"
        raw = rfq_request.get(field) or 0

        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.0

        # Heuristic: values in base units are typically large integers (>= 1e6).
        # If the value looks like a small number, treat it as already being in tokens.
        is_base_units = False
        if isinstance(raw, (int, float)):
            is_base_units = value >= 10**_TOKEN_DECIMALS
        elif isinstance(raw, str):
            if "." in raw:
                is_base_units = False
            else:
                is_base_units = value >= 10**_TOKEN_DECIMALS

        return value / (10**_TOKEN_DECIMALS) if is_base_units else value

    def _cap_size(self, request_tokens: float, price: float) -> float:
        """Return the largest quote size (in tokens) that fits within per-quote and portfolio limits."""
        if price <= 0:
            return 0.0
        max_by_quote = self._max_quote_size_usdc / price
        max_by_exposure = self.available_exposure_usdc / price
        return round(min(request_tokens, max_by_quote, max_by_exposure), 6)
