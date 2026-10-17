"""The algo's rules and bookkeeping, with no I/O.

Shared by the live server (runner.py, with Kite orders or paper fills) and the
simulator (sim.py). Everything comes in as a call carrying the moment it
happened - a 1-minute bar that just closed, live prices, the clock, an order's
progress - and the core answers with what to do (Enter / Exit intents) and with
events to show and log.

Quantities are never kept as separate counters: a trade's orders (Kite's, or
completed paper orders) are the record of what was sold and bought back. Each
trade has a `want`: the units per leg it should hold once its orders settle.
Exit decisions only lower it; whoever executes (paper fills here, Kite orders
in live.py) buys back the difference, so a failed order is simply sent again
and a position can never be bought back past zero.

The rules (README.md, Algo trading):
- entry, at a 1-minute close: a double bottom sell confirmed by that bar, with
  the close below VWAP and below VWMA 10; one trade per chart, Simultaneous
  Trades at once, Max trades per day, nothing after Last entry;
- exits, at a 1-minute close: the newest column is X with its top box above the
  lower of VWAP and VWMA 10 (stop); the close at entry - target/3 books half
  (rounded up to whole lots) and adds a breakeven stop at the entry; the close at
  entry - target books the rest;
- on the live price: Max loss per trade, and the Daily loss limit (which also
  switches the algo Off for the day);
- on the clock: the Exit Time closes everything and switches the algo Off.
"""

import math
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time
from typing import ClassVar

from pnf.pnf import Tip
from pnf.series import UNDERLYINGS

PAPER, LIVE = "paper", "live"
MODES = (PAPER, LIVE)
SELL, BUY = "SELL", "BUY"
KINDS = ("CE", "PE")

# Trade states. A failed trade is one whose entry did not complete: whatever was sold of
# it is bought back at once.
ENTERING, OPEN, CLOSED, FAILED = "entering", "open", "closed", "failed"

# Order statuses that are final (Kite's), plus LOST: an order whose outcome was unknown
# and that never showed up in Kite's order book.
FINAL = {"COMPLETE", "REJECTED", "CANCELLED", "LOST"}

# Why a trade was (partly) bought back.
STOP = "stop"
TARGET1 = "target1"
BREAKEVEN = "breakeven"
TARGET = "target"
MAX_LOSS = "max_loss"
DAILY_LOSS = "daily_loss"
EXIT_TIME = "exit_time"
EXIT_ALL = "exit_all"
BY_YOU = "by_you"
UNWIND = "unwind"
EXTERNAL = "external"
DAY_END = "day_end"
LATE = "late"
REASONS = {
    STOP: "Stop: an X above the lower of VWAP and VWMA 10",
    TARGET1: "First target",
    BREAKEVEN: "Breakeven stop",
    TARGET: "Full target",
    MAX_LOSS: "Max loss per trade",
    DAILY_LOSS: "Daily loss limit",
    EXIT_TIME: "Exit Time",
    EXIT_ALL: "Exit all algo trades (by you)",
    BY_YOU: "Exit (by you)",
    UNWIND: "Entry failed: buying back what was sold",
    EXTERNAL: "Bought back outside the algo",
    DAY_END: "Still open at the end of the day",
    LATE: "Kite listed an order late",
}

EXTERNAL_ID = "external-"  # the orders standing for buy-backs made outside the algo

EARLIEST = time(9, 15)
LATEST_EXIT = time(15, 25)  # before Zerodha squares off intraday (MIS) F&O positions at 15:26
EPS = 1e-9  # price comparisons: closes and levels are in paise
MINUS = chr(0x2212)  # the minus sign the page shows in amounts and chart names
JOIN = chr(0x2060)  # a word joiner: keeps the sign and the amount on one line
DASH = chr(0x2013)  # an unknown value


class AlgoError(Exception):
    """An action the algo refuses; the message is shown on the page."""


class SettingsError(ValueError):
    """Settings that cannot be used: {field: message}, shown next to each field."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


# ---------------- Formatting (event texts) ----------------


def inr(n: float) -> str:
    """Whole rupees in Indian digit grouping: 100000 -> '1,00,000'."""
    digits = str(abs(round(n)))
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head, *groups, tail] if head else [*groups, tail])


def fmt_rupees(v: float) -> str:
    return f"{'+' if round(v) >= 0 else MINUS}{JOIN}₹{inr(v)}"


def fmt_price(p: float | None) -> str:
    return DASH if p is None else f"{p:,.2f}"


def chart_label(offset: int) -> str:
    """The chart's name as the page shows it (written with a minus sign, not a hyphen)."""
    return "ATM" if offset == 0 else f"ATM {'+' if offset > 0 else MINUS} {abs(offset)}"


def parse_hhmm(text: str) -> time:
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(text).strip())
    if not m:
        raise ValueError(f"{text!r} is not a time like 14:45")
    return time(int(m.group(1)), int(m.group(2)))


# ---------------- Settings ----------------


def _default_qty() -> dict[str, int]:
    return {k: u.algo_qty for k, u in UNDERLYINGS.items()}


def _default_target() -> dict[str, float]:
    return {k: float(u.algo_target) for k, u in UNDERLYINGS.items()}


@dataclass
class Settings:
    """What the Algo menu sets. Quantity and Target Points are per index."""

    mode: str = PAPER
    max_open: int = 1  # Simultaneous Trades, across the three charts
    last_entry: str = "14:45"
    exit_time: str = "15:12"
    max_loss: int = 100_000  # rupees, per trade
    daily_loss: int = 100_000  # rupees, all of the day's algo trades
    max_trades: int = 5  # entries per day
    qty: dict[str, int] = field(default_factory=_default_qty)
    target: dict[str, float] = field(default_factory=_default_target)

    JSON_NAMES: ClassVar[dict[str, str]] = {
        "mode": "mode",
        "maxOpen": "max_open",
        "lastEntry": "last_entry",
        "exitTime": "exit_time",
        "maxLoss": "max_loss",
        "dailyLoss": "daily_loss",
        "maxTrades": "max_trades",
        "qty": "qty",
        "target": "target",
    }

    @property
    def last_entry_at(self) -> time:
        return parse_hhmm(self.last_entry)

    @property
    def exit_at(self) -> time:
        return parse_hhmm(self.exit_time)

    def to_json(self) -> dict:
        return {name: getattr(self, attr) for name, attr in self.JSON_NAMES.items()}

    def updated(self, changes: dict) -> "Settings":
        """A copy with `changes` (JSON names) applied and checked; raises SettingsError."""
        errors: dict[str, str] = {}
        values = {attr: getattr(self, attr) for attr in self.JSON_NAMES.values()}
        values["qty"], values["target"] = dict(self.qty), dict(self.target)
        for name, value in changes.items():
            attr = self.JSON_NAMES.get(name)
            if attr is None:
                errors[name] = f"Unknown setting {name!r}"
            elif attr in ("qty", "target"):
                if not isinstance(value, dict):
                    errors[name] = "Expected a value per index"
                    continue
                for key, v in value.items():
                    if key not in UNDERLYINGS:
                        errors[f"{name}.{key}"] = f"Unknown index {key!r}"
                    elif attr == "qty":
                        values["qty"][key] = _whole(v, errors, f"qty.{key}")
                    else:
                        values["target"][key] = _points(v, errors, f"target.{key}")
            elif attr in ("max_open", "max_loss", "daily_loss", "max_trades"):
                values[attr] = _whole(value, errors, name)
            else:
                values[attr] = value if isinstance(value, str) else str(value)
        if errors:
            raise SettingsError(errors)
        result = Settings(**values)
        problems = result.problems()
        if problems:
            raise SettingsError(problems)
        return result

    def problems(self, lot_sizes: dict[str, int | None] | None = None) -> dict[str, str]:
        """{field: message} for every setting that cannot be used. With `lot_sizes` (per
        index, when known) quantities are also checked against whole lots."""
        errors = {}
        if self.mode not in MODES:
            errors["mode"] = "Mode is Paper or Live"
        if not 1 <= self.max_open <= 3:
            errors["maxOpen"] = "Simultaneous Trades is 1, 2 or 3 (one per chart)"
        times = {}
        for name, attr in (("lastEntry", "last_entry"), ("exitTime", "exit_time")):
            try:
                times[name] = parse_hhmm(getattr(self, attr))
            except ValueError:
                errors[name] = "Enter a time like 14:45"
        exit_at, last = times.get("exitTime"), times.get("lastEntry")
        if exit_at is not None and not EARLIEST < exit_at <= LATEST_EXIT:
            errors["exitTime"] = "Between 09:16 and 15:25 (Zerodha squares off intraday positions at 15:26)"
        if last is not None and not EARLIEST <= last <= LATEST_EXIT:
            errors["lastEntry"] = "Between 09:15 and 15:25"
        elif last is not None and exit_at is not None and last > exit_at:
            errors["lastEntry"] = "No later than the Exit Time"
        if not 1_000 <= self.max_loss <= 10_000_000:
            errors["maxLoss"] = "Between ₹1,000 and ₹1,00,00,000"
        if not 1_000 <= self.daily_loss <= 10_000_000:
            errors["dailyLoss"] = "Between ₹1,000 and ₹1,00,00,000"
        if not 1 <= self.max_trades <= 50:
            errors["maxTrades"] = "Between 1 and 50"
        for key in UNDERLYINGS:
            problem = quantity_problem(key, self.qty.get(key, 0), (lot_sizes or {}).get(key))
            if problem:
                errors[f"qty.{key}"] = problem
            if not 0.05 <= self.target.get(key, 0) <= 10_000:
                errors[f"target.{key}"] = "Between 0.05 and 10,000 points"
        return errors

    @classmethod
    def from_json(cls, data) -> "Settings":
        """Settings saved earlier; anything missing or unusable keeps its default."""
        result = cls()
        if not isinstance(data, dict):
            return result
        try:
            return result.updated(data)
        except SettingsError:
            pass
        # Field by field, until nothing more applies: settings that depend on each other
        # (Last entry before Exit Time) may need another pass.
        left = dict(data)
        while left:
            progress = False
            for name, value in list(left.items()):
                try:
                    result = result.updated({name: value})
                except SettingsError:
                    continue
                del left[name]
                progress = True
            if not progress:
                break
        return result


def _whole(value, errors: dict, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)  # JSON parsers let NaN and Infinity through
        or value != int(value)
    ):
        errors[name] = "Enter a whole number"
        return 0
    return int(value)


def _points(value, errors: dict, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        errors[name] = "Enter a number of points"
        return 0.0
    return round(float(value), 2)


def quantity_problem(underlying: str, qty: int, lot_size: int | None) -> str | None:
    """Why `qty` cannot be one algo trade on `underlying`, or None."""
    u = UNDERLYINGS[underlying]
    if qty < 1:
        return "Enter a quantity"
    if qty > u.max_sell_qty:
        return f"At most {inr(u.max_sell_qty)} (twice the default {inr(u.algo_qty)})"
    if lot_size:
        if qty % lot_size:
            return f"A multiple of the lot size, {lot_size}"
        if qty < 2 * lot_size:
            return f"At least 2 lots ({2 * lot_size}), so half can be booked at the first target"
    return None


def half_units(units: int, lot_size: int) -> int:
    """What the first target books: half of the lots, rounded up (25 lots: 13)."""
    lots = units // lot_size
    return math.ceil(lots / 2) * lot_size


# ---------------- Chart rules ----------------


@dataclass
class Candidate:
    """A double bottom sell confirmed by the bar that just closed."""

    col: int
    level: float  # the broken bottom (price)
    close: float
    vwap: float | None
    vwma: float | None

    @property
    def below_vwap(self) -> bool:
        return self.vwap is not None and self.close < self.vwap

    @property
    def below_vwma(self) -> bool:
        return self.vwma is not None and self.close < self.vwma

    @property
    def qualifies(self) -> bool:
        return self.below_vwap and self.below_vwma


def entry_candidate(tip: Tip, bar) -> Candidate | None:
    """The double bottom sell the bar just closed confirmed, with its VWAP and VWMA 10."""
    if tip is None or tip.sell is None:
        return None
    col, level = tip.sell
    return Candidate(col, level, bar[1], tip.vwap, tip.vwma)


def stop_level(tip: Tip) -> float | None:
    """The lower of VWAP and VWMA 10 at the newest column (either alone when the other is unknown)."""
    known = [v for v in (tip.vwap, tip.vwma) if v is not None]
    return min(known) if known else None


def stop_hit(tip: Tip) -> bool:
    """The newest column is X and its top box is above the lower of VWAP and VWMA 10."""
    level = stop_level(tip)
    return tip.dir == "X" and level is not None and tip.top > level + EPS


# ---------------- Trades ----------------


@dataclass
class Order:
    id: str
    kind: str  # CE | PE
    side: str  # SELL (entry) | BUY (exit)
    qty: int
    filled: int = 0
    avg: float = 0.0  # average price of what filled
    status: str = "OPEN"  # Kite's status; UNKNOWN: placed, but the reply was lost
    exit: int | None = None  # the exit decision a BUY carries out
    placed_at: float = 0.0  # epoch seconds
    message: str = ""

    @property
    def done(self) -> bool:
        return self.status in FINAL

    @property
    def working(self) -> int:
        """Units it may still fill."""
        return 0 if self.done else max(0, self.qty - self.filled)


@dataclass
class Leg:
    kind: str
    key: str  # Neo "<segment>|<token>": where its live price comes from
    symbol: str
    last: float | None = None  # the latest price seen


@dataclass
class ExitDecision:
    at: int  # epoch seconds
    reason: str
    keep: int  # units per leg the trade keeps after it
    note: str = ""  # what triggered it, e.g. "close 169.50 at or below 170.00"
    reported: bool = False


@dataclass
class Trade:
    id: str
    seq: int  # the day's trade number (order tag)
    day: str  # ISO date
    mode: str
    underlying: str
    expiry: str  # ISO date
    strike: int
    chart: str  # chart key at entry: atm_minus | atm | atm_plus
    offset: int
    lot_size: int
    qty: int  # units sold per leg
    target: float  # Target Points, as set when it opened
    max_loss: int  # rupees, as set when it opened
    signal: dict  # {"ts", "close", "vwap", "vwma", "level"}
    opened_at: int
    legs: dict[str, Leg]
    state: str = ENTERING
    managed: bool = True  # False once you take it over (Algo Off)
    want: int = 0  # units per leg it should hold once its orders settle
    half_done: bool = False
    exits: list[ExitDecision] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    stop: dict | None = None  # {"level", "at"}: the lower of VWAP and VWMA 10 at the last close
    refused: str = ""  # why an entry order was refused before it was placed
    closed_at: int | None = None
    reason: str = ""  # why it closed or failed

    @property
    def name(self) -> str:
        return f"{self.underlying} {inr(self.strike)} {chart_label(self.offset)}"

    @property
    def group(self) -> str:
        """The straddle's key, as kite.contracts.group_key builds it."""
        return f"{self.underlying}|{self.expiry}|{self.strike}"

    def _sum(self, kind: str, side: str) -> tuple[int, float]:
        orders = [o for o in self.orders if o.kind == kind and o.side == side]
        return sum(o.filled for o in orders), sum(o.filled * o.avg for o in orders)

    def sold(self, kind: str) -> int:
        return self._sum(kind, SELL)[0]

    def bought(self, kind: str) -> int:
        return self._sum(kind, BUY)[0]

    def open(self, kind: str) -> int:
        return self.sold(kind) - self.bought(kind)

    def working(self, kind: str, side: str) -> int:
        return sum(o.working for o in self.orders if o.kind == kind and o.side == side)

    def avg(self, kind: str) -> float | None:
        sold, value = self._sum(kind, SELL)
        return value / sold if sold else None

    @property
    def holding(self) -> int:
        """Units short per leg (the larger, should the legs differ)."""
        return max(0, *(self.open(k) for k in KINDS))

    @property
    def entry(self) -> float | None:
        """The straddle price sold at (CE average + PE average), once the entry is complete."""
        if self.state == ENTERING or any(self.avg(k) is None for k in KINDS):
            return None
        return round(self.avg("CE") + self.avg("PE"), 2)

    @property
    def target1(self) -> float | None:
        return None if self.entry is None else round(self.entry - self.target / 3, 2)

    @property
    def target2(self) -> float | None:
        return None if self.entry is None else round(self.entry - self.target, 2)

    @property
    def flat(self) -> bool:
        """Nothing short, and no order that could still fill. A leg bought back past zero (an
        order Kite listed late) counts as flat: the trade is done, and an alarm reports the long."""
        return all(self.open(k) <= 0 for k in KINDS) and not any(o.working for o in self.orders)

    @property
    def active(self) -> bool:
        """Holds (or may still come to hold) a position."""
        if self.closed_at is not None:
            return False
        return self.state == ENTERING or not self.flat

    def price(self, kind: str, prices: dict | None) -> float | None:
        leg = self.legs[kind]
        value = (prices or {}).get(leg.key)
        return value if value is not None else leg.last

    def booked(self) -> float:
        """Rupees booked: each leg's buy-backs against its average sell price, up to what it
        sold (anything bought back past zero is not part of the trade)."""
        total = 0.0
        for kind in KINDS:
            avg = self.avg(kind)
            bought, value = self._sum(kind, BUY)
            if avg is not None and bought:
                total += (avg - value / bought) * min(bought, self.sold(kind))
        return total

    def pnl(self, prices: dict | None = None) -> float:
        """Booked plus open P&L in rupees, the open part at `prices` (else the last seen)."""
        if self.closed_at is not None:  # final: worked out once (the day's limit asks every tick)
            # ... for these orders: a late order found in Kite's book changes it.
            key = (self.closed_at, len(self.orders), sum(o.filled for o in self.orders))
            cached = self.__dict__.get("_final_pnl")
            if cached is None or cached[0] != key:
                cached = self.__dict__["_final_pnl"] = (key, self.booked())
            return cached[1]
        total = self.booked()
        for kind in KINDS:
            avg, price, held = self.avg(kind), self.price(kind, prices), max(0, self.open(kind))
            if held and avg is not None and price is not None:
                total += (avg - price) * held
        return total

    def decision_fills(self, index: int) -> tuple[int, float | None, float]:
        """Units per leg, straddle price and P&L of what exit decision `index` bought back."""
        orders = [o for o in self.orders if o.exit == index and o.filled]
        per_leg = {k: sum(o.filled for o in orders if o.kind == k) for k in KINDS}
        units = max(per_leg.values()) if per_leg else 0
        if not units:
            return 0, None, 0.0
        price = 0.0
        pnl = 0.0
        for kind in KINDS:
            leg_orders = [o for o in orders if o.kind == kind]
            filled = sum(o.filled for o in leg_orders)
            if filled:
                value = sum(o.filled * o.avg for o in leg_orders)
                price += value / filled
                pnl += (self.avg(kind) or 0) * filled - value
        return units, round(price, 2), pnl

    def to_json(self) -> dict:
        data = asdict(self)
        return data

    @classmethod
    def from_json(cls, data: dict) -> "Trade":
        data = dict(data)
        if not isinstance(data.get("legs"), dict):
            raise TypeError("a trade's legs are a map")
        data["legs"] = {k: Leg(**v) for k, v in data["legs"].items()}
        data["exits"] = [ExitDecision(**e) for e in data.get("exits", [])]
        data["orders"] = [Order(**o) for o in data.get("orders", [])]
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def trade_json(t: Trade) -> dict:
    """A trade as the page shows it (the live page and the simulator alike)."""
    exits = []
    for i, d in enumerate(t.exits):
        units, price, pnl = t.decision_fills(i)
        exits.append(
            {
                "at": d.at,
                "reason": d.reason,
                "text": REASONS.get(d.reason, d.reason),
                "note": d.note,
                "units": units,
                "price": price,
                "pnl": round(pnl, 2),
            }
        )
    return {
        "id": t.id,
        "day": t.day,
        "mode": t.mode,
        "name": t.name,
        "underlying": t.underlying,
        "expiry": t.expiry,
        "strike": t.strike,
        "chart": t.chart,
        "offset": t.offset,
        "lotSize": t.lot_size,
        "qty": t.qty,
        "target": t.target,
        "maxLoss": t.max_loss,
        "state": t.state,
        "active": t.active,
        "managed": t.managed,
        "want": t.want,
        "holding": t.holding,
        "halfDone": t.half_done,
        "entry": t.entry,
        "target1": t.target1,
        "target2": t.target2,
        "stop": t.stop,
        # Live P&L is worked out in the page from the live prices: booked + (avg - price) x open.
        "legs": {
            k: {"key": leg.key, "symbol": leg.symbol, "open": t.open(k), "avg": t.avg(k)}
            for k, leg in t.legs.items()
        },
        "booked": round(t.booked(), 2),
        "pnl": round(t.pnl(), 2),  # at the last prices seen: closed trades, and the page's fallback
        "openedAt": t.opened_at,
        "closedAt": t.closed_at,
        "reason": t.reason,
        "reasonText": REASONS.get(t.reason, t.reason),
        "signal": t.signal,
        "exits": exits,
        "working": sum(1 for o in t.orders if not o.done),
    }


# ---------------- Inputs and intents ----------------


@dataclass
class ChartView:
    """One chart as the algo sees it at a minute close."""

    key: str
    offset: int
    strike: int
    underlying: str
    expiry: date
    ce: dict  # Neo leg: {"key", "symbol", ...}
    pe: dict
    lot_size: int | None
    bar: tuple | None = None  # (ts, close, volume): the 1-minute bar that just closed, if any
    tip: Tip | None = None  # the chart's newest column with that bar (pnf.pnf.Tip)
    blocked: str = ""  # why no algo trade may open on it now (e.g. your own position there)


@dataclass
class Enter:
    trade: Trade


@dataclass
class Exit:
    trade: Trade
    decision: int  # index into trade.exits


def chart_priority(view: ChartView) -> tuple:
    """ATM first, then the lower strike, then the higher (for the last free slots)."""
    return (view.offset != 0, view.offset)


# ---------------- The algo ----------------


class AlgoCore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.on = False  # takes new trades
        self.on_mode = self.settings.mode  # the mode it was switched On in
        self.day: date | None = None
        self.seq = 0  # trades opened today
        self.trades: list[Trade] = []
        self.halted = ""  # why no more trades today (the daily loss limit), else ""
        self.exit_time_done = False
        self.events: list[dict] = []

    # ---------------- State ----------------

    @property
    def mode(self) -> str:
        return self.on_mode if self.on else self.settings.mode

    def active(self) -> list[Trade]:
        return [t for t in self.trades if t.active]

    def managed(self) -> list[Trade]:
        return [t for t in self.active() if t.managed]

    def todays(self, mode: str | None = None) -> list[Trade]:
        day, mode = self.day.isoformat() if self.day else "", mode or self.mode
        return [t for t in self.trades if t.day == day and t.mode == mode]

    def day_pnl(self, prices: dict | None = None, mode: str | None = None) -> float:
        return sum(t.pnl(prices) for t in self.todays(mode))

    def trade(self, trade_id: str) -> Trade | None:
        return next((t for t in self.trades if t.id == trade_id), None)

    def take_events(self) -> list[dict]:
        events, self.events = self.events, []
        return events

    def _event(self, kind: str, now: datetime, title: str, detail: str = "", trade: Trade | None = None):
        self.events.append(
            {
                "kind": kind,  # opened | booked | closed | failed | skipped | state
                "title": title,
                "detail": detail,
                "trade": trade.id if trade else None,
                "mode": trade.mode if trade else self.mode,
                "at": int(now.timestamp()),
                # The trade's P&L in rupees once it closed (the page colours the toast by it).
                "pnl": round(trade.pnl(), 2) if trade and kind == "closed" else None,
            }
        )

    def _tag(self, trade: Trade) -> str:
        return " (paper)" if trade.mode == PAPER else ""

    def roll(self, now: datetime) -> None:
        """Start a new day: counters reset, Algo Off, the earlier days' trades go. A paper
        trade still open is closed at its last price. A live one is let go with an alert:
        its MIS position cannot outlast its day (Zerodha squares it off at 15:26), and
        Kite's new order book no longer lists its orders."""
        today = now.date()
        if self.day == today:
            return
        previous, self.day = self.day, today
        if previous is None:
            return
        self.seq, self.halted, self.exit_time_done, self.on = 0, "", False, False
        for t in self.trades:
            if not t.active:
                continue
            if t.mode == PAPER:
                index = self._decide(now, t, 0, DAY_END, "closed at its last price")
                self._fill_exit_paper(t, index, {}, now)
                continue
            self._event(
                "failed",
                now,
                f"Algo trade left open overnight: {t.name}",
                f"Still open when {date.fromisoformat(t.day):%d %b} ended, so the algo no longer manages it."
                " Zerodha squares MIS positions off at 15:26: check Kite's positions and P&L.",
                t,
            )
        self.trades = []

    def _note_prices(self, prices: dict | None) -> None:
        if not prices:
            return
        for t in self.active():
            for leg in t.legs.values():
                if prices.get(leg.key) is not None:
                    leg.last = prices[leg.key]

    # ---------------- Switching ----------------

    def switch_on(self, now: datetime) -> None:
        """Start taking trades, in the mode the settings say; raises AlgoError."""
        self.roll(now)
        if self.on:
            return
        if self.halted:
            raise AlgoError(f"The algo is stopped for today: {self.halted}.")
        s = self.settings
        if self.exit_time_done or now.time() >= s.exit_at:
            raise AlgoError(f"The Exit Time ({s.exit_time}) has passed: the algo takes no more trades today.")
        if now.time() >= s.last_entry_at:
            raise AlgoError(f"It is past the Last entry time ({s.last_entry}): no more trades today.")
        if len(self.todays(s.mode)) >= s.max_trades:
            raise AlgoError(f"Max trades per day ({s.max_trades}) already reached today.")
        problems = s.problems()
        if problems:
            raise AlgoError(f"Fix the settings first: {next(iter(problems.values()))}.")
        self.on, self.on_mode = True, s.mode
        self._event("state", now, f"Algo switched On ({s.mode})", "Takes trades on double bottom sells.")

    def switch_off(self, now: datetime) -> list[Trade]:
        """Stop everything: no new trades, and open trades are left to you (a failed
        entry still buys back what it sold). Returns the trades handed over."""
        self.roll(now)
        was_on = self.on
        self.on = False
        released = [t for t in self.managed() if t.state != FAILED]
        for t in released:
            t.managed = False
        if was_on or released:
            detail = (
                f"{len(released)} open trade{'s' if len(released) > 1 else ''} left to you: "
                + ", ".join(t.name for t in released)
                if released
                else ""
            )
            self._event("state", now, "Algo switched Off", detail)
        return released

    def stop_entries(self, now: datetime, why: str) -> None:
        """No new trades until switched On again; open trades are still managed."""
        if not self.on:
            return
        self.on = False
        self._event("state", now, "Algo Off for new trades", f"{why} Its open trades are still managed.")

    def exit_all(self, now: datetime) -> list[Exit]:
        """Buy back every open algo trade at once, and switch Off."""
        self.roll(now)
        self.on = False
        intents = []
        for t in self.active():
            t.managed = True
            if t.want > 0 or t.state == ENTERING:
                intents.append(Exit(t, self._decide(now, t, 0, EXIT_ALL)))
        self._event(
            "state", now, "Exit all algo trades", f"{len(intents)} trade(s) being bought back; Algo Off."
        )
        return intents

    def exit_trade(self, now: datetime, trade_id: str) -> Exit:
        t = self.trade(trade_id)
        if t is None or not t.active:
            raise AlgoError("That algo trade is not open.")
        t.managed = True
        return Exit(t, self._decide(now, t, 0, BY_YOU))

    # ---------------- Decisions ----------------

    def on_minute(self, now: datetime, views: list[ChartView], prices: dict | None = None) -> list:
        """A 1-minute bar closed on the charts in `views`: exit rules, then entries."""
        self.roll(now)
        self._note_prices(prices)
        intents: list = []
        for t in self.managed():
            if t.state != OPEN or t.want == 0:
                continue
            view = self._view_of(t, views)
            if view is not None and view.bar is not None and view.tip is not None:
                intents += self._exit_rules(now, t, view)
        if self.on and not self.halted:
            intents += self._entries(now, views)
        return intents

    def on_prices(self, now: datetime, prices: dict) -> list:
        """Live prices: Max loss per trade and the Daily loss limit."""
        self.roll(now)
        self._note_prices(prices)
        intents: list = []
        for t in self.managed():
            if t.state == OPEN and t.want > 0 and t.pnl(prices) <= -t.max_loss + EPS:
                note = f"trade P&L {fmt_rupees(t.pnl(prices))}"
                intents.append(Exit(t, self._decide(now, t, 0, MAX_LOSS, note)))
        day = self.day_pnl(prices)
        if not self.halted and day <= -self.settings.daily_loss + EPS:
            self.halted = f"the daily loss limit of {fmt_rupees(-self.settings.daily_loss)} was reached"
            self.on = False
            for t in self.managed():
                if t.want > 0:
                    intents.append(Exit(t, self._decide(now, t, 0, DAILY_LOSS, f"day {fmt_rupees(day)}")))
            self._event(
                "state",
                now,
                "Daily loss limit reached: Algo Off for today",
                f"Today's algo P&L {fmt_rupees(day)}; every open algo trade is being bought back.",
            )
        return intents

    def on_clock(self, now: datetime, prices: dict | None = None) -> list:
        """The Exit Time closes every open trade and switches the algo Off for the day."""
        self.roll(now)
        self._note_prices(prices)
        if self.exit_time_done or now.time() < self.settings.exit_at:
            return []
        self.exit_time_done = True
        intents = [Exit(t, self._decide(now, t, 0, EXIT_TIME)) for t in self.managed() if t.want > 0]
        if self.on or intents:
            self.on = False
            self._event(
                "state",
                now,
                f"Exit Time {self.settings.exit_time}: Algo Off for today",
                f"{len(intents)} open trade(s) being bought back." if intents else "",
            )
        return intents

    def _view_of(self, t: Trade, views: list[ChartView]) -> ChartView | None:
        return next(
            (v for v in views if v.ce["key"] == t.legs["CE"].key and v.pe["key"] == t.legs["PE"].key), None
        )

    def _exit_rules(self, now: datetime, t: Trade, view: ChartView) -> list[Exit]:
        tip, (ts, close) = view.tip, view.bar[:2]
        level = stop_level(tip)
        t.stop = None if level is None else {"level": round(level, 2), "at": ts + 60}
        if stop_hit(tip):
            note = f"X at {fmt_price(tip.top)} above {fmt_price(level)}"
            return [Exit(t, self._decide(now, t, 0, STOP, note))]
        entry = t.entry
        if entry is None:
            return []
        if close <= t.target2 + EPS:
            note = f"close {fmt_price(close)} at or below {fmt_price(t.target2)}"
            return [Exit(t, self._decide(now, t, 0, TARGET, note))]
        if not t.half_done and close <= t.target1 + EPS:
            t.half_done = True
            base = min(t.want, t.holding)
            note = f"close {fmt_price(close)} at or below {fmt_price(t.target1)}"
            return [Exit(t, self._decide(now, t, base - half_units(base, t.lot_size), TARGET1, note))]
        if t.half_done and close >= entry - EPS:
            note = f"close {fmt_price(close)} back at the entry {fmt_price(entry)}"
            return [Exit(t, self._decide(now, t, 0, BREAKEVEN, note))]
        return []

    def _decide(self, now: datetime, t: Trade, keep: int, reason: str, note: str = "") -> int:
        """Lower what the trade should hold to `keep` units per leg; returns the decision's index."""
        t.want = max(0, min(keep, t.want))
        t.exits.append(ExitDecision(at=int(now.timestamp()), reason=reason, keep=t.want, note=note))
        return len(t.exits) - 1

    def _entries(self, now: datetime, views: list[ChartView]) -> list[Enter]:
        intents = []
        for view in sorted(views, key=chart_priority):
            if view.bar is None or view.tip is None:
                continue
            c = entry_candidate(view.tip, view.bar)
            if c is None:
                continue
            name = f"{view.underlying} {inr(view.strike)} {chart_label(view.offset)}"
            if not c.qualifies:
                if c.below_vwap:  # it raised an alert, so say why it was not traded
                    why = (
                        f"the close {fmt_price(c.close)} is not below VWMA 10 ({fmt_price(c.vwma)})"
                        if c.vwma is not None
                        else "VWMA 10 is not known yet"
                    )
                    self._event("skipped", now, f"Algo did not trade a double bottom sell: {name}", why)
                continue
            why = self._entry_block(now, view)
            if why:
                self._event("skipped", now, f"Algo skipped a double bottom sell: {name}", why)
                continue
            intents.append(Enter(self._open(now, view, c)))
        return intents

    def _entry_block(self, now: datetime, view: ChartView) -> str:
        s = self.settings
        close_minute = now.replace(second=0, microsecond=0).time()
        if close_minute > s.last_entry_at:
            return f"after the Last entry time ({s.last_entry})"
        if len(self.todays(self.on_mode)) >= s.max_trades:
            return f"Max trades per day ({s.max_trades}) reached"
        legs = (view.ce["key"], view.pe["key"])
        mine = [t for t in self.active() if t.mode == self.on_mode]  # paper trades never hold live slots
        if any((t.legs["CE"].key, t.legs["PE"].key) == legs for t in mine):
            return "this chart already has an algo trade"
        if len(mine) >= s.max_open:
            return f"no free slot (Simultaneous Trades: {s.max_open})"
        if view.blocked:
            return view.blocked
        if not view.lot_size:
            return "the lot size is not known"
        problem = quantity_problem(view.underlying, s.qty[view.underlying], view.lot_size)
        if problem:
            return f"Quantity {inr(s.qty[view.underlying])}: {problem.lower()}"
        return ""

    def _open(self, now: datetime, view: ChartView, c: Candidate) -> Trade:
        self.seq += 1
        u, s = view.underlying, self.settings
        known = [v for v in (c.vwap, c.vwma) if v is not None]
        t = Trade(
            id=f"{self.day.isoformat()}-{self.seq}",
            seq=self.seq,
            day=self.day.isoformat(),
            mode=self.on_mode,
            underlying=u,
            expiry=view.expiry.isoformat(),
            strike=view.strike,
            chart=view.key,
            offset=view.offset,
            lot_size=view.lot_size,
            qty=s.qty[u],
            target=s.target[u],
            max_loss=s.max_loss,
            signal={
                "ts": view.bar[0],
                "close": c.close,
                "vwap": c.vwap,
                "vwma": c.vwma,
                "level": round(c.level, 2),
            },
            opened_at=int(now.timestamp()),
            legs={
                "CE": Leg("CE", view.ce["key"], view.ce.get("symbol", "")),
                "PE": Leg("PE", view.pe["key"], view.pe.get("symbol", "")),
            },
            want=s.qty[u],
            # Its stop from the start: the lower of VWAP and VWMA 10 at the signal's close.
            stop={"level": round(min(known), 2), "at": view.bar[0] + 60} if known else None,
        )
        self.trades.append(t)
        return t

    # ---------------- Orders and fills ----------------

    def add_order(self, t: Trade, order: Order, now: datetime) -> None:
        t.orders.append(order)
        self.settle(t, now)

    def entry_refused(self, t: Trade, why: str, now: datetime) -> None:
        """An entry order was refused before it was placed (or the other leg was held back)."""
        t.refused = t.refused or why
        self.settle(t, now)

    def external_fills(self, t: Trade, fills: dict[str, tuple[int, float | None]], now: datetime) -> None:
        """Units bought back outside the algo, seen in Kite's positions: {kind: (units,
        price)}. The algo goes on managing only what is left of the trade."""
        index = self._decide(now, t, t.want, EXTERNAL)
        for kind, (units, price) in fills.items():
            if units <= 0:
                continue
            t.orders.append(
                Order(
                    id=f"{EXTERNAL_ID}{t.id}-{kind}-{len(t.orders)}",
                    kind=kind,
                    side=BUY,
                    qty=units,
                    filled=units,
                    avg=price if price is not None else t.legs[kind].last or 0.0,
                    status="COMPLETE",
                    exit=index,
                    placed_at=now.timestamp(),
                )
            )
        t.want = min(t.want, t.holding)
        t.exits[index].keep = t.want
        self.settle(t, now)

    def reopen(self, t: Trade, now: datetime, why: str) -> None:
        """A closed trade Kite shows short again (an order Kite listed late, found by its
        tag): managed again, to buy back what it left short."""
        t.closed_at = None
        t.managed = True
        if t.state == CLOSED:
            t.state = OPEN
        self._decide(now, t, 0, LATE, why)
        self._event(
            "failed", now, f"Algo found a late order: {t.name}", f"{why}; buying back what is short.", t
        )

    def fill_paper(self, intent, prices: dict, now: datetime) -> None:
        """Fill an intent at once at `prices` (paper mode and the simulator)."""
        t = intent.trade
        if isinstance(intent, Enter):
            missing = [k for k in KINDS if t.price(k, prices) is None]
            if missing:
                self.entry_refused(t, f"no live price for the {' and '.join(missing)}", now)
                return
            for kind in KINDS:
                price = t.price(kind, prices)
                order = Order(
                    id=f"paper-{t.id}-{kind}-sell",
                    kind=kind,
                    side=SELL,
                    qty=t.qty,
                    filled=t.qty,
                    avg=price,
                    status="COMPLETE",
                    placed_at=now.timestamp(),
                )
                t.orders.append(order)
            self.settle(t, now)
        else:
            self._fill_exit_paper(t, intent.decision, prices, now)

    def _fill_exit_paper(self, t: Trade, index: int, prices: dict, now: datetime) -> None:
        for kind in KINDS:
            units = t.open(kind) - t.working(kind, BUY) - t.want
            price = t.price(kind, prices)
            if units > 0 and price is not None:
                t.orders.append(
                    Order(
                        id=f"paper-{t.id}-{kind}-buy-{len(t.orders)}",
                        kind=kind,
                        side=BUY,
                        qty=units,
                        filled=units,
                        avg=price,
                        status="COMPLETE",
                        exit=index,
                        placed_at=now.timestamp(),
                    )
                )
        self.settle(t, now)

    def settle(self, t: Trade, now: datetime) -> None:
        """Bring a trade's state up to date with its orders: an entry that completed or
        failed, exit decisions carried out, and the trade closing."""
        if t.state == ENTERING:
            sells = [o for o in t.orders if o.side == SELL]
            if all(t.sold(k) >= t.qty for k in KINDS) and not any(o.working for o in sells):
                t.state = OPEN
                self._report_open(t, now)
            elif t.refused or any(o.done and o.filled < o.qty for o in sells):
                self._fail(t, now)
        for index, d in enumerate(t.exits):
            if d.reported or any(o.working for o in t.orders if o.exit == index):
                continue
            superseded = index < len(t.exits) - 1
            if not superseded and any(t.open(k) > d.keep for k in KINDS):
                continue
            d.reported = True
            closing = not superseded and t.holding == 0 and t.state == OPEN
            units, price, pnl = t.decision_fills(index)
            if units and not closing and d.reason not in (UNWIND, DAY_END):
                what = "booked half" if d.reason == TARGET1 else "bought back part"
                after = f" · breakeven stop at {fmt_price(t.entry)}" if d.reason == TARGET1 else ""
                self._event(
                    "booked",
                    now,
                    f"Algo {what}{self._tag(t)}: {t.name}",
                    f"{REASONS[d.reason]} · {inr(units)} @ {fmt_price(price)} ({fmt_rupees(pnl)}){after}",
                    t,
                )
        if t.closed_at is None and t.state in (OPEN, FAILED) and t.flat:
            t.closed_at = int(now.timestamp())
            if t.state == OPEN:
                t.state = CLOSED
                last = t.exits[-1] if t.exits else None
                t.reason = last.reason if last else ""
                units, price, _ = t.decision_fills(len(t.exits) - 1) if last else (0, None, 0.0)
                how = REASONS.get(t.reason, t.reason)
                if units:
                    how += f" · bought back {inr(units)} @ {fmt_price(price)}"
                self._event(
                    "closed",
                    now,
                    f"Algo closed {t.name}{self._tag(t)}: {fmt_rupees(t.pnl())}",
                    f"{how} · sold at {fmt_price(t.entry)}",
                    t,
                )
            elif any(t.sold(k) for k in KINDS):
                self._event(
                    "closed", now, f"Algo bought back the failed entry: {t.name}", fmt_rupees(t.pnl()), t
                )

    def _report_open(self, t: Trade, now: datetime) -> None:
        stop = t.signal.get("vwma"), t.signal.get("vwap")
        level = min(v for v in stop if v is not None) if any(v is not None for v in stop) else None
        self._event(
            "opened",
            now,
            f"Algo sold {t.name}{self._tag(t)}",
            f"{inr(t.qty)} @ {fmt_price(t.entry)}"
            f" (CE {fmt_price(t.avg('CE'))} + PE {fmt_price(t.avg('PE'))})"
            f" · targets {fmt_price(t.target1)} / {fmt_price(t.target2)}"
            f" · stop: an X above {fmt_price(level)}",
            t,
        )

    def _fail(self, t: Trade, now: datetime) -> None:
        """The entry did not complete: buy back whatever was sold, and stop taking trades."""
        t.state = FAILED
        problems = [
            f"{o.kind} {o.status.lower()}" + (f": {o.message}" if o.message else "")
            for o in t.orders
            if o.side == SELL and o.done and o.filled < o.qty
        ]
        t.reason = t.refused or "; ".join(problems) or "an entry order did not fill"
        t.managed = True  # its own unwinding, whatever the switch says
        if any(t.open(k) or t.working(k, SELL) for k in KINDS):
            self._decide(now, t, 0, UNWIND)
        else:
            t.want = 0
        was_on, self.on = self.on, False
        self._event(
            "failed",
            now,
            f"Algo entry failed: {t.name}",
            f"{t.reason}. Anything sold is being bought back"
            + (
                "; the algo switched Off - check margin and Kite, then switch it On again." if was_on else "."
            ),
            t,
        )

    # ---------------- Saving ----------------

    def to_json(self) -> dict:
        return {
            "version": 1,
            "settings": self.settings.to_json(),
            "day": self.day.isoformat() if self.day else None,
            "seq": self.seq,
            "halted": self.halted,
            "exitTimeDone": self.exit_time_done,
            "trades": [t.to_json() for t in self.trades],
        }

    @classmethod
    def from_json(cls, data: dict) -> "AlgoCore":
        """Restore after a restart: always Off for new trades; open trades stay managed."""
        if not isinstance(data, dict):
            raise TypeError("the saved state is a map")
        core = cls(Settings.from_json(data.get("settings")))
        core.day = date.fromisoformat(data["day"]) if data.get("day") else None
        core.seq = int(data.get("seq") or 0)
        core.halted = str(data.get("halted") or "")
        core.exit_time_done = bool(data.get("exitTimeDone"))
        core.trades = [Trade.from_json(t) for t in data.get("trades", [])]
        return core
