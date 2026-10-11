"""Run the server: .venv/bin/python -m pnf [--port 8765]"""

import argparse
import logging

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="NIFTY straddle Point & Figure server")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Bound to localhost only: the server holds a trading session.
    uvicorn.run("pnf.app:app", host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
