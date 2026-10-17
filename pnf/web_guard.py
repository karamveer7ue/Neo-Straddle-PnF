"""Keep other web pages out of the local servers (live app and simulator).

Both servers listen on 127.0.0.1 only, but any page open in the same browser
can still reach them: by sending requests (CSRF), by opening a WebSocket (no
CORS for those), through DNS rebinding (an attacker's name resolving to
127.0.0.1), or by framing the app and steering the user's click onto Sell
(clickjacking: a framed page is the app's own origin, so an Origin check
cannot see it). `LocalOnly` wraps a whole app, so no route can be added
without the protection:

- every request and WebSocket must name a loopback Host, and carry an Origin
  of that same host when the browser sends one;
- a request that changes state (anything but GET/HEAD) is refused when the
  browser says it comes from another site (`Sec-Fetch-Site`);
- every response forbids framing and carries a strict Content-Security-Policy
  (the pages load only their own scripts and styles).
"""

import re
from urllib.parse import urlsplit

LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
HOST_RE = re.compile(r"(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?")  # the whole header, port included
SAFE_METHODS = {"GET", "HEAD"}
REFUSED = b"Requests must come from the app's own page."


def same_origin(host: str | None, origin: str | None) -> bool:
    """True for a request from this app's own page: a loopback Host, and an Origin
    (when sent) of that same host. A DNS-rebinding page has its own host name."""
    if not host or not HOST_RE.fullmatch(host):  # the whole value: it is quoted in the CSP
        return False
    if origin is None:
        return True  # same-origin GETs and top-level navigations may omit it
    parts = urlsplit(origin)
    return parts.scheme == "https" and parts.netloc == host  # both servers are HTTPS-only


def security_headers(host: str) -> list[tuple[bytes, bytes]]:
    """Headers for every response; `host` has passed `same_origin`."""
    csp = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        f"connect-src 'self' wss://{host}; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    return [
        (b"content-security-policy", csp.encode()),
        (b"x-frame-options", b"DENY"),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
    ]


class LocalOnly:
    """ASGI middleware applying the rules above to HTTP and WebSocket alike."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host")
        allowed = same_origin(host, headers.get("origin"))
        if allowed and scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            allowed = headers.get("sec-fetch-site", "same-origin") == "same-origin"
        if not allowed:
            if scope["type"] == "websocket":
                await receive()  # websocket.connect; closing before accepting answers it with 403
                await send({"type": "websocket.close", "code": 1008})
                return
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": REFUSED})
            return
        if scope["type"] == "websocket":
            return await self.app(scope, receive, send)

        extra = security_headers(host)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)
