"""Run the simulator: .venv/bin/python -m pnf_sim [--port 8766], served at https://127.0.0.1:8766"""

import argparse
import logging

import uvicorn

from pnf.tls import ensure_cert


def main() -> None:
    parser = argparse.ArgumentParser(description="Index straddle Point & Figure simulator")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cert, key = ensure_cert()  # the same certificate as the live app
    uvicorn.run(
        "pnf_sim.app:app",
        host="127.0.0.1",
        port=args.port,
        log_level="info",
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
    )


if __name__ == "__main__":
    main()
