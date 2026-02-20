import json
import os

import dotenv

dotenv.load_dotenv()

BETFAIR_CONFIG = {
    "APP_KEY": os.environ.get("BETFAIR_APP_KEY"),
    "SESSION_TOKEN": os.environ.get("BETFAIR_SESSION_TOKEN"),
    "RPC_URL": os.environ.get("BETFAIR_RPC_URL"),
    # Streaming API credentials (optional - if not set, falls back to polling)
    "USERNAME": os.environ.get("BETFAIR_USERNAME"),
    "PASSWORD": os.environ.get("BETFAIR_PASSWORD"),
    # SSL certs for Betfair login (streaming). Directory with .crt+.key or single .pem path.
    # If unset, betfairlightweight defaults to /certs (often missing); set to e.g. ./certs or full path.
    "CERTS": os.environ.get("BETFAIR_CERTS"),
    "CERT_FILE": os.environ.get("BETFAIR_CERT_FILE"),
}

POLYMARKET_CONFIG = {
    "ADDRESS": os.environ.get("POLYMARKET_ADDRESS"),
    "PRIVATE_KEY": os.environ.get("POLYMARKET_PRIVATE_KEY"),
    "SIGNATURE_TYPE": int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1")),
}

POLYMARKET_RTDS_CONFIG = {
    # Polymarket Real-Time Data Streaming (RTDS) websocket used for topics like "rfq".
    # Ref: https://github.com/Polymarket/real-time-data-client
    "WS_URL": os.environ.get("POLYMARKET_RTDS_WS_URL", "wss://ws-live-data.polymarket.com"),
    "PING_INTERVAL_SECONDS": float(os.environ.get("POLYMARKET_RTDS_PING_INTERVAL_SECONDS", "5")),
    "RECONNECT_DELAY_SECONDS": float(os.environ.get("POLYMARKET_RTDS_RECONNECT_DELAY_SECONDS", "5")),
    # If true, log full raw websocket messages (noisy).
    "LOG_RAW_MESSAGES": os.environ.get("POLYMARKET_RTDS_LOG_RAW_MESSAGES", "false").lower()
    in ("1", "true", "yes", "y"),
}

# Safety switches for RFQ execution (applies to both polling + streaming listeners if used there).
RFQ_EXECUTION_CONFIG = {
    # If true, the bot will NOT submit quotes or approve orders; it will only log decisions.
    # DEFAULT: true (safe mode - no trades, just logs)
    "DRY_RUN": os.environ.get("RFQ_DRY_RUN", "true").lower() in ("1", "true", "yes", "y"),
    # If true, the bot may approve accepted RFQ orders (this can lead to real trades).
    # DEFAULT: false (never approve unless explicitly enabled)
    "APPROVE_ORDERS": os.environ.get("RFQ_APPROVE_ORDERS", "false").lower()
    in ("1", "true", "yes", "y"),
}


ODDSAPI_CONFIG = {"API_KEY": os.environ.get("ODDSAPI_API_KEY")}

# Mapping: Polymarket token_id (str) → Betfair selectionId (int)
# Set via TOKEN_MAP env var as a JSON object, e.g.:
#   TOKEN_MAP='{"token_abc_yes": 12345, "token_abc_no": 12346}'
TOKEN_MAP: dict[str, int] = json.loads(os.environ.get("TOKEN_MAP", "{}"))

# RFQ system tunables — all overridable via environment variables
RFQ_CONFIG = {
    "POLL_INTERVAL": float(os.environ.get("RFQ_POLL_INTERVAL", "5")),
    "QUOTE_TTL": float(os.environ.get("RFQ_QUOTE_TTL", "300")),
    "SPREAD": float(os.environ.get("RFQ_SPREAD", "0.03")),
    "MAX_QUOTE_SIZE_USDC": float(os.environ.get("RFQ_MAX_QUOTE_SIZE_USDC", "100")),
    "MAX_EXPOSURE_USDC": float(os.environ.get("RFQ_MAX_EXPOSURE_USDC", "1000")),
}
