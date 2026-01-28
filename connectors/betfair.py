import logging

import requests

import settings
from .base import BaseConnector

logger = logging.getLogger(__name__)


class BetfairConnector(BaseConnector):
    def __init__(self):
        super().__init__("betfair")
        self.app_key = settings.BETFAIR_CONFIG["APP_KEY"]
        self.session_token = settings.BETFAIR_CONFIG["SESSION_TOKEN"]
        self.rpc_url = "https://api.betfair.com/exchange/betting/json-rpc/v1"

    def _get_headers(self) -> dict:
        """Get headers for Betfair requests."""
        headers = {
            "content-type": "application/json",
            "X-Application": self.app_key,
            "X-Authentication": self.session_token,
        }
        return headers

    def _rpc_call(self, method: str, params: dict) -> dict:
        """Make rpc call to Betfair."""
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        resp = requests.post(
            self.rpc_url, json=payload, headers=self._get_headers(), timeout=10
        )
        data = resp.json()
        if "error" in data and data["error"] is not None:
            raise RuntimeError(data["error"])
        return data

    def _build_market_filter(self, eventTypeId: int, marketType: str) -> dict:
        """Build a market filter for Betfair."""
        return {"eventTypeIds": [str(eventTypeId)], "marketTypeCodes": [marketType]}

    def connect(self) -> bool:
        """Connect to Betfair."""
        try:
            self._rpc_call("SportsAPING/v1.0/listEventTypes", {"filter": {}})
            self.is_connected = True
            logger.debug("Betfair connector connected successfully.")
            return True
        except Exception:
            logger.error("Betfair connector failed to connect.")
            self.is_connected = False
            return False

    def disconnect(self) -> bool:
        """Disconnect from Betfair."""
        self.is_connected = False
        return True

    def get_markets(self) -> list:
        """Return all markets."""
        return self._get_markets_for_cricket()

    def _get_markets_for_cricket(self) -> list:
        """Return all cricket markets."""
        eventTypeId = 4
        return self._fetch_markets(eventTypeId=eventTypeId)

    def _fetch_markets(self, eventTypeId: int):
        """Fetch all markets by making rpc call to listMarketCatalogue endpoint."""
        try:
            params = {
                "filter": self._build_market_filter(eventTypeId, "MATCH_ODDS"),
                "maxResults": "100",
                "marketProjection": [
                    "EVENT",
                    "RUNNER_DESCRIPTION",
                    "MARKET_START_TIME",
                ],
            }
            response = self._rpc_call("SportsAPING/v1.0/listMarketCatalogue", params)
            return response.get("result", [])
        except Exception as e:
            logger.error(f"Error getting markets: {e}")
            return []

    def fetch_odds(self, market_id: str) -> list:
        """Fetch odds for a given market ID."""
        try:
            params = {
                "marketIds": [market_id],
                "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
            }
            response = self._rpc_call("SportsAPING/v1.0/listMarketBook", params)
            return response.get("result", [])
        except Exception as e:
            logger.error(f"Error fetching odds for market {market_id}: {e}")
            return []
