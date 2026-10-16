"""Send Kite order requests through a relay on a server with a static IP.

Kite accepts orders only from the static IP registered in its developer
console, and this app runs on a machine without one. kite/relay/kite-route.php,
hosted on such a server, forwards the order calls to Kite. Only order
placement, modification and cancellation go through it (the only calls Kite
checks the IP of); everything else still goes straight to Kite.

Each relayed request is signed with the shared secret KITE_RELAY_SECRET:
X-Relay-Signature = hex HMAC-SHA256 of "<timestamp>\\n<METHOD>\\n<path>\\n
<authorization>\\n<content-type>\\n<x-kite-version>\\n<body>" - everything the
relay forwards - which the relay checks, refuses after 60 seconds, and accepts
only once, so a captured request can be neither changed nor sent again.

    .venv/bin/python -m kite.order_relay check   # is the relay reachable, and its outgoing IP
    .venv/bin/python -m kite.order_relay php     # write relay-upload/kite-route.php with the secret filled in
"""

import hashlib
import hmac
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

ROOT = Path(__file__).resolve().parent.parent
PHP_TEMPLATE = Path(__file__).parent / "relay" / "kite-route.php"
PHP_UPLOAD = ROOT / "relay-upload" / "kite-route.php"  # gitignored: holds the secret
SECRET_PLACEHOLDER = "PASTE-KITE_RELAY_SECRET-FROM-.env-HERE"
RELAYED_METHODS = ("POST", "PUT", "DELETE")
HOST_REFUSALS = (401, 403, 404, 405)  # web-host replies that mean the relay script never ran
MIN_SECRET_LENGTH = 32
SECRET_CHARS = re.compile(r"[A-Za-z0-9_-]+")  # pasted into a PHP string literal by write_php
# The template's overrides for the app's tests; the upload copy has them taken out.
TEST_OVERRIDES = {
    "getenv('KITE_ROUTE_UPSTREAM') ?: KITE_API": "KITE_API",
    "$override = getenv('KITE_ROUTE_SEEN_FILE');  // the app's tests only": "$override = '';",
}


def signature(
    secret: str,
    timestamp: str,
    method: str,
    target: str,
    body: bytes,
    auth: str = "",
    content_type: str = "",
    kite_version: str = "",
) -> str:
    """The relay signature: over the method, path, body and every header the relay forwards."""
    head = "\n".join([timestamp, method, target, auth, content_type, kite_version, ""])
    return hmac.new(secret.encode(), head.encode() + body, hashlib.sha256).hexdigest()


def check_settings(url: str, secret: str) -> None:
    """Raise RuntimeError for unusable relay settings."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise RuntimeError(f"KITE_ORDER_RELAY must be an https URL, not {url!r}")
    check_secret(secret)


def check_secret(secret: str) -> None:
    if len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(f"KITE_RELAY_SECRET must be at least {MIN_SECRET_LENGTH} characters")
    if not SECRET_CHARS.fullmatch(secret):
        raise RuntimeError(
            "KITE_RELAY_SECRET may hold only letters, digits, '-' and '_' (e.g. 64 hex digits)"
        )


class RelayAuth(requests.auth.AuthBase):
    """Reroutes order calls of a KiteConnect session to the relay, signed.

    Installed as the session's auth, it sees every prepared request. Order
    calls (POST/PUT/DELETE under /orders) are sent to `<relay>?path=<path>`
    instead of api.kite.trade; the Kite authorization is copied to a header
    of its own, in case the web server strips Authorization before PHP sees it.
    """

    def __init__(self, url: str, secret: str):
        self.url = url
        self.secret = secret

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        if r.method in RELAYED_METHODS and urlsplit(r.url).path.startswith("/orders"):
            target = r.path_url
            body = r.body or b""
            body = body.encode() if isinstance(body, str) else body
            timestamp = str(int(time.time()))
            auth = r.headers.get("Authorization", "")
            r.headers["X-Relay-Timestamp"] = timestamp
            r.headers["X-Relay-Signature"] = signature(
                self.secret,
                timestamp,
                r.method,
                target,
                body,
                auth=auth,
                content_type=r.headers.get("Content-Type", ""),
                kite_version=r.headers.get("X-Kite-Version", ""),
            )
            if auth:
                r.headers["X-Kite-Authorization"] = auth
            r.url = f"{self.url}?path={quote(target, safe='')}"
            r.register_hook("response", self._explain_host_refusal)
        return r

    def _explain_host_refusal(self, res: requests.Response, *args, **kwargs) -> requests.Response:
        """The web host's own refusal (an HTML 404 or firewall 403: the relay never ran, so
        nothing reached Kite) as a Kite-style error, instead of an unreadable reply that
        would count as an order of unknown outcome."""
        if res.status_code in HOST_REFUSALS and "json" not in res.headers.get("Content-Type", ""):
            message = (
                f"Order relay: {self.url} answered {res.status_code} - is kite-route.php uploaded there, "
                "and not blocked by the host's firewall?"
            )
            res._content = json.dumps(
                {"status": "error", "error_type": "PermissionException", "message": message}
            ).encode()
            res.headers["Content-Type"] = "application/json"
        return res


def whoami(url: str, secret: str, timeout: float = 15) -> dict:
    """Ask the relay for its outgoing IP (the one to register with Kite)."""
    timestamp = str(int(time.time()))
    headers = {
        "X-Relay-Timestamp": timestamp,
        "X-Relay-Signature": signature(secret, timestamp, "GET", "whoami", b""),
    }
    try:
        res = requests.get(url, params={"path": "whoami"}, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach the relay at {url}: {exc}") from None
    try:
        data = res.json()
    except ValueError:
        hint = " - is kite-route.php uploaded there?" if res.status_code == 404 else ""
        raise RuntimeError(f"{url} answered {res.status_code}, not the relay{hint}") from None
    if res.status_code != 200 or data.get("status") != "success":
        raise RuntimeError(f"The relay answered {res.status_code}: {data.get('message') or data}")
    return data["data"]


def write_php(secret: str, out: Path = PHP_UPLOAD) -> Path:
    """A copy of kite-route.php with the secret filled in, ready to upload (and without
    the template's test-only overrides, which the web host's environment could set)."""
    check_secret(secret)
    text = PHP_TEMPLATE.read_text()
    if SECRET_PLACEHOLDER not in text:
        raise RuntimeError(f"{PHP_TEMPLATE} has no secret placeholder")
    for test_only, production in TEST_OVERRIDES.items():
        if test_only not in text:
            raise RuntimeError(f"{PHP_TEMPLATE} has changed: {test_only!r} not found")
        text = text.replace(test_only, production)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text.replace(SECRET_PLACEHOLDER, secret))
    return out


def main(argv: list[str]) -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    url = os.getenv("KITE_ORDER_RELAY", "").strip()
    secret = os.getenv("KITE_RELAY_SECRET", "").strip()
    command = argv[0] if argv else "check"
    if command == "php":
        try:
            out = write_php(secret)
        except RuntimeError as exc:
            print(f"{exc} (set it in .env).", file=sys.stderr)
            return 1
        print(f"Wrote {out} - upload it to your web host as kite-route.php.")
        return 0
    if command == "check":
        if not url:
            print("KITE_ORDER_RELAY is not set in .env: orders go straight to Kite.", file=sys.stderr)
            return 1
        try:
            check_settings(url, secret)
            info = whoami(url, secret)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        skew = int(time.time()) - int(info.get("time", time.time()))
        version = info.get("version", 4)
        print(f"Relay OK: {url}")
        if info.get("ip"):
            print(f"Kite sees orders from IPv{version} {info['ip']}: register this one with Kite.")
        else:
            print(
                f"Warning: the relay has no IPv{version} address; change KITE_IP_VERSION in kite-route.php."
            )
        if info.get("otherIp"):
            print(f"(Its IPv{6 if version == 4 else 4} address {info['otherIp']} is not used for Kite.)")
        if abs(skew) > 20:
            print(f"Warning: this machine's clock is {skew:+d} s off the relay's (limit 60 s).")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
