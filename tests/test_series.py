from datetime import date, datetime

import pytest

from pnf.series import (
    IST,
    expiry_choices,
    expiry_roles,
    history_start,
    is_market_window,
    last_sessions,
    merge_tail,
    parse_candles,
    round_to_strike,
    straddle_series,
)


def ts(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=IST).timestamp())


@pytest.mark.parametrize(
    "price, step, strike",
    [
        (22314.95, 50, 22300),
        (22325.0, 50, 22350),  # halves round up
        (22324.99, 50, 22300),
        (22350.0, 50, 22350),
        (72349.5, 100, 72300),  # SENSEX: 100-point strikes
        (72350.0, 100, 72400),
    ],
)
def test_round_to_strike_rounds_halves_up(price, step, strike):
    assert round_to_strike(price, step) == strike


# Real NIFTY expiry list as returned on 2026-10-11 (holiday-shifted dates included).
EXPIRIES = ["2026-10-13", "2026-10-19", "2026-10-27", "2026-11-03", "2026-11-09", "2026-11-23", "2026-12-29"]


def test_expiry_roles_pick_two_weeklies_and_two_monthlies():
    roles = expiry_roles(EXPIRIES, date(2026, 10, 11))
    assert roles == {
        "week1": date(2026, 10, 13),
        "week2": date(2026, 10, 19),
        "month1": date(2026, 10, 27),
        "month2": date(2026, 11, 23),
    }
    assert [c["label"] for c in expiry_choices(roles)] == [
        "13 Oct \u00b7 weekly",
        "19 Oct \u00b7 weekly",
        "27 Oct \u00b7 monthly",
        "23 Nov \u00b7 monthly",
    ]


def test_expiry_shared_by_weekly_and_monthly_is_listed_once_as_monthly():
    roles = expiry_roles(EXPIRIES, date(2026, 10, 20))  # the upcoming weekly is the Oct monthly
    assert roles["week1"] == roles["month1"] == date(2026, 10, 27)
    assert [(c["role"], c["date"]) for c in expiry_choices(roles)] == [
        ("month1", "2026-10-27"),
        ("week2", "2026-11-03"),
        ("month2", "2026-11-23"),
    ]


def test_expiry_day_itself_is_still_upcoming():
    assert expiry_roles(EXPIRIES, date(2026, 10, 13))["week1"] == date(2026, 10, 13)
    with pytest.raises(ValueError):
        expiry_roles(EXPIRIES, date(2027, 1, 1))


def test_parse_candles_keeps_session_minutes_only():
    candles = [
        ["2026-10-09T09:14:00+0530", 1, 1, 1, 100.0, 0],
        ["2026-10-09T09:15:00+0530", 100, 103.0, 98.0, 101.0, 500],
        ["2026-10-09T15:29:00+0530", 1, 102.0, 102.0, 102.0, 0],
        ["2026-10-09T15:30:00+0530", 1, 1, 1, 103.0, 0],
        ["2026-10-09T15:39:00+0530", 1, 1, 1, 104.0, 0],
    ]
    assert parse_candles(candles) == [
        (ts("2026-10-09T09:15:00"), 101.0, 500),
        (ts("2026-10-09T15:29:00"), 102.0, 0),
    ]


def test_straddle_series_forward_fills_illiquid_leg():
    a, b, c = ts("2026-10-09T09:15:00"), ts("2026-10-09T09:16:00"), ts("2026-10-09T09:17:00")
    ce = [(a, 100.0, 10), (b, 101.5, 10), (c, 102.0, 10)]
    pe = [(b, 50.25, 10)]  # PE first trades at 09:16, then not at 09:17
    assert [bar[:2] for bar in straddle_series(ce, pe)] == [(b, 151.75), (c, 152.25)]


def test_straddle_series_carries_combined_minute_volume():
    a, b = ts("2026-10-09T09:15:00"), ts("2026-10-09T09:16:00")
    ce = [(a, 100.0, 7), (b, 101.0, 4)]
    pe = [(a, 50.0, 3)]  # no PE candle at 09:16: price carried forward, no volume
    assert straddle_series(ce, pe) == [(a, 150.0, 10), (b, 151.0, 4)]


def test_last_sessions_filters_by_ist_date():
    bars = [(ts("2026-10-08T15:29:00"), 1.0), (ts("2026-10-09T09:15:00"), 2.0)]
    assert last_sessions(bars, [date(2026, 10, 9)]) == [bars[1]]


def test_merge_tail_appends_only_newer_bars():
    base = [(1, 10.0), (2, 11.0)]
    assert merge_tail(base, [(2, 99.0), (3, 12.0)]) == [(1, 10.0), (2, 11.0), (3, 12.0)]
    assert merge_tail([], [(5, 1.0)]) == [(5, 1.0)]


def test_history_start_covers_requested_sessions_with_slack():
    start = history_start(date(2026, 10, 12), 15)
    assert (date(2026, 10, 12) - start).days == 28  # fits one 30-day 1-min request


def test_market_window():
    assert is_market_window(datetime(2026, 10, 12, 9, 0, tzinfo=IST))
    assert not is_market_window(datetime(2026, 10, 12, 15, 31, tzinfo=IST))
    assert not is_market_window(datetime(2026, 10, 11, 11, 0, tzinfo=IST))  # Sunday


def test_describe_extracts_sdk_error_messages():
    from pnf.neo import describe

    assert describe({"error": [{"code": "10300", "message": "Invalid TOTP"}]}) == "Invalid TOTP"
    fault = {"status": "ERROR", "fault": {"code": 422, "message": "Invalid neosymbol"}}
    assert describe(fault) == "Invalid neosymbol"
    assert describe({"Error Message": "Complete the 2fa process"}) == "Complete the 2fa process"
    assert describe({"stat": "Ok"}) == ""


def test_static_files_must_be_revalidated_by_browsers():
    """Regression (2026-10-12): without Cache-Control a browser reused an old
    cached pnf.js next to a new simulator.js and the page script failed."""
    import asyncio

    import httpx

    from pnf.app import app as live_app
    from pnf_sim.app import app as sim_app

    async def check(app, path):
        # Straight to the ASGI app; no lifespan, since static files need no gateway.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            first = await client.get(path)
            assert first.status_code == 200
            assert first.headers["cache-control"] == "no-cache"
            again = await client.get(path, headers={"if-none-match": first.headers["etag"]})
            assert again.status_code == 304  # revalidation stays cheap
            assert again.headers["cache-control"] == "no-cache"

    cases = [(live_app, "/static/pnf.js"), (sim_app, "/static/pnf.js"), (sim_app, "/sim/simulator.js")]
    for app, path in cases:
        asyncio.run(check(app, path))
