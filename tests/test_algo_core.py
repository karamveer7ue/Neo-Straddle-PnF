"""The algo's rules and bookkeeping (algo/core.py), with paper fills on scripted charts.

Every scenario is a straddle whose 1-minute closes are scripted, so the Point &
Figure chart (pnf/pnf.py, identical to the page's) produces the signals and lines
the rules act on. With a first close of 200 the box is exactly 1.00.
"""

import json
from datetime import date, datetime, timedelta

import pytest

from algo.core import (
    BREAKEVEN,
    BY_YOU,
    CLOSED,
    DAILY_LOSS,
    DAY_END,
    ENTERING,
    EXIT_ALL,
    EXIT_TIME,
    EXTERNAL,
    FAILED,
    LIVE,
    MAX_LOSS,
    MINUS,
    OPEN,
    PAPER,
    STOP,
    TARGET,
    TARGET1,
    UNWIND,
    AlgoCore,
    AlgoError,
    ChartView,
    Enter,
    Order,
    Settings,
    SettingsError,
    fmt_rupees,
    half_units,
    inr,
)
from pnf.pnf import build_pnf, tip_of
from pnf.series import IST, UNDERLYINGS

MONDAY = date(2026, 10, 12)
EXPIRY = date(2026, 10, 13)
BASE = datetime(2026, 10, 12, 9, 15, tzinfo=IST)

# A rising zigzag, then a double bottom sell at 208 (bar 18), below VWAP (210.68).
ENTRY = [200, 210, 206, 211, 207, 212, 208, 213, 209, 214, 210, 215, 211, 216, 212, 216, 211, 214, 208]
SIGNAL_BAR = len(ENTRY) - 1
# From the entry at 208 with NIFTY's target of 30: first target 198, full target 178.
TO_FIRST_TARGET = [205, 202, 199, 198]
STRIKES = {"atm_minus": (-200, 24800), "atm": (0, 25000), "atm_plus": (200, 25200)}


def ts(i: int, start: datetime = BASE) -> int:
    return int((start + timedelta(minutes=i)).timestamp())


def leg(strike: int, kind: str) -> dict:
    return {"key": f"nse_fo|{strike}{kind}", "symbol": f"NIFTY{strike}{kind}"}


def split(close: float) -> tuple[float, float]:
    """CE and PE prices that add up to the straddle close."""
    ce = round(close * 0.6, 2)
    return ce, round(close - ce, 2)


class Market:
    """Scripted closes per chart (None: no bar that minute), played into a core with paper fills."""

    def __init__(self, closes: dict[str, list], underlying="NIFTY", lot=65, start=BASE, history=None):
        self.closes = closes
        self.underlying, self.lot, self.start = underlying, lot, start
        self.history = history or {}  # chart key -> bars before `start` (earlier days)
        self.blocked: dict[str, str] = {}
        self.events: list[dict] = []

    def legs(self, key: str) -> tuple[dict, dict]:
        strike = STRIKES[key][1]
        return leg(strike, "CE"), leg(strike, "PE")

    def bars(self, key: str, upto: int) -> list:
        own = [(ts(i, self.start), float(c), 1000) for i, c in enumerate(self.closes[key][: upto + 1])]
        return [*self.history.get(key, []), *(b for b in own if b[1] is not None)]

    def prices(self, i: int) -> dict:
        out = {}
        for key, closes in self.closes.items():
            if i < len(closes) and closes[i] is not None:
                ce, pe = self.legs(key)
                out[ce["key"]], out[pe["key"]] = split(closes[i])
        return out

    def views(self, i: int) -> list[ChartView]:
        views = []
        for key, closes in self.closes.items():
            offset, strike = STRIKES[key]
            ce, pe = self.legs(key)
            has_bar = i < len(closes) and closes[i] is not None
            bars = self.bars(key, i)
            views.append(
                ChartView(
                    key, offset, strike, self.underlying, EXPIRY, ce, pe, self.lot,
                    bar=bars[-1] if has_bar else None,
                    tip=tip_of(build_pnf(bars), bars[-1][0]) if has_bar else None,
                    blocked=self.blocked.get(key, ""),
                )
            )  # fmt: skip
        return views

    def minute(self, core: AlgoCore, i: int) -> list[dict]:
        """Bar i closes (at the start of minute i + 1): the minute rules, live prices, the clock."""
        now = self.start + timedelta(minutes=i + 1)
        prices = self.prices(i)
        for intents in (
            core.on_minute(now, self.views(i), prices),
            core.on_prices(now, prices),
            core.on_clock(now, prices),
        ):
            for intent in intents:
                core.fill_paper(intent, prices, now)
        events = core.take_events()
        self.events += events
        return events

    def run(self, core: AlgoCore, start: int = 0, end: int | None = None) -> list[dict]:
        end = max(len(c) for c in self.closes.values()) if end is None else end
        out = []
        for i in range(start, end):
            out += self.minute(core, i)
        return out


def kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events if e["kind"] != "state"]


def plain(**changes) -> Settings:
    """Settings with no margin under VWAP (the scripts here were written for plain "below
    VWAP"; the margin has its own tests), plus `changes`."""
    return Settings().updated({"belowVwap": {"NIFTY": 0, "SENSEX": 0, "BANKNIFTY": 0}, **changes})


def switched_on(**changes) -> AlgoCore:
    core = AlgoCore(plain(**changes))
    core.switch_on(BASE)
    core.take_events()
    return core


def entered(after: list | None = None, **changes) -> tuple[AlgoCore, Market]:
    core = switched_on(**changes)
    market = Market({"atm": ENTRY + (after or [])})
    market.run(core, 0, SIGNAL_BAR + 1)
    return core, market


# ---------------- Settings ----------------


def test_defaults_are_the_agreed_ones():
    s = Settings()
    assert s.qty == {"NIFTY": 1690, "BANKNIFTY": 720, "SENSEX": 480}
    assert s.target == {"NIFTY": 30.0, "BANKNIFTY": 99.0, "SENSEX": 90.0}
    assert (s.mode, s.max_open, s.first_entry, s.last_entry, s.exit_time) == (
        PAPER,
        1,
        "09:10",
        "14:45",
        "15:12",
    )
    assert s.below_vwap == {"NIFTY": 30.0, "BANKNIFTY": 100.0, "SENSEX": 90.0}  # points under VWAP to enter
    assert (s.max_loss, s.daily_loss, s.max_trades) == (100_000, 100_000, 5)
    assert s.days == {"NIFTY": [0, 1, 4], "BANKNIFTY": [], "SENSEX": [2, 3]}  # Mon Tue Fri; never; Wed Thu
    # Every sell from the app is capped at twice the default quantity.
    caps = {k: u.max_sell_qty for k, u in UNDERLYINGS.items()}
    assert caps == {"NIFTY": 3380, "BANKNIFTY": 1440, "SENSEX": 960}
    assert s.problems({"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}) == {}


def test_settings_are_checked_field_by_field():
    s = Settings()

    def error(changes, field):
        with pytest.raises(SettingsError) as e:
            s.updated(changes)
        return e.value.errors[field]

    assert error({"qty": {"NIFTY": 3445}}, "qty.NIFTY") == "At most 3,380 (twice the default 1,690)"
    assert error({"lastEntry": "15:20"}, "lastEntry") == "No later than the Exit Time"
    for odd in (float("nan"), float("inf"), True):  # JSON parsers let NaN and Infinity through
        assert error({"maxLoss": odd}, "maxLoss") == "Enter a whole number"
    assert error({"exitTime": "15:26"}, "exitTime").startswith("Between 09:16 and 15:25")
    assert error({"exitTime": "3pm"}, "exitTime") == "Enter a time like 14:45"
    assert error({"maxOpen": 4}, "maxOpen").startswith("Simultaneous Trades is 1, 2 or 3")
    assert error({"maxLoss": "lots"}, "maxLoss") == "Enter a whole number"
    assert error({"dailyLoss": 500}, "dailyLoss").startswith("Between")
    assert error({"target": {"SENSEX": 0}}, "target.SENSEX").startswith("Between 0.05")
    assert error({"mode": "real"}, "mode") == "Mode is Paper or Live"
    assert error({"bogus": 1}, "bogus") == "Unknown setting 'bogus'"
    # Changed together, dependent times are fine; settings are never changed in place.
    later = s.updated({"exitTime": "15:20", "lastEntry": "15:00", "target": {"NIFTY": 32.5}})
    assert (later.last_entry, later.exit_time, later.target["NIFTY"]) == ("15:00", "15:20", 32.5)
    assert (s.last_entry, s.target["NIFTY"]) == ("14:45", 30.0)
    # Lots are checked once the lot size is known.
    assert Settings(qty={**s.qty, "NIFTY": 1700}).problems({"NIFTY": 65})["qty.NIFTY"] == (
        "A multiple of the lot size, 65"
    )
    assert (
        Settings(qty={**s.qty, "NIFTY": 65})
        .problems({"NIFTY": 65})["qty.NIFTY"]
        .startswith("At least 2 lots (130)")
    )


def test_saved_settings_load_even_when_they_depend_on_each_other():
    saved = {"lastEntry": "15:15", "exitTime": "15:20", "maxOpen": 2, "qty": {"SENSEX": 500}}
    saved["maxTrades"] = "x"
    s = Settings.from_json(saved)
    assert (s.last_entry, s.exit_time, s.max_open, s.qty["SENSEX"]) == ("15:15", "15:20", 2, 500)
    assert s.max_trades == 5  # unusable: kept at its default
    assert Settings.from_json("garbage") == Settings()


def test_rupees_and_lots():
    assert [inr(v) for v in (999, 33_800, 100_000, 12_345_678, -5_070)] == [
        "999", "33,800", "1,00,000", "1,23,45,678", "5,070",
    ]  # fmt: skip
    assert fmt_rupees(8450) == "+\u2060₹8,450"
    assert fmt_rupees(-5070) == f"{MINUS}\u2060₹5,070"
    # The first target books half the lots, rounded up.
    assert half_units(1690, 65) == 845  # 26 lots: 13
    assert half_units(500, 20) == 260  # 25 lots: 13, keeping 12
    assert half_units(480, 20) == 240
    assert half_units(65, 65) == 65  # a single lot goes whole


# ---------------- Entries ----------------


def test_a_double_bottom_sell_below_vwap_opens_a_trade():
    core, market = entered()
    [t] = core.trades
    assert (t.state, t.mode, t.qty, t.lot_size, t.chart) == (OPEN, PAPER, 1690, 65, "atm")
    assert (t.entry, t.target1, t.target2, t.want) == (208.0, 198.0, 178.0, 1690)
    assert t.signal["ts"] == ts(SIGNAL_BAR) and t.signal["close"] == 208.0
    assert t.signal["vwap"] == pytest.approx(210.684, abs=1e-3) and "vwma" not in t.signal
    assert t.opened_at == ts(SIGNAL_BAR + 1)  # at that bar's close
    assert kinds(market.events) == ["opened"]
    assert market.events[-1]["detail"] == (
        "1,690 @ 208.00 (CE 124.80 + PE 83.20) · targets 198.00 / 178.00 · stop: an X above 210.68"
    )
    # An earlier double bottom sell (bar 16, close 211) was above VWAP: no trade, nothing said.
    assert [e["title"] for e in market.events] == ["Algo sold NIFTY 25,000 ATM (paper)"]


def test_only_vwap_counts_vwma_does_not():
    # Yesterday traded low, so VWMA 10 sits far under today's prices; today gapped up and
    # VWAP restarted high. The user dropped VWMA 10 from the rules: below VWAP alone trades.
    friday = datetime(2026, 10, 9, 9, 15, tzinfo=IST)
    yesterday = [150, 158, 154, 159, 155, 160, 156, 161, 157, 162, 158, 163, 159]
    history = {"atm": [(ts(i, friday), float(c), 1000) for i, c in enumerate(yesterday)]}
    core = switched_on()
    market = Market({"atm": [200, 205, 201, 204, 200, 203, 198]}, history=history)
    events = market.run(core)
    t = core.trades[0]  # (stopped by the X to 203, then sold again at 198)
    assert t.entry == 200.0 and t.stop["level"] == pytest.approx(202.17)  # VWAP then
    assert kinds(events)[0] == "opened"


def test_an_index_is_traded_only_on_its_days():
    wednesday = datetime(2026, 10, 14, 9, 15, tzinfo=IST)
    core = AlgoCore(plain())
    core.switch_on(wednesday)
    core.take_events()
    events = Market({"atm": ENTRY}, start=wednesday).run(core)
    assert core.trades == []
    assert [(e["kind"], e["detail"]) for e in events] == [("skipped", "NIFTY is not traded on Wednesdays")]
    core = AlgoCore(plain(days={"NIFTY": [2]}))  # Wednesdays only, then
    core.switch_on(wednesday)
    Market({"atm": ENTRY}, start=wednesday).run(core)
    assert len(core.trades) == 1
    assert Settings().updated({"days": {"SENSEX": [4, 0, 0]}}).days["SENSEX"] == [0, 4]
    for bad in ([5], ["Mon"], [True], 2):
        with pytest.raises(SettingsError, match="Weekdays, Monday to Friday"):
            Settings().updated({"days": {"NIFTY": bad}})


def test_no_trades_while_off():
    core = AlgoCore()
    market = Market({"atm": ENTRY})
    assert market.run(core) == [] and core.trades == []


def test_the_last_slot_goes_to_the_atm_chart_first():
    core = switched_on()  # Simultaneous Trades: 1
    market = Market({"atm_plus": ENTRY, "atm_minus": ENTRY, "atm": ENTRY})
    events = market.run(core)
    assert [t.chart for t in core.trades] == ["atm"]
    skipped = [e["title"] for e in events if e["kind"] == "skipped"]
    assert skipped == [
        f"Algo skipped a double bottom sell: NIFTY 24,800 ATM {MINUS} 200",
        "Algo skipped a double bottom sell: NIFTY 25,200 ATM + 200",
    ]
    reasons = {e["detail"] for e in events if e["kind"] == "skipped"}
    assert reasons == {"no free slot (Simultaneous Trades: 1)"}

    core = switched_on(maxOpen=3)
    Market({"atm_plus": ENTRY, "atm_minus": ENTRY, "atm": ENTRY}).run(core)
    assert sorted(t.chart for t in core.trades) == ["atm", "atm_minus", "atm_plus"]


def test_one_trade_per_chart():
    core, market = entered([199, 202, 196])  # a second double bottom sell at 196, still in the trade
    events = market.run(core, SIGNAL_BAR + 1)
    assert len(core.trades) == 1
    skip = next(e for e in events if e["kind"] == "skipped")
    assert skip["detail"] == "this chart already has an algo trade"


def test_entries_stop_at_max_trades_and_after_the_last_entry_time():
    core = switched_on(maxTrades=1)
    market = Market({"atm": [*ENTRY, 211, 205, 201, 204, 200, 197]})
    market.run(core)
    assert len(core.trades) == 1 and core.trades[0].state == CLOSED  # stopped out at 211
    assert any(e["detail"] == "Max trades per day (1) reached" for e in market.events)

    core = switched_on(lastEntry="09:30", exitTime="15:12")
    events = Market({"atm": ENTRY}).run(core)  # the signal bar closes at 09:34
    assert core.trades == []
    assert [e["detail"] for e in events] == ["not before the Last entry time (09:30)"]
    core = switched_on(lastEntry="09:34")  # "before": a close at 09:34 itself is too late
    assert Market({"atm": ENTRY}).run(core)[-1]["detail"] == "not before the Last entry time (09:34)"
    core = switched_on(firstEntry="09:40", lastEntry="09:50")
    assert Market({"atm": ENTRY}).run(core)[-1]["detail"] == "before the First entry time (09:40)"


def test_the_close_must_be_the_margin_below_vwap():
    # The signal closes at 208 with VWAP at 210.68: 2.68 points under it.
    core = AlgoCore(Settings())  # NIFTY's default margin of 30
    core.switch_on(BASE)
    core.take_events()
    events = Market({"atm": ENTRY}).run(core)
    assert core.trades == []
    assert [(e["kind"], e["detail"]) for e in events] == [
        ("skipped", "the close 208.00 is not 30 points below VWAP (210.68)")
    ]
    assert events[0]["title"] == "Algo did not trade a double bottom sell: NIFTY 25,000 ATM"
    core = switched_on(belowVwap={"NIFTY": 2.5})
    Market({"atm": ENTRY}).run(core, 0, SIGNAL_BAR + 1)
    assert len(core.trades) == 1
    core = switched_on(belowVwap={"NIFTY": 2.7})
    Market({"atm": ENTRY}).run(core, 0, SIGNAL_BAR + 1)
    assert core.trades == []
    with pytest.raises(SettingsError, match="Between 0 and 10,000 points"):
        Settings().updated({"belowVwap": {"NIFTY": -1}})
    with pytest.raises(SettingsError, match="Before the Last entry time"):
        Settings().updated({"firstEntry": "14:45"})


def test_your_own_position_on_the_straddle_or_odd_lots_block_an_entry():
    core = switched_on()
    market = Market({"atm": ENTRY})
    market.blocked["atm"] = "your own position is open on this straddle"
    assert [e["detail"] for e in market.run(core)] == ["your own position is open on this straddle"]
    assert core.trades == []

    core = switched_on()
    events = Market({"atm": ENTRY}, lot=75).run(core)  # 1690 is not whole lots of 75
    assert [e["detail"] for e in events] == ["Quantity 1,690: a multiple of the lot size, 75"]


# ---------------- Exits ----------------


def test_half_at_the_first_target_then_the_rest_at_breakeven():
    core, market = entered([*TO_FIRST_TARGET, 201, 204, 207, 208])
    events = market.run(core, SIGNAL_BAR + 1)
    [t] = core.trades
    assert [(d.reason, d.keep) for d in t.exits] == [(TARGET1, 845), (BREAKEVEN, 0)]
    assert t.state == CLOSED and t.pnl() == pytest.approx(8_450)  # 845 x 10, then 845 x 0
    assert [(e["kind"], e["detail"]) for e in events] == [
        ("booked", "First target · 845 @ 198.00 (+\u2060₹8,450) · breakeven stop at 208.00"),
        ("closed", "Breakeven stop · bought back 845 @ 208.00 · sold at 208.00"),
    ]
    assert events[-1]["title"] == "Algo closed NIFTY 25,000 ATM (paper): +\u2060₹8,450"


def test_half_at_the_first_target_then_the_rest_at_the_full_target():
    core, market = entered([*TO_FIRST_TARGET, 195, 190, 185, 180, 178])
    market.run(core, SIGNAL_BAR + 1)
    [t] = core.trades
    assert [(d.reason, d.keep) for d in t.exits] == [(TARGET1, 845), (TARGET, 0)]
    assert t.pnl() == pytest.approx(33_800)  # 845 x 10 + 845 x 30: the README's example


def test_an_x_above_the_lower_line_stops_the_whole_trade():
    core, market = entered([211])  # X 209..211 against VWAP 210.70
    events = market.run(core, SIGNAL_BAR + 1)
    [t] = core.trades
    assert (t.state, t.reason, t.pnl()) == (CLOSED, STOP, pytest.approx(-5_070))
    assert events[-1]["detail"] == ("Stop: an X above VWAP · bought back 1,690 @ 211.00 · sold at 208.00")


def test_after_the_half_the_stop_still_works_alongside_breakeven():
    # A long stretch low after the first target pulls VWAP under the entry; an X above
    # it at 205 stops the rest in profit, before any breakeven (a close at 208 or more).
    low = [198, 196, 199, 196] * 5
    core, market = entered([*TO_FIRST_TARGET, *low, 205])
    market.run(core, SIGNAL_BAR + 1)
    [t] = core.trades
    assert [d.reason for d in t.exits] == [TARGET1, STOP]
    assert t.stop["level"] < 205 < t.entry
    assert t.pnl() == pytest.approx(845 * 10 + 845 * 3)


def test_odd_lots_book_the_larger_half_first():
    core = switched_on(qty={"SENSEX": 500}, days={"SENSEX": [0]})  # 25 lots of 20; traded this Monday
    market = Market({"atm": [*ENTRY, 205, 202, 199, 178]}, underlying="SENSEX", lot=20)
    market.run(core)
    [t] = core.trades
    assert t.target1 == 178.0 and t.qty == 500  # SENSEX target 90: a third is 30
    assert [(d.reason, d.keep) for d in t.exits] == [(TARGET1, 240)]  # 13 lots booked, 12 kept


def test_max_loss_per_trade_acts_on_the_live_price():
    core, _ = entered()
    [t] = core.trades
    now = BASE + timedelta(minutes=25, seconds=17)
    ce, pe = t.legs["CE"].key, t.legs["PE"].key
    assert core.on_prices(now, {ce: 140.0, pe: 127.0}) == []  # 267.00: -99,710
    [intent] = core.on_prices(now, {ce: 140.0, pe: 127.2})  # 267.20: -1,00,048
    core.fill_paper(intent, {ce: 140.0, pe: 127.2}, now)
    assert (t.state, t.reason, round(t.pnl())) == (CLOSED, MAX_LOSS, -100_048)


def test_the_daily_loss_limit_exits_everything_and_stops_for_the_day():
    core, market = entered([211], dailyLoss=5_000)
    market.run(core, SIGNAL_BAR + 1)  # the stop loses 5,070
    assert not core.on and core.halted == f"the daily loss limit of {MINUS}\u2060₹5,000 was reached"
    with pytest.raises(AlgoError, match="stopped for today"):
        core.switch_on(BASE + timedelta(minutes=30))

    # With a trade open, the limit buys it back (on the live price) as well.
    core, _ = entered(dailyLoss=20_000)
    [t] = core.trades
    now = BASE + timedelta(minutes=22)
    prices = {t.legs["CE"].key: 130.0, t.legs["PE"].key: 90.0}  # 220: -20,280
    [intent] = core.on_prices(now, prices)
    core.fill_paper(intent, prices, now)
    assert (t.state, t.reason, core.on) == (CLOSED, DAILY_LOSS, False)


def test_the_exit_time_closes_everything_and_switches_off_for_the_day():
    core, market = entered([207, 207, 207], lastEntry="09:35", exitTime="09:36")
    market.run(core, SIGNAL_BAR + 1)
    [t] = core.trades
    assert (t.state, t.reason, core.on, core.exit_time_done) == (CLOSED, EXIT_TIME, False, True)
    assert t.exits[-1].at == int(datetime(2026, 10, 12, 9, 36, tzinfo=IST).timestamp())
    with pytest.raises(AlgoError, match="Exit Time"):
        core.switch_on(BASE + timedelta(minutes=30))


def test_exit_all_and_exit_one_trade():
    core, _ = entered(maxOpen=3)
    now = BASE + timedelta(minutes=21)
    prices = {}
    [intent] = core.exit_all(now)
    core.fill_paper(intent, prices, now)  # at the last prices seen
    assert (core.trades[0].state, core.trades[0].reason, core.on) == (CLOSED, EXIT_ALL, False)

    core, _ = entered()
    intent = core.exit_trade(now, core.trades[0].id)
    core.fill_paper(intent, prices, now)
    assert (core.trades[0].reason, core.on) == (BY_YOU, True)  # the algo stays On
    with pytest.raises(AlgoError, match="not open"):
        core.exit_trade(now, core.trades[0].id)


# ---------------- Off, restart and the next day ----------------


def test_switching_off_leaves_open_trades_to_you():
    core, market = entered([211])
    released = core.switch_off(BASE + timedelta(minutes=19, seconds=30))
    assert [t.id for t in released] == [core.trades[0].id]
    market.run(core, SIGNAL_BAR + 1)  # the stop would fire here
    assert core.trades[0].state == OPEN and core.trades[0].exits == []


def test_a_restart_keeps_managing_open_trades_but_takes_no_new_ones():
    core, market = entered([211])
    restored = AlgoCore.from_json(json.loads(json.dumps(core.to_json())))  # as saved to disk
    assert not restored.on and restored.seq == 1 and restored.day == MONDAY
    [t] = restored.trades
    assert (t.state, t.managed, t.entry) == (OPEN, True, 208.0)
    market.run(restored, SIGNAL_BAR + 1)
    assert (t.state, t.reason) == (CLOSED, STOP)


def test_a_new_day_starts_off_and_closes_a_paper_trade_left_open():
    core, _ = entered()
    [t] = core.trades
    core.switch_off(BASE + timedelta(minutes=20))  # left open, then forgotten
    core.take_events()
    core.on_clock(datetime(2026, 10, 13, 9, 0, tzinfo=IST))
    assert core.day == date(2026, 10, 13) and core.seq == 0 and not core.on
    assert core.trades == []  # yesterday's trades go
    assert (t.state, t.reason, t.pnl()) == (CLOSED, DAY_END, 0)  # closed at its last price (208)
    [closed] = core.take_events()
    assert closed["title"] == "Algo closed NIFTY 25,000 ATM (paper): +\u2060₹0"


# ---------------- Orders (live) ----------------


def live_entry(**changes) -> tuple[AlgoCore, Market, list]:
    core = switched_on(mode=LIVE, **changes)
    market = Market({"atm": ENTRY})
    now = BASE + timedelta(minutes=SIGNAL_BAR + 1)
    intents = core.on_minute(now, market.views(SIGNAL_BAR), market.prices(SIGNAL_BAR))
    return core, market, intents


def test_a_live_entry_opens_once_both_legs_fill():
    core, _, [intent] = live_entry()
    assert isinstance(intent, Enter)
    t = intent.trade
    now = BASE + timedelta(minutes=19, seconds=1)
    ce = Order("101", "CE", "SELL", 1690)
    pe = Order("102", "PE", "SELL", 1690)
    core.add_order(t, ce, now)
    core.add_order(t, pe, now)
    assert t.state == ENTERING and t.entry is None and t.active
    ce.filled, ce.avg, ce.status = 1690, 124.9, "COMPLETE"
    core.settle(t, now)
    assert t.state == ENTERING
    pe.filled, pe.avg, pe.status = 1690, 83.05, "COMPLETE"
    core.settle(t, now)
    assert (t.state, t.entry, t.target1) == (OPEN, 207.95, 197.95)
    assert kinds(core.take_events()) == ["opened"]


def test_a_failed_leg_is_bought_back_and_the_algo_switches_off():
    core, _, [intent] = live_entry()
    t = intent.trade
    now = BASE + timedelta(minutes=19, seconds=1)
    core.add_order(t, Order("101", "CE", "SELL", 1690, filled=1690, avg=124.9, status="COMPLETE"), now)
    core.add_order(t, Order("102", "PE", "SELL", 1690, status="REJECTED", message="Insufficient funds"), now)
    assert (t.state, t.want, core.on) == (FAILED, 0, False)
    assert t.reason == "PE rejected: Insufficient funds"
    assert [d.reason for d in t.exits] == [UNWIND] and t.active  # the CE is still short
    [failed] = [e for e in core.take_events() if e["kind"] == "failed"]
    assert failed["detail"].startswith("PE rejected: Insufficient funds. Anything sold is being bought back")
    core.add_order(t, Order("103", "CE", "BUY", 1690, filled=1690, avg=125.5, status="COMPLETE", exit=0), now)
    assert not t.active and t.closed_at is not None
    assert round(t.pnl()) == round((124.9 - 125.5) * 1690)


def test_an_entry_refused_before_any_order_fails_with_nothing_to_buy_back():
    core, _, [intent] = live_entry()
    t = intent.trade
    core.entry_refused(t, "CE refused: Insufficient funds; PE not sent", BASE + timedelta(minutes=19))
    assert (t.state, t.exits, t.active, core.on) == (FAILED, [], False, False)


def test_a_position_bought_back_outside_the_algo_is_followed():
    core, _, [intent] = live_entry()
    t = intent.trade
    now = BASE + timedelta(minutes=19, seconds=1)
    for kind, avg in (("CE", 124.8), ("PE", 83.2)):
        core.add_order(t, Order(f"s{kind}", kind, "SELL", 1690, filled=1690, avg=avg, status="COMPLETE"), now)
    # You press Exit 50% on the chart: Kite shows 845 of each leg bought back.
    core.external_fills(t, {"CE": (845, 120.0), "PE": (845, 80.0)}, now)
    assert (t.state, t.holding, t.want) == (OPEN, 845, 845)
    assert [d.reason for d in t.exits] == [EXTERNAL]
    booked = [e for e in core.take_events() if e["kind"] == "booked"]
    assert booked[0]["detail"] == "Bought back outside the algo · 845 @ 200.00 (+\u2060₹6,760)"
    # Then Exit Full: the trade is closed, and the algo stops managing it.
    core.external_fills(t, {"CE": (845, 121.0), "PE": (845, 81.0)}, now)
    assert (t.state, t.reason, t.active) == (CLOSED, EXTERNAL, False)
