"""Build a past session's straddle data for the simulator.

A run covers the trading days from start_date to end_date (both before
today), replayed back-to-back on one chart like the live page's multi-day
view. Strikes are fixed for the whole run from the start day's index open
(or an ATM the user sets). Expired contracts cannot be fetched from the
API, so the run uses one of the next four listed expiries.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise

from pnf.neo import NeoError, NeoGateway
from pnf.series import IST, Underlying, in_session, now_ist, round_to_strike, session_date

log = logging.getLogger(__name__)

EXPIRIES_OFFERED = 4  # the next four listed expiries (NIFTY: Tuesdays, SENSEX: Thursdays)
MAX_RANGE_DAYS = 30  # the history API serves at most 30 days of 1-minute candles per request


class SimError(ValueError):
    """A request the simulator cannot serve; the message is shown to the user."""


@dataclass(frozen=True)
class SimRequest:
    underlying: Underlying
    expiry: date
    start: date
    end: date
    atm: int | None  # None: from the start day's open


def validate_dates(start: date, end: date, today: date) -> None:
    """Both dates must be in the past, in order, and within one history request."""
    if end >= today:
        raise SimError(f"The end date must be before today ({today:%d %b %Y}); pick a past session.")
    if start > end:
        raise SimError("The start date must be on or before the end date.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise SimError(f"Pick at most {MAX_RANGE_DAYS} days (the history API's limit for 1-minute data).")
    if start < today - timedelta(days=5 * 365):  # within the API's 5 years, leap days aside
        raise SimError("The history API serves at most 5 years back; pick a later start date.")


def offered_expiries(listed: list[str], today: date) -> list[date]:
    """The next EXPIRIES_OFFERED listed expiries on or after today, nearest first."""
    upcoming = sorted({d for d in (date.fromisoformat(e) for e in listed) if d >= today})
    return upcoming[:EXPIRIES_OFFERED]


def session_candles(raw: list) -> dict[int, list[float]]:
    """Session-only 1-minute candles keyed by minute: ts -> [open, high, low, close, volume]."""
    out = {}
    for row in raw:
        moment = datetime.fromisoformat(row[0]).astimezone(IST)
        if in_session(moment):
            volume = int(row[5]) if len(row) > 5 and row[5] else 0
            out[int(moment.timestamp())] = [
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                volume,
            ]
    return out


PATH_STEPS = 4  # price points played within each simulated minute


def candle_path(candle: list[float]) -> list[float]:
    """How a minute's price most plausibly moved: open, both extremes, close.

    A rising candle is taken to dip first (open, low, high, close), a falling
    one to rally first (open, high, low, close) - the usual convention for
    replaying OHLC data.
    """
    o, h, low, c = candle[:4]
    return [o, low, h, c] if c >= o else [o, h, low, c]


def straddle_rows(ce: dict[int, list[float]], pe: dict[int, list[float]]) -> list[list]:
    """[ts, CE+PE close, CE+PE volume, CE close, PE close, path] per minute.

    `path` is PATH_STEPS points [straddle, CE, PE] played during the minute,
    ending at the close. The legs move step by step together (a rising
    market lifts the CE while it presses the PE), so their sum is a fair
    stand-in for the straddle's intra-minute path.

    Same rules as the live page's series: each leg carries its last close
    forward through minutes it did not trade (a flat path, adding no
    volume), and the series starts once both legs have traded.
    """
    rows = []
    last_ce = last_pe = None
    for ts in sorted(ce.keys() | pe.keys()):
        volume = 0
        if ts in ce:
            last_ce, volume = ce[ts][3], volume + ce[ts][4]
        if ts in pe:
            last_pe, volume = pe[ts][3], volume + pe[ts][4]
        if last_ce is None or last_pe is None:
            continue
        ce_path = candle_path(ce[ts]) if ts in ce else [last_ce] * PATH_STEPS
        pe_path = candle_path(pe[ts]) if ts in pe else [last_pe] * PATH_STEPS
        path = [[round(c + p, 2), c, p] for c, p in zip(ce_path, pe_path, strict=True)]
        rows.append([ts, round(last_ce + last_pe, 2), volume, last_ce, last_pe, path])
    return rows


async def build_run(gw: NeoGateway, req: SimRequest) -> dict:
    """Fetch everything one simulation needs; the browser does the playback."""
    u = req.underlying
    index = session_candles(await gw.history(f"{u.index_segment}|{u.index_token}", req.start, req.end))
    sessions = sorted({session_date(ts) for ts in index})
    if not sessions:
        raise SimError(f"No {u.key} trading sessions between {req.start:%d %b} and {req.end:%d %b %Y}.")
    earlier = await _index_before(gw, u, req.start)
    # The session just before the run is drawn as context, so the replay starts on a chart
    # that already has history (SMA, signals and box size as on the live page).
    context_day = max((session_date(ts) for ts in earlier), default=None)
    prev_closes = _previous_closes(earlier, index, sessions)
    replay_start = min(index)  # first minute of the run proper

    first_open = index[min(index)][0]
    atm_auto = round_to_strike(first_open, u.strike_step)
    atm = req.atm or atm_auto
    strikes = {key: atm + off for key, off in u.chart_offsets.items()}
    contracts = await gw.option_contracts(u, req.expiry, list(strikes.values()))
    lot_size = await contract_lot_size(gw, u, req.expiry)

    charts = []
    for key, off in u.chart_offsets.items():
        strike = strikes[key]
        ce, pe = contracts.get((strike, "CE")), contracts.get((strike, "PE"))
        chart = {"key": key, "offset": off, "strike": strike, "error": None, "series": []}
        if not (ce and pe):
            chart["error"] = f"No {u.key} {strike} straddle listed for the {req.expiry:%d %b %Y} expiry."
        else:
            chart["ce"], chart["pe"] = ce["symbol"], pe["symbol"]
            ce_candles = await _leg_candles(gw, ce["key"], context_day, req.start, req.end)
            pe_candles = await _leg_candles(gw, pe["key"], context_day, req.start, req.end)
            chart["series"] = straddle_rows(ce_candles, pe_candles)
            if not any(row[0] >= replay_start for row in chart["series"]):
                chart["error"] = (
                    f"The {strike} straddle of the {req.expiry:%d %b} expiry has no trades on these dates "
                    "(it may not have been listed yet). Try later dates or a nearer expiry."
                )
        charts.append(chart)

    if all(c["error"] for c in charts):
        raise SimError(charts[1]["error"])
    return {
        "underlying": u.to_json(),
        "expiry": req.expiry.isoformat(),
        "start": req.start.isoformat(),
        "end": req.end.isoformat(),
        "sessions": [d.isoformat() for d in sessions],
        "contextDate": context_day.isoformat() if context_day else None,
        "replayStart": replay_start,  # minutes before this are context, drawn at load
        "indexOpen": first_open,
        "atm": atm,
        "atmAuto": atm_auto,
        "charts": charts,
        "spot": [[ts, c[3], candle_path(c)] for ts, c in sorted(index.items())],
        "pathSteps": PATH_STEPS,
        "lotSize": lot_size,  # units per lot, for the simulated trades' rupee P&L
        "spotPrevCloses": {d.isoformat(): v for d, v in prev_closes.items()},  # for the spot's change
    }


async def _index_before(gw: NeoGateway, u: Underlying, start: date) -> dict[int, list[float]]:
    """Index candles for the days just before the run (best effort: {} if unavailable)."""
    try:
        return session_candles(
            await gw.history(
                f"{u.index_segment}|{u.index_token}", start - timedelta(days=10), start - timedelta(days=1)
            )
        )
    except Exception as exc:  # context and the first day's change are optional
        log.warning("Index history before %s unavailable: %s", start, str(exc)[:200])
        return {}


def _previous_closes(earlier: dict, index: dict, sessions: list[date]) -> dict[date, float]:
    """Each session's previous index close: the day before's last minute."""
    out = {}
    if earlier:
        out[sessions[0]] = earlier[max(earlier)][3]
    for prev, day in pairwise(sessions):
        out[day] = index[max(ts for ts in index if session_date(ts) == prev)][3]
    return out


async def _leg_candles(gw: NeoGateway, neosymbol: str, context_day: date | None, start: date, end: date):
    """One leg's candles for the context day plus the run.

    One request when the whole span fits the API's 30-day limit, otherwise
    the context day separately (the run itself is already within the limit).
    """
    if context_day is None:
        return session_candles(await gw.history(neosymbol, start, end))
    if (end - context_day).days + 1 <= MAX_RANGE_DAYS:
        return session_candles(await gw.history(neosymbol, context_day, end))
    candles = session_candles(await gw.history(neosymbol, start, end))
    try:  # context only: a contract not yet listed that day must not fail the run
        context = session_candles(await gw.history(neosymbol, context_day, context_day))
    except NeoError as exc:
        log.warning("No context day %s for %s: %s", context_day, neosymbol, str(exc)[:200])
        context = {}
    return {**context, **candles}


# Used only if the contract list cannot be read; lot sizes change over time, so the
# exchange's own figure is preferred.
FALLBACK_LOT_SIZES = {"NIFTY": 65, "SENSEX": 20}


async def contract_lot_size(gw: NeoGateway, u: Underlying, expiry: date) -> int:
    """Units per lot for the underlying's options of this expiry, from the scrip master."""
    try:
        rows = await gw._call_with_retry(  # the gateway's paced call; option_contracts drops lot sizes
            gw.client.search_scrip,
            exchange_segment=u.fo_segment,
            symbol=u.key.lower(),
            expiry=expiry.strftime("%d%b%Y"),
            option_type="CE",
        )
        for row in rows if isinstance(rows, list) else []:
            if str(row.get("pSymbolName", "")).strip().upper() == u.key and int(row.get("lLotSize") or 0) > 0:
                return int(row["lLotSize"])
    except Exception as exc:  # display-only: fall back rather than fail the run
        log.warning("Lot size lookup failed for %s %s: %s", u.key, expiry, str(exc)[:200])
    return FALLBACK_LOT_SIZES[u.key]


def default_dates(today: date) -> tuple[date, date]:
    """The most recent weekday before today, as both start and end."""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d, d


def today_ist() -> date:
    return now_ist().date()
