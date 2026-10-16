"""Simulator data building (pnf_sim.replay) against a fake gateway."""

import asyncio
from collections import OrderedDict
from datetime import date, datetime

import pytest

from pnf.neo import NeoError
from pnf.series import IST, UNDERLYINGS
from pnf_sim.replay import (
    SimError,
    SimRequest,
    build_run,
    candle_path,
    default_dates,
    latest_session,
    offered_expiries,
    session_candles,
    straddle_rows,
    validate_dates,
)

TODAY = date(2026, 10, 12)  # Monday


def ts(day: date, h: int, m: int) -> int:
    return int(datetime(day.year, day.month, day.day, h, m, tzinfo=IST).timestamp())


def candle(day: date, h: int, m: int, o: float, c: float, v: int) -> list:
    t = datetime(day.year, day.month, day.day, h, m, tzinfo=IST)
    return [t.strftime("%Y-%m-%dT%H:%M:%S+0530"), o, max(o, c), min(o, c), c, v]


def test_dates_must_be_past_ordered_and_within_30_days():
    validate_dates(date(2026, 10, 9), date(2026, 10, 9), TODAY)
    with pytest.raises(SimError, match="before today"):
        validate_dates(date(2026, 10, 9), TODAY, TODAY)  # today itself is not a past session
    with pytest.raises(SimError, match="on or before"):
        validate_dates(date(2026, 10, 9), date(2026, 10, 8), TODAY)
    with pytest.raises(SimError, match="at most 30"):
        validate_dates(date(2026, 9, 1), date(2026, 10, 9), TODAY)
    with pytest.raises(SimError, match="5 years"):
        validate_dates(date(2021, 10, 1), date(2021, 10, 9), TODAY)


def test_offers_the_next_four_listed_expiries():
    listed = ["2026-11-03", "2026-10-13", "2026-10-06", "2026-10-19", "2026-10-27", "2026-11-09"]
    assert offered_expiries(listed, TODAY, UNDERLYINGS["NIFTY"]) == [
        date(2026, 10, 13),
        date(2026, 10, 19),
        date(2026, 10, 27),
        date(2026, 11, 3),
    ]


def test_offers_only_the_next_two_monthlies_without_weeklies():
    # BANKNIFTY's real list on 2026-10-16: monthlies, then quarterlies.
    listed = ["2026-10-27", "2026-11-23", "2026-12-29", "2027-03-30", "2027-06-29", "2027-09-28"]
    assert offered_expiries(listed, TODAY, UNDERLYINGS["BANKNIFTY"]) == [
        date(2026, 10, 27),
        date(2026, 11, 23),
    ]


def test_default_dates_are_the_last_weekday_before_today():
    assert default_dates(TODAY) == (date(2026, 10, 9), date(2026, 10, 9))  # Monday -> Friday


def test_default_dates_skip_a_holiday_the_index_did_not_trade_on():
    # 9 Oct a holiday: the index history's newest session before today is 8 Oct.
    class HolidayFriday(FakeGateway):
        async def history(self, neosymbol, start, end):
            rows = await super().history(neosymbol, start, end)
            return [r for r in rows if not r[0].startswith("2026-10-09")]

    latest = asyncio.run(latest_session(HolidayFriday(), UNDERLYINGS["NIFTY"], TODAY))
    assert default_dates(TODAY, latest) == (date(2026, 10, 8), date(2026, 10, 8))


def test_session_candles_and_straddle_rows():
    d = date(2026, 10, 9)
    ce = session_candles(
        [candle(d, 9, 14, 1, 1, 1), candle(d, 9, 15, 100, 101, 10), candle(d, 9, 16, 101, 102, 5)]
    )
    pe = session_candles([candle(d, 9, 16, 50, 49, 7)])
    assert list(ce) == [ts(d, 9, 15), ts(d, 9, 16)]  # 09:14 is outside the session
    # PE first trades at 09:16, so the straddle starts there: [ts, close, volume, CE, PE, path].
    rows = straddle_rows(ce, pe)
    assert [r[:5] for r in rows] == [[ts(d, 9, 16), 151.0, 12, 102.0, 49.0]]
    # CE rose 101 -> 102 (dips first: O, L, H, C); PE fell 50 -> 49 (rallies first: O, H, L, C).
    assert rows[0][5] == [
        [151.0, 101.0, 50.0],
        [151.0, 101.0, 50.0],
        [151.0, 102.0, 49.0],
        [151.0, 102.0, 49.0],
    ]


def test_candle_path_and_flat_path_for_a_leg_that_did_not_trade():
    assert candle_path([100, 105, 98, 104, 1]) == [100, 98, 105, 104]  # rising: dip first
    assert candle_path([100, 105, 98, 99, 1]) == [100, 105, 98, 99]  # falling: rally first
    d = date(2026, 10, 9)
    ce = session_candles([candle(d, 9, 15, 100, 101, 10), candle(d, 9, 16, 101, 103, 5)])
    pe = session_candles([candle(d, 9, 15, 50, 49, 7)])  # no PE trade at 09:16
    path = straddle_rows(ce, pe)[1][5]
    assert [p[2] for p in path] == [49.0] * 4  # PE held at its last close
    assert path[-1][0] == 152.0  # the path ends at the minute's close


class FakeGateway:
    """Index and option candles for 8-9 Oct; options start trading on `options_from`."""

    def __init__(self, options_from=date(2026, 10, 8), lot_size=75):
        self.options_from = options_from
        self.lot_size = lot_size
        self.calls = []
        self.client = self  # stands in for the SDK client's search_scrip

    def search_scrip(self, **kwargs):
        if self.lot_size is None:
            raise RuntimeError("scrip master unavailable")
        name = kwargs["symbol"].upper()
        return [{"pSymbolName": name, "lLotSize": self.lot_size}]

    async def _call_with_retry(self, fn, **kwargs):
        return fn(**kwargs)

    async def option_contracts(self, u, expiry, strikes):
        return {
            (s, k): {
                "key": f"{u.fo_segment}|{s}{k}",
                "segment": u.fo_segment,
                "token": f"{s}{k}",
                "symbol": f"X{s}{k}",
            }
            for s in strikes
            for k in ("CE", "PE")
        }

    async def history(self, neosymbol, start, end):
        self.calls.append((neosymbol, start, end))
        rows = []
        for day in (date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)):
            if not start <= day <= end:
                continue
            if neosymbol.endswith(("CE", "PE")):
                if day < self.options_from:
                    continue
                base = 100.0 if neosymbol.endswith("CE") else 50.0
                rows += [candle(day, 9, 15 + i, base, base + i, 10) for i in range(3)]
            else:  # index: opens at 22,314.95 on 8 Oct, 22,400 on 9 Oct
                o = 22314.95 if day == date(2026, 10, 8) else 22400.0
                rows += [candle(day, 9, 15 + i, o, o + i, 0) for i in range(3)]
        return rows


def test_build_run_fixes_strikes_from_the_start_days_open():
    gw = FakeGateway()
    req = SimRequest(UNDERLYINGS["NIFTY"], date(2026, 10, 13), date(2026, 10, 8), date(2026, 10, 9), atm=None)
    run = asyncio.run(build_run(gw, req))
    assert run["sessions"] == ["2026-10-08", "2026-10-09"]
    assert run["atm"] == run["atmAuto"] == 22300  # 8 Oct open 22,314.95
    assert [c["strike"] for c in run["charts"]] == [22100, 22300, 22500]
    assert len(run["charts"][1]["series"]) == 6  # 3 minutes on each day
    # Previous closes: 8 Oct from the request before the run (7 Oct's last close), 9 Oct from 8 Oct.
    assert run["spotPrevCloses"] == {"2026-10-08": 22402.0, "2026-10-09": 22316.95}
    assert run["lotSize"] == 75  # from the contract list
    before_run = [c for c in gw.calls if c[2] == date(2026, 10, 7)]
    assert before_run and before_run[0][1] == date(2026, 9, 28)


def test_build_run_with_a_set_atm_and_sensex_strikes():
    req = SimRequest(
        UNDERLYINGS["SENSEX"], date(2026, 10, 15), date(2026, 10, 9), date(2026, 10, 9), atm=72500
    )
    run = asyncio.run(build_run(FakeGateway(), req))
    assert [c["strike"] for c in run["charts"]] == [72000, 72500, 73000]
    assert run["charts"][1]["ce"] == "X72500CE"


def test_contracts_not_yet_trading_are_refused_clearly():
    gw = FakeGateway(options_from=date(2026, 10, 20))
    req = SimRequest(UNDERLYINGS["NIFTY"], date(2026, 10, 27), date(2026, 10, 8), date(2026, 10, 9), atm=None)
    with pytest.raises(SimError, match="no trades on these dates"):
        asyncio.run(build_run(gw, req))


def test_no_sessions_in_the_range_is_refused():
    req = SimRequest(
        UNDERLYINGS["NIFTY"], date(2026, 10, 13), date(2026, 10, 10), date(2026, 10, 11), atm=None
    )
    with pytest.raises(SimError, match="No NIFTY trading sessions"):
        asyncio.run(build_run(FakeGateway(), req))


def test_lot_size_falls_back_when_the_contract_list_fails():
    req = SimRequest(
        UNDERLYINGS["SENSEX"], date(2026, 10, 15), date(2026, 10, 9), date(2026, 10, 9), atm=None
    )
    run = asyncio.run(build_run(FakeGateway(lot_size=None), req))
    assert run["lotSize"] == 20


def test_the_session_before_the_run_is_drawn_as_context():
    gw = FakeGateway(options_from=date(2026, 10, 7))
    req = SimRequest(UNDERLYINGS["NIFTY"], date(2026, 10, 13), date(2026, 10, 8), date(2026, 10, 9), atm=None)
    run = asyncio.run(build_run(gw, req))
    assert run["contextDate"] == "2026-10-07"
    assert run["sessions"] == ["2026-10-08", "2026-10-09"]  # the replayed days
    assert run["replayStart"] == ts(date(2026, 10, 8), 9, 15)
    series = run["charts"][1]["series"]
    assert len(series) == 9 and series[0][0] == ts(date(2026, 10, 7), 9, 15)  # 3 context + 6 replay minutes
    assert run["atm"] == 22300  # strikes still from the start day's open
    # The context day and the run fit one request per leg.
    assert ("nse_fo|22300CE", date(2026, 10, 7), date(2026, 10, 9)) in gw.calls


def test_context_day_is_fetched_separately_when_the_span_exceeds_30_days():
    gw = FakeGateway(options_from=date(2026, 10, 7))
    start, end = date(2026, 10, 8), date(2026, 11, 6)  # a full 30-day run
    req = SimRequest(UNDERLYINGS["NIFTY"], date(2026, 11, 10), start, end, atm=None)
    asyncio.run(build_run(gw, req))
    leg = [c[1:] for c in gw.calls if c[0] == "nse_fo|22300CE"]
    assert sorted(leg) == [(date(2026, 10, 7), date(2026, 10, 7)), (start, end)]


def test_a_contract_not_trading_on_the_context_day_starts_at_the_run():
    run = asyncio.run(
        build_run(
            FakeGateway(),
            SimRequest(  # options trade from 8 Oct only
                UNDERLYINGS["NIFTY"], date(2026, 10, 13), date(2026, 10, 8), date(2026, 10, 9), atm=None
            ),
        )
    )
    assert run["contextDate"] == "2026-10-07"
    assert run["charts"][1]["series"][0][0] == run["replayStart"]
    assert not any(c["error"] for c in run["charts"])


def test_identical_simulate_requests_share_one_build(monkeypatch):
    import httpx

    import pnf_sim.app as sim_app

    builds = []

    async def slow_build(gw, req):
        builds.append(req)
        await asyncio.sleep(0.05)
        return {"ok": True}

    monkeypatch.setattr(sim_app, "build_run", slow_build)
    monkeypatch.setattr(sim_app, "today_ist", lambda: TODAY)

    async def listed(u):
        return ["2026-10-13"]

    async def scenario():
        sim_app.app.state.gw = type("GW", (), {"expiries": staticmethod(listed)})()
        sim_app.app.state.runs, sim_app.app.state.building = OrderedDict(), {}
        body = {"underlying": "NIFTY", "expiry": "2026-10-13", "start": "2026-10-09", "end": "2026-10-09"}
        transport = httpx.ASGITransport(app=sim_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            replies = await asyncio.gather(*(client.post("/api/simulate", json=body) for _ in range(3)))
        return [r.json() for r in replies]

    assert asyncio.run(scenario()) == [{"ok": True}] * 3
    assert len(builds) == 1


def test_a_cached_run_needs_no_api_call_and_a_build_is_kept_when_its_caller_leaves(monkeypatch):
    import httpx

    import pnf_sim.app as sim_app

    builds, expiry_calls = [], []

    async def slow_build(gw, req):
        builds.append(req)
        await asyncio.sleep(0.05)
        return {"ok": True}

    monkeypatch.setattr(sim_app, "build_run", slow_build)
    monkeypatch.setattr(sim_app, "today_ist", lambda: TODAY)

    async def listed(u):
        expiry_calls.append(u.key)
        return ["2026-10-13"]

    async def scenario():
        sim_app.app.state.gw = type("GW", (), {"expiries": staticmethod(listed)})()
        sim_app.app.state.runs, sim_app.app.state.building = OrderedDict(), {}
        body = {"underlying": "NIFTY", "expiry": "2026-10-13", "start": "2026-10-09", "end": "2026-10-09"}
        transport = httpx.ASGITransport(app=sim_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            leaving = asyncio.create_task(client.post("/api/simulate", json=body))
            await asyncio.sleep(0.01)
            leaving.cancel()  # e.g. the page reloaded mid-build
            await asyncio.sleep(0.1)
            again = await client.post("/api/simulate", json=body)
        return again.json()

    assert asyncio.run(scenario()) == {"ok": True}
    assert len(builds) == 1  # the abandoned build was cached, not thrown away
    assert expiry_calls == ["NIFTY"]  # the cached run was served without asking the API


def test_a_failed_context_day_fetch_does_not_fail_the_run():
    class ContextRefused(FakeGateway):
        async def history(self, neosymbol, start, end):
            if start == end == date(2026, 10, 7) and neosymbol.endswith(("CE", "PE")):
                raise NeoError("no data before listing")
            return await super().history(neosymbol, start, end)

    gw = ContextRefused(options_from=date(2026, 10, 7))
    start, end = date(2026, 10, 8), date(2026, 11, 6)  # long enough to fetch the context day apart
    req = SimRequest(UNDERLYINGS["NIFTY"], date(2026, 11, 10), start, end, atm=None)
    run = asyncio.run(build_run(gw, req))
    assert run["charts"][1]["series"][0][0] == ts(date(2026, 10, 8), 9, 15)
