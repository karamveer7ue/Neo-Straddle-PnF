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

from pnf.neo import NeoGateway
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
    prev_closes = await _previous_closes(gw, u, req.start, index, sessions)

    first_open = index[min(index)][0]
    atm_auto = round_to_strike(first_open, u.strike_step)
    atm = req.atm or atm_auto
    strikes = {key: atm + off for key, off in u.chart_offsets.items()}
    contracts = await gw.option_contracts(u, req.expiry, list(strikes.values()))

    charts = []
    for key, off in u.chart_offsets.items():
        strike = strikes[key]
        ce, pe = contracts.get((strike, "CE")), contracts.get((strike, "PE"))
        chart = {"key": key, "offset": off, "strike": strike, "error": None, "series": []}
        if not (ce and pe):
            chart["error"] = f"No {u.key} {strike} straddle listed for the {req.expiry:%d %b %Y} expiry."
        else:
            chart["ce"], chart["pe"] = ce["symbol"], pe["symbol"]
            ce_candles = session_candles(await gw.history(ce["key"], req.start, req.end))
            pe_candles = session_candles(await gw.history(pe["key"], req.start, req.end))
            chart["series"] = straddle_rows(ce_candles, pe_candles)
            if not chart["series"]:
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
        "indexOpen": first_open,
        "atm": atm,
        "atmAuto": atm_auto,
        "charts": charts,
        "spot": [[ts, c[3], candle_path(c)] for ts, c in sorted(index.items())],
        "pathSteps": PATH_STEPS,
        "spotPrevCloses": {d.isoformat(): v for d, v in prev_closes.items()},  # for the spot's change
    }


async def _previous_closes(gw: NeoGateway, u: Underlying, start: date, index: dict, sessions: list[date]):
    """Each session's previous index close: the day before's last minute.

    The first session's comes from a separate request for the days before
    the run (best effort - the change is just not shown if it fails).
    """
    out = {}
    try:
        earlier = session_candles(
            await gw.history(
                f"{u.index_segment}|{u.index_token}", start - timedelta(days=10), start - timedelta(days=1)
            )
        )
        if earlier:
            out[sessions[0]] = earlier[max(earlier)][3]
    except Exception as exc:  # display-only
        log.warning("Previous close before %s unavailable: %s", start, str(exc)[:200])
    for prev, day in pairwise(sessions):
        out[day] = index[max(ts for ts in index if session_date(ts) == prev)][3]
    return out


def default_dates(today: date) -> tuple[date, date]:
    """The most recent weekday before today, as both start and end."""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d, d


def today_ist() -> date:
    return now_ist().date()
