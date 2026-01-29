import logging

import requests

from settings import ODDSAPI_CONFIG as config

logger = logging.getLogger(__name__)


class OddsAPIConnector:
    def __init__(self):
        self.host = "https://api.the-odds-api.com/v4"
        self.api_key = config.get("API_KEY")
        if not self.api_key:
            raise ValueError("ODDSAPI_CONFIG must set API_KEY")

    def _request(self, path: str, params: dict | None = None) -> dict | list:
        url = f"{self.host}{path}"
        if params is None:
            params = {}
        params.setdefault("apiKey", self.api_key)
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            logger.warning("Odds API rate limit...")
        resp.raise_for_status()
        return resp.json()

    def get_sports(self, all_sports: bool = False) -> list:
        path = "/sports/"
        params = {}
        if all_sports:
            params["all"] = "true"
        return self._request(path, params) if params else self._request(path)

    def get_events(self, sport_key: str) -> list:
        path = f"/sports/{sport_key}/events"
        return self._request(path)

    def get_odds(
        self,
        sport_key: str,
        regions: str = "uk",
        markets: str = "h2h",
        odds_format: str = "decimal",
    ) -> list:
        path = f"/sports/{sport_key}/odds"
        params = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format,
        }
        return self._request(path, params)

    def get_event_odds(
        self,
        sport_key: str,
        event_id: str,
        regions: str = "uk",
        markets: str = "h2h",
        odds_format: str = "decimal",
    ) -> dict:
        path = f"/sports/{sport_key}/events/{event_id}/odds"
        params = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format,
        }
        return self._request(path, params)
