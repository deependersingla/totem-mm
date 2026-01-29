import logging

from py_clob_client.client import ClobClient
from py_clob_client.rfq import (
    RfqUserQuote,
    GetRfqRequestsParams,
    GetRfqQuotesParams,
    GetRfqBestQuoteParams,
    ApproveOrderParams,
    CancelRfqQuoteParams,
)

from settings import POLYMARKET_CONFIG as config

logger = logging.getLogger(__name__)


class PolymarketConnector:
    def __init__(self):
        self.host = "https://clob.polymarket.com"
        self.chain_id = 137
        self.private_key = config["PRIVATE_KEY"]
        self.signature_type = config.get("SIGNATURE_TYPE", 1)
        self.funder = config.get("ADDRESS")

        self._client = ClobClient(
            host=self.host,
            chain_id=self.chain_id,
            key=self.private_key,
            signature_type=self.signature_type,
            funder=self.funder,
        )
        api_creds = self._client.create_or_derive_api_key()
        self._client.set_api_creds(api_creds)

        if not self._client.get_ok():
            logger.error(f"Failed to initialize Polymarket client")
            raise RuntimeError("Failed to initialize Polymarket client")

    def get_requests(
        self,
        offset: str | None = None,
        limit: int | None = None,
        state: str | None = None,
        request_ids: list[str] | None = None,
        markets: list[str] | None = None,
    ) -> dict:
        params = GetRfqRequestsParams(
            offset=offset,
            limit=limit,
            state=state,
            request_ids=request_ids,
            markets=markets,
        )
        return self._client.rfq.get_rfq_requests(params)

    def create_quote(
        self,
        request_id: str,
        token_id: str,
        price: float,
        side: str,
        size: float,
    ) -> dict:
        user_quote = RfqUserQuote(
            request_id=request_id,
            token_id=token_id,
            price=price,
            side=side,
            size=size,
        )
        return self._client.rfq.create_rfq_quote(user_quote)

    def approve_order(self, request_id: str, quote_id: str, expiration: int) -> dict:
        params = ApproveOrderParams(
            request_id=request_id,
            quote_id=quote_id,
            expiration=expiration,
        )
        return self._client.rfq.approve_rfq_order(params)

    def get_quoter_quotes(
        self,
        offset: str | None = None,
        limit: int | None = None,
        state: str | None = None,
        quote_ids: list[str] | None = None,
        request_ids: list[str] | None = None,
        markets: list[str] | None = None,
    ) -> dict:
        params = GetRfqQuotesParams(
            offset=offset,
            limit=limit,
            state=state,
            quote_ids=quote_ids,
            request_ids=request_ids,
            markets=markets,
        )
        return self._client.rfq.get_rfq_quoter_quotes(params)

    def get_best_quote(self, request_id: str) -> dict:
        params = GetRfqBestQuoteParams(request_id=request_id)
        return self._client.rfq.get_rfq_best_quote(params)

    def cancel_quote(self, quote_id: str) -> str:
        return self._client.rfq.cancel_rfq_quote(
            CancelRfqQuoteParams(quote_id=quote_id)
        )
