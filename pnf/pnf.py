"""Point & Figure construction, intraday VWAP and VWMA: the Python twin of static/pnf.js.

The browser draws the charts with static/pnf.js; the server-side algo (algo/)
trades on this module. The two must agree to the last box and the last bit:
the algo has to act on exactly what the chart shows. tests/test_pnf_parity.py
runs both on the same random series and requires identical results, so change
one only together with the other (same operations, in the same order).

Box size is fixed for the whole chart: box_pct of the first close. Rows are
integer box indexes on a grid anchored at that first close, so row k is the
price p0 + k * box. An X column's top is the highest row the price has
reached (floor); an O column's bottom is the lowest (ceil). A new column
starts only after a move of `reversal` boxes against the current one.

PnfBuilder adds one bar at a time and is the only implementation of these rules:
build_pnf runs it over a whole series, and the simulator's algo reads its newest
column (`tip`) after every bar instead of rebuilding the chart each minute.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

EPS = 1e-9
IST_OFFSET_S = 5.5 * 3600


def ist_day(ts: float) -> int:
    """IST calendar day number of an epoch-seconds timestamp (identifies the session)."""
    return math.floor((ts + IST_OFFSET_S) / 86400)


@dataclass
class Box:
    row: int
    ts: int  # the bar that printed it


@dataclass
class Column:
    dir: str  # "X" | "O"
    boxes: list[Box]
    hi: int
    lo: int
    start_ts: int  # first bar processed while this was the current column
    end_ts: int  # last such bar: where its VWAP is sampled
    volume: float  # what those bars traded


@dataclass
class Signal:
    type: str  # "buy" (double top) | "sell" (double bottom)
    col: int  # the breakout column
    row: int  # the breakout box
    level: int  # the previous column's top (buy) or bottom (sell) row
    ts: int  # the bar that printed the breakout box


@dataclass
class Pnf:
    p0: float
    box: float
    reversal: int
    columns: list[Column]
    hi: int
    lo: int
    signals: list[Signal]
    vwma_period: int
    vwma: list[float | None]  # price, one per column (None before `vwma_period` columns or without volume)
    vwap: list[float | None]  # intraday VWAP as it stood when each column last updated
    vwap_by_ts: dict[int, float | None]

    def price(self, row: float) -> float:
        return self.p0 + row * self.box

    def row_of(self, price: float) -> float:
        return (price - self.p0) / self.box

    def vwap_at(self, ts: int) -> float | None:
        """Intraday VWAP as of the bar at `ts` (None if no such bar or no volume yet)."""
        return self.vwap_by_ts.get(ts)


@dataclass(frozen=True)
class Tip:
    """The chart's newest column at a bar, as the algo reads it."""

    dir: str  # "X" | "O"
    top: float  # the price of its highest box
    vwap: float | None  # intraday VWAP as of that bar
    vwma: float | None  # VWMA of the newest column (price)
    sell: tuple[int, float] | None  # a double bottom sell that bar confirmed: (column, broken bottom price)


class PnfBuilder:
    """A chart built one [timestamp, close, volume?] bar at a time."""

    def __init__(self, p0: float, box_pct: float = 0.5, reversal: int = 3, vwma_period: int = 10):
        self.p0 = p0
        self.box = (p0 * box_pct) / 100
        self.reversal = reversal
        self.vwma_period = vwma_period
        self.columns: list[Column] = []
        self.vwap_by_ts: dict[int, float | None] = {}
        self._day = None
        self._pv = 0.0
        self._volume = 0.0

    @property
    def ok(self) -> bool:
        return self.box > 0

    def price(self, row: float) -> float:
        return self.p0 + row * self.box

    def _row_of(self, p: float) -> float:
        return (p - self.p0) / self.box

    def _up_row(self, p: float) -> int:
        return math.floor(self._row_of(p) + EPS)

    def _down_row(self, p: float) -> int:
        return math.ceil(self._row_of(p) - EPS)

    def _open(self, direction: str, start: int, end: int, ts: int) -> Column:
        step = 1 if direction == "X" else -1
        boxes = [Box(r, ts) for r in range(start, end + step, step)]
        column = Column(direction, boxes, max(start, end), min(start, end), ts, ts, 0)
        self.columns.append(column)
        return column

    def add(self, bar: Sequence) -> None:
        ts, p = bar[0], bar[1]
        v = (bar[2] if len(bar) > 2 else 0) or 0
        self._add_box(ts, p, v)
        # Intraday VWAP: each close weighted by its volume, restarted every session.
        if ist_day(ts) != self._day:
            self._day = ist_day(ts)
            self._pv = 0
            self._volume = 0
        self._pv += p * v
        self._volume += v
        self.vwap_by_ts[ts] = self._pv / self._volume if self._volume > 0 else None

    def _add_box(self, ts: int, p: float, v: float) -> None:
        col = self.columns[-1] if self.columns else None
        if col is None:
            u = self._up_row(p)
            d = self._down_row(p)
            if u >= 1:
                col = self._open("X", 1, u, ts)
            elif d <= -1:
                col = self._open("O", -1, d, ts)
            if col is not None:
                col.volume += v
            return
        if col.dir == "X":
            u = self._up_row(p)
            if u > col.hi:
                col.boxes.extend(Box(r, ts) for r in range(col.hi + 1, u + 1))
                col.hi = u
            else:
                d = self._down_row(p)
                if d <= col.hi - self.reversal:
                    col = self._open("O", col.hi - 1, d, ts)
        else:
            d = self._down_row(p)
            if d < col.lo:
                col.boxes.extend(Box(r, ts) for r in range(col.lo - 1, d - 1, -1))
                col.lo = d
            else:
                u = self._up_row(p)
                if u >= col.lo + self.reversal:
                    col = self._open("X", col.lo + 1, u, ts)
        col.end_ts = ts
        col.volume += v

    def tip(self, ts: int) -> Tip | None:
        """The newest column after the bar at `ts` (None while there is no column)."""
        if not self.columns:
            return None
        i = len(self.columns) - 1
        col = self.columns[i]
        sell = None
        if i >= 2 and col.dir == "O" and col.lo < self.columns[i - 2].lo:
            prev = self.columns[i - 2]
            if next(b.ts for b in col.boxes if b.row == prev.lo - 1) == ts:
                sell = (i, self.price(prev.lo))
        vwma = vwma_at(self.columns, i, self.vwma_period)
        vwma_price = None if vwma is None else self.price(vwma)
        return Tip(col.dir, self.price(col.hi), self.vwap_by_ts.get(ts), vwma_price, sell)

    def chart(self) -> Pnf:
        """The whole chart (sharing this builder's columns)."""
        columns = self.columns
        return Pnf(
            p0=self.p0,
            box=self.box,
            reversal=self.reversal,
            columns=columns,
            hi=max([0, *(c.hi for c in columns)]),
            lo=min([0, *(c.lo for c in columns)]),
            signals=find_signals(columns),
            vwma_period=self.vwma_period,
            vwma=[None if row is None else self.price(row) for row in column_vwma(columns, self.vwma_period)],
            vwap=[self.vwap_by_ts.get(c.end_ts) for c in columns],
            vwap_by_ts=self.vwap_by_ts,
        )


def build_pnf(
    bars: Sequence[Sequence], box_pct: float = 0.5, reversal: int = 3, vwma_period: int = 10
) -> Pnf | None:
    """Build the chart from [timestamp, close, volume?] bars, oldest first."""
    if not bars:
        return None
    builder = PnfBuilder(bars[0][1], box_pct, reversal, vwma_period)
    if not builder.ok:
        return None
    for bar in bars:
        builder.add(bar)
    return builder.chart()


def tip_of(pnf: Pnf | None, ts: int) -> Tip | None:
    """PnfBuilder.tip, read from a whole chart built up to the bar at `ts`."""
    if pnf is None or not pnf.columns:
        return None
    col = pnf.columns[-1]
    sell = None
    for s in reversed(pnf.signals):
        if s.ts < ts:
            break
        if s.ts == ts and s.type == "sell":
            sell = (s.col, pnf.price(s.level))
            break
    return Tip(col.dir, pnf.price(col.hi), pnf.vwap_at(ts), pnf.vwma[-1], sell)


def vwma_at(columns: list[Column], i: int, period: int) -> float | None:
    """Volume-weighted moving average of column midpoints at column i, in rows: the
    midpoints of columns i - period + 1 .. i, each weighted by the volume traded while
    it was the current column. None before `period` columns, and without volume."""
    if i < period - 1:
        return None
    pv = 0.0
    volume = 0.0
    for col in columns[i - period + 1 : i + 1]:
        pv += ((col.hi + col.lo) / 2) * col.volume
        volume += col.volume
    return pv / volume if volume > 0 else None


def column_vwma(columns: list[Column], period: int) -> list[float | None]:
    """vwma_at for every column."""
    return [vwma_at(columns, i, period) for i in range(len(columns))]


def find_signals(columns: list[Column]) -> list[Signal]:
    """Double top buy: an X column one box above the previous X column's top.
    Double bottom sell: an O column one box below the previous O column's bottom."""
    signals = []
    for i in range(2, len(columns)):
        col = columns[i]
        prev = columns[i - 2]  # columns alternate, so i - 2 has the same direction
        if col.dir == "X" and col.hi > prev.hi:
            row = prev.hi + 1
            ts = next(b.ts for b in col.boxes if b.row == row)
            signals.append(Signal("buy", i, row, prev.hi, ts))
        elif col.dir == "O" and col.lo < prev.lo:
            row = prev.lo - 1
            ts = next(b.ts for b in col.boxes if b.row == row)
            signals.append(Signal("sell", i, row, prev.lo, ts))
    return signals
