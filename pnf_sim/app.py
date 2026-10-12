"""FastAPI server for the straddle simulator page.

A separate app (run with `python -m pnf_sim`) so the live page is never
affected. It reuses the live app's modules and static files read-only:
the gateway, the series helpers, and the P&F engine and chart renderer.
Only market data is needed, so it never logs in (no TOTP).
"""

import logging
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from pnf.neo import NeoError, NeoGateway
from pnf.series import UNDERLYINGS
from pnf.static_files import RevalidatingStaticFiles

from .replay import (
    EXPIRIES_OFFERED,
    MAX_RANGE_DAYS,
    SimError,
    SimRequest,
    build_run,
    default_dates,
    offered_expiries,
    today_ist,
    validate_dates,
)

ROOT = Path(__file__).resolve().parent.parent
LIVE_STATIC = ROOT / "pnf" / "static"  # shared, read-only: P&F engine, chart, alerts, styles
SIM_STATIC = Path(__file__).parent / "static"
RUN_CACHE_SIZE = 8  # recent runs kept, so replaying the same days is instant

log = logging.getLogger(__name__)


def make_gateway() -> NeoGateway:
    load_dotenv(ROOT / ".env")
    if not os.getenv("CONSUMER_KEY"):
        raise RuntimeError("CONSUMER_KEY missing from .env")
    # Market data needs only the consumer key; the simulator never logs in.
    return NeoGateway(consumer_key=os.environ["CONSUMER_KEY"], mobile_number="", ucc="", mpin="")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.gw = make_gateway()
    app.state.runs = OrderedDict()
    yield


app = FastAPI(title="Straddle simulator", lifespan=lifespan)
app.mount("/static", RevalidatingStaticFiles(directory=LIVE_STATIC), name="static")
app.mount("/sim", RevalidatingStaticFiles(directory=SIM_STATIC), name="sim")


class SimulateBody(BaseModel):
    underlying: Literal["NIFTY", "SENSEX"]
    expiry: date
    start: date
    end: date
    atm: int | None = Field(default=None, ge=1000, le=1_000_000)  # None: from the start day's open


@app.get("/")
async def page():
    return FileResponse(SIM_STATIC / "simulator.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/options")
async def options(underlying: Literal["NIFTY", "SENSEX"] = "NIFTY"):
    """What the form can offer: the next listed expiries and the date limits."""
    u = UNDERLYINGS[underlying]
    today = today_ist()
    try:
        expiries = offered_expiries(await app.state.gw.expiries(u), today)
    except NeoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
    start, end = default_dates(today)
    return {
        "underlying": u.to_json(),
        "underlyings": list(UNDERLYINGS),
        "today": today.isoformat(),
        "latestDate": (end).isoformat(),
        "defaultStart": start.isoformat(),
        "defaultEnd": end.isoformat(),
        "maxRangeDays": MAX_RANGE_DAYS,
        "expiries": [e.isoformat() for e in expiries],
        "expiriesOffered": EXPIRIES_OFFERED,
    }


@app.post("/api/simulate")
async def simulate(body: SimulateBody):
    u = UNDERLYINGS[body.underlying]
    today = today_ist()
    try:
        validate_dates(body.start, body.end, today)
        if body.atm is not None and body.atm % u.strike_step:
            raise SimError(f"{u.key} ATM must be a multiple of {u.strike_step}.")
        listed = offered_expiries(await app.state.gw.expiries(u), today)
        if body.expiry not in listed:
            raise SimError("Pick one of the listed expiries (expired contracts cannot be fetched).")
        key = (body.underlying, body.expiry, body.start, body.end, body.atm, today)
        runs: OrderedDict = app.state.runs
        if key not in runs:
            req = SimRequest(underlying=u, expiry=body.expiry, start=body.start, end=body.end, atm=body.atm)
            runs[key] = await build_run(app.state.gw, req)
            while len(runs) > RUN_CACHE_SIZE:
                runs.popitem(last=False)
        runs.move_to_end(key)
        return runs[key]
    except SimError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NeoError as exc:
        log.exception("Simulation data failed")
        raise HTTPException(status_code=502, detail=f"The market-data API failed: {exc}"[:400]) from exc
