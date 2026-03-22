"""Entry point for the Polymarket trading simulator."""

import logging
import sys

import uvicorn

from .app_state import AppState
from .config import SERVER_HOST, SERVER_PORT
from .server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    state = AppState()
    app = create_app(state)

    port = SERVER_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    print(f"\n  Polymarket Trading Simulator")
    print(f"  Open http://localhost:{port} in your browser\n")

    uvicorn.run(app, host=SERVER_HOST, port=port, log_level="warning")


if __name__ == "__main__":
    main()
