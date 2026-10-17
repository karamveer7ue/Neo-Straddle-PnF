"""The guard that keeps other sites' pages out of both local servers."""

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import pnf.app
import pnf_sim.app
from pnf.web_guard import LocalOnly

HOST = "127.0.0.1:8765"
OWN = f"https://{HOST}"


@pytest.fixture
def http():
    app = FastAPI()
    app.add_middleware(LocalOnly)

    @app.get("/page")
    async def page():
        return {"ok": True}

    @app.post("/act")
    async def act():
        return {"done": True}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_json({"hello": True})
        await socket.close()

    return TestClient(app, base_url=OWN)


def test_both_apps_are_guarded():
    for app in (pnf.app.app, pnf_sim.app.app):
        assert any(m.cls is LocalOnly for m in app.user_middleware)


def test_every_response_forbids_framing_and_carries_a_strict_csp(http):
    res = http.get("/page")
    assert res.status_code == 200
    assert res.headers["x-frame-options"] == "DENY"
    csp = res.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp and f"wss://{HOST}" in csp
    assert res.headers["x-content-type-options"] == "nosniff"


def test_requests_from_other_sites_or_hosts_are_refused(http):
    assert http.get("/page", headers={"host": "rebind.evil.example"}).status_code == 403  # DNS rebinding
    assert http.post("/act", headers={"origin": "https://evil.example"}).status_code == 403
    assert http.post("/act", headers={"origin": "https://127.0.0.1:8766"}).status_code == 403  # the simulator
    assert http.post("/act", headers={"origin": OWN}).status_code == 200
    # The Host is quoted in the CSP: only a plain loopback host and port pass.
    bad_host = "127.0.0.1:1; script-src * 'unsafe-inline'"
    assert http.get("/page", headers={"host": bad_host}).status_code == 403
    assert http.post("/act", headers={"origin": OWN.replace("https", "http")}).status_code == 403
    # A browser that says the request comes from another site is refused, whatever the Origin.
    assert http.post("/act", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert http.post("/act", headers={"sec-fetch-site": "same-site"}).status_code == 403
    assert http.post("/act", headers={"origin": OWN, "sec-fetch-site": "same-origin"}).status_code == 200
    # Top-level navigations (e.g. Kite's redirect back after login) arrive cross-site as GETs.
    assert http.get("/page", headers={"sec-fetch-site": "cross-site"}).status_code == 200


def test_websockets_from_other_sites_are_refused(http):
    with http.websocket_connect(f"wss://{HOST}/ws", headers={"origin": OWN}) as ws:
        assert ws.receive_json() == {"hello": True}
    with (
        pytest.raises(WebSocketDisconnect),
        http.websocket_connect(f"wss://{HOST}/ws", headers={"origin": "https://evil.example"}),
    ):
        pass
