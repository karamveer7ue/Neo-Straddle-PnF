"""Simulator data building (pnf_sim.replay) against a fake gateway."""

import asyncio
from datetime import date, datetime

import pytest

from pnf.series import IST, UNDERLYINGS
from pnf_sim.replay import (
    SimError,
    SimRequest,
    build_run,
    candle_path,
    default_dates,
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


def test_offers_the_next_four_listed_expiries():
    listed = ["2026-11-03", "2026-10-13", "2026-10-06", "2026-10-19", "2026-10-27", "2026-11-09"]
    assert offered_expiries(listed, TODAY) == [
        date(2026, 10, 13),
        date(2026, 10, 19),
        date(2026, 10, 27),
        date(2026, 11, 3),
    ]


def test_default_dates_are_the_last_weekday_before_today():
    assert default_dates(TODAY) == (date(2026, 10, 9), date(2026, 10, 9))  # Monday -> Friday


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

    def __init__(self, options_from=date(2026, 10, 8)):
        self.options_from = options_from
        self.calls = []

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
