import json
import logging
import time
from typing import Any, Dict, Optional
import requests

from settings import BETFAIR_CONFIG as config

logger = logging.getLogger(__name__)


class BetfairAPIError(RuntimeError):
    """Exception raised for errors in the Betfair API."""

class BetfairTransportError(RuntimeError):
    """Exception raised for errors in the Betfair transport."""

class BetfairClient:

    """
    Thin transport-layer client for Betfair JSON-RPC APIs.
    """

    DEFAULT_TIMEOUT = 10
    DEFAULT_MAX_RETRIES = 3
    DEFAULT_RETRY_DELAY = 1

    def __init__(
        self, 
        app_key: Optional[str] = None, 
        session_token: Optional[str] = None,
        rpc_url: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
    ):
        self.app_key = app_key or config["APP_KEY"]
        self.session_token = session_token or config["SESSION_TOKEN"]
        self.rpc_url = rpc_url or config["RPC_URL"] or "https://api.betfair.com/exchange/betting/json-rpc/v1"
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        self._validate_config()

        self._session = requests.Session()
        self._session.headers.update(self._build_base_headers())

        logger.info("BetfairClient initialized (rpc_url=%s)", self.rpc_url)
        

    def _validate_config(self) -> None:
        if not self.app_key:
            raise RuntimeError("BETFAIR_CONFIG must set APP_KEY")
        if not self.session_token:
            raise RuntimeError("BETFAIR_CONFIG must set SESSION_TOKEN")
        if not self.rpc_url:
            raise RuntimeError("BETFAIR_CONFIG must set RPC_URL")


    def _build_base_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Application": self.app_key,
            "X-Authentication": self.session_token,
        }



    # PUBLIC API METHODS
    def call(
        self, 
        method: str, 
        params: Dict[str, Any],
        request_id: int = 1
    ) -> Dict[str, Any]:
        """Make a JSON-RPC call to the Betfair API."""

        payload = self._build_payload(method, params, request_id)
        self._log_request(method, payload)

        response = self._post(payload)

        self._log_response(method, response)

        self._validate_response(response)

        return response



    # INTERNALS

    def _build_payload(
        self,
        method: str,
        params: Dict[str, Any],
        request_id: Optional[int],
    )-> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": request_id or int(time.time() * 1000),
        }


    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        last_exception = None
        
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.post(
                    self.rpc_url,
                    json=payload,
                    timeout=self.timeout,
                )
                
                if not resp.ok:
                    logger.error(
                        "Betfair HTTP error %s: %s",
                        resp.status_code,
                        resp.text,
                    )
                    raise BetfairTransportError(
                        f"HTTP {resp.status_code}: {resp.text}"
                    )

                try:
                    return resp.json()
                except json.JSONDecodeError as exc:
                    logger.error("Invalid JSON response from Betfair: %s", resp.text)
                    raise BetfairTransportError("Invalid JSON response") from exc
                    
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                last_exception = exc
                if attempt < self.max_retries:
                    delay = self.retry_delay * (2 ** attempt)
                    logger.warning(
                        "Betfair connection error (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                        str(exc),
                    )
                    time.sleep(delay)
                    continue
                else:
                    logger.error(
                        "Betfair transport error after %d attempts",
                        self.max_retries + 1,
                    )
                    raise BetfairTransportError(str(exc)) from exc
                    
            except requests.RequestException as exc:
                logger.exception("Betfair transport error")
                raise BetfairTransportError(str(exc)) from exc
        
        if last_exception:
            raise BetfairTransportError(str(last_exception)) from last_exception


    def _validate_response(self, response: Dict[str, Any]) -> None:
        """
        Normalize Betfair API errors into Python exceptions.
        """
        if "error" in response and response["error"]:
            logger.error("Betfair API error: %s", response["error"])
            raise BetfairAPIError(response["error"])

        if "result" in response and isinstance(response["result"], dict):
            exception = response["result"].get("exception")
            if exception:
                logger.error("Betfair APINGException: %s", exception)
                raise BetfairAPIError(exception)


    # LOGGING METHODS

    def _log_request(self, method: str, payload: Dict[str, Any]) -> None:
        logger.debug(
            "Betfair RPC REQUEST method=%s payload=%s",
            method,
            json.dumps(payload, separators=(",", ":")),
        )

    def _log_response(self, method: str, response: Dict[str, Any]) -> None:
        logger.debug(
            "Betfair RPC RESPONSE method=%s response=%s",
            method,
            json.dumps(response, separators=(",", ":")),
        )
        

