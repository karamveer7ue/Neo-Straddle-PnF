"""HTTP and WebSocket endpoints for Kite trading, added to the P&F app by `install`.

    GET  /kite/login         -> redirect to Kite's login page
    GET  /callback           <- Kite's redirect after login: register https://127.0.0.1:<port>/callback
                                as the Kite app's redirect URL (/kite/callback works too)
    POST /api/kite/sell      {underlying, expiry, strike, quantity}
    POST /api/kite/exit      {group, fraction}
    POST /api/kite/logout
    WS   /kite/ws            position and order updates
    /kite/static/...         the trading UI (trading.js, trading.css)

The server listens on localhost only, but any web page open in the same
browser can still send requests to it. Every endpoint here therefore checks
that the request comes from this app's own page: the Host must be a
loopback name (a DNS-rebinding page has its own host name) and the Origin,
when the browser sends one, must be that same host.
"""

import asyncio
import logging
import os
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from pnf.series import UnderlyingKey
from pnf.static_files import RevalidatingStaticFiles

from .broker import DEFAULT_MAX_LOTS, DEFAULT_PRODUCT, KiteBroker
from .gateway import KiteError, KiteGateway, SessionExpired
from .order_relay import RelayAuth, check_settings

STATIC = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}

log = logging.getLogger(__name__)
router = APIRouter()


def make_broker() -> KiteBroker:
    """A broker from .env (already loaded by the app); unconfigured without the keys."""
    key, secret = os.getenv("KITE_API_KEY", "").strip(), os.getenv("KITE_API_SECRET", "").strip()
    product = os.getenv("KITE_PRODUCT", DEFAULT_PRODUCT).strip().upper()
    if product not in ("NRML", "MIS"):
        raise RuntimeError(f"KITE_PRODUCT must be NRML or MIS, not {product!r}")
    try:
        max_lots = int(os.getenv("KITE_MAX_LOTS", str(DEFAULT_MAX_LOTS)))
    except ValueError as exc:
        raise RuntimeError("KITE_MAX_LOTS must be a whole number") from exc
    if max_lots < 1:
        raise RuntimeError("KITE_MAX_LOTS must be at least 1")
    relay_url = os.getenv("KITE_ORDER_RELAY", "").strip()
    relay_secret = os.getenv("KITE_RELAY_SECRET", "").strip()
    order_auth = None
    if relay_url:  # orders go out through a server with a static IP (see kite/order_relay.py)
        check_settings(relay_url, relay_secret)
        order_auth = RelayAuth(relay_url, relay_secret)
        log.info("Kite orders go through the relay at %s", relay_url)
    gateway = KiteGateway(key, secret, order_auth=order_auth) if key and secret else None
    if gateway is None:
        log.info("Kite trading is off: KITE_API_KEY / KITE_API_SECRET not in .env")
    return KiteBroker(gateway, product=product, max_lots=max_lots)


def install(app: FastAPI) -> None:
    """Add the Kite routes and static files to `app`; app.state.broker must be set at startup."""
    app.include_router(router)
    app.mount("/kite/static", RevalidatingStaticFiles(directory=STATIC), name="kite-static")


def same_origin(host: str | None, origin: str | None) -> bool:
    """True for a request from this app's own page (see the module docstring)."""
    if not host:
        return False
    hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    if hostname not in LOOPBACK:
        return False
    if origin is None:
        return True  # same-origin GETs and top-level navigations may omit it
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and parts.netloc == host


def check_origin(request: Request) -> None:
    if not same_origin(request.headers.get("host"), request.headers.get("origin")):
        raise HTTPException(status_code=403, detail="Requests must come from the app's own page.")


def broker_of(request: Request) -> KiteBroker:
    check_origin(request)
    return request.app.state.broker


class SellBody(BaseModel):
    underlying: UnderlyingKey
    expiry: date
    strike: int = Field(ge=1000, le=1_000_000)
    quantity: int = Field(ge=1, le=1_000_000)


class ExitBody(BaseModel):
    group: str = Field(min_length=1, max_length=64)
    fraction: Literal[0.5, 1]


def failed(exc: KiteError) -> HTTPException:
    status = 401 if isinstance(exc, SessionExpired) else 400
    return HTTPException(status_code=status, detail=str(exc)[:400])


@router.get("/kite/login")
async def login(request: Request):
    broker = broker_of(request)
    if not broker.configured:
        raise HTTPException(
            status_code=503, detail="Kite is not set up: add KITE_API_KEY and KITE_API_SECRET to .env."
        )
    return RedirectResponse(broker.login_url(), status_code=303)


@router.get("/callback")
@router.get("/kite/callback")
async def callback(request: Request, request_token: str = "", status: str = "", state: str = ""):
    """Kite's redirect after login. Errors are shown on the page it returns to."""
    broker = broker_of(request)
    if status != "success" or not request_token:
        broker.report("Kite login was cancelled or failed - connect Kite again.")
    else:
        try:
            await broker.complete_login(request_token, state)
        except KiteError as exc:
            broker.report(f"Kite login failed: {exc}")
        except Exception as exc:
            log.exception("Kite login failed")
            broker.report(f"Kite login failed: {exc}")
    return RedirectResponse("/", status_code=303)


@router.post("/api/kite/sell")
async def sell(body: SellBody, request: Request):
    try:
        return await broker_of(request).sell(body.underlying, body.expiry, body.strike, body.quantity)
    except KiteError as exc:
        raise failed(exc) from exc


@router.post("/api/kite/exit")
async def exit_position(body: ExitBody, request: Request):
    try:
        return await broker_of(request).exit(body.group, float(body.fraction))
    except KiteError as exc:
        raise failed(exc) from exc


@router.post("/api/kite/logout")
async def logout(request: Request):
    await broker_of(request).logout()
    return {"ok": True}


@router.websocket("/kite/ws")
async def updates(ws: WebSocket):
    if not same_origin(ws.headers.get("host"), ws.headers.get("origin")):
        await ws.close(code=1008)
        return
    broker: KiteBroker = ws.app.state.broker
    await ws.accept()
    queue = broker.subscribe()

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
        broker.unsubscribe(queue)
        sender.cancel()
