"""Engine behaviour against a fake gateway and a frozen IST clock."""

import asyncio
from datetime import date, datetime, timedelta

import pytest
from neo_api_client.websocket.feed import SFeedScrip

from pnf import engine as engine_mod
from pnf.engine import Engine
from pnf.neo import NeoError
from pnf.series import IST

TODAY = date(2026, 10, 12)  # a Monday
PAST_DAYS = [date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)]


def candles_for(day: date, minutes: int, base: float) -> list:
    start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    rows = []
    for i in range(minutes):
        t = start + timedelta(minutes=i)
        close = round(base + i * 0.05, 2)
        rows.append([t.strftime("%Y-%m-%dT%H:%M:%S+0530"), close, close, close, close, 100])
    return rows


class FakeGateway:
    def __init__(self, today_minutes=45, today_fails=False):
        self.logged_in = False
        self.today_minutes = today_minutes
        self.today_fails = today_fails
        self.history_calls = []

    async def expiries(self):
        return ["2026-10-27", "2026-10-20", "2026-10-13", "2026-11-24"]

    async def nifty_quote(self):
        return {"ltp": 22520.45, "open": 22314.95, "prev_close": 22231.8}

    async def option_contracts(self, expiry, strikes):
        kinds = ("CE", "PE")
        return {(s, k): {"token": f"{s}{k}", "symbol": f"NIFTY{s}{k}"} for s in strikes for k in kinds}

    async def history(self, token, from_date, to_date):
        self.history_calls.append((token, from_date, to_date))
        if self.today_fails and to_date >= TODAY:
            raise NeoError("same-day candles unavailable")
        base = 100.0 if token.endswith("CE") else 50.0
        rows = []
        for day in [*PAST_DAYS, TODAY]:
            if from_date <= day <= to_date:
                rows += candles_for(day, self.today_minutes if day == TODAY else 375, base)
        return rows


@pytest.fixture
def clock(monkeypatch):
    now = {"t": datetime(2026, 10, 12, 10, 0, 30, tzinfo=IST)}
    monkeypatch.setattr(engine_mod, "now_ist", lambda: now["t"])
    return now


@pytest.fixture
def make_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_mod, "CONFIG_FILE", tmp_path / "config.json")

    def make(**gateway_kwargs):
        return Engine(FakeGateway(**gateway_kwargs))

    return make


def ist_ts(h: int, m: int, day: date = TODAY) -> int:
    return int(datetime(day.year, day.month, day.day, h, m, tzinfo=IST).timestamp())


def tick(token: str, price: float, atp: float = 0.0) -> SFeedScrip:
    zero = dict.fromkeys(
        [
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "net_change",
            "net_change_percent",
            "upper_circuit_limit",
            "lower_circuit_limit",
            "yearly_high",
            "yearly_low",
            "total_traded_value",
        ],
        0.0,
    )
    counts = dict.fromkeys(
        [
            "last_trade_time",
            "last_update_time",
            "last_trade_qty",
            "total_buy_quantity",
            "total_sell_quantity",
            "volume_traded_today",
            "open_interest",
        ],
        0,
    )
    return SFeedScrip(
        exchange_segment="nse_fo",
        instrument_token=token,
        level=4,
        last_traded_price=price,
        average_trade_price=atp,
        market_lot=65,
        precision=2,
        multiplier=1,
        **zero,
        **counts,
    )


def test_build_picks_atm_from_open_and_nearest_expiry(clock, make_engine):
    eng = make_engine()
    asyncio.run(eng._build())
    assert eng.phase == "ready"
    assert eng.expiry == date(2026, 10, 13)
    assert eng.atm == eng.atm_auto == 22300
    assert [c.strike for c in eng.charts] == [22100, 22300, 22500]
    assert eng.window_dates == [date(2026, 10, 8), date(2026, 10, 9), TODAY]
    series = eng.charts[1].series()
    assert len(series) == 375 * 2 + 45
    assert series[0] == (ist_ts(9, 15, date(2026, 10, 8)), 150.0, 150.0)
    # VWAP is anchored per session: the second minute averages two equal-volume closes.
    assert series[1] == (ist_ts(9, 16, date(2026, 10, 8)), 150.1, 150.05)
    assert series[375] == (ist_ts(9, 15, date(2026, 10, 9)), 150.0, 150.0)


def test_atm_override_and_persistence(clock, make_engine):
    eng = make_engine()
    eng.atm_override = 22400
    eng._save_config()
    asyncio.run(eng._build())
    assert [c.strike for c in eng.charts] == [22200, 22400, 22600]
    assert eng.atm_auto == 22300

    restored = make_engine()
    assert restored.atm_override == 22400


def test_expiry_role_selects_contract_and_lists_options(clock, make_engine):
    eng = make_engine()
    eng.expiry_role = "month2"
    asyncio.run(eng._build())
    assert eng.expiry == date(2026, 11, 24)
    assert [o["role"] for o in eng.expiry_options] == ["week1", "week2", "month1", "month2"]
    assert eng.snapshot()["config"]["expiryRole"] == "month2"


def test_today_history_refused_falls_back_to_past_days(clock, make_engine):
    eng = make_engine(today_fails=True)
    asyncio.run(eng._build())
    assert eng.phase == "ready"
    # Today stays in the window so the box size will not shift once data arrives.
    assert eng.window_dates == [date(2026, 10, 8), date(2026, 10, 9), TODAY]
    assert eng.charts[1].series()[-1][0] == ist_ts(15, 29, date(2026, 10, 9))


def test_holiday_is_left_out_of_the_window(clock, make_engine):
    eng = make_engine(today_minutes=0)
    asyncio.run(eng._build())
    assert eng.window_dates == PAST_DAYS


def test_live_minute_bars_and_reconcile(clock, make_engine):
    eng = make_engine(today_fails=True)
    asyncio.run(eng._build())
    eng.feed_state = "live"
    eng._on_feed(tick("22300CE", 120.5, atp=118.0))
    eng._on_feed(tick("22300PE", 60.25, atp=61.5))

    minute = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
    eng._close_minute(minute)
    atm = eng.charts[1]
    assert atm.series()[-1] == (ist_ts(10, 0), 180.75, 179.5)
    assert eng._live_json()["vwaps"]["atm"] == 179.5
    eng._close_minute(minute)  # the same minute never closes twice
    assert len(atm.live) == 1

    # Official candles for today arrive and cover the live bar's minute.
    eng.gw.today_fails = False
    eng.gw.today_minutes = 50  # 09:15 .. 10:04
    asyncio.run(eng._reconcile())
    assert atm.live == []
    assert atm.series()[-1][0] == ist_ts(10, 4)


def test_minute_close_needs_live_feed_and_session(clock, make_engine):
    eng = make_engine()
    asyncio.run(eng._build())
    eng._on_feed(tick("22300CE", 120.5))
    eng._on_feed(tick("22300PE", 60.25))
    before = len(eng.charts[1].series())

    eng._close_minute(datetime(2026, 10, 12, 10, 30, tzinfo=IST))  # feed not live
    eng.feed_state = "live"
    eng._close_minute(datetime(2026, 10, 12, 15, 30, tzinfo=IST))  # after the last 1-min candle
    assert len(eng.charts[1].series()) == before

    eng._close_minute(datetime(2026, 10, 12, 10, 30, tzinfo=IST))
    assert len(eng.charts[1].series()) == before + 1


def test_past_days_are_cached_between_builds(clock, make_engine):
    eng = make_engine()
    asyncio.run(eng._build())
    first = len(eng.gw.history_calls)
    asyncio.run(eng._build())
    second = eng.gw.history_calls[first:]
    # Only today's candles are re-fetched for the same strikes.
    assert len(second) == 6
    assert all(f == t == TODAY for _, f, t in second)
