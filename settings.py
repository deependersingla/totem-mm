import os

import dotenv

dotenv.load_dotenv()

BETFAIR_CONFIG = {
    "APP_KEY": os.environ.get("BETFAIR_APP_KEY"),
    "SESSION_TOKEN": os.environ.get("BETFAIR_SESSION_TOKEN"),
}

POLYMARKET_CONFIG = {
    "ADDRESS": os.environ.get("POLYMARKET_ADDRESS"),
    "PRIVATE_KEY": os.environ.get("POLYMARKET_PRIVATE_KEY"),
    "SIGNATURE_TYPE": int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1")),
}


ODDSAPI_CONFIG = {"API_KEY": os.environ.get("ODDSAPI_API_KEY")}
