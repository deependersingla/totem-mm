import json
import os

import dotenv

dotenv.load_dotenv()

BETFAIR_CONFIG = {
    "APP_KEY": os.environ.get("BETFAIR_APP_KEY"),
    "SESSION_TOKEN": os.environ.get("BETFAIR_SESSION_TOKEN"),
    "RPC_URL": os.environ.get("BETFAIR_RPC_URL"),
}

POLYMARKET_CONFIG = {
    "ADDRESS": os.environ.get("POLYMARKET_ADDRESS"),
    "PRIVATE_KEY": os.environ.get("POLYMARKET_PRIVATE_KEY"),
    "SIGNATURE_TYPE": int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1")),
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
