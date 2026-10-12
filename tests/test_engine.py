"""Engine behaviour against a fake gateway and a frozen IST clock."""

import asyncio
from datetime import date, datetime, timedelta

import pytest
from neo_api_client.websocket.feed import SFeedScrip

from pnf import engine as engine_mod
from pnf.engine import Engine
from pnf.neo import NeoError
from pnf.series import IST, UNDERLYINGS

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


QUOTES = {
    "NIFTY": {"ltp": 22520.45, "open": 22314.95, "prev_close": 22231.8},
    "SENSEX": {"ltp": 72472.33, "open": 72349.5, "prev_close": 71593.24},
}


def leg(segment: str, strike: int, kind: str) -> dict:
    """Fake leg; the token spells out strike and kind, e.g. "22300CE"."""
    token = f"{strike}{kind}"
    return {"key": f"{segment}|{token}", "segment": segment, "token": token, "symbol": f"X{token}"}


class FakeGateway:
    def __init__(self, today_minutes=45, today_fails=False, future_fails=False):
        self.logged_in = False
        self.future_fails = future_fails
        self.today_minutes = today_minutes
        self.today_fails = today_fails
        self.history_calls = []
        self.expiry_calls = []
        self.subscriptions = []  # what the fake feed was asked for

    async def expiries(self, u):
        self.expiry_calls.append(u.key)
        return ["2026-10-27", "2026-10-20", "2026-10-13", "2026-11-24"]

    async def index_quote(self, u):
        return QUOTES[u.key]

    async def near_future(self, u, today):
        if self.future_fails:
            raise NeoError("futures search failed")
        token = f"{u.key}FUT"
        return {
            "key": f"{u.fo_segment}|{token}",
            "segment": u.fo_segment,
            "token": token,
            "symbol": f"{u.key}26OCTFUT",
            "expiry": "2026-10-27",
        }

    async def quote(self, segment, token, what):
        return {"ltp": 22612.6, "open": 22600.0, "prev_close": 22535.2}

    async def option_contracts(self, u, expiry, strikes):
        return {(s, k): leg(u.fo_segment, s, k) for s in strikes for k in ("CE", "PE")}

    def create_feed(self):
        return FakeFeed(self)

    async def history(self, neosymbol, from_date, to_date):
        self.history_calls.append((neosymbol, from_date, to_date))
        if self.today_fails and to_date >= TODAY:
            raise NeoError("same-day candles unavailable")
        base = 100.0 if neosymbol.endswith("CE") else 50.0
        rows = []
        for day in [*PAST_DAYS, TODAY]:
            if from_date <= day <= to_date:
                rows += candles_for(day, self.today_minutes if day == TODAY else 375, base)
        return rows


class FakeFeed:
    """Records subscriptions, then ends without messages."""

    def __init__(self, gw):
        self.gw = gw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def subscribe_scrips(self, tokens):
        self.gw.subscriptions.append(("scrips", [(t.exchange_segment, t.instrument_token) for t in tokens]))

    async def subscribe_index(self, tokens):
        self.gw.subscriptions.append(("index", [(t.exchange_segment, t.instrument_token) for t in tokens]))

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.fixture
def clock(monkeypatch):
    now = {"t": datetime(2026, 10, 12, 10, 0, 30, tzinfo=IST)}
    monkeypatch.setattr(engine_mod, "now_ist", lambda: now["t"])
    return now


@pytest.fixture
def make_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(engine_mod, "ALERTS_FILE", tmp_path / "alerts.json")

    def make(**gateway_kwargs):
        return Engine(FakeGateway(**gateway_kwargs))

    return make


def ist_ts(h: int, m: int, day: date = TODAY) -> int:
    return int(datetime(day.year, day.month, day.day, h, m, tzinfo=IST).timestamp())


def tick(token: str, price: float, volume: int = 0, segment: str = "nse_fo") -> SFeedScrip:
    """A full scrip packet; `volume` is the leg's running day volume."""
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
            "open_interest",
        ],
        0,
    )
    return SFeedScrip(
        exchange_segment=segment,
        instrument_token=token,
        level=4,
        last_traded_price=price,
        average_trade_price=0.0,
        volume_traded_today=volume,
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
    # Today the fake serves 09:15-09:59; at 10:00:30 only candles whose minute ended
    # at least a minute ago count, so 09:59 (still settling) is left out.
    assert len(series) == 375 * 2 + 44
    # (ts, straddle close, CE+PE volume); each leg trades 100 a minute.
    assert series[0] == (ist_ts(9, 15, date(2026, 10, 8)), 150.0, 200)
    assert series[1] == (ist_ts(9, 16, date(2026, 10, 8)), 150.1, 200)
    assert series[375] == (ist_ts(9, 15, date(2026, 10, 9)), 150.0, 200)


def test_atm_override_and_persistence(clock, make_engine):
    eng = make_engine()
    eng.atm_overrides["NIFTY"] = 22400
    eng._save_config()
    asyncio.run(eng._build())
    assert [c.strike for c in eng.charts] == [22200, 22400, 22600]
    assert eng.atm_auto == 22300

    restored = make_engine()
    assert restored.atm_override == 22400


def test_sensex_builds_from_bse_with_its_own_strikes(clock, make_engine):
    eng = make_engine()
    eng.underlying_key = "SENSEX"
    asyncio.run(eng._build())
    assert eng.phase == "ready"
    assert eng.gw.expiry_calls == ["SENSEX"]
    assert eng.atm == 72300  # open 72,349.50 rounded to the 100-point strike grid
    assert [c.strike for c in eng.charts] == [71800, 72300, 72800]  # ATM +/- 500
    assert [c.offset for c in eng.charts] == [-500, 0, 500]
    assert all(n.startswith("bse_fo|") for n, _, _ in eng.gw.history_calls)
    assert eng.charts[1].ce["key"] == "bse_fo|72300CE"
    assert eng.spot == QUOTES["SENSEX"]["ltp"]
    assert eng.snapshot()["config"]["built"] == UNDERLYINGS["SENSEX"].to_json()


def test_each_underlying_keeps_its_own_atm_override(clock, make_engine):
    async def scenario(eng):
        eng.configure("SENSEX", 3, "week1", atm=72500)
        eng.configure("NIFTY", 3, "week1")  # switching back: NIFTY's choice is kept
        eng._build_task.cancel()

    eng = make_engine()
    asyncio.run(scenario(eng))
    assert eng.atm_overrides == {"NIFTY": None, "SENSEX": 72500}
    assert eng.underlying_key == "NIFTY"
    restored = make_engine()
    assert restored.atm_overrides == {"NIFTY": None, "SENSEX": 72500}


def test_old_config_file_with_a_single_override_still_loads(make_engine):
    engine_mod.CONFIG_FILE.write_text('{"atm": 22400, "days": 5, "expiry": "month1"}')
    eng = make_engine()
    assert eng.atm_overrides == {"NIFTY": 22400, "SENSEX": None}
    assert (eng.underlying_key, eng.days, eng.expiry_role) == ("NIFTY", 5, "month1")


def test_live_feed_subscribes_to_the_built_exchange(clock, make_engine):
    async def scenario(eng):
        await eng._build()
        eng.gw.logged_in = True
        task = asyncio.create_task(eng._run_feed())
        while len(eng.gw.subscriptions) < 2:
            await asyncio.sleep(0.01)
        task.cancel()

    eng = make_engine()
    eng.underlying_key = "SENSEX"
    asyncio.run(scenario(eng))
    kinds = dict(eng.gw.subscriptions)
    assert kinds["index"] == [("bse_cm", "SENSEX")]
    assert ("bse_fo", "72300CE") in kinds["scrips"] and len(kinds["scrips"]) == 7  # 6 legs + future
    assert ("bse_fo", "SENSEXFUT") in kinds["scrips"]

    # Ticks are keyed by segment, so a BSE token never feeds an NSE leg.
    eng._on_feed(tick("72300CE", 410.0, segment="bse_fo"))
    assert eng.ltp["bse_fo|72300CE"] == 410.0


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
    # First sight of each leg sets its volume baseline (the day so far).
    eng._on_feed(tick("22300CE", 118.0, volume=50_000))
    eng._on_feed(tick("22300PE", 62.0, volume=40_000))
    # During 10:00 the legs trade 300 + 100 more.
    eng._on_feed(tick("22300CE", 120.5, volume=50_300))
    eng._on_feed(tick("22300PE", 60.25, volume=40_100))
    atm = eng.charts[1]
    assert eng._live_json()["volumes"]["atm"] == 400  # traded so far in the forming minute

    eng._close_minute(datetime(2026, 10, 12, 10, 0, tzinfo=IST))
    assert atm.series()[-1] == (ist_ts(10, 0), 180.75, 400)
    assert eng._live_json()["volumes"]["atm"] == 0  # the next minute starts from zero
    eng._close_minute(datetime(2026, 10, 12, 10, 0, tzinfo=IST))  # never closes twice
    assert len(atm.live) == 1

    # 10:01 trades 200 + 200 more.
    eng._on_feed(tick("22300CE", 130.5, volume=50_500))
    eng._on_feed(tick("22300PE", 60.25, volume=40_300))
    eng._close_minute(datetime(2026, 10, 12, 10, 1, tzinfo=IST))
    assert atm.series()[-1] == (ist_ts(10, 1), 190.75, 400)

    # Official candles for today arrive. Only settled minutes (up to 09:58 at
    # 10:00:30) replace live data; the newer live bars stay.
    eng.gw.today_fails = False
    eng.gw.today_minutes = 50  # the API would serve 09:15 .. 10:04
    asyncio.run(eng._reconcile())
    assert [b[0] for b in atm.live] == [ist_ts(10, 0), ist_ts(10, 1)]
    assert atm.series()[-3] == (ist_ts(9, 58), 154.3, 200)  # official candle and volume
    assert atm.series()[-1] == (ist_ts(10, 1), 190.75, 400)


def test_refresh_serving_the_minute_in_progress_does_not_freeze_the_chart(clock, make_engine):
    """Regression (2026-10-12): the history API includes the minute still in
    progress. Stored as final, it blocked that minute's live bar, and the
    chart stopped while live prices kept updating."""
    clock["t"] = datetime(2026, 10, 12, 9, 32, 0, 300000, tzinfo=IST)  # refresh runs at 09:32:00
    eng = make_engine(today_minutes=18)  # the API serves 09:15 .. 09:32 (09:32 just started)
    asyncio.run(eng._build())
    atm = eng.charts[1]
    eng.feed_state = "live"
    eng._on_feed(tick("22300CE", 120.0, volume=1000))
    eng._on_feed(tick("22300PE", 60.0, volume=1000))
    asyncio.run(eng._reconcile())
    assert atm.series()[-1][0] < ist_ts(9, 31)  # neither 09:31 nor the forming 09:32
    # The live 09:32 bar closes normally at 09:33.
    clock["t"] = datetime(2026, 10, 12, 9, 33, 0, 200000, tzinfo=IST)
    eng._close_minute(datetime(2026, 10, 12, 9, 32, tzinfo=IST))
    assert atm.series()[-1][:2] == (ist_ts(9, 32), 180.0)


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


def alert(n: int) -> dict:
    return {
        "key": f"k{n}",
        "type": "buy",
        "underlying": "NIFTY",
        "offset": 0,
        "strike": 22500,
        "expiry": "2026-10-13",
        "level": 200.0,
        "price": 201.0,
        "barTs": 1_791_777_000 + n * 60,
    }


def test_alert_log_dedupes_keeps_newest_30_and_survives_restart(clock, make_engine):
    eng = make_engine()
    assert eng.log_alert(alert(1)) is True
    assert eng.log_alert(alert(1)) is False  # a second tab reporting the same signal
    for n in range(2, 40):
        eng.log_alert(alert(n))
    assert len(eng.alerts) == 30
    assert [a["key"] for a in eng.alerts[:2]] == ["k39", "k38"]  # newest first
    assert eng.alerts[-1]["key"] == "k10"  # older ones dropped
    assert eng.alerts[0]["loggedAt"] == int(clock["t"].timestamp())
    assert eng.snapshot()["alerts"] == eng.alerts

    restored = make_engine()
    assert restored.alerts == eng.alerts


def test_near_month_future_is_quoted_and_follows_live_ticks(clock, make_engine):
    eng = make_engine()
    asyncio.run(eng._build())
    fut = eng.snapshot()["future"]
    assert fut == {"symbol": "NIFTY26OCTFUT", "expiry": "2026-10-27", "ltp": 22612.6, "prevClose": 22535.2}
    eng.feed_state = "live"
    eng._on_feed(tick("NIFTYFUT", 22650.0))
    assert eng.snapshot()["future"]["ltp"] == 22650.0


def test_charts_still_build_when_the_future_is_unavailable(clock, make_engine):
    eng = make_engine(future_fails=True)
    asyncio.run(eng._build())
    assert eng.phase == "ready"
    assert eng.snapshot()["future"] is None
