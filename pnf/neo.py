"""Async gateway over the (blocking) Kotak Neo SDK.

All REST calls run in a worker thread, one at a time, with a minimum gap
between them: the market-data endpoints answer bursts with 429s. Transient
failures (429, 5xx, timeouts and network errors) are retried with backoff;
anything else raises.
"""

import asyncio
import contextlib
import logging
import time
from datetime import date, datetime

from neo_api_client import NeoAPI
from neo_api_client.exceptions import ApiException

from .series import Underlying

log = logging.getLogger(__name__)

MIN_CALL_GAP = 1.5  # seconds between REST calls
MAX_RETRIES = 6
RETRY_BACKOFF_CAP = 60


class NeoError(RuntimeError):
    """A non-transient API failure."""


def error_code(response) -> int | None:
    """Error code (>=400) from a failure response, else None.

    Seen shapes: {"code": 429, ...}, {"code": 503, ...},
    {"status": "ERROR", "fault": {"code": 422, ...}}, and from a non-JSON
    reply (e.g. a gateway's HTML 503 page) {"Error": ..., "StatusCode": 503}.
    """
    if not isinstance(response, dict):
        return None
    for holder, field in ((response, "code"), (response.get("fault"), "code"), (response, "StatusCode")):
        code = holder.get(field) if isinstance(holder, dict) else None
        if isinstance(code, int) and code >= 400:
            return code
    return None


def wrapped_exception(response) -> ApiException | None:
    """The ApiException some SDK calls return as {"error": exc} instead of raising."""
    found = response.get("error") if isinstance(response, dict) else None
    return found if isinstance(found, ApiException) else None


def describe(response) -> str:
    """The human-readable message inside an SDK error response.

    Error shapes vary: {"error": [{"message": ...}]}, {"fault": {"message":
    ...}}, {"Error Message": ...}, {"errMsg": ...}. Falls back to the repr.
    """
    if isinstance(response, dict):
        for key in ("message", "Error Message", "errMsg", "emsg", "Error", "error", "fault"):
            found = describe(response[key]) if key in response else ""
            if found:
                return found
        return ""
    if isinstance(response, list):
        return "; ".join(filter(None, (describe(item) for item in response)))
    return str(response) if response else ""


def transient_status(status) -> bool:
    """429, 5xx, and 0 - the SDK's status for timeouts and network errors."""
    return isinstance(status, int) and (status in (0, 429) or 500 <= status < 600)


def is_transient(response) -> bool:
    """A failure worth retrying: a raised or returned ApiException with a transient
    status (or none: an unknown transport failure), or a reply with a 429/5xx code."""
    exc = response if isinstance(response, ApiException) else wrapped_exception(response)
    if exc is not None:
        return exc.status is None or transient_status(exc.status)
    return transient_status(error_code(response))


def is_error(response) -> bool:
    if not isinstance(response, dict):
        return False
    if error_code(response) is not None or response.get("stat") == "Not_Ok":
        return True
    if str(response.get("status", "")).lower() == "error":
        return True
    return any(k in response for k in ("Error", "error", "Error Message", "errMsg"))


class NeoGateway:
    def __init__(self, consumer_key: str, mobile_number: str, ucc: str, mpin: str):
        self.client = NeoAPI(consumer_key=consumer_key, environment="prod")
        self._mobile_number = mobile_number
        self._ucc = ucc
        self._mpin = mpin
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self.logged_in = False

    async def _call(self, fn, **kwargs):
        """Run one SDK call in a thread, spaced MIN_CALL_GAP from the last."""
        async with self._lock:
            wait = self._last_call + MIN_CALL_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            call = asyncio.ensure_future(asyncio.to_thread(fn, **kwargs))
            try:
                return await asyncio.shield(call)
            except asyncio.CancelledError:
                # The thread cannot be stopped: hold the lock until it finishes, so a
                # cancelled build's request never overlaps the next one (429s). Further
                # cancels while waiting must not cut that short either.
                while not call.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait({call})
                if not call.cancelled():
                    call.exception()  # retrieved: the caller is gone, its result unwanted
                raise
            finally:
                self._last_call = time.monotonic()

    async def _call_with_retry(self, fn, **kwargs):
        """Call with backoff on transient failures: 429s, 5xx replies, and the
        ApiException the SDK raises for network trouble (timeouts, dropped
        connections). Any other exception is a fault, not bad luck: no retry."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await self._call(fn, **kwargs)
            except ApiException as exc:
                if not is_transient(exc):
                    raise NeoError(f"{fn.__name__} failed: {exc}") from exc
                response = exc
            if not is_transient(response):
                return response
            if attempt == MAX_RETRIES:
                break
            wait = min(2 ** (attempt + 1), RETRY_BACKOFF_CAP)
            what = type(response).__name__ if isinstance(response, Exception) else error_code(response)
            log.warning(
                "Transient %s from %s, retry %d/%d in %ds", what, fn.__name__, attempt + 1, MAX_RETRIES, wait
            )
            await asyncio.sleep(wait)
        raise NeoError(f"{fn.__name__} still failing after {MAX_RETRIES} retries: {response}")

    # ---------------- Market data (consumer key only) ----------------

    async def expiries(self, u: Underlying) -> list[str]:
        response = await self._call_with_retry(
            self.client.expiries, exchange=u.fo_segment, underlying=u.key, instrument_type="option"
        )
        if is_error(response) or not response.get("expiries"):
            raise NeoError(f"Could not load {u.key} expiries: {response}")
        return response["expiries"]

    async def index_quote(self, u: Underlying) -> dict:
        """Latest index quote: {'ltp', 'open', 'prev_close'} (latest session)."""
        return await self.quote(u.index_segment, u.index_token, f"the {u.key} index")

    async def quote(self, segment: str, token: str, what: str) -> dict:
        """Latest quote for one instrument: {'ltp', 'open', 'prev_close'}."""
        response = await self._call_with_retry(
            self.client.quotes,
            instrument_tokens=[{"instrument_token": token, "exchange_segment": segment}],
            quote_type="all",
        )
        if not isinstance(response, list) or not response:
            raise NeoError(f"Could not load the quote for {what}: {response}")
        row = response[0]
        ohlc = row.get("ohlc", {})
        ltp = float(row["ltp"])
        return {
            "ltp": ltp,
            "open": float(ohlc.get("open") or ltp),
            "prev_close": round(ltp - float(row.get("change") or 0), 2),  # no float leftovers
        }

    async def near_future(self, u: Underlying, today: date) -> dict:
        """The near-month index future: the first contract expiring on or after today.

        Returns a leg like option_contracts() does, plus its expiry date.
        """
        response = await self._call_with_retry(
            self.client.search_scrip, exchange_segment=u.fo_segment, symbol=u.key.lower(), option_type="FUT"
        )
        if not isinstance(response, list):
            raise NeoError(f"Futures search failed for {u.key}: {response}")
        contracts = []
        for row in response:
            if str(row.get("pSymbolName", "")).strip().upper() != u.key:
                continue
            expiry = datetime.strptime(row["pExpiryDate"], "%d%b%Y").date()
            if expiry >= today:
                contracts.append((expiry, row))
        if not contracts:
            raise NeoError(f"No {u.key} future expiring on or after {today}")
        expiry, row = min(contracts, key=lambda c: c[0])
        token = str(row["pSymbol"])
        return {
            "key": f"{u.fo_segment}|{token}",
            "segment": u.fo_segment,
            "token": token,
            "symbol": row["pTrdSymbol"],
            "expiry": expiry.isoformat(),
        }

    async def option_contracts(
        self, u: Underlying, expiry: date, strikes: list[int]
    ) -> dict[tuple[int, str], dict]:
        """Map (strike, 'CE'|'PE') -> leg for one expiry of the underlying's options.

        A leg is {"key": "<segment>|<token>", "segment", "token", "symbol"}; the key
        is unique across exchanges (NSE and BSE token numbers can overlap).
        """
        response = await self._call_with_retry(
            self.client.search_scrip,
            exchange_segment=u.fo_segment,
            symbol=u.key.lower(),
            expiry=expiry.strftime("%d%b%Y"),
            option_type="CE,PE",
        )
        if not isinstance(response, list):
            raise NeoError(f"Scrip search failed for {u.key} {expiry}: {response}")
        wanted = set(strikes)
        found = {}
        for row in response:
            if str(row.get("pSymbolName", "")).strip().upper() != u.key:
                continue  # substring search also matches e.g. NIFTYNXT50
            strike = round(float(row["dStrikePrice;"]) / 100)  # scrip master stores paise
            if strike in wanted:
                token = str(row["pSymbol"])
                found[(strike, str(row["pOptionType"]).upper())] = {
                    "key": f"{u.fo_segment}|{token}",
                    "segment": u.fo_segment,
                    "token": token,
                    "symbol": row["pTrdSymbol"],
                }
        return found

    async def history(self, neosymbol: str, from_date: date, to_date: date) -> list:
        """Raw 1-min candles for a "<segment>|<token>" instrument over [from_date, to_date]."""
        response = await self._call_with_retry(
            self.client.historical_data,
            neosymbol=neosymbol,
            interval="1min",
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
        )
        # Status casing varies and "Partial response" still carries valid
        # candles - judge by the candles first. Without any, only a plain success
        # reply means "no candles": every other shape (an error in any of Neo's
        # forms, a non-JSON page) must not pass for an empty day, which the engine
        # would take for a holiday and cache.
        data = response.get("data") if isinstance(response, dict) else None
        candles = data.get("candles") if isinstance(data, dict) else None
        if isinstance(candles, list) and candles:
            return candles
        status = str(response.get("status", "")).lower() if isinstance(response, dict) else ""
        if not isinstance(response, dict) or is_error(response) or status not in ("success", ""):
            raise NeoError(f"History failed for {neosymbol} {from_date}..{to_date}: {response}")
        return []

    # ---------------- Session (TOTP + MPIN) ----------------

    async def login(self, totp: str) -> None:
        response = await self._call(
            self.client.totp_login, mobile_number=self._mobile_number, ucc=self._ucc, totp=totp
        )
        if is_error(response):
            raise NeoError(f"TOTP login failed: {describe(response) or response}")
        response = await self._call(self.client.totp_validate, mpin=self._mpin)
        if is_error(response) or not self.client.configuration.edit_token:
            raise NeoError(f"MPIN validation failed: {describe(response) or response}")
        self.logged_in = True

    def create_feed(self):
        return self.client.create_websocket()
