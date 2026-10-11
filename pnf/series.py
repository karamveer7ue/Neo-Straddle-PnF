"""Pure helpers for building NIFTY straddle 1-min close series.

Nothing in here talks to the network, so it is unit-tested directly
(tests/test_series.py). Timestamps are epoch seconds of the candle's
minute start; all calendar logic is in IST.
"""

import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

SESSION_OPEN = time(9, 15)
LAST_MINUTE = time(15, 29)  # last 1-min candle of the 09:15-15:30 session
STRIKE_STEP = 50  # NIFTY option strike interval

# Chart key -> strike offset from ATM, in display order (left to right).
CHART_OFFSETS = {"atm_minus_200": -200, "atm": 0, "atm_plus_200": 200}

MIN_DAYS, MAX_DAYS = 1, 15  # 15 trading days stays inside one 30-day 1-min request

# One leg's 1-min candle: (minute start epoch seconds, close, typical price, volume).
Candle = tuple[int, float, float, int]
# One straddle minute: (minute start epoch seconds, CE+PE close, CE+PE VWAP or None).
Bar = tuple[int, float, float | None]


def now_ist() -> datetime:
    return datetime.now(IST)


def round_to_strike(price: float, step: int = STRIKE_STEP) -> int:
    """Nearest strike, rounding halves up (not Python's banker's rounding)."""
    return math.floor(price / step + 0.5) * step


# Expiry roles offered in the UI. A role (not a date) is what the user
# picks, so the choice rolls forward by itself once a contract expires.
EXPIRY_ROLES = {
    "week1": "weekly",
    "week2": "next weekly",
    "month1": "monthly",
    "month2": "next monthly",
}
DEFAULT_EXPIRY_ROLE = "week1"


def expiry_roles(expiries: list[str], today: date) -> dict[str, date]:
    """Date of each role: the next two weeklies and the next two monthlies.

    A monthly is the last listed expiry of its calendar month. Roles can
    share a date (late in a month the next weekly is the monthly).
    """
    upcoming = sorted({d for d in (date.fromisoformat(e) for e in expiries) if d >= today})
    if not upcoming:
        raise ValueError(f"No expiry on or after {today} in {expiries}")
    month_end = {}
    for d in upcoming:
        month_end[(d.year, d.month)] = d  # ascending, so the month's last wins
    monthlies = sorted(month_end.values())
    roles = {"week1": upcoming[0], "month1": monthlies[0]}
    if len(upcoming) > 1:
        roles["week2"] = upcoming[1]
    if len(monthlies) > 1:
        roles["month2"] = monthlies[1]
    return roles


def expiry_choices(roles: dict[str, date]) -> list[dict]:
    """Dropdown options, one per distinct date, sorted by date.

    A date held by both a weekly and a monthly role is listed under the
    monthly role - the more stable name for that contract.
    """
    by_date = {}
    for role in ("week1", "week2", "month1", "month2"):  # monthly roles override
        if role in roles:
            by_date[roles[role]] = role
    return [
        {"role": role, "date": d.isoformat(), "label": f"{d:%d %b} \u00b7 {EXPIRY_ROLES[role]}"}
        for d, role in sorted(by_date.items())
    ]


def in_session(moment: datetime) -> bool:
    """True if a candle starting at this IST moment belongs to the regular session."""
    return SESSION_OPEN <= moment.time() <= LAST_MINUTE


def is_market_window(moment: datetime) -> bool:
    """Weekday between pre-open and close - when a live feed is worth having."""
    return moment.weekday() < 5 and time(9, 0) <= moment.time() <= time(15, 30)


def history_start(today: date, days: int) -> date:
    """Calendar start date that holds at least `days` trading sessions.

    Weekends cost 2 of every 7 days; a week of slack covers holidays.
    """
    return today - timedelta(days=math.ceil(days * 7 / 5) + 7)


def parse_candles(candles: list) -> list[Candle]:
    """Turn raw historical_data() candles into session-only leg candles.

    Candles look like ["2026-10-09T09:15:00+0530", o, h, l, c, v]. The feed
    also returns post-close candles up to ~15:39, which are dropped.
    """
    out = {}
    for row in candles:
        moment = datetime.fromisoformat(row[0]).astimezone(IST)
        if in_session(moment):
            high, low, close = float(row[2]), float(row[3]), float(row[4])
            volume = int(row[5]) if len(row) > 5 and row[5] else 0
            ts = int(moment.timestamp())
            out[ts] = (ts, close, (high + low + close) / 3, volume)
    return [out[ts] for ts in sorted(out)]


def straddle_series(ce: list[Candle], pe: list[Candle]) -> list[Bar]:
    """CE + PE close and CE + PE VWAP per minute.

    Illiquid strikes skip minutes, so each leg carries its last close
    forward; the series starts once both legs have traded. Each leg's VWAP
    is anchored at the session open (typical price x volume, cumulated
    from 09:15) and is None until both legs have traded volume that day.
    """
    legs = [{c[0]: c for c in ce}, {c[0]: c for c in pe}]
    last = [None, None]
    sums = [[0.0, 0], [0.0, 0]]  # per leg: [sum(typical * volume), sum(volume)]
    day = None
    out = []
    for ts in sorted(legs[0].keys() | legs[1].keys()):
        if session_date(ts) != day:
            day = session_date(ts)
            sums = [[0.0, 0], [0.0, 0]]
        for i, leg in enumerate(legs):
            candle = leg.get(ts)
            if candle:
                last[i] = candle[1]
                sums[i][0] += candle[2] * candle[3]
                sums[i][1] += candle[3]
        if last[0] is None or last[1] is None:
            continue
        vwap = None
        if sums[0][1] and sums[1][1]:
            vwap = round(sums[0][0] / sums[0][1] + sums[1][0] / sums[1][1], 2)
        out.append((ts, round(last[0] + last[1], 2), vwap))
    return out


def session_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, IST).date()


def last_sessions(bars: list, dates: list[date]) -> list:
    """Keep only bars whose IST date is in `dates`."""
    wanted = set(dates)
    return [b for b in bars if session_date(b[0]) in wanted]


def merge_tail(base: list[Bar], tail: list[Bar]) -> list[Bar]:
    """Append only the tail bars that are newer than the base's last bar."""
    if not base:
        return list(tail)
    last = base[-1][0]
    return base + [b for b in tail if b[0] > last]


def minute_start(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)
