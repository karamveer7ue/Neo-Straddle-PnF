"""The PHP order relay (kite/relay/kite-route.php) end to end.

The real relay runs under PHP's built-in server between the real kiteconnect
client and a fake Kite API, so the signing, routing and refusals are tested
exactly as deployed. Skipped where PHP is not installed.
"""

import asyncio
import json
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
import requests
from kiteconnect import KiteConnect
from kiteconnect import exceptions as kex

from kite import order_relay
from kite.gateway import KiteGateway, OutcomeUnknown
from kite.order_relay import RelayAuth, signature

PHP = shutil.which("php")
RELAY = Path(__file__).resolve().parent.parent / "kite" / "relay" / "kite-route.php"
SECRET = "s" * 40

pytestmark = pytest.mark.skipif(PHP is None, reason="PHP is not installed")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeKiteApi(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict]] = []  # requests received, reset per test

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.seen.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": self.rfile.read(length).decode(),
            }
        )
        if self.path.startswith("/missing"):  # a web host's own 404 page
            page = b"<html><body>404 Not Found</body></html>"
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if self.command == "POST" and self.path == "/orders/regular":
            payload = {"status": "success", "data": [{"order_id": "777"}]}  # an autoslice reply
        elif self.path.startswith("/orders"):
            payload = {"status": "success", "data": [] if self.command == "GET" else {"order_id": "777"}}
        else:
            payload = {"status": "success", "data": {}}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = _handle

    def log_message(self, *args):
        pass


@contextmanager
def php_relay(upstream: str):
    """The relay under PHP's built-in server, forwarding to `upstream`; yields its URL."""
    port = free_port()
    env = {"KITE_RELAY_SECRET": SECRET, "KITE_ROUTE_UPSTREAM": upstream, "PATH": "/usr/bin:/bin"}
    php = subprocess.Popen(
        [PHP, "-S", f"127.0.0.1:{port}", str(RELAY)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/kite-route.php"
    try:
        for _ in range(50):
            try:
                requests.get(url, timeout=0.2)
                break
            except requests.ConnectionError:
                time.sleep(0.1)
        yield url
    finally:
        php.terminate()
        php.wait()


@pytest.fixture
def relay():
    """(relay URL, fake Kite URL, requests the fake Kite received)."""
    FakeKiteApi.seen = []
    api = ThreadingHTTPServer(("127.0.0.1", 0), FakeKiteApi)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    api_url = f"http://127.0.0.1:{api.server_address[1]}"
    with php_relay(api_url) as url:
        yield url, api_url, FakeKiteApi.seen
    api.shutdown()


def kite_client(api_url: str, relay_url: str, secret: str = SECRET) -> KiteConnect:
    kite = KiteConnect(api_key="key", access_token="tok", root=api_url)
    kite.reqsession.auth = RelayAuth(relay_url, secret)
    return kite


ORDER = dict(
    variety="regular", exchange="NFO", tradingsymbol="NIFTY26O1322300CE", transaction_type="SELL",
    quantity=65, product="NRML", order_type="MARKET", validity="DAY", market_protection=-1, tag="pnf",
)  # fmt: skip


def test_orders_go_through_the_relay_and_reads_go_direct(relay):
    relay_url, api_url, seen = relay
    kite = kite_client(api_url, relay_url)
    assert kite.place_autoslice_order(**ORDER) == [{"order_id": "777"}]
    [placed] = seen
    assert (placed["method"], placed["path"]) == ("POST", "/orders/regular")
    assert placed["headers"]["Authorization"] == "token key:tok"
    assert placed["headers"]["User-Agent"] == "pnf-kite-route/1"  # it came from the relay
    assert "market_protection=-1" in placed["body"] and "autoslice=true" in placed["body"]

    kite.cancel_order("regular", "777")
    assert (seen[1]["method"], seen[1]["path"]) == ("DELETE", "/orders/regular/777")

    kite.orders()  # a read: straight to Kite, not through the relay
    assert seen[2]["path"] == "/orders" and "X-Relay-Signature" not in seen[2]["headers"]


def test_a_wrong_secret_is_refused_and_nothing_reaches_kite(relay):
    relay_url, api_url, seen = relay
    kite = kite_client(api_url, relay_url, secret="x" * 40)
    with pytest.raises(kex.PermissionException, match="bad signature"):
        kite.place_autoslice_order(**ORDER)
    assert seen == []


def test_an_old_request_cannot_be_replayed(relay, monkeypatch):
    relay_url, api_url, seen = relay
    monkeypatch.setattr(order_relay.time, "time", lambda: 1_000_000_000.0)  # signed long ago
    with pytest.raises(kex.PermissionException, match="stale timestamp"):
        kite_client(api_url, relay_url).place_autoslice_order(**ORDER)
    assert seen == []


def test_only_order_endpoints_are_relayed(relay):
    relay_url, _, seen = relay
    ts = str(int(time.time()))
    for method, target in (
        ("POST", "/portfolio/positions"),
        ("GET", "/orders"),
        ("POST", "/orders/regular/../../x"),
    ):
        res = requests.request(
            method,
            relay_url,
            params={"path": target},
            headers={
                "X-Relay-Timestamp": ts,
                "X-Relay-Signature": signature(SECRET, ts, method, target, b""),
            },
            timeout=5,
        )
        assert res.status_code in (403, 405), (method, target, res.text)
        assert res.json()["error_type"] == "PermissionException"
    assert seen == []


def test_kite_unreachable_from_the_relay_is_an_unknown_outcome():
    dead = f"http://127.0.0.1:{free_port()}"  # nothing listens here
    with php_relay(dead) as relay_url:
        client = KiteConnect(api_key="key", access_token="tok", root=dead)
        orders = KiteConnect(api_key="key", access_token="tok", root=dead)
        gw = KiteGateway(
            "key", "secret", client=client, order_client=orders, order_auth=RelayAuth(relay_url, SECRET)
        )
        with pytest.raises(OutcomeUnknown, match="no reply from Kite"):
            asyncio.run(
                gw.place_market_order(
                    exchange="NFO", tradingsymbol="X", side="SELL", quantity=65, product="NRML", tag="pnf"
                )
            )


def test_relay_settings_are_checked():
    with pytest.raises(RuntimeError, match="https"):
        order_relay.check_settings("http://your-domain.example/kite-route.php", SECRET)
    with pytest.raises(RuntimeError, match="at least 32"):
        order_relay.check_settings("https://your-domain.example/kite-route.php", "short")
    order_relay.check_settings("https://your-domain.example/kite-route.php", SECRET)


def test_the_upload_copy_has_the_secret_filled_in(tmp_path):
    out = order_relay.write_php(SECRET, tmp_path / "kite-route.php")
    text = out.read_text()
    assert f"const RELAY_SECRET = '{SECRET}';" in text
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert order_relay.SECRET_PLACEHOLDER in RELAY.read_text()  # the template keeps no secret


def test_a_relay_that_is_not_uploaded_is_reported_as_such(relay):
    _, api_url, _ = relay
    kite = kite_client(api_url, f"{api_url}/missing/kite-route.php")
    with pytest.raises(kex.PermissionException, match=r"answered 404 - is kite-route\.php uploaded there"):
        kite.place_autoslice_order(**ORDER)
