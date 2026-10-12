"""FastAPI server for the NIFTY straddle Point & Figure screen."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .engine import KEEP, Engine
from .neo import NeoError, NeoGateway
from .series import DEFAULT_EXPIRY_ROLE, DEFAULT_UNDERLYING, MAX_DAYS, MIN_DAYS, UNDERLYINGS

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
    app.state.engine = make_engine()
    app.state.engine.start()
    yield
    await app.state.engine.stop()


app = FastAPI(title="NIFTY straddle P&F", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class LoginBody(BaseModel):
    totp: str = Field(pattern=r"^\s*\d{6}\s*$")


class ConfigBody(BaseModel):
    underlying: Literal["NIFTY", "SENSEX"] = DEFAULT_UNDERLYING
    # An override strike for `underlying`, or None for the day's open. Leaving the
    # field out keeps that underlying's stored choice (used when switching index).
    atm: int | None = Field(default=None, ge=1000, le=1_000_000)
    days: int = Field(ge=MIN_DAYS, le=MAX_DAYS)
    expiry: Literal["week1", "week2", "month1", "month2"] = DEFAULT_EXPIRY_ROLE


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
    step = UNDERLYINGS[body.underlying].strike_step
    if body.atm is not None and body.atm % step:
        raise HTTPException(status_code=422, detail=f"{body.underlying} ATM must be a multiple of {step}")
    atm = body.atm if "atm" in body.model_fields_set else KEEP
    app.state.engine.configure(body.underlying, body.days, body.expiry, atm=atm)
    return body.model_dump()


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
