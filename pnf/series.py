"""Pure helpers for building index straddle 1-min close series.

Nothing in here talks to the network, so it is unit-tested directly
(tests/test_series.py). Timestamps are epoch seconds of the candle's
minute start; all calendar logic is in IST.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

SESSION_OPEN = time(9, 15)
LAST_MINUTE = time(15, 29)  # last 1-min candle of the 09:15-15:30 session


@dataclass(frozen=True)
class Underlying:
    """An index whose ATM and side straddles are charted."""

    key: str  # "NIFTY"; also the scrip-master pSymbolName and the expiries() underlying
    fo_segment: str  # where its options trade
    index_segment: str  # where the index itself is quoted
    index_token: str  # the index token for quotes, history and the WebSocket
    strike_step: int
    side_offset: int  # the side charts sit at ATM - offset and ATM + offset
    max_order_qty: int  # the exchange's freeze quantity: the most one option order may carry
    algo_qty: int  # the algo's default quantity per trade (units of the CE and of the PE)
    algo_target: int  # the algo's default Target Points (straddle points)
    weekly: bool = True  # lists weekly expiries; BANKNIFTY has monthlies only

    @property
    def max_sell_qty(self) -> int:
        """The most one sell from this app may carry (the algo or a Sell button): twice
        the algo's default quantity, as a guard against a mistyped quantity."""
        return 2 * self.algo_qty

    @property
    def expiry_roles(self) -> tuple[str, ...]:
        """The expiry roles this underlying offers (see EXPIRY_ROLES)."""
        return tuple(EXPIRY_ROLES) if self.weekly else ("month1", "month2")

    @property
    def default_expiry_role(self) -> str:
        return self.expiry_roles[0]

    @property
    def chart_offsets(self) -> dict[str, int]:
        """Chart key -> strike offset from ATM, in display order (left to right)."""
        return {"atm_minus": -self.side_offset, "atm": 0, "atm_plus": self.side_offset}

    def to_json(self) -> dict:
        return {"key": self.key, "strikeStep": self.strike_step, "sideOffset": self.side_offset}


# NIFTY and BANKNIFTY trade on NSE; SENSEX on BSE. Index tokens are names, not numbers.
# The order is the index menu's. max_order_qty is one below the scrip master's
# lFreezeQty on NSE (an order must stay under it) and equal to it on BSE (NSE's
# limits as revised on 5 Oct 2026). The algo defaults are the user's: about half of
# each exchange limit (NIFTY 26 lots of 65, BANKNIFTY 24 of 30, SENSEX 24 of 20).
UNDERLYINGS = {
    "NIFTY": Underlying(
        "NIFTY",
        "nse_fo",
        "nse_cm",
        "Nifty 50",
        strike_step=50,
        side_offset=200,
        max_order_qty=3510,
        algo_qty=1690,
        algo_target=30,
    ),
    "BANKNIFTY": Underlying(
        "BANKNIFTY",
        "nse_fo",
        "nse_cm",
        "Nifty Bank",
        strike_step=100,
        side_offset=500,
        max_order_qty=1440,
        algo_qty=720,
        algo_target=99,
        weekly=False,
    ),
    "SENSEX": Underlying(
        "SENSEX",
        "bse_fo",
        "bse_cm",
        "SENSEX",
        strike_step=100,
        side_offset=500,
        max_order_qty=1000,
        algo_qty=480,
        algo_target=90,
    ),
}
DEFAULT_UNDERLYING = "NIFTY"
UnderlyingKey = Literal[tuple(UNDERLYINGS)]  # request validation: one of the UNDERLYINGS keys

MIN_DAYS, MAX_DAYS = 1, 15  # 15 trading days stays inside one 30-day 1-min request

# One leg's 1-min candle: (minute start epoch seconds, close, volume).
Candle = tuple[int, float, int]
# One straddle minute: (minute start epoch seconds, CE+PE close, CE+PE volume that minute).
# The browser weights closes by this volume to get each P&F column's VWAP.
Bar = tuple[int, float, int]


def now_ist() -> datetime:
    return datetime.now(IST)


def round_to_strike(price: float, step: int) -> int:
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
ExpiryRole = Literal[tuple(EXPIRY_ROLES)]


def expiry_roles(expiries: list[str], today: date, weekly: bool = True) -> dict[str, date]:
    """Date of each role: the next two weeklies and the next two monthlies.

    A monthly is the last listed expiry of its calendar month. Roles can
    share a date (late in a month the next weekly is the monthly). Without
    weeklies (`weekly` False) only the monthly roles are returned.
    """
    upcoming = sorted({d for d in (date.fromisoformat(e) for e in expiries) if d >= today})
    if not upcoming:
        raise ValueError(f"No expiry on or after {today} in {expiries}")
    month_end = {}
    for d in upcoming:
        month_end[(d.year, d.month)] = d  # ascending, so the month's last wins
    monthlies = sorted(month_end.values())
    roles = {"month1": monthlies[0]}
    if len(monthlies) > 1:
        roles["month2"] = monthlies[1]
    if weekly:
        roles["week1"] = upcoming[0]
        if len(upcoming) > 1:
            roles["week2"] = upcoming[1]
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
        # The date already tells the two weeklies (and the two monthlies) apart.
        {"role": role, "date": d.isoformat(), "label": f"{d:%d %b} \u00b7 {EXPIRY_ROLES[role].split()[-1]}"}
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
            volume = int(row[5]) if len(row) > 5 and row[5] else 0
            ts = int(moment.timestamp())
            out[ts] = (ts, float(row[4]), volume)
    return [out[ts] for ts in sorted(out)]


def straddle_series(ce: list[Candle], pe: list[Candle]) -> list[Bar]:
    """CE + PE close per minute, with the volume both legs traded that minute.

    Illiquid strikes skip minutes, so each leg carries its last close
    forward (contributing no volume); the series starts once both legs
    have traded.
    """
    legs = [{c[0]: c for c in ce}, {c[0]: c for c in pe}]
    last = [None, None]
    out = []
    for ts in sorted(legs[0].keys() | legs[1].keys()):
        minute_volume = 0
        for i, leg in enumerate(legs):
            candle = leg.get(ts)
            if candle:
                last[i] = candle[1]
                minute_volume += candle[2]
        if last[0] is not None and last[1] is not None:
            out.append((ts, round(last[0] + last[1], 2), minute_volume))
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
