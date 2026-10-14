"""Async gateway over the (blocking) Kite Connect SDK.

Every REST call runs in a worker thread. Reads go one at a time through one
SDK client (it shares one `requests` session); orders have their own client
and lock, so an order never waits behind a slow read or the daily
instrument dump. Reads are retried on transient failures (429, 5xx,
timeouts, network errors). Order placement is retried only on a 429: that
reply means the order was refused before it reached the order system, while
any other failure (a timeout above all) may have placed it, so it is
reported and never sent twice.
"""

import asyncio
import contextlib
import logging

import requests
from kiteconnect import KiteConnect
from kiteconnect import exceptions as kex

log = logging.getLogger(__name__)

READ_RETRIES = 3
ORDER_RETRIES_ON_429 = 2
RETRY_DELAY = 1.0  # seconds; doubled on each retry
TIMEOUT = 10  # seconds per request


class KiteError(RuntimeError):
    """A failure to show the user (Kite's own message where there is one)."""


class SessionExpired(KiteError):
    """The access token is missing, expired or revoked: log in again."""


class OutcomeUnknown(KiteError):
    """An order request failed in a way that may still have placed it."""


def _transient(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    code = getattr(exc, "code", None)
    return isinstance(exc, kex.KiteException) and (code == 429 or (isinstance(code, int) and code >= 500))


def _message(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:300]


class KiteGateway:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        client: KiteConnect | None = None,
        order_client: KiteConnect | None = None,
        order_auth: requests.auth.AuthBase | None = None,
    ):
        """`order_auth` (an order_relay.RelayAuth) sends order calls through a relay."""
        self.api_key = api_key
        self._secret = api_secret
        self.client = client or KiteConnect(api_key=api_key, timeout=TIMEOUT)
        if order_client is None:
            order_client = KiteConnect(api_key=api_key, timeout=TIMEOUT) if client is None else client
        self.order_client = order_client
        if order_auth is not None:
            self.order_client.reqsession.auth = order_auth
        self._lock = asyncio.Lock()
        self._order_lock = asyncio.Lock() if order_client is not self.client else self._lock

    @property
    def access_token(self) -> str | None:
        return self.client.access_token

    def set_access_token(self, token: str | None) -> None:
        self.client.set_access_token(token)
        self.order_client.set_access_token(token)

    def login_url(self, state: str) -> str:
        # Kite hands `redirect_params` back to the redirect URL, so the callback can
        # tell a login it started from one it did not.
        return f"{self.client.login_url()}&redirect_params={requests.utils.quote(f'state={state}', safe='')}"

    async def _run(self, fn, *args, _lock: asyncio.Lock | None = None, **kwargs):
        """One SDK call in a thread; a cancelled caller still waits for the thread,
        so calls never overlap on a client's session."""
        async with _lock or self._lock:
            call = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
            try:
                return await asyncio.shield(call)
            except asyncio.CancelledError:
                while not call.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait({call})
                raise

    async def _read(self, name: str, *args, urgent: bool = False, **kwargs):
        """Call SDK method `name`, retrying transient failures. An `urgent` read (one an
        order is about to be sized from) goes through the order client, so it never
        waits behind a background refresh or the instrument dump."""
        client, lock = (self.order_client, self._order_lock) if urgent else (self.client, self._lock)
        fn = getattr(client, name)
        delay = RETRY_DELAY
        for attempt in range(READ_RETRIES + 1):
            try:
                return await self._run(fn, *args, _lock=lock, **kwargs)
            except kex.TokenException as exc:
                raise SessionExpired(_message(exc)) from exc
            except Exception as exc:
                if attempt == READ_RETRIES or not _transient(exc):
                    raise KiteError(_message(exc)) from exc
                log.warning("Kite %s failed (%s), retrying in %.0fs", name, _message(exc), delay)
            await asyncio.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")

    async def generate_session(self, request_token: str) -> dict:
        """Exchange the login redirect's request token; sets the access token."""
        session = await self._read("generate_session", request_token, api_secret=self._secret)
        self.order_client.set_access_token(session["access_token"])
        return session

    async def profile(self) -> dict:
        return await self._read("profile")

    async def positions(self, urgent: bool = False) -> dict:
        return await self._read("positions", urgent=urgent)

    async def orders(self, urgent: bool = False) -> list[dict]:
        return await self._read("orders", urgent=urgent)

    async def instruments(self, exchange: str) -> list[dict]:
        return await self._read("instruments", exchange)

    async def invalidate(self) -> None:
        await self._read("invalidate_access_token")

    async def place_market_order(
        self, *, exchange: str, tradingsymbol: str, side: str, quantity: int, product: str, tag: str
    ) -> list[dict]:
        """Place a MARKET order with automatic market protection, sliced by Kite when
        the quantity is above the exchange's freeze limit.

        Returns one entry per slice: {"order_id": ...} or {"error": message}.
        Raises KiteError when nothing was placed, OutcomeUnknown when that is not certain.
        """
        params = {
            "variety": KiteConnect.VARIETY_REGULAR,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": side,
            "quantity": quantity,
            "product": product,
            "order_type": KiteConnect.ORDER_TYPE_MARKET,
            "validity": KiteConnect.VALIDITY_DAY,
            "market_protection": -1,  # required on API market orders; -1 is Kite's automatic band
            "tag": tag,
        }
        delay = RETRY_DELAY
        for attempt in range(ORDER_RETRIES_ON_429 + 1):
            try:
                reply = await self._run(
                    self.order_client.place_autoslice_order, _lock=self._order_lock, **params
                )
                return parse_slices(reply)
            except kex.TokenException as exc:
                raise SessionExpired(_message(exc)) from exc
            except kex.KiteException as exc:
                if exc.code == 429 and attempt < ORDER_RETRIES_ON_429:
                    log.warning("Order for %s rate limited, retrying in %.0fs", tradingsymbol, delay)
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                if isinstance(exc, (kex.DataException, kex.NetworkException)) or exc.code >= 500:
                    # The order system may have taken it before failing to answer.
                    raise OutcomeUnknown(_message(exc)) from exc
                raise KiteError(_message(exc)) from exc
            except requests.RequestException as exc:
                raise OutcomeUnknown(_message(exc)) from exc
        raise AssertionError("unreachable")


def parse_slices(reply) -> list[dict]:
    """Normalise an autoslice reply: a list with one {"order_id"} or {"error"} per slice.

    Kite returns a list for autoslice orders; a bare {"order_id"} (a regular
    order reply) is accepted too.
    """
    items = reply if isinstance(reply, list) else [reply]
    slices = []
    for item in items:
        if isinstance(item, dict) and item.get("order_id"):
            slices.append({"order_id": str(item["order_id"])})
        elif isinstance(item, dict) and item.get("error"):
            err = item["error"]
            slices.append({"error": str(err.get("message") if isinstance(err, dict) else err)[:300]})
        else:
            slices.append({"error": f"Unexpected reply from Kite: {str(item)[:200]}"})
    if not slices:
        raise OutcomeUnknown("Kite returned no order IDs")
    return slices
