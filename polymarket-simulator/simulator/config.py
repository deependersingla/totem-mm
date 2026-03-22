"""Configuration for the simulator."""

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DATA_API = "https://data-api.polymarket.com"

# Simulator defaults
DEFAULT_CASH = 10_000.0
WS_PING_INTERVAL = 10  # seconds
WS_RECONNECT_BASE = 2  # seconds
WS_RECONNECT_MAX = 30  # seconds
BOOK_BROADCAST_INTERVAL = 0.1  # seconds — throttle UI updates
SNIPE_THRESHOLD_MS = 500  # max ms for a flash order to be flagged
SNIPE_BUFFER_SIZE = 500
EVENT_LOG_SIZE = 500
MAX_FILL_FRACTION_WARN = 0.5  # warn if simulated fill > 50% of level
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8899
