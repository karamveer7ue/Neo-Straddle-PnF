"""The algo over a simulator run (algo/sim.py): the same rules as the live page, replayed
minute by minute, with the page's in-minute price steps as the live prices."""

import time
from datetime import datetime, timedelta

import pytest

from algo.core import CLOSED, MAX_LOSS, OPEN, TARGET, TARGET1
from algo.sim import leg_keys, simulate
from pnf.series import IST
from tests.test_algo_core import ENTRY, SIGNAL_BAR, TO_FIRST_TARGET, plain, split

MONDAY = datetime(2026, 10, 12, 9, 15, tzinfo=IST)


def ts(i: int, start: datetime = MONDAY) -> int:
    return int((start + timedelta(minutes=i)).timestamp())


def rows(closes: list, start: datetime = MONDAY, paths: dict | None = None) -> list:
    """The run's straddle rows: [ts, close, volume, CE, PE, path]; the path steps of a
    minute are flat at its close unless `paths` gives them (minute index -> 4 closes)."""
    out = []
    for i, close in enumerate(closes):
        steps = (paths or {}).get(i, [close] * 4)
        path = [[s, *split(s)] for s in steps]
        out.append([ts(i, start), float(close), 1000, *split(close), path])
    return out


def run_of(series: list, context: list | None = None) -> dict:
    """A run as pnf_sim.replay.build_run returns it (only what the algo reads)."""
    return {
        "underlying": {"key": "NIFTY"},
        "expiry": "2026-10-13",
        "lotSize": 65,
        "pathSteps": 4,
        "replayStart": series[0][0],
        "charts": [
            {
                "key": "atm",
                "offset": 0,
                "strike": 25000,
                "error": None,
                "series": (context or []) + series,
                "ce": "NIFTY26O1325000CE",
                "pe": "NIFTY26O1325000PE",
            }
        ],
    }


def frame_at(trade: dict, t: int) -> dict | None:
    """The trade as the page shows it at replay time t."""
    shown = [f for f in trade["frames"] if f["at"] <= t]
    return shown[-1]["view"] if shown else None


def test_the_same_trades_as_the_live_rules():
    result = simulate(run_of(rows([*ENTRY, *TO_FIRST_TARGET, 195, 190, 185, 180, 178])), plain())
    [trade] = result["trades"]
    last = trade["frames"][-1]["view"]
    assert (last["state"], last["reason"], last["booked"]) == (CLOSED, TARGET, 33_800)
    assert [e["reason"] for e in last["exits"]] == [TARGET1, TARGET]
    assert result["total"] == {"trades": 1, "won": 1, "lost": 0, "booked": 33_800}
    assert last["legs"]["CE"]["key"] == leg_keys("atm")[0]
    # Toasts for the replay: no "(paper)", it is a simulation.
    titles = [e["title"] for e in result["events"]]
    assert "Algo sold NIFTY 25,000 ATM" in titles and "Algo booked half: NIFTY 25,000 ATM" in titles


def test_the_page_can_show_any_moment_of_the_replay():
    result = simulate(run_of(rows([*ENTRY, *TO_FIRST_TARGET, 195, 190, 185, 180, 178])), plain())
    [trade] = result["trades"]
    opened = ts(SIGNAL_BAR + 1)  # the signal bar's close
    assert frame_at(trade, opened - 1) is None  # not sold yet
    assert frame_at(trade, opened)["state"] == OPEN
    half = ts(SIGNAL_BAR + 1 + len(TO_FIRST_TARGET))
    assert frame_at(trade, half - 1)["holding"] == 1690
    assert frame_at(trade, half)["holding"] == 845 and frame_at(trade, half)["halfDone"]
    # Its stop as it stood each minute: the lower of VWAP and VWMA 10 at that close.
    assert trade["stops"][0] == [opened, 210.68]
    assert len(trade["stops"]) > 3
    # The algo itself: On from the first minute, Off after the Exit Time.
    assert result["status"][0]["on"] is True


def test_a_loss_limit_inside_a_minute_exits_at_that_step():
    # The minute after the entry spikes to 270 and closes back at 209: the max loss acts
    # on the spike (a live price), not on the close.
    series = rows([*ENTRY, 209], paths={SIGNAL_BAR + 1: [209, 270, 205, 209]})
    result = simulate(run_of(series), plain())
    [trade] = result["trades"]
    last = trade["frames"][-1]["view"]
    assert (last["state"], last["reason"]) == (CLOSED, MAX_LOSS)
    assert last["exits"][0]["price"] == 270.0
    assert trade["frames"][-1]["at"] == ts(SIGNAL_BAR + 1) + 15  # the second of four steps


def test_switched_on_mid_replay_it_trades_only_from_then():
    series = rows([*ENTRY, *TO_FIRST_TARGET])
    assert simulate(run_of(series), plain(), start_at=ts(SIGNAL_BAR + 2))["trades"] == []
    assert len(simulate(run_of(series), plain(), start_at=ts(SIGNAL_BAR))["trades"]) == 1


def test_each_session_starts_on_and_the_exit_time_closes_it():
    tuesday = MONDAY + timedelta(days=1)
    s = plain(lastEntry="09:35", exitTime="09:36")
    monday = rows([*ENTRY, 207, 207, 207])  # sold at 09:34, closed at 09:36
    result = simulate(run_of(monday + rows([*ENTRY, 207, 207, 207], start=tuesday)), s)
    assert len(result["trades"]) == 2  # Tuesday traded again
    assert all(t["frames"][-1]["view"]["reason"] == "exit_time" for t in result["trades"])
    days = {st["day"] for st in result["status"] if st["on"]}
    assert days == {"2026-10-12", "2026-10-13"}


def test_the_context_day_is_chart_history_and_never_traded():
    friday = MONDAY - timedelta(days=3)
    context = rows(ENTRY, start=friday)  # a qualifying signal on the context day
    result = simulate(run_of(rows([209, 210, 209]), context=context), plain())
    assert result["trades"] == []


def test_a_month_of_minutes_replays_in_a_few_seconds():
    # 30 sessions of zigzags on three charts: what a 30-day run asks of it.
    zig = [*ENTRY, *TO_FIRST_TARGET, 201, 204, 207, 208] * 15
    series = []
    for day in range(30):
        start = MONDAY + timedelta(days=day)
        series += rows(zig[:375], start=start)
    run = run_of(series)
    charts = (("atm_minus", -200), ("atm", 0), ("atm_plus", 200))
    run["charts"] = [{**run["charts"][0], "key": k, "offset": o} for k, o in charts]
    began = time.perf_counter()
    result = simulate(run, plain(maxOpen=3, maxTrades=50))
    assert time.perf_counter() - began < 8
    assert result["total"]["trades"] > 30


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_the_replay_is_always_on_paper(mode):
    result = simulate(run_of(rows(ENTRY)), plain(mode=mode))
    assert result["settings"]["mode"] == "paper"
    assert result["trades"][0]["frames"][0]["view"]["mode"] == "paper"


def test_the_simulators_algo_endpoint():
    from collections import OrderedDict

    from fastapi.testclient import TestClient

    import pnf_sim.app as sim_app

    run = {**run_of(rows([*ENTRY, *TO_FIRST_TARGET])), "key": "NIFTY|run"}
    sim_app.app.state.runs = OrderedDict({("NIFTY", "run"): run})
    http = TestClient(sim_app.app, base_url="https://127.0.0.1:8766")
    # Without a run: the settings alone are checked, and completed.
    checked = http.post("/api/algo", json={"settings": {"exitTime": "15:20"}}).json()["settings"]
    assert checked["exitTime"] == "15:20" and checked["qty"]["NIFTY"] == 1690
    bad = http.post("/api/algo", json={"settings": {"qty": {"NIFTY": 3445}}})
    assert bad.status_code == 422 and bad.json()["detail"]["errors"]["qty.NIFTY"].startswith("At most 3,380")
    # A run must still be loaded, and the quantity whole lots of its lot size.
    assert http.post("/api/algo", json={"run": "gone", "settings": {}}).status_code == 409
    odd = http.post("/api/algo", json={"run": "NIFTY|run", "settings": {"qty": {"NIFTY": 1700}}})
    assert odd.json()["detail"]["errors"] == {"qty.NIFTY": "A multiple of the lot size, 65"}
    result = http.post("/api/algo", json={"run": "NIFTY|run", "settings": {"belowVwap": {"NIFTY": 0}}}).json()
    assert result["total"]["trades"] == 1 and result["settings"]["mode"] == "paper"
    # With the default margin (30 points under VWAP) this script's signal is not traded.
    assert http.post("/api/algo", json={"run": "NIFTY|run", "settings": {}}).json()["total"]["trades"] == 0


def test_a_run_on_a_day_the_index_is_not_traded_makes_no_trades():
    wednesday = datetime(2026, 10, 14, 9, 15, tzinfo=IST)
    out = simulate(run_of(rows(ENTRY + TO_FIRST_TARGET, start=wednesday)), plain())
    assert out["trades"] == [] and out["total"]["trades"] == 0
    assert all(e["kind"] != "opened" for e in out["events"])
    traded = simulate(
        run_of(rows(ENTRY + TO_FIRST_TARGET, start=wednesday)), plain(**{"days": {"NIFTY": [2]}})
    )
    assert traded["total"]["trades"] == 1
