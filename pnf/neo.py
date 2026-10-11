"""Async gateway over the (blocking) Kotak Neo SDK.

All REST calls run in a worker thread, one at a time, with a minimum gap
between them: the market-data endpoints answer bursts with 429s. Transient
failures (429, 5xx) are retried with backoff; anything else raises.
"""

import asyncio
import logging
import time
from datetime import date

from neo_api_client import NeoAPI

log = logging.getLogger(__name__)

MIN_CALL_GAP = 1.5  # seconds between REST calls
MAX_RETRIES = 6
RETRY_BACKOFF_CAP = 60

NIFTY_INDEX = "Nifty 50"  # index token for quotes, history and the WebSocket


class NeoError(RuntimeError):
    """A non-transient API failure."""


def error_code(response) -> int | None:
    """Error code (>=400) from a failure response, else None.

    Seen shapes: {"code": 429, ...}, {"code": 503, ...} and
    {"status": "ERROR", "fault": {"code": 422, ...}}.
    """
    if not isinstance(response, dict):
        return None
    for holder in (response, response.get("fault")):
        code = holder.get("code") if isinstance(holder, dict) else None
        if isinstance(code, int) and code >= 400:
            return code
    return None


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


def is_transient(response) -> bool:
    code = error_code(response)
    return code == 429 or (code is not None and 500 <= code < 600)


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
            try:
                return await asyncio.to_thread(fn, **kwargs)
            finally:
                self._last_call = time.monotonic()

    async def _call_with_retry(self, fn, **kwargs):
        response = await self._call(fn, **kwargs)
        for attempt in range(1, MAX_RETRIES + 1):
            if not is_transient(response):
                return response
            wait = min(2**attempt, RETRY_BACKOFF_CAP)
            log.warning(
                "Transient %s from %s, retry %d/%d in %ds",
                error_code(response),
                fn.__name__,
                attempt,
                MAX_RETRIES,
                wait,
            )
            await asyncio.sleep(wait)
            response = await self._call(fn, **kwargs)
        raise NeoError(f"{fn.__name__} still failing after {MAX_RETRIES} retries: {response}")

    # ---------------- Market data (consumer key only) ----------------

    async def expiries(self) -> list[str]:
        response = await self._call_with_retry(
            self.client.expiries, exchange="nse_fo", underlying="NIFTY", instrument_type="option"
        )
        if is_error(response) or not response.get("expiries"):
            raise NeoError(f"Could not load NIFTY expiries: {response}")
        return response["expiries"]

    async def nifty_quote(self) -> dict:
        """Latest NIFTY quote: {'ltp', 'open', 'prev_close'} (latest session)."""
        response = await self._call_with_retry(
            self.client.quotes,
            instrument_tokens=[{"instrument_token": NIFTY_INDEX, "exchange_segment": "nse_cm"}],
            quote_type="all",
        )
        if not isinstance(response, list) or not response:
            raise NeoError(f"Could not load the NIFTY quote: {response}")
        row = response[0]
        ohlc = row.get("ohlc", {})
        ltp = float(row["ltp"])
        return {
            "ltp": ltp,
            "open": float(ohlc.get("open") or ltp),
            "prev_close": ltp - float(row.get("change") or 0),
        }

    async def option_contracts(self, expiry: date, strikes: list[int]) -> dict[tuple[int, str], dict]:
        """Map (strike, 'CE'|'PE') -> {'token', 'symbol'} for NIFTY options of one expiry."""
        response = await self._call_with_retry(
            self.client.search_scrip,
            exchange_segment="nse_fo",
            symbol="nifty",
            expiry=expiry.strftime("%d%b%Y"),
            option_type="CE,PE",
        )
        if not isinstance(response, list):
            raise NeoError(f"Scrip search failed for NIFTY {expiry}: {response}")
        wanted = set(strikes)
        found = {}
        for row in response:
            if str(row.get("pSymbolName", "")).strip().upper() != "NIFTY":
                continue  # substring search also matches e.g. NIFTYNXT50
            strike = round(float(row["dStrikePrice;"]) / 100)  # scrip master stores paise
            if strike in wanted:
                found[(strike, str(row["pOptionType"]).upper())] = {
                    "token": str(row["pSymbol"]),
                    "symbol": row["pTrdSymbol"],
                }
        return found

    async def history(self, token: str, from_date: date, to_date: date) -> list:
        """Raw 1-min candles for an nse_fo token over [from_date, to_date]."""
        response = await self._call_with_retry(
            self.client.historical_data,
            neosymbol=f"nse_fo|{token}",
            interval="1min",
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
        )
        # Status casing varies and "Partial response" still carries valid
        # candles - judge by error codes and status, not the message.
        status = str(response.get("status", "")).lower() if isinstance(response, dict) else ""
        if (
            not isinstance(response, dict)
            or error_code(response) is not None
            or status not in ("success", "")
        ):
            raise NeoError(f"History failed for {token} {from_date}..{to_date}: {response}")
        return response.get("data", {}).get("candles", [])

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
