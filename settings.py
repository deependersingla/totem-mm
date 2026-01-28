import os

import dotenv

dotenv.load_dotenv()

BETFAIR_CONFIG = {
    "APP_KEY": os.environ.get("BETFAIR_APP_KEY"),
    "SESSION_TOKEN": os.environ.get("BETFAIR_SESSION_TOKEN"),
}

POLYMARKET_CONFIG = {
    "API_KEY": os.environ.get("POLYMARKET_API_KEY"),
    "SECRET": os.environ.get("POLYMARKET_SECRET"),
    "PASSPHRASE": os.environ.get("POLYMARKET_PASSPHRASE"),
    "ADDRESS": os.environ.get("POLYMARKET_ADDRESS"),
}
