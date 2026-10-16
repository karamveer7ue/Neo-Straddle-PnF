"""FastAPI server for the index straddle Point & Figure screen."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import kite.web

from .engine import KEEP, Engine
from .neo import NeoError, NeoGateway
from .series import (
    DEFAULT_UNDERLYING,
    EXPIRY_ROLES,
    MAX_DAYS,
    MIN_DAYS,
    UNDERLYINGS,
    ExpiryRole,
    UnderlyingKey,
)
from .static_files import RevalidatingStaticFiles

STATIC = Path(__file__).parent / "static"

log = logging.getLogger(__name__)


def make_engine() -> Engine:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    missing = [k for k in ("CONSUMER_KEY", "MOBILE_NUMBER", "UCC", "MPIN") if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing from .env: {', '.join(missing)}")
    gateway = NeoGateway(
        consumer_key=os.environ["CONSUMER_KEY"],
        mobile_number=os.environ["MOBILE_NUMBER"],
        ucc=os.environ["UCC"],
        mpin=os.environ["MPIN"],
    )
    return Engine(gateway)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = make_engine()  # loads .env, which the Kite broker reads too
    app.state.broker = kite.web.make_broker()
    app.state.engine.start()
    app.state.broker.start()
    yield
    await app.state.broker.stop()
    await app.state.engine.stop()


app = FastAPI(title="Straddle P&F", lifespan=lifespan)
app.mount("/static", RevalidatingStaticFiles(directory=STATIC), name="static")
kite.web.install(app)  # order placement and positions through Zerodha Kite


class LoginBody(BaseModel):
    totp: str = Field(pattern=r"^\s*\d{6}\s*$")


class ConfigBody(BaseModel):
    underlying: UnderlyingKey = DEFAULT_UNDERLYING
    # An override strike for `underlying`, or None for the day's open. Leaving the
    # field out keeps that underlying's stored choice (used when switching index).
    atm: int | None = Field(default=None, ge=1000, le=1_000_000)
    days: int = Field(ge=MIN_DAYS, le=MAX_DAYS)
    # An expiry role `underlying` offers; leaving it out keeps its stored choice.
    expiry: ExpiryRole | None = None


class SignalAlert(BaseModel):
    key: str = Field(min_length=1, max_length=200)  # identifies the signal, for de-duplication
    type: Literal["buy", "sell"]
    underlying: UnderlyingKey
    offset: int
    strike: int
    expiry: str | None = Field(default=None, max_length=10)
    level: float  # the broken top (buy) or bottom (sell)
    price: float  # the breakout box
    barTs: int  # the 1-minute bar that confirmed the signal


class ErrorAlert(BaseModel):
    """An error a page showed (e.g. a Kite order not placed or rejected)."""

    key: str = Field(min_length=1, max_length=200)  # shared by pages showing the same error
    type: Literal["error"]
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=600)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/api/login")
async def login(body: LoginBody):
    try:
        await app.state.engine.login(body.totp.strip())
    except NeoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:400]) from exc
    except Exception as exc:
        log.exception("Login failed")
        raise HTTPException(status_code=502, detail=f"Login failed: {exc}"[:400]) from exc
    return {"ok": True}


@app.post("/api/config", status_code=202)
async def configure(body: ConfigBody):
    u = UNDERLYINGS[body.underlying]
    if body.atm is not None and body.atm % u.strike_step:
        raise HTTPException(status_code=422, detail=f"{u.key} ATM must be a multiple of {u.strike_step}")
    if body.expiry is not None and body.expiry not in u.expiry_roles:
        raise HTTPException(status_code=422, detail=f"{u.key} has no {EXPIRY_ROLES[body.expiry]} expiry")
    atm = body.atm if "atm" in body.model_fields_set else KEEP
    expiry = body.expiry if body.expiry is not None else KEEP
    app.state.engine.configure(body.underlying, body.days, expiry, atm=atm)
    return body.model_dump()


@app.post("/api/alerts")
async def log_alert(body: Annotated[SignalAlert | ErrorAlert, Field(discriminator="type")]):
    return {"logged": app.state.engine.log_alert(body.model_dump())}


@app.websocket("/ws")
async def updates(ws: WebSocket):
    engine: Engine = app.state.engine
    await ws.accept()
    queue = engine.subscribe()

    async def send_loop():
        while True:
            await ws.send_json(await queue.get())

    sender = asyncio.create_task(send_loop())
    try:
        while True:
            await ws.receive_text()  # client never sends; this just detects disconnects
    except WebSocketDisconnect:
        pass
    finally:
        engine.unsubscribe(queue)
        sender.cancel()
