"""Run the simulator: .venv/bin/python -m pnf_sim [--port 8766]"""

import argparse
import logging

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="NIFTY / SENSEX straddle Point & Figure simulator")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("pnf_sim.app:app", host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
