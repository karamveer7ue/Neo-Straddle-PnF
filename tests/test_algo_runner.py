"""The algo on the live server, end to end: the engine's charts (a fake Neo feed), the
runner, and orders through the real broker into a fake Kite (tests/kite_fakes.py).

Charts are scripted minute by minute (the closes of tests/test_algo_core.py), and each
minute close reaches the runner the way the engine delivers it. Kite fills at the
legs' prices of that moment, as a market order would.
"""

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta

import pytest
import requests
from fastapi.testclient import TestClient
from kiteconnect import exceptions as kex

from algo import live as live_mod
from algo.core import (
    CLOSED,
    EXIT_TIME,
    EXTERNAL,
    FAILED,
    LIVE,
    OPEN,
    PAPER,
    STOP,
    TARGET,
    TARGET1,
    UNWIND,
)
from algo.runner import AlgoRunner
from kite import broker as broker_mod
from kite.broker import KiteBroker
from kite.gateway import KiteGateway
from pnf import engine as engine_mod
from pnf.engine import Engine
from pnf.series import IST
from tests.kite_fakes import FakeKite, OrderClient
from tests.test_algo_core import ENTRY, SIGNAL_BAR, TO_FIRST_TARGET, split
from tests.test_engine import QUOTES, FakeGateway

BASE = datetime(2026, 10, 12, 9, 15, tzinfo=IST)  # Monday; the fake NIFTY charts: 22100 / 22300 / 22500
CE, PE = "NIFTY26O1322300CE", "NIFTY26O1322300PE"  # Kite's names for the ATM straddle (13 Oct expiry)
GROUP = "NIFTY|2026-10-13|22300"


def symbol(strike: int, kind: str) -> str:
    return f"NIFTY26O13{strike}{kind}"


class Rig:
    """The engine, the broker and the algo runner, wired as in the app."""

    def __init__(self, clock, tmp_path):
        self.clock = clock
        self.eng = Engine(FakeGateway())
        self.fake = FakeKite()
        # Two clients, as in the app: a refresh can run while an order is being sent.
        self.broker = KiteBroker(
            KiteGateway("key", "secret", client=self.fake, order_client=OrderClient(self.fake))
        )
        self.fake.access_token = "access-123"
        self.fake.clock = lambda: self.clock["t"]
        self.broker.user = {"id": "AB1234", "name": "Kams"}
        self.state_file = tmp_path / "algo.json"
        self.runner: AlgoRunner | None = None
        self.queue = None
        self.closes: dict[str, list] = {}

    async def start(self, runner: AlgoRunner | None = None):
        await self.eng._build()
        self.eng.built_for = (BASE.date(), True)  # as built after the open (the script starts at 09:15)
        self.eng.gw.logged_in = True
        self.eng.feed_state = "live"
        for chart in self.eng.charts:
            chart.history, chart.live = [], []  # scripted below, minute by minute
        await self.broker.refresh()
        self.runner = runner or AlgoRunner(self.eng, self.broker, state_file=self.state_file)
        self.runner.start()
        if runner is None:  # the scripted charts were written for plain "below VWAP"
            self.runner.update_settings({"belowVwap": {"NIFTY": 0}})
        self.queue = self.runner.subscribe()

    def chart(self, key: str = "atm"):
        return next(c for c in self.eng.charts if c.key == key)

    def price(self, key: str, close: float) -> None:
        """The legs trade at `close` now: on the Neo feed, and at Kite."""
        chart = self.chart(key)
        ce, pe = split(close)
        self.eng.ltp[chart.ce["key"]], self.eng.ltp[chart.pe["key"]] = ce, pe
        self.fake.prices[symbol(chart.strike, "CE")] = ce
        self.fake.prices[symbol(chart.strike, "PE")] = pe

    def close_bar(self, i: int, key: str = "atm") -> None:
        """Bar i of the script closes: the engine's minute close at the start of minute i + 1
        (the orders it causes are on their way)."""
        close = self.closes[key][i]
        chart = self.chart(key)
        chart.live.append((int((BASE + timedelta(minutes=i)).timestamp()), float(close), 1000))
        self.price(key, close)
        self.clock["t"] = BASE + timedelta(minutes=i + 1, seconds=0.25)
        self.runner.on_minute_closed(BASE + timedelta(minutes=i), self.clock["t"])

    async def bar(self, i: int, key: str = "atm") -> None:
        """Bar i closes, and its orders go through."""
        self.close_bar(i, key)
        await self.settle()

    async def run(self, start: int = 0, end: int | None = None, key: str = "atm") -> None:
        for i in range(start, len(self.closes[key]) if end is None else end):
            await self.bar(i, key)

    async def settle(self, refreshes: int = 3) -> None:
        """Let the runner's order tasks finish and Kite's poller come round a few times."""
        for _ in range(refreshes):
            while self.runner._tasks:
                await asyncio.gather(*list(self.runner._tasks), return_exceptions=True)
            await self.broker.refresh()
        while self.runner._tasks:
            await asyncio.gather(*list(self.runner._tasks), return_exceptions=True)

    def messages(self, kind: str) -> list[dict]:
        out = []
        while not self.queue.empty():
            msg = self.queue.get_nowait()
            if msg["type"] == kind:
                out.append(msg)
        return out

    def placed(self) -> list[tuple]:
        fields = ("tradingsymbol", "transaction_type", "quantity", "product", "tag")
        return [tuple(p[f] for f in fields) for p in self.fake.placed]

    def held(self) -> dict[str, int]:
        rows = self.fake.positions()["net"]
        return {r["tradingsymbol"]: r["quantity"] for r in rows if r["product"] == "MIS"}


@pytest.fixture
def rig(monkeypatch, tmp_path):
    clock = {"t": BASE}
    monkeypatch.setattr(engine_mod, "now_ist", lambda: clock["t"])
    monkeypatch.setattr(broker_mod, "now_ist", lambda: clock["t"])
    monkeypatch.setattr(engine_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(engine_mod, "ALERTS_FILE", tmp_path / "alerts.json")
    monkeypatch.setattr(broker_mod, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(broker_mod, "JOURNAL_FILE", tmp_path / "journal.json")
    monkeypatch.setattr(broker_mod, "REREAD_DELAY", 0)
    monkeypatch.setattr("kite.gateway.RETRY_DELAY", 0)
    return Rig(clock, tmp_path)


def go_live(rig: Rig) -> None:
    rig.runner.update_settings({"mode": LIVE})
    rig.runner.switch_on(rig.runner.summary())


# ---------------- Paper ----------------


def test_a_paper_trade_from_the_live_charts(rig):
    async def scenario():
        await rig.start()
        assert rig.runner.start_problem() == ""
        rig.runner.switch_on(rig.runner.summary())
        rig.closes["atm"] = [*ENTRY, *TO_FIRST_TARGET]
        await rig.run()

    asyncio.run(scenario())
    [t] = rig.runner.core.trades
    assert (t.mode, t.state, t.entry, t.half_done, t.holding) == (PAPER, OPEN, 208.0, True, 845)
    assert t.lot_size == 65 and t.legs["CE"].key == "nse_fo|22300CE"
    assert rig.fake.placed == []  # paper: no orders at all
    logged = [(a["kind"], a["title"]) for a in rig.eng.alerts if a["type"] == "algo"]
    assert ("opened", "Algo sold NIFTY 22,300 ATM (paper)") in logged
    assert ("booked", "Algo booked half (paper): NIFTY 22,300 ATM") in logged
    events = [m["kind"] for m in rig.messages("algo-event")]
    assert events == ["state", "opened", "booked"]
    saved = json.loads(rig.state_file.read_text())
    assert saved["trades"][0]["half_done"] and saved["seq"] == 1


def test_switching_on_needs_the_live_feed_and_a_matching_confirmation(rig):
    async def scenario():
        await rig.start()
        rig.eng.feed_state = "reconnecting"
        assert rig.runner.start_problem().startswith("Go live first")
        with pytest.raises(Exception, match="Go live first"):
            rig.runner.switch_on()
        rig.eng.feed_state = "live"
        rig.clock["t"] = BASE + timedelta(days=2)  # a Wednesday: not a NIFTY day
        assert rig.runner.start_problem() == "NIFTY is not traded on Wednesdays (Days, in the Algo menu)."
        rig.clock["t"] = BASE
        stale = {**rig.runner.summary(), "qty": 1300}  # the page showed another quantity
        with pytest.raises(Exception, match="changed meanwhile"):
            rig.runner.switch_on(stale)
        rig.runner.switch_on(rig.runner.summary())
        assert rig.runner.core.on and rig.runner.locked()

    asyncio.run(scenario())


# ---------------- Live, through Kite ----------------


def test_a_live_trade_sells_and_buys_back_through_kite(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = [*ENTRY, *TO_FIRST_TARGET, 195, 190, 185, 180, 178]
        await rig.run()

    asyncio.run(scenario())
    tag = "pnfalgo001"
    assert rig.placed() == [
        (CE, "SELL", 1690, "MIS", tag),
        (PE, "SELL", 1690, "MIS", tag),
        (CE, "BUY", 845, "MIS", tag),  # first target: half
        (PE, "BUY", 845, "MIS", tag),
        (CE, "BUY", 845, "MIS", tag),  # full target: the rest
        (PE, "BUY", 845, "MIS", tag),
    ]
    p = rig.fake.placed[0]
    assert p["order_type"] == "MARKET" and p["market_protection"] == -1
    [t] = rig.runner.core.trades
    assert (t.state, t.reason, t.entry) == (CLOSED, TARGET, 208.0)
    assert t.pnl() == pytest.approx(33_800)
    assert rig.held() == {CE: 0, PE: 0}


def test_kites_order_toasts_for_algo_orders_are_marked_as_the_algos(rig):
    """The page leaves those to the algo, which reports its own orders."""

    async def scenario():
        await rig.start()
        kite_queue = rig.broker.subscribe()
        go_live(rig)
        rig.closes["atm"] = ENTRY
        await rig.run()
        orders = []
        while not kite_queue.empty():
            msg = kite_queue.get_nowait()
            if msg["type"] == "order":
                orders.append(msg)
        return orders

    orders = asyncio.run(scenario())
    seen = [(o["kind"], o["status"], o["algo"]) for o in orders]
    assert seen == [("CE", "COMPLETE", True), ("PE", "COMPLETE", True)]


def test_a_rejected_leg_is_bought_back_and_the_algo_switches_off(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.fake.reject = {PE: "Insufficient funds. Required margin is 2,15,410"}
        rig.closes["atm"] = ENTRY
        await rig.run()

    asyncio.run(scenario())
    assert rig.placed() == [
        (CE, "SELL", 1690, "MIS", "pnfalgo001"),
        (PE, "SELL", 1690, "MIS", "pnfalgo001"),  # rejected by Kite's risk check
        (CE, "BUY", 1690, "MIS", "pnfalgo001"),  # the CE is not left short alone
    ]
    [t] = rig.runner.core.trades
    assert (t.state, t.active, rig.runner.core.on) == (FAILED, False, False)
    assert t.reason == "PE rejected: Insufficient funds. Required margin is 2,15,410"
    assert rig.held() == {CE: 0}
    failed = [a for a in rig.eng.alerts if a.get("kind") == "failed"]
    assert failed and failed[0]["title"] == "Algo entry failed: NIFTY 22,300 ATM"


def test_an_exit_kite_leaves_unfilled_is_cancelled_and_sent_again(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = [*ENTRY, 211]  # entry, then the stop
        await rig.run(0, SIGNAL_BAR + 1)
        rig.fake.hold_open = {CE, PE}  # the buy-backs stay OPEN (beyond the protection band)
        await rig.bar(SIGNAL_BAR + 1)
        [t] = rig.runner.core.trades
        assert t.want == 0 and t.holding == 1690 and len(rig.fake.placed) == 4
        for o in t.orders:
            o.placed_at -= live_mod.EXIT_TIMEOUT + 1  # they have been working too long
        rig.fake.hold_open = set()
        await rig.settle()
        return t

    t = asyncio.run(scenario())
    assert rig.fake.calls.count("cancel_order") == 2
    assert [p[1:3] for p in rig.placed()[2:]] == [("BUY", 1690)] * 4  # sent again after the cancels
    assert (t.state, t.reason) == (CLOSED, STOP)
    assert rig.held() == {CE: 0, PE: 0}


def test_a_position_closed_outside_the_algo_is_noticed(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = ENTRY
        await rig.run()
        await rig.broker.exit(GROUP, 1)  # you press Exit Full on the chart
        await rig.settle()

    asyncio.run(scenario())
    [t] = rig.runner.core.trades
    assert (t.state, t.reason, t.active) == (CLOSED, EXTERNAL, False)
    assert rig.held() == {CE: 0, PE: 0}


def test_an_exit_never_buys_back_more_than_kite_shows_short(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = [*ENTRY, 211]
        await rig.run(0, SIGNAL_BAR + 1)
        # Half is bought back in the Kite app, and the stop fires before the algo has seen it.
        for s in (CE, PE):
            rig.fake.place_order(
                variety="regular", exchange="NFO", tradingsymbol=s, transaction_type="BUY", quantity=845,
                product="MIS", order_type="MARKET", tag="app",
            )  # fmt: skip
        await rig.bar(SIGNAL_BAR + 1)

    asyncio.run(scenario())
    algo_buys = [p for p in rig.placed() if p[1] == "BUY" and p[4] == "pnfalgo001"]
    assert algo_buys == [(CE, "BUY", 845, "MIS", "pnfalgo001"), (PE, "BUY", 845, "MIS", "pnfalgo001")]
    assert rig.held() == {CE: 0, PE: 0}  # flat, never long
    [t] = rig.runner.core.trades
    assert t.state == CLOSED and t.active is False


def test_your_own_position_on_a_straddle_keeps_the_algo_out_of_it(rig):
    async def scenario():
        await rig.start()
        await rig.broker.sell("NIFTY", rig.eng.expiry, 22300, 65)  # a manual NRML sell
        await rig.broker.refresh()
        go_live(rig)
        rig.closes["atm"] = ENTRY
        await rig.run()

    asyncio.run(scenario())
    assert rig.runner.core.trades == []
    skipped = [a for a in rig.eng.alerts if a.get("kind") == "skipped"]
    assert skipped[0]["detail"] == "your own position is open on this straddle"


def test_the_exit_time_closes_live_trades_through_kite(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = ENTRY
        await rig.run()
        rig.clock["t"] = datetime(2026, 10, 12, 15, 12, 0, 300000, tzinfo=IST)
        rig.runner.on_loop(rig.clock["t"])
        await rig.settle()

    asyncio.run(scenario())
    [t] = rig.runner.core.trades
    assert (t.state, t.reason, rig.runner.core.on) == (CLOSED, EXIT_TIME, False)
    assert rig.held() == {CE: 0, PE: 0}


# ---------------- Alarms, restarts, the page ----------------


def test_alarms_ring_while_the_feed_or_kite_is_down_with_a_trade_open(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = ENTRY
        await rig.run()
        rig.messages("algo-ring")
        rig.eng.feed_state = "reconnecting"
        rig.runner.on_loop(rig.clock["t"])
        first = rig.messages("algo-ring")
        rig.runner.on_loop(rig.clock["t"])  # within 10 seconds: no second ring
        assert rig.messages("algo-ring") == []
        rig.runner._rang_at -= 10
        rig.runner.on_loop(rig.clock["t"])
        again = rig.messages("algo-ring")
        rig.broker.user = None  # Kite disconnected too
        rig.runner.on_loop(rig.clock["t"])
        both = rig.messages("algo-ring")
        return first, again, both

    first, again, both = asyncio.run(scenario())
    assert [a["key"] for a in first[0]["alarms"]] == ["feed"]
    assert first[0]["alarms"][0]["title"] == "Live feed is down"
    assert len(again) == 1  # rung again after 10 seconds
    assert [a["key"] for a in both[0]["alarms"]] == ["feed", "kite"]  # a new alarm rings at once
    logged = [a["title"] for a in rig.eng.alerts if a.get("kind") == "alarm"]
    assert logged == ["Kite is disconnected", "Live feed is down"]  # each logged once


def test_a_restart_keeps_managing_the_open_trade(rig, tmp_path):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = [*ENTRY, 211]
        await rig.run(0, SIGNAL_BAR + 1)
        await rig.runner.stop()
        rig.runner.engine.listeners.clear()
        rig.broker.listeners.clear()
        restarted = AlgoRunner(rig.eng, rig.broker, state_file=rig.state_file)
        assert not restarted.core.on and restarted.core.trades[0].managed
        assert restarted.locked()  # the charts stay put while it manages the trade
        rig.runner = restarted
        restarted.start()
        await rig.bar(SIGNAL_BAR + 1)  # the stop

    asyncio.run(scenario())
    [t] = rig.runner.core.trades
    assert (t.state, t.reason) == (CLOSED, STOP)
    assert rig.held() == {CE: 0, PE: 0}


@pytest.mark.parametrize("text", ["{not json", "[]", '{"trades": [{"legs": []}]}', "[" * 100_000])
def test_an_unreadable_state_file_is_kept_aside(rig, text):
    """Whatever is in the file, the server comes up; the file is kept, under its own name."""
    rig.state_file.write_text(text)
    runner = AlgoRunner(rig.eng, rig.broker, state_file=rig.state_file)
    assert runner.core.trades == [] and not rig.state_file.exists()
    [kept] = rig.state_file.parent.glob("algo.json.unreadable-*")
    assert kept.read_text() == text
    asyncio.run(runner.stop())


def test_the_endpoints_and_the_lock_on_the_charts(rig):
    import pnf.app

    asyncio.run(rig.start())
    app = pnf.app.app
    app.state.engine, app.state.broker, app.state.algo = rig.eng, rig.broker, rig.runner
    http = TestClient(app, base_url="https://127.0.0.1:8765")
    rig.eng.rebuild = lambda: None

    res = http.post("/api/algo/settings", json={"changes": {"qty": {"NIFTY": 3445}}})
    assert res.status_code == 422
    assert res.json()["detail"]["errors"] == {"qty.NIFTY": "At most 3,380 (twice the default 1,690)"}
    res = http.post("/api/algo/settings", json={"changes": {"qty": {"NIFTY": 1700}}})
    assert res.json()["detail"]["errors"] == {"qty.NIFTY": "A multiple of the lot size, 65"}
    assert (
        http.post("/api/algo/settings", json={"changes": {"exitTime": "15:15"}}).json()["settings"][
            "exitTime"
        ]
        == "15:15"
    )

    stale = {**rig.runner.summary(), "strikes": [1, 2, 3]}
    assert http.post("/api/algo/on", json={"expect": stale}).status_code == 409
    on = http.post("/api/algo/on", json={"expect": rig.runner.summary()})
    assert on.status_code == 200 and on.json()["on"] and on.json()["locked"]
    # While the algo is On, the index, expiry, ATM and days cannot change.
    locked = http.post("/api/config", json={"underlying": "SENSEX", "days": 3})
    assert locked.status_code == 409 and "switch it Off first" in locked.json()["detail"]
    assert http.post("/api/algo/settings", json={"changes": {"mode": "live"}}).status_code == 422
    assert http.post("/api/algo/off").json()["on"] is False
    assert http.post("/api/config", json={"underlying": "NIFTY", "days": 3}).status_code == 202
    # Other sites cannot reach them.
    assert http.post("/api/algo/on", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    with http.websocket_connect("/algo/ws", headers={"Host": "127.0.0.1:8765"}) as ws:
        state = ws.receive_json()
        assert state["type"] == "algo" and state["settings"]["exitTime"] == "15:15"
        assert state["caps"] == {"NIFTY": 3380, "BANKNIFTY": 1440, "SENSEX": 960}


def test_states_are_numbered_so_a_page_never_goes_back_to_an_older_one(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = ENTRY
        await rig.run()
        [t] = rig.runner.core.trades
        rig.messages("algo")
        rig.runner.exit_trade(t.id)
        answer = rig.runner.state_json()  # what the Exit request answers with
        await rig.settle()  # Kite fills the buy-backs: pushed as newer states
        return answer, rig.messages("algo")

    answer, pushed = asyncio.run(scenario())
    assert [tr["state"] for tr in answer["trades"]] == [OPEN] and answer["trades"][0]["want"] == 0
    assert pushed[-1]["trades"][0]["state"] == CLOSED
    assert pushed[-1]["version"] > answer["version"] and pushed[-1]["boot"] == answer["boot"]
    assert len({m["version"] for m in pushed}) == len(pushed)  # each push numbered anew


# ---------------- Kite's order book is the record ----------------
#
# Every order with a trade's tag in Kite's order book is that trade's, whatever happened
# to the reply that placed it; a position counts as bought back outside the algo only
# when an order that is not the algo's explains it.


def app_buy_back(rig: Rig, tradingsymbol: str, quantity: int) -> None:
    """A buy-back placed in the Kite app (no tag)."""
    rig.fake.place_order(
        variety="regular", exchange="NFO", tradingsymbol=tradingsymbol, transaction_type="BUY",
        quantity=quantity, product="MIS", order_type="MARKET", tag=None,
    )  # fmt: skip


def hold_order_call(rig: Rig, when, then=None):
    """Hold the next order call `when(fake)` picks, in its thread, until released (then
    `then()`, which may raise): the rest of the app goes on meanwhile."""
    started, release = threading.Event(), threading.Event()

    def hook(fake):
        if not started.is_set() and when(fake):
            started.set()
            release.wait(5)
            if then:
                then()

    rig.fake.before["place_order"] = hook
    return started, release


async def wait_for(event: threading.Event) -> None:
    for _ in range(500):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the order call never came")


@pytest.mark.parametrize("half", [False, True], ids=["stop", "first-target"])
def test_a_refresh_while_a_buy_back_is_on_its_way_does_not_take_it_for_an_outside_one(rig, half):
    """Reads and orders run side by side: a refresh can show the CE bought back while the
    reply placing the PE's buy-back is still on its way."""

    async def scenario():
        await rig.start()
        go_live(rig)
        if half:
            rig.closes["atm"] = [*ENTRY, *TO_FIRST_TARGET, 195, 190, 185, 180, 178]
            exit_bar = SIGNAL_BAR + len(TO_FIRST_TARGET)
        else:
            rig.closes["atm"] = [*ENTRY, 211]
            exit_bar = SIGNAL_BAR + 1
        await rig.run(0, exit_bar)
        # The PE's buy-back call is held (the CE's is placed and filled by then).
        started, release = hold_order_call(
            rig, lambda fake: sum(p["transaction_type"] == "BUY" for p in fake.placed) % 2 == 1
        )
        rig.close_bar(exit_bar)
        await wait_for(started)
        await rig.broker.refresh()  # the poller comes round meanwhile
        release.set()
        await rig.settle()
        await rig.run(exit_bar + 1)
        [t] = rig.runner.core.trades
        return t

    t = asyncio.run(scenario())
    assert rig.held() == {CE: 0, PE: 0}
    assert (t.state, t.active, t.open("CE"), t.open("PE")) == (CLOSED, False, 0, 0)
    assert [d.reason for d in t.exits] == ([TARGET1, TARGET] if half else [STOP])  # nothing "outside"
    buys = [p[2] for p in rig.placed() if p[1] == "BUY"]
    assert buys == ([845] * 4 if half else [1690] * 2)


@pytest.mark.parametrize("pe_sent", [True, False], ids=["both-legs-sent", "pe-never-sent"])
def test_an_entry_cut_short_by_a_crash_is_found_by_its_tag(rig, monkeypatch, pe_sent):
    """The server dies while the PE is being sent, with nothing saved since the signal.
    Back up, it finds the trade's orders in Kite's order book: both legs (the trade goes
    on), or the CE alone (the PE counts as never placed: the entry fails, and the CE is
    bought back). A graceful stop saves more, so this covers it too."""
    monkeypatch.setattr(live_mod, "UNKNOWN_SETTLE", 0.0)  # no waiting for a late listing

    def connection_lost():
        if not pe_sent:
            raise requests.ConnectionError("Connection aborted.")

    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = ENTRY
        await rig.run(0, SIGNAL_BAR)
        started, release = hold_order_call(rig, lambda fake: len(fake.placed) == 1, connection_lost)
        rig.close_bar(SIGNAL_BAR)  # the signal: the CE is sent, then the PE
        await wait_for(started)
        # The server dies (a power cut, kill -9): nothing more is saved, and its tasks end.
        rig.runner._save = lambda force=False: None
        rig.runner._unlock()  # a dead process holds no lock
        tasks = list(rig.runner._tasks)
        for task in tasks:
            task.cancel()
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        rig.fake.before = {}
        rig.eng.listeners.clear()
        rig.broker.listeners.clear()
        rig.runner = AlgoRunner(rig.eng, rig.broker, state_file=rig.state_file)  # the server is back
        rig.runner.start()
        await rig.settle()
        [t] = rig.runner.core.trades
        return t

    t = asyncio.run(scenario())
    if pe_sent:
        assert (t.state, t.entry, t.managed, t.holding) == (OPEN, 208.0, True, 1690)
        assert rig.held() == {CE: -1690, PE: -1690}
    else:
        assert (t.state, t.active, t.reason) == (FAILED, False, "PE lost: Kite never listed it: not placed")
        assert rig.held() == {CE: 0}  # the CE was not left short alone
    assert rig.runner.alarms() == []


def test_an_entry_order_kite_lists_late_is_found_and_bought_back(rig):
    """The CE's reply is lost, and Kite lists the order only after it counted as never placed
    (the entry failed meanwhile): it is still found by its tag, and bought back."""

    async def scenario():
        await rig.start()
        go_live(rig)
        place = rig.fake.place_order

        def placed_but_reply_lost(**params):
            order_id = place(**params)
            if params["transaction_type"] == "SELL" and params["tradingsymbol"] == CE:
                raise requests.ReadTimeout("Read timed out. (read timeout=10)")
            return order_id

        rig.fake.place_order = placed_but_reply_lost
        rig.fake.lag_orders = rig.fake.lag_fills = 1  # Kite is slow to list it
        rig.closes["atm"] = ENTRY
        await rig.run()
        [t] = rig.runner.core.trades
        [ce] = rig.runner.live.unknown(t)  # being looked for, by its tag
        assert t.state == FAILED and "outcome is unknown" in t.reason
        ce.placed_at -= live_mod.UNKNOWN_SETTLE + 1  # not listed 15 s on: counted as not placed
        await rig.settle()
        assert (ce.status, t.active) == ("LOST", False)
        rig.fake.lag_orders = rig.fake.lag_fills = 0  # ... and then Kite lists it
        await rig.settle()
        return t

    t = asyncio.run(scenario())
    assert rig.held() == {CE: 0}
    assert (t.state, t.active) == (FAILED, False)
    [late] = [a for a in rig.eng.alerts if a.get("title") == "Algo found a late order: NIFTY 22,300 ATM"]
    assert late["detail"] == "Kite listed the CE sell of 1,690 late; buying back what is short."


def test_a_failed_entry_whose_leg_you_buy_back_in_kite_closes(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.fake.reject = {PE: "Insufficient funds"}

        def refuse_buy_backs(fake):  # Kite refuses the algo's unwinding buy-back for now
            if len(fake.placed) >= 2:
                raise kex.InputException("RMS: blocked for now", code=400)

        rig.fake.before["place_order"] = refuse_buy_backs
        rig.closes["atm"] = ENTRY
        await rig.run()
        [t] = rig.runner.core.trades
        assert (t.state, t.holding) == (FAILED, 1690)
        assert rig.runner.live.problems[t.id] == "CE: RMS: blocked for now"
        rig.fake.before, rig.fake.reject = {}, {}
        app_buy_back(rig, CE, 1690)  # alarmed ("Unhedged leg"), you buy the CE back in Kite
        await rig.settle()
        return t

    t = asyncio.run(scenario())
    assert (t.state, t.active, t.reason) == (FAILED, False, "PE rejected: Insufficient funds")
    assert [d.reason for d in t.exits] == [UNWIND, EXTERNAL]
    assert rig.held() == {CE: 0} and not rig.runner.locked()
    assert rig.runner.alarms() == []


def test_a_live_trade_left_open_overnight_is_let_go_with_an_alert(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = ENTRY
        await rig.run()
        # The Exit Time sends the buy-backs; Kite leaves them open, and the Mac sleeps.
        rig.fake.hold_open = {CE, PE}
        rig.clock["t"] = datetime(2026, 10, 12, 15, 12, 0, 300000, tzinfo=IST)
        rig.runner.on_loop(rig.clock["t"])
        while rig.runner._tasks:
            await asyncio.gather(*list(rig.runner._tasks), return_exceptions=True)
        # The next morning: a new order book, and Zerodha squared the MIS positions off.
        rig.fake.hold_open, rig.fake.order_book, rig.fake.fills = set(), [], []
        rig.clock["t"] = datetime(2026, 10, 13, 9, 20, tzinfo=IST)
        rig.runner.on_loop(rig.clock["t"])
        await rig.settle()

    asyncio.run(scenario())
    assert rig.runner.core.trades == [] and not rig.runner.locked()
    assert rig.fake.calls.count("cancel_order") == 0 and rig.runner.alarms() == []
    [left] = [
        a for a in rig.eng.alerts if a.get("title") == "Algo trade left open overnight: NIFTY 22,300 ATM"
    ]
    assert left["detail"].startswith("Still open when 12 Oct ended, so the algo no longer manages it.")


def test_a_rejected_buy_back_is_sent_again_after_a_growing_pause(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = [*ENTRY, 211]  # entry, then the stop
        await rig.run(0, SIGNAL_BAR + 1)
        rig.fake.reject = {CE: "RMS: order rejected"}
        await rig.bar(SIGNAL_BAR + 1)
        [t] = rig.runner.core.trades

        def ce_buy_backs():
            return sum(1 for p in rig.placed() if p[:2] == (CE, "BUY"))

        live = rig.runner.live
        for _ in range(10):  # the poller's refreshes: nothing is sent during the pause
            await rig.settle(1)
        assert ce_buy_backs() == 1
        assert live.problems[t.id] == "Kite rejected the CE buy-back: RMS: order rejected"
        live._next_try[t.id] = 0  # the pause is over: sent again, rejected again
        await rig.settle()
        assert ce_buy_backs() == 2
        assert live_mod.RETRY_DELAY < live._next_try[t.id] - time.monotonic() <= 2 * live_mod.RETRY_DELAY
        rig.fake.reject = {}
        rig.runner.exit_trade(t.id)  # Exit: sent at once, whatever the pause
        await rig.settle()
        return t

    t = asyncio.run(scenario())
    assert (t.state, rig.held()) == (CLOSED, {CE: 0, PE: 0})
    assert rig.runner.live.problems == {}


def test_the_algo_waits_for_todays_charts_and_takes_no_trades_on_charts_that_changed(rig):
    async def scenario():
        await rig.start()
        rig.eng.built_for = (BASE.date(), False)  # built before the open: rebuilt at 09:16
        assert rig.runner.start_problem().startswith("Today's charts are not built yet")
        rig.eng.built_for = (BASE.date(), True)
        go_live(rig)
        # Other strikes come in under it (only a new day's rebuild does that).
        quote = {**QUOTES["NIFTY"], "open": QUOTES["NIFTY"]["open"] + 200}

        async def index_quote(u):
            return quote

        rig.eng.gw.index_quote = index_quote
        await rig.eng._build()
        rig.closes["atm"] = ENTRY
        await rig.run()

    asyncio.run(scenario())
    assert not rig.runner.core.on and rig.runner.core.trades == []
    [off] = [a for a in rig.eng.alerts if a.get("title") == "Algo Off for new trades"]
    assert off["detail"].startswith("The charts changed (index, expiry or strikes) after it was switched On.")


def test_a_buy_back_the_order_book_does_not_list_yet_is_not_guessed_at(rig):
    """Kite's positions show the trade bought back before its order book lists the order
    that did it: reported, never taken as an exit, until an order explains it. A round
    trip of yours on the CE earlier in the day explains nothing."""

    async def scenario():
        await rig.start()
        rig.fake.place_order(
            variety="regular", exchange="NFO", tradingsymbol=CE, transaction_type="SELL",
            quantity=65, product="MIS", order_type="MARKET", tag=None,
        )  # fmt: skip
        app_buy_back(rig, CE, 65)
        go_live(rig)
        rig.closes["atm"] = ENTRY
        await rig.run()
        [t] = rig.runner.core.trades
        app_buy_back(rig, CE, 1690)
        app_buy_back(rig, PE, 1690)
        rig.fake.lag_orders = 2  # in the positions, not yet in the order book
        await rig.settle()
        assert (t.state, t.holding, t.exits) == (OPEN, 1690, [])
        [gap] = [a for a in rig.runner.alarms() if a["key"] == f"gap:{t.id}"]
        assert gap["detail"].startswith("Kite shows 1,690 CE and 1,690 PE less short than the algo holds")
        rig.fake.lag_orders = 0  # the order book catches up: it was you, in the Kite app
        await rig.settle()
        return t

    t = asyncio.run(scenario())
    assert (t.state, t.reason, t.active) == (CLOSED, EXTERNAL, False)
    assert rig.runner.alarms() == []


def test_algo_positions_no_trade_holds_raise_an_alarm_and_tags_are_never_reused(rig):
    """The saved state is lost mid-trade (deleted, say): Kite's position from the algo's
    orders is reported, and new trades number on from the tags Kite shows today."""

    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = ENTRY
        await rig.run()
        await rig.runner.stop()
        rig.eng.listeners.clear()
        rig.broker.listeners.clear()
        rig.state_file.unlink()
        rig.runner = AlgoRunner(rig.eng, rig.broker, state_file=rig.state_file)
        rig.runner.start()
        await rig.settle()

    asyncio.run(scenario())
    alarms = {a["key"]: a for a in rig.runner.alarms()}
    assert alarms[f"orphan:{CE}"]["title"] == f"Algo position not managed: {CE}"
    assert alarms[f"orphan:{PE}"]["detail"].startswith("Kite shows 1,690 short (MIS) from the algo's orders")
    assert rig.runner.core.seq == 1  # the next trade is pnfalgo002


def test_a_cancel_kite_does_not_act_on_is_sent_again_later(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = [*ENTRY, 211]
        await rig.run(0, SIGNAL_BAR + 1)
        rig.fake.hold_open = {CE, PE}  # the buy-backs stay open
        await rig.bar(SIGNAL_BAR + 1)
        [t] = rig.runner.core.trades
        sent = []
        rig.fake.cancel_order = lambda variety, order_id, parent_order_id=None: sent.append(order_id)
        for o in t.orders:
            o.placed_at -= live_mod.EXIT_TIMEOUT + 1
        await rig.settle()
        assert len(sent) == 2  # once each, not at every refresh
        for order_id in rig.runner.live._cancels:
            rig.runner.live._cancels[order_id] -= live_mod.CANCEL_AGAIN + 1
        await rig.settle()
        return sent

    sent = asyncio.run(scenario())
    assert len(sent) == 4  # sent again, as Kite still showed them working


def test_a_trade_reopened_by_a_late_order_reports_its_new_pnl(rig):
    """Closed at nothing (its CE counted as not placed), then reopened when Kite lists the
    CE sell late and bought back higher: its P&L, and the day's, include that loss."""

    async def scenario():
        await rig.start()
        go_live(rig)
        place = rig.fake.place_order

        def placed_but_reply_lost(**params):
            order_id = place(**params)
            if params["transaction_type"] == "SELL" and params["tradingsymbol"] == CE:
                raise requests.ReadTimeout("Read timed out.")
            return order_id

        rig.fake.place_order = placed_but_reply_lost
        rig.fake.lag_orders = rig.fake.lag_fills = 1
        rig.closes["atm"] = ENTRY
        await rig.run()
        [t] = rig.runner.core.trades
        [ce] = rig.runner.live.unknown(t)
        ce.placed_at -= live_mod.UNKNOWN_SETTLE + 1
        await rig.settle()
        assert (ce.status, t.active, t.pnl()) == ("LOST", False, 0)  # closed, its P&L worked out
        rig.fake.prices[CE] += 50  # the CE is dearer by the time Kite lists the sell
        rig.fake.lag_orders = rig.fake.lag_fills = 0
        await rig.settle()
        return t

    t = asyncio.run(scenario())
    assert (t.state, t.active) == (FAILED, False)
    assert t.pnl() == t.booked() == pytest.approx(-50 * 1690)
    assert rig.runner.core.day_pnl(mode=LIVE) == pytest.approx(-50 * 1690)


def test_positions_ahead_of_the_order_book_never_make_a_buy_back_outside_one(rig):
    """The algo's half exit shows in Kite's positions two refreshes before its order book
    lists it, and you had sold and bought back 65 of the CE by hand earlier that day: no
    gap is booked (or alarmed), and the trade still buys back everything it sold."""

    async def scenario():
        await rig.start()
        rig.fake.place_order(
            variety="regular", exchange="NFO", tradingsymbol=CE, transaction_type="SELL",
            quantity=65, product="MIS", order_type="MARKET", tag=None,
        )  # fmt: skip
        app_buy_back(rig, CE, 65)
        await rig.broker.refresh()
        go_live(rig)
        rig.closes["atm"] = [*ENTRY, *TO_FIRST_TARGET, 195, 190, 185, 180, 178]
        first_target = SIGNAL_BAR + len(TO_FIRST_TARGET)
        await rig.run(0, first_target)
        rig.close_bar(first_target)
        while rig.runner._tasks:
            await asyncio.gather(*list(rig.runner._tasks), return_exceptions=True)
        rig.fake.lag_orders = 2  # the order book is behind the positions
        for _ in range(3):
            await rig.broker.refresh()
        [t] = rig.runner.core.trades
        assert rig.runner.live.mismatch == {} and EXTERNAL not in [d.reason for d in t.exits]
        rig.fake.lag_orders = 0
        await rig.settle()
        await rig.run(first_target + 1)
        return t

    t = asyncio.run(scenario())
    assert (t.state, t.reason) == (CLOSED, TARGET)
    assert rig.held() == {CE: 0, PE: 0} and rig.runner.alarms() == []


def test_your_own_mis_long_on_a_contract_the_algo_traded_is_not_its_alarm(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = [*ENTRY, 211]
        await rig.run()  # entry, then the stop: closed
        app_buy_back(rig, CE, 65)  # later, you buy 65 of that CE yourself (MIS)
        await rig.settle()

    asyncio.run(scenario())
    assert rig.runner.alarms() == []


def test_only_one_server_runs_the_algo_on_an_account(rig):
    """A second server on the same state file (an old one left running on another port)
    must not trade too: it says so, takes no part, and never writes the file."""
    asyncio.run(rig.start())
    second = AlgoRunner(rig.eng, rig.broker, state_file=rig.state_file)
    assert second.disabled.startswith("Another server is already running the algo")
    assert second.start_problem() == second.disabled and second.live is None
    listeners = (list(rig.eng.listeners), list(rig.broker.listeners))
    second.start()
    assert (rig.eng.listeners, rig.broker.listeners) == listeners  # not wired in
    rig.runner.update_settings({"maxTrades": 7})
    second._save(force=True)
    assert json.loads(rig.state_file.read_text())["settings"]["maxTrades"] == 7  # untouched by the second
    asyncio.run(rig.runner.stop())  # the first stops: the lock is free again
    third = AlgoRunner(rig.eng, rig.broker, state_file=rig.state_file)
    assert third.disabled == "" and third.core.settings.max_trades == 7
    asyncio.run(third.stop())


def test_an_exit_time_already_past_is_refused_while_trades_are_open(rig):
    async def scenario():
        await rig.start()
        go_live(rig)
        rig.closes["atm"] = ENTRY
        await rig.run()
        rig.clock["t"] = datetime(2026, 10, 12, 11, 0, tzinfo=IST)
        rig.runner.update_settings({"lastEntry": "10:00"})
        with pytest.raises(Exception, match="Already past"):
            rig.runner.update_settings({"exitTime": "10:30"})  # a typo would exit everything now
        rig.runner.update_settings({"exitTime": "11:30"})
        [t] = rig.runner.core.trades
        assert t.active and rig.runner.core.settings.exit_time == "11:30"
        rig.runner.switch_off()
        rig.runner.exit_all()
        await rig.settle()
        rig.runner.update_settings({"exitTime": "10:30"})  # nothing open: fine

    asyncio.run(scenario())
    assert rig.runner.core.settings.exit_time == "10:30"
