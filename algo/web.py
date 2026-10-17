"""HTTP and WebSocket endpoints for the algo, added to the P&F app by `install`.

    POST /api/algo/on        {expect}: switch On; `expect` is what the page showed for
                             confirmation (index, expiry, strikes, mode, quantity...),
                             refused (409) if anything changed meanwhile
    POST /api/algo/off       switch Off: open algo trades are left to you
    POST /api/algo/settings  {changes}: the Algo menu's settings (422 with {errors} per field)
    POST /api/algo/exit-all  buy back every open algo trade at once, and switch Off
    POST /api/algo/exit      {trade}: buy back one algo trade now
    WS   /algo/ws            the algo's state whenever it changes, and its events
    /algo/static/...         the Algo menu and its chart marks (algo.js, algo.css)

Like the Kite endpoints, every one accepts only requests from the app's own page
(loopback Host, same-origin Origin): other sites must not reach them.
"""

import asyncio
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from pnf.static_files import RevalidatingStaticFiles
from pnf.web_guard import same_origin

from .core import AlgoError, SettingsError
from .runner import AlgoRunner

STATIC = Path(__file__).parent / "static"
router = APIRouter()


def install(app: FastAPI) -> None:
    """Add the routes and static files; app.state.algo must be set at startup."""
    app.include_router(router)
    app.mount("/algo/static", RevalidatingStaticFiles(directory=STATIC), name="algo-static")


def runner_of(request: Request) -> AlgoRunner:
    if not same_origin(request.headers.get("host"), request.headers.get("origin")):
        raise HTTPException(status_code=403, detail="Requests must come from the app's own page.")
    return request.app.state.algo


class OnBody(BaseModel):
    expect: dict | None = None


class SettingsBody(BaseModel):
    changes: dict


class ExitBody(BaseModel):
    trade: str = Field(min_length=1, max_length=40)


def refused(exc: AlgoError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc)[:400])


@router.post("/api/algo/on")
async def switch_on(body: OnBody, request: Request):
    runner = runner_of(request)
    try:
        runner.switch_on(body.expect)
    except AlgoError as exc:
        raise refused(exc) from exc
    return runner.state_json()


@router.post("/api/algo/off")
async def switch_off(request: Request):
    runner = runner_of(request)
    runner.switch_off()
    return runner.state_json()


@router.post("/api/algo/settings")
async def settings(body: SettingsBody, request: Request):
    runner = runner_of(request)
    try:
        runner.update_settings(body.changes)
    except SettingsError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc
    return runner.state_json()


@router.post("/api/algo/exit-all")
async def exit_all(request: Request):
    runner = runner_of(request)
    runner.exit_all()
    return runner.state_json()


@router.post("/api/algo/exit")
async def exit_trade(body: ExitBody, request: Request):
    runner = runner_of(request)
    try:
        runner.exit_trade(body.trade)
    except AlgoError as exc:
        raise refused(exc) from exc
    return runner.state_json()


@router.websocket("/algo/ws")
async def updates(ws: WebSocket):
    if not same_origin(ws.headers.get("host"), ws.headers.get("origin")):
        await ws.close(code=1008)
        return
    runner: AlgoRunner = ws.app.state.algo
    await ws.accept()
    queue = runner.subscribe()

    async def send_loop():
        while True:
            await ws.send_json(await queue.get())

    sender = asyncio.create_task(send_loop())
    try:
        while True:
            await ws.receive_text()  # the page never sends; this just detects disconnects
    except WebSocketDisconnect:
        pass
    finally:
        runner.unsubscribe(queue)
        sender.cancel()
