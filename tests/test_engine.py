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


LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}

QUOTES = {
    "NIFTY": {"ltp": 22520.45, "open": 22314.95, "prev_close": 22231.8},
    "SENSEX": {"ltp": 72472.33, "open": 72349.5, "prev_close": 71593.24},
    "BANKNIFTY": {"ltp": 55927.15, "open": 55861.4, "prev_close": 55810.5},
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
        self.today_fails_for: set[str] = set()  # legs whose same-day candles are refused
        self.fails_for: set[str] = set()  # legs whose every history request fails
        self.empty_for: set[str] = set()  # legs whose history comes back empty
        self.future_quote = {"ltp": 22612.6, "open": 22600.0, "prev_close": 22535.2}

    async def expiries(self, u):
        self.expiry_calls.append(u.key)
        if u.key == "BANKNIFTY":  # monthlies, then a quarterly
            return ["2026-10-27", "2026-11-24", "2026-12-29", "2027-03-30"]
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
        return dict(self.future_quote)

    async def option_contracts(self, u, expiry, strikes):
        lot = LOT_SIZES[u.key]  # the scrip master's lLotSize
        return {(s, k): {**leg(u.fo_segment, s, k), "lotSize": lot} for s in strikes for k in ("CE", "PE")}

    def create_feed(self):
        return FakeFeed(self)

    async def history(self, neosymbol, from_date, to_date):
        self.history_calls.append((neosymbol, from_date, to_date))
        if neosymbol in self.fails_for:
            raise NeoError("Some error occurred while fetching data")
        if (self.today_fails or neosymbol in self.today_fails_for) and to_date >= TODAY:
            raise NeoError("same-day candles unavailable")
        if neosymbol in self.empty_for:
            return []
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


def test_each_underlying_keeps_its_own_atm_override_and_expiry(clock, make_engine):
    async def scenario(eng):
        eng.configure("NIFTY", 3, "week2")
        eng.configure("SENSEX", 3, atm=72500)
        eng.configure("BANKNIFTY", 3, "month2")
        eng.configure("NIFTY", 3)  # switching back: NIFTY's choices are kept
        eng._build_task.cancel()

    eng = make_engine()
    asyncio.run(scenario(eng))
    assert eng.atm_overrides == {"NIFTY": None, "BANKNIFTY": None, "SENSEX": 72500}
    assert eng.expiry_role_of == {"NIFTY": "week2", "BANKNIFTY": "month2", "SENSEX": "week1"}
    assert (eng.underlying_key, eng.expiry_role) == ("NIFTY", "week2")
    restored = make_engine()
    assert restored.atm_overrides == eng.atm_overrides
    assert restored.expiry_role_of == eng.expiry_role_of


def test_an_expiry_role_the_underlying_lacks_is_refused(make_engine):
    eng = make_engine()
    with pytest.raises(ValueError, match="BANKNIFTY has no week1 expiry"):
        eng.configure("BANKNIFTY", 3, "week1")
    assert eng.underlying_key == "NIFTY"


def test_old_config_file_with_a_single_override_still_loads(make_engine):
    engine_mod.CONFIG_FILE.write_text('{"atm": 22400, "days": 5, "expiry": "month1"}')
    eng = make_engine()
    assert eng.atm_overrides == {"NIFTY": 22400, "BANKNIFTY": None, "SENSEX": None}
    assert (eng.underlying_key, eng.days, eng.expiry_role) == ("NIFTY", 5, "month1")
    assert eng.expiry_role_of == {"NIFTY": "month1", "BANKNIFTY": "month1", "SENSEX": "month1"}


def test_old_shared_weekly_role_gives_banknifty_its_monthly(make_engine):
    engine_mod.CONFIG_FILE.write_text('{"underlying": "SENSEX", "atm": {}, "days": 3, "expiry": "week2"}')
    eng = make_engine()
    assert eng.expiry_role_of == {"NIFTY": "week2", "BANKNIFTY": "month1", "SENSEX": "week2"}


def test_banknifty_builds_monthly_straddles_on_nse(clock, make_engine):
    async def scenario(eng):
        await eng._build()
        eng.gw.logged_in = True
        task = asyncio.create_task(eng._run_feed())
        while len(eng.gw.subscriptions) < 2:
            await asyncio.sleep(0.01)
        task.cancel()

    eng = make_engine()
    eng.underlying_key = "BANKNIFTY"
    asyncio.run(scenario(eng))
    assert eng.phase == "ready"
    assert eng.expiry == date(2026, 10, 27)  # its first monthly; there are no weeklies
    assert [(o["role"], o["date"]) for o in eng.expiry_options] == [
        ("month1", "2026-10-27"),
        ("month2", "2026-11-24"),
    ]
    assert eng.atm == 55900  # open 55,861.40 on the 100-point grid
    assert [c.strike for c in eng.charts] == [55400, 55900, 56400]
    assert eng.charts[1].ce["key"] == "nse_fo|55900CE"
    snap = eng.snapshot()["config"]
    assert (snap["expiryRole"], snap["built"]["key"]) == ("month1", "BANKNIFTY")
    assert dict(eng.gw.subscriptions)["index"] == [("nse_cm", "Nifty Bank")]


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
    eng.expiry_role_of["NIFTY"] = "month2"
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


# ---------------- Regressions from the code review ----------------


def test_failed_rebuild_keeps_the_generation_so_the_old_charts_stay_live(clock, make_engine):
    eng = make_engine()
    asyncio.run(eng._build())
    gen = eng.generation
    eng.feed_state = "live"
    eng._on_feed(tick("22300CE", 120.0, volume=10))
    eng._on_feed(tick("22300PE", 60.0, volume=10))

    async def failing_expiries(u):
        raise NeoError("429 storm")

    eng.gw.expiries = failing_expiries
    asyncio.run(eng._build())
    assert eng.phase == "error"
    assert eng.generation == gen  # the browser's generation still matches: nothing is dropped
    queue = eng.subscribe()
    queue.get_nowait()  # snapshot
    eng._close_minute(datetime(2026, 10, 12, 10, 0, tzinfo=IST))
    bars = [m for m in (queue.get_nowait() for _ in range(queue.qsize())) if m["type"] == "bar"]
    assert bars and all(m["generation"] == gen for m in bars)


def test_live_bars_survive_a_rebuild_of_the_same_straddles(clock, make_engine):
    eng = make_engine(today_fails=True)  # same-day history refused: live bars are today's only data
    asyncio.run(eng._build())
    eng.feed_state = "live"
    eng._on_feed(tick("22300CE", 120.0, volume=10))
    eng._on_feed(tick("22300PE", 60.0, volume=10))
    eng._close_minute(datetime(2026, 10, 12, 10, 0, tzinfo=IST))
    asyncio.run(eng._build())  # e.g. the day count changed
    assert eng.charts[1].series()[-1][:2] == (ist_ts(10, 0), 180.0)


def test_volume_baseline_resets_on_subscribe_and_after_the_overnight_reset(clock, make_engine):
    async def scenario(eng):
        await eng._build()
        eng._volume_mark["nse_fo|22300CE"] = 5_000  # left over from before a drop
        eng.day_volume["nse_fo|22300CE"] = 5_000
        eng.gw.logged_in = True
        task = asyncio.create_task(eng._run_feed())
        while len(eng.gw.subscriptions) < 2:
            await asyncio.sleep(0.01)
        task.cancel()

    eng = make_engine()
    asyncio.run(scenario(eng))
    assert "nse_fo|22300CE" not in eng._volume_mark  # the next packet sets a fresh baseline
    # The exchange's day volume restarts at zero: a smaller value is today's volume so far.
    eng._volume_mark.update({"nse_fo|22300CE": 900_000, "nse_fo|22300PE": 0})
    eng.day_volume.update({"nse_fo|22300CE": 1_200, "nse_fo|22300PE": 300})
    assert eng._minute_volume(eng.charts[1]) == 1_500


def test_an_empty_day_before_0930_is_provisional_and_rechecked_at_0931(clock, make_engine):
    clock["t"] = datetime(2026, 10, 12, 9, 16, 5, tzinfo=IST)
    eng = make_engine(today_minutes=0)  # nothing traded yet, or a holiday: cannot tell yet
    asyncio.run(eng._build())
    assert TODAY in eng.window_dates
    assert eng.built_for == (TODAY, False)
    assert eng._recheck_at == datetime(2026, 10, 12, 9, 31, tzinfo=IST)
    clock["t"] = datetime(2026, 10, 12, 9, 31, 10, tzinfo=IST)
    asyncio.run(eng._build())  # the 09:31 recheck: still empty, so a holiday
    assert TODAY not in eng.window_dates and eng.built_for == (TODAY, True)


def test_the_0916_rollover_on_a_trading_day_is_final(clock, make_engine):
    """At 09:16 the settle filter hides the 09:15 candle; the build must still see
    that today traded, or every rollover is provisional and rechecked at 09:31."""
    clock["t"] = datetime(2026, 10, 12, 9, 16, 5, tzinfo=IST)
    eng = make_engine(today_minutes=2)  # 09:15, plus 09:16 in progress
    asyncio.run(eng._build())
    assert TODAY in eng.window_dates
    assert eng.built_for == (TODAY, True) and eng._recheck_at is None


def test_a_provisional_build_before_0916_still_takes_the_0916_rollover(clock, make_engine):
    clock["t"] = datetime(2026, 10, 12, 9, 15, 30, tzinfo=IST)
    eng = make_engine(today_minutes=0)
    asyncio.run(eng._build())
    assert eng._recheck_at == datetime(2026, 10, 12, 9, 16, tzinfo=IST)


def test_live_bars_of_a_day_found_to_be_a_holiday_are_dropped(clock, make_engine):
    clock["t"] = datetime(2026, 10, 12, 9, 16, 5, tzinfo=IST)
    eng = make_engine(today_minutes=0)
    asyncio.run(eng._build())
    eng.feed_state = "live"
    for m in range(16, 21):  # the feed stays up on a holiday: flat bars from the last LTPs
        eng._close_minute(datetime(2026, 10, 12, 9, m, tzinfo=IST))
    assert eng.charts[1].series()[-1][0] == ist_ts(9, 20)
    clock["t"] = datetime(2026, 10, 12, 9, 31, 10, tzinfo=IST)
    asyncio.run(eng._build())  # the 09:31 recheck: a holiday
    assert TODAY not in eng.window_dates
    assert all(b[0] < ist_ts(9, 15) for b in eng.charts[1].series())


def test_the_0915_bar_survives_the_overnight_rollover(clock, make_engine):
    friday = date(2026, 10, 9)
    clock["t"] = datetime(2026, 10, 9, 15, 40, tzinfo=IST)
    eng = make_engine(today_fails=True)  # Monday's same-day history will be refused
    asyncio.run(eng._build())
    assert eng.window_dates[-1] == friday
    eng.feed_state = "live"
    eng._on_feed(tick("22300CE", 125.0, volume=0))
    eng._on_feed(tick("22300PE", 65.0, volume=0))
    eng._on_feed(tick("22300CE", 126.0, volume=500))
    clock["t"] = datetime(2026, 10, 12, 9, 16, 0, 100000, tzinfo=IST)
    eng._close_minute(datetime(2026, 10, 12, 9, 15, tzinfo=IST))  # before the 09:16 rebuild
    asyncio.run(eng._build())
    assert eng.charts[1].series()[-1] == (ist_ts(9, 15), 191.0, 500)


def test_a_holiday_takes_no_live_bars_once_decided(clock, make_engine):
    clock["t"] = datetime(2026, 10, 12, 9, 40, tzinfo=IST)
    eng = make_engine(today_minutes=0)
    asyncio.run(eng._build())
    assert eng.built_for == (TODAY, True) and TODAY not in eng.window_dates
    eng.feed_state = "live"
    before = eng.charts[1].series()
    eng._close_minute(datetime(2026, 10, 12, 9, 39, tzinfo=IST))
    assert eng.charts[1].series() == before


def test_a_day_built_as_a_holiday_is_rebuilt_once_the_feed_shows_trading(clock, make_engine):
    clock["t"] = datetime(2026, 10, 12, 11, 0, 5, tzinfo=IST)
    eng = make_engine(today_minutes=0)  # every leg's candles for today come back empty
    asyncio.run(eng._build())
    assert eng.built_for == (TODAY, True) and TODAY not in eng.window_dates
    eng.feed_state = "live"
    eng._on_feed(tick("22300CE", 118.0, volume=50_000))
    eng._on_feed(tick("22300PE", 62.0, volume=40_000))
    eng._close_minute(datetime(2026, 10, 12, 10, 59, tzinfo=IST))
    assert eng.built_for == (TODAY, True)  # no trades seen yet: still a holiday
    eng._on_feed(tick("22300CE", 118.5, volume=50_300))  # the market is trading after all
    eng._close_minute(datetime(2026, 10, 12, 11, 0, tzinfo=IST))
    assert eng.built_for == (TODAY, False)  # the clock loop rebuilds
    asyncio.run(eng._build())
    assert TODAY in eng.window_dates


def test_a_leg_whose_today_failed_is_never_paired_with_a_stale_price(clock, make_engine):
    eng = make_engine()
    eng.gw.today_fails_for = {"nse_fo|22300PE"}
    asyncio.run(eng._build())
    atm = eng.charts[1]
    # The CE has today, the PE only up to Friday: the ATM chart stops at Friday's close
    # rather than pairing today's CE with Friday's PE.
    assert atm.series()[-1][0] == ist_ts(15, 29, date(2026, 10, 9))
    assert eng.charts[0].series()[-1][0] == ist_ts(9, 58)  # the other charts have today
    eng.feed_state = "live"
    eng._on_feed(tick("22300CE", 118.0, volume=50_000))
    eng._on_feed(tick("22300PE", 62.0, volume=40_000))
    eng._close_minute(datetime(2026, 10, 12, 10, 0, tzinfo=IST))
    assert atm.series()[-1] == (ist_ts(10, 0), 180.0, 0)
    asyncio.run(eng._reconcile())  # the PE still fails: its live bar stays, nothing stale comes in
    assert atm.series()[-1] == (ist_ts(10, 0), 180.0, 0)
    assert all(b[0] <= ist_ts(15, 29, date(2026, 10, 9)) for b in atm.history)
    eng.gw.today_fails_for = set()
    asyncio.run(eng._reconcile())
    assert atm.history[-1][0] == ist_ts(9, 58)  # both legs known again


def test_a_leg_whose_history_fails_costs_only_its_chart_and_is_retried(clock, make_engine):
    clock["t"] = datetime(2026, 10, 12, 8, 0, tzinfo=IST)  # before the open: no past-only fallback
    eng = make_engine()
    eng.gw.fails_for = {"nse_fo|22500CE"}
    asyncio.run(eng._build())
    assert eng.phase == "ready"
    assert [bool(c.error) for c in eng.charts] == [False, False, True]
    assert eng.charts[2].load_failed and eng._retry_at is not None
    eng.gw.fails_for = set()
    asyncio.run(eng._build())
    assert not any(c.error for c in eng.charts) and eng._retry_at is None
    # When every chart fails, the build fails, and is retried later still.
    eng.gw.fails_for = {"nse_fo|22100CE", "nse_fo|22300PE", "nse_fo|22500CE"}
    eng._past_cache.clear()  # nothing cached to fall back on
    asyncio.run(eng._build())
    assert eng.phase == "error" and eng._retry_at is not None


def test_an_empty_history_reply_is_not_cached(clock, make_engine):
    eng = make_engine()
    eng.gw.empty_for = {"nse_fo|22300PE"}
    asyncio.run(eng._build())
    assert not eng.charts[1].series()
    eng.gw.empty_for = set()
    asyncio.run(eng._build())
    assert eng.charts[1].series()[0][0] == ist_ts(9, 15, date(2026, 10, 8))


def test_the_futures_previous_close_follows_a_rebuild_with_the_feed_live(clock, make_engine):
    eng = make_engine()
    asyncio.run(eng._build())
    eng.feed_state = "live"
    eng.future_ltp = 22650.0  # from the feed
    eng.gw.future_quote = {"ltp": 22700.0, "open": 22690.0, "prev_close": 22640.0}  # the next day
    asyncio.run(eng._build())
    assert (eng.future_ltp, eng.future_prev_close) == (22650.0, 22640.0)


def test_a_refresh_with_less_of_today_never_erases_official_bars(clock, make_engine):
    eng = make_engine()
    asyncio.run(eng._build())
    before = eng.charts[1].series()
    eng.gw.today_minutes = 0  # an empty "success" reply
    asyncio.run(eng._reconcile())
    assert eng.charts[1].series() == before


def test_past_days_cached_under_an_earlier_today_are_dropped(clock, make_engine):
    eng = make_engine()
    eng._past_cache[("nse_fo|X", date(2026, 9, 1), date(2026, 10, 9))] = [(1, 1.0, 1)]
    asyncio.run(eng._build())
    assert all(k[2] == TODAY for k in eng._past_cache)


def make_test_gateway(monkeypatch):
    from pnf import neo as neo_mod

    monkeypatch.setattr(neo_mod, "MIN_CALL_GAP", 0)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(neo_mod.asyncio, "sleep", no_sleep)
    return neo_mod.NeoGateway("key", "", "", "")


def test_gateway_retries_network_errors_then_gives_up_with_a_neo_error(monkeypatch):
    from neo_api_client.exceptions import ApiException

    gw = make_test_gateway(monkeypatch)
    calls = []

    def flaky(**kwargs):
        calls.append(1)
        if len(calls) < 3:  # how the SDK reports a timeout or a dropped connection
            raise ApiException(status=0, reason="Request timeout after 30 seconds")
        return {"ok": True}

    assert asyncio.run(gw._call_with_retry(flaky)) == {"ok": True}
    assert len(calls) == 3

    def always_down(**kwargs):
        raise ApiException(status=0, reason="Unable to connect to server")

    with pytest.raises(NeoError, match="still failing"):
        asyncio.run(gw._call_with_retry(always_down))


@pytest.mark.parametrize(
    "reply",
    [
        {"Error": "Unexpected response format", "StatusCode": 503},  # an HTML error page
        {"code": 429},
        "returned",  # scrip search returns {"error": ApiException} instead of raising
    ],
)
def test_gateway_retries_every_transient_reply_shape(monkeypatch, reply):
    from neo_api_client.exceptions import ApiException

    gw = make_test_gateway(monkeypatch)
    if reply == "returned":
        reply = {"error": ApiException(status=502, reason="Bad Gateway")}
    calls = []

    def flaky(**kwargs):
        calls.append(1)
        return reply if len(calls) < 2 else {"ok": True}

    assert asyncio.run(gw._call_with_retry(flaky)) == {"ok": True}
    assert len(calls) == 2


def test_gateway_does_not_retry_faults(monkeypatch):
    from neo_api_client.exceptions import ApiException

    gw = make_test_gateway(monkeypatch)
    calls = []

    def rejected(**kwargs):
        calls.append(1)
        raise ApiException(status=400, reason="Bad Request")

    with pytest.raises(NeoError, match="Bad Request"):
        asyncio.run(gw._call_with_retry(rejected))

    def broken(**kwargs):
        calls.append(1)
        raise TypeError("a bug, not bad luck")

    with pytest.raises(TypeError):
        asyncio.run(gw._call_with_retry(broken))
    assert len(calls) == 2


def test_gateway_holds_the_lock_until_a_cancelled_call_finishes_even_if_cancelled_again(monkeypatch):
    import threading

    from pnf import neo as neo_mod

    monkeypatch.setattr(neo_mod, "MIN_CALL_GAP", 0)
    gw = neo_mod.NeoGateway("key", "", "", "")
    release = threading.Event()
    running = []

    def slow(**kwargs):
        running.append(1)
        release.wait(5)
        running.pop()
        return {"ok": True}

    def overlap_check(**kwargs):
        return {"overlapped": bool(running)}

    async def scenario():
        first = asyncio.create_task(gw._call(slow))
        while not running:
            await asyncio.sleep(0.01)
        first.cancel()
        await asyncio.sleep(0.01)
        first.cancel()  # a second cancel while it waits for the thread
        second = asyncio.create_task(gw._call(overlap_check))
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        return await second

    assert asyncio.run(scenario()) == {"overlapped": False}


def test_errors_shown_on_a_page_go_into_the_alert_log(clock, make_engine):
    from fastapi.testclient import TestClient

    import pnf.app

    eng = make_engine()
    pnf.app.app.state.engine = eng  # no lifespan: the endpoint only needs the engine
    http = TestClient(pnf.app.app, base_url="https://127.0.0.1:8765")
    error = {
        "type": "error",
        "key": "order:42:REJECTED",
        "title": "NIFTY 22,300: CE sell rejected",
        "detail": "Margin",
    }
    assert http.post("/api/alerts", json=error).json() == {"logged": True}
    assert http.post("/api/alerts", json=error).json() == {"logged": False}  # another tab, same error
    assert eng.alerts[0] == {**error, "loggedAt": int(clock["t"].timestamp())}
    # An error entry has no signal fields, and a signal still needs them.
    assert http.post("/api/alerts", json={"type": "error", "key": "x"}).status_code == 422
    assert http.post("/api/alerts", json={**error, "type": "sell"}).status_code == 422


def test_config_endpoint_switches_index_keeping_its_expiry(make_engine):
    from fastapi.testclient import TestClient

    import pnf.app

    eng = make_engine()
    eng.rebuild = lambda: None  # only the stored choices matter here
    pnf.app.app.state.engine = eng
    http = TestClient(pnf.app.app, base_url="https://127.0.0.1:8765")
    assert http.post(
        "/api/config", json={"underlying": "BANKNIFTY", "days": 3, "expiry": "month2"}
    ).is_success
    assert http.post("/api/config", json={"underlying": "NIFTY", "days": 3}).is_success
    assert eng.expiry_role_of == {"NIFTY": "week1", "BANKNIFTY": "month2", "SENSEX": "week1"}
    # BANKNIFTY has no weeklies, and an unknown index is refused.
    refused = http.post("/api/config", json={"underlying": "BANKNIFTY", "days": 3, "expiry": "week1"})
    assert refused.status_code == 422 and "no weekly expiry" in refused.json()["detail"]
    assert http.post("/api/config", json={"underlying": "FINNIFTY", "days": 3}).status_code == 422
    assert eng.underlying_key == "NIFTY"


@pytest.mark.parametrize(
    "reply",
    [
        {"stat": "Not_Ok", "errMsg": "Invalid neosymbol"},
        {"error": [{"message": "Bad request"}]},
        {"errMsg": "Something went wrong"},
        {"Error": "Unexpected response format", "StatusCode": 200},
        {"status": "error", "message": "down"},
    ],
)
def test_history_errors_never_pass_for_an_empty_day(monkeypatch, reply):
    from pnf import neo as neo_mod

    monkeypatch.setattr(neo_mod, "MIN_CALL_GAP", 0)
    gw = neo_mod.NeoGateway("key", "", "", "")
    monkeypatch.setattr(gw.client, "historical_data", lambda **kwargs: reply)
    with pytest.raises(NeoError):
        asyncio.run(gw.history("nse_fo|1", TODAY, TODAY))
    monkeypatch.setattr(
        gw.client, "historical_data", lambda **kwargs: {"status": "success", "data": {"candles": []}}
    )
    assert asyncio.run(gw.history("nse_fo|1", TODAY, TODAY)) == []
    partial = {
        "status": "Partial response",
        "data": {"candles": [["2026-10-12T09:15:00+0530", 1, 1, 1, 1, 5]]},
    }
    monkeypatch.setattr(gw.client, "historical_data", lambda **kwargs: partial)
    assert len(asyncio.run(gw.history("nse_fo|1", TODAY, TODAY))) == 1


def test_the_sdk_log_never_records_login_credentials(tmp_path):
    import io
    import logging

    import structlog

    from pnf import neo as neo_mod

    sdk = logging.getLogger("neo_api_client")
    sdk_format = structlog.stdlib.ProcessorFormatter
    file_handler = next(h for h in sdk.handlers if isinstance(h.formatter, sdk_format))
    out = io.StringIO()
    capture = logging.StreamHandler(out)
    capture.setFormatter(file_handler.formatter)  # the SDK's own JSON rendering
    sdk.addHandler(capture)
    try:
        neo_mod.NeoGateway("key", "9876543210", "UCC123", "135790")
        structlog.get_logger("neo_api_client.rest").error(
            "api_error_response",
            body={"mobileNumber": "9876543210", "ucc": "UCC123", "mpin": "135790"},
            query_params={"ucc": "UCC123"},
            status_code=401,
        )
    finally:
        sdk.removeHandler(capture)
    text = out.getvalue()
    assert "api_error_response" in text and "401" in text  # the event itself is kept
    assert "9876543210" not in text and "UCC123" not in text and "13***90" not in text


class SilentFeed(FakeFeed):
    """Connects, then never delivers a packet (a connection that died without a close)."""

    async def __anext__(self):
        await asyncio.Event().wait()


def test_a_feed_that_goes_silent_in_session_is_reconnected(clock, make_engine):
    async def scenario(eng):
        await eng._build()
        eng.gw.logged_in = True
        eng.gw.create_feed = lambda: SilentFeed(eng.gw)
        eng._start_feed()
        while eng.feed_state != "live":
            await asyncio.sleep(0.01)
        first = eng._feed_task
        eng._on_feed(tick("22300CE", 125.0))  # a packet keeps it alive
        eng._check_feed_stale(clock["t"])
        assert eng.feed_state == "live" and eng._feed_task is first
        eng._feed_at -= engine_mod.FEED_STALE_SECONDS + 1  # then nothing for over 30 s
        eng._check_feed_stale(clock["t"].replace(hour=15, minute=35))  # after the session: no judgement
        assert eng.feed_state == "live"
        eng._check_feed_stale(clock["t"])
        assert eng.feed_state in ("reconnecting", "connecting") and eng._feed_task is not first
        await asyncio.sleep(0.05)
        assert first.cancelled() or first.done()
        eng._feed_task.cancel()

    asyncio.run(scenario(make_engine()))
