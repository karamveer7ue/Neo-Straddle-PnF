"""FastAPI server for the straddle simulator page.

A separate app (run with `python -m pnf_sim`) so the live page is never
affected. It reuses the live app's modules and static files read-only:
the gateway, the series helpers, and the P&F engine and chart renderer.
Only market data is needed, so it never logs in (no TOTP).
"""

import asyncio
import logging
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from algo.core import Settings, SettingsError, quantity_problem
from algo.sim import simulate as simulate_algo
from pnf.neo import NeoError, NeoGateway
from pnf.series import DEFAULT_UNDERLYING, UNDERLYINGS, UnderlyingKey
from pnf.static_files import RevalidatingStaticFiles
from pnf.web_guard import LocalOnly

from .replay import (
    MAX_RANGE_DAYS,
    SimError,
    SimRequest,
    build_run,
    default_dates,
    latest_session,
    offered_expiries,
    today_ist,
    validate_dates,
)

ROOT = Path(__file__).resolve().parent.parent
LIVE_STATIC = ROOT / "pnf" / "static"  # shared, read-only: P&F engine, chart, alerts, styles
ALGO_STATIC = ROOT / "algo" / "static"  # shared, read-only: the Algo menu and chart marks
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
    app.state.building = {}  # key -> asyncio.Task, so identical requests share one build
    yield


app = FastAPI(title="Straddle simulator", lifespan=lifespan)
app.add_middleware(LocalOnly)  # other sites' pages: no requests, WebSockets or framing
app.mount("/static", RevalidatingStaticFiles(directory=LIVE_STATIC), name="static")
app.mount("/algo/static", RevalidatingStaticFiles(directory=ALGO_STATIC), name="algo")
app.mount("/sim", RevalidatingStaticFiles(directory=SIM_STATIC), name="sim")


class SimulateBody(BaseModel):
    underlying: UnderlyingKey
    expiry: date
    start: date
    end: date
    atm: int | None = Field(default=None, ge=1000, le=1_000_000)  # None: from the start day's open


@app.get("/")
async def page():
    return FileResponse(SIM_STATIC / "simulator.html", headers={"Cache-Control": "no-cache"})


_latest_sessions: dict[tuple[str, date], date] = {}  # (index, today) -> newest session before today


@app.get("/api/options")
async def options(underlying: UnderlyingKey = DEFAULT_UNDERLYING):
    """What the form can offer: the next listed expiries and the date limits."""
    u = UNDERLYINGS[underlying]
    today = today_ist()
    try:
        expiries = offered_expiries(await app.state.gw.expiries(u), today, u)
    except NeoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
    key = (u.key, today)
    latest = _latest_sessions.get(key)
    if latest is None:  # one index history request per index and day, while it answers
        latest = await latest_session(app.state.gw, u, today)
        for old in [k for k in _latest_sessions if k[1] != today]:
            del _latest_sessions[old]
        if latest is not None:
            _latest_sessions[key] = latest
    start, end = default_dates(today, latest)
    return {
        "underlying": u.to_json(),
        "underlyings": list(UNDERLYINGS),
        "today": today.isoformat(),
        "latestDate": (end).isoformat(),
        "defaultStart": start.isoformat(),
        "defaultEnd": end.isoformat(),
        "maxRangeDays": MAX_RANGE_DAYS,
        "expiries": [e.isoformat() for e in expiries],
        "algo": {
            "settings": Settings().to_json(),
            "caps": {k: v.max_sell_qty for k, v in UNDERLYINGS.items()},
            "defaults": {k: {"qty": v.algo_qty, "target": v.algo_target} for k, v in UNDERLYINGS.items()},
        },
    }


@app.post("/api/simulate")
async def simulate(body: SimulateBody):
    u = UNDERLYINGS[body.underlying]
    today = today_ist()
    key = (body.underlying, body.expiry, body.start, body.end, body.atm, today)
    runs: OrderedDict = app.state.runs
    try:
        if key in runs:  # validated when it was built (the key includes today)
            runs.move_to_end(key)
            return runs[key]
        task = app.state.building.get(key)
        if task is None:
            validate_dates(body.start, body.end, today)
            if body.atm is not None and body.atm % u.strike_step:
                raise SimError(f"{u.key} ATM must be a multiple of {u.strike_step}.")
            listed = offered_expiries(await app.state.gw.expiries(u), today, u)
            if body.expiry not in listed:
                raise SimError("Pick one of the listed expiries (expired contracts cannot be fetched).")
            task = app.state.building.get(key)  # another request may have started it meanwhile
            if task is None:
                req = SimRequest(
                    underlying=u, expiry=body.expiry, start=body.start, end=body.end, atm=body.atm
                )
                task = start_build(key, req)
        # shield: one caller going away must not cancel a build others (or the cache) want
        return await asyncio.shield(task)
    except SimError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NeoError as exc:
        raise HTTPException(status_code=502, detail=f"The market-data API failed: {exc}"[:400]) from exc


class AlgoBody(BaseModel):
    run: str | None = Field(default=None, max_length=200)  # the run's key; None: just check the settings
    settings: dict = Field(default_factory=dict)  # the Algo menu's (JSON names)
    startAt: int | None = None  # when the algo was switched On in the replay (epoch seconds)


@app.post("/api/algo")
async def algo(body: AlgoBody):
    """Every algo trade of a loaded run with these settings (algo/sim.py), or, without a
    run, the settings checked and completed."""
    try:
        settings = Settings().updated(body.settings)
    except SettingsError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc
    if body.run is None:
        return {"settings": settings.to_json()}
    run = next((r for r in app.state.runs.values() if r.get("key") == body.run), None)
    if run is None:
        raise HTTPException(status_code=409, detail="This run is no longer loaded: press Load again.")
    u = run["underlying"]["key"]
    problem = quantity_problem(u, settings.qty[u], run["lotSize"])
    if problem:
        raise HTTPException(status_code=422, detail={"errors": {f"qty.{u}": problem}})
    # Up to a few seconds of work for a long run: off the event loop.
    return await asyncio.to_thread(simulate_algo, run, settings, body.startAt)


def start_build(key: tuple, req: SimRequest) -> asyncio.Task:
    """Build a run once for everyone asking, and cache it even if they all went away."""
    building: dict = app.state.building

    async def build():
        try:
            run = await build_run(app.state.gw, req)
        except NeoError:
            log.exception("Simulation data failed")  # once, however many requests wait on it
            raise
        except SimError:
            raise
        except Exception as exc:  # e.g. an odd API row: a readable error, not a bare 500
            log.exception("Simulation build failed")
            raise NeoError(f"unexpected reply ({type(exc).__name__}: {exc})") from exc
        finally:
            building.pop(key, None)
        run["key"] = "|".join(map(str, key))  # how the page names this run to /api/algo
        runs: OrderedDict = app.state.runs
        runs[key] = run
        while len(runs) > RUN_CACHE_SIZE:
            runs.popitem(last=False)
        return run

    task = building[key] = asyncio.create_task(build())
    return task
