"""Kite trading: contracts, exit sizing, the broker against a fake Kite, and the endpoints."""

import asyncio
import json
import os
from datetime import date, datetime

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kiteconnect import exceptions as kex

import kite.web
from kite import broker as broker_mod
from kite.book import BUY, SELL, plan_exit
from kite.broker import KiteBroker, token_expired
from kite.contracts import ContractBook
from kite.gateway import KiteError, KiteGateway, OutcomeUnknown, parse_slices
from kite.order_relay import RelayAuth
from pnf.series import IST
from tests.kite_fakes import INSTRUMENTS, FakeKite

TODAY = date(2026, 10, 12)
NIFTY_EXPIRY = date(2026, 10, 13)
CE, PE = "NIFTY26O1322300CE", "NIFTY26O1322300PE"


def book() -> ContractBook:
    return ContractBook({ex: rows for ex, rows in INSTRUMENTS.items()}, TODAY)


@pytest.fixture
def clock(monkeypatch):
    now = {"t": datetime(2026, 10, 12, 10, 0, 30, tzinfo=IST)}
    monkeypatch.setattr(broker_mod, "now_ist", lambda: now["t"])
    return now


@pytest.fixture
def files(monkeypatch, tmp_path):
    monkeypatch.setattr(broker_mod, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(broker_mod, "JOURNAL_FILE", tmp_path / "journal.json")
    monkeypatch.setattr("kite.gateway.RETRY_DELAY", 0)
    return tmp_path


@pytest.fixture
def connected(clock, files):
    """A broker logged in to a fake Kite, contracts loaded, no positions."""

    def make(**kwargs):
        fake = FakeKite(prices={CE: 120.0, PE: 110.0})
        b = KiteBroker(KiteGateway("key", "secret", client=fake), **kwargs)
        fake.access_token = "access-123"
        b.user = {"id": "AB1234", "name": "Kams"}
        asyncio.run(b.refresh())
        return b, fake

    return make


# ---------------- Contracts ----------------


def test_contract_book_finds_index_options_on_each_exchange():
    b = book()
    ce, pe = b.straddle("NIFTY", NIFTY_EXPIRY, 22300)
    assert (ce.exchange, ce.tradingsymbol, ce.kind, ce.lot_size) == ("NFO", CE, "CE", 65)
    assert pe.tradingsymbol == PE
    sensex = b.option("SENSEX", date(2026, 10, 15), 72300, "PE")
    assert (sensex.exchange, sensex.lot_size) == ("BFO", 20)
    assert b.by_key(f"NFO:{CE}") == ce
    bank = b.option("BANKNIFTY", date(2026, 10, 27), 56000, "CE")
    assert (bank.exchange, bank.tradingsymbol, bank.lot_size) == ("NFO", "BANKNIFTY26OCT56000CE", 30)
    # Other names (FINNIFTY), futures and unlisted strikes are not straddles here.
    assert b.option("FINNIFTY", date(2026, 10, 27), 24000, "CE") is None
    assert b.straddle("NIFTY", NIFTY_EXPIRY, 22325) is None
    assert b.lot_sizes()["NIFTY|2026-10-20"] == 65
    assert b.lot_sizes()["SENSEX|2026-10-22"] == 20


# ---------------- Exit sizing ----------------


def short(symbol, qty, product="NRML"):
    return {"exchange": "NFO", "tradingsymbol": symbol, "product": product, "quantity": -qty}


def test_exit_full_and_half_in_whole_lots():
    ce, pe = book().straddle("NIFTY", NIFTY_EXPIRY, 22300)
    net = [short(CE, 650), short(PE, 325)]  # 10 lots and 5 lots
    full = plan_exit([ce, pe], net, [], 1)
    assert [(r.contract.tradingsymbol, r.side, r.quantity, r.product) for r in full] == [
        (CE, BUY, 650, "NRML"),
        (PE, BUY, 325, "NRML"),
    ]
    half = plan_exit([ce, pe], net, [], 0.5)
    assert [(r.contract.kind, r.quantity) for r in half] == [("CE", 325), ("PE", 130)]  # 5 lots, 2 lots
    # A single lot is not halved.
    assert plan_exit([ce, pe], [short(CE, 65), short(PE, 65)], [], 0.5) == []


def test_exit_leaves_out_quantity_already_being_exited():
    ce, pe = book().straddle("NIFTY", NIFTY_EXPIRY, 22300)
    net = [short(CE, 650), short(PE, 650)]
    working = [
        {"exchange": "NFO", "tradingsymbol": CE, "product": "NRML", "transaction_type": BUY,
         "status": "OPEN", "pending_quantity": 650},
        {"exchange": "NFO", "tradingsymbol": PE, "product": "NRML", "transaction_type": BUY,
         "status": "COMPLETE", "pending_quantity": 0},
    ]  # fmt: skip
    # A second Exit Full must not buy the CE again (that would turn it long).
    assert [(r.contract.kind, r.quantity) for r in plan_exit([ce, pe], net, working, 1)] == [("PE", 650)]


def test_exit_uses_each_legs_own_product_and_side():
    ce, pe = book().straddle("NIFTY", NIFTY_EXPIRY, 22300)
    net = [short(CE, 130, "MIS"), short(CE, 65, "NRML"), {**short(PE, -65), "quantity": 65}]
    plan = plan_exit([ce, pe], net, [], 1)
    assert [(r.contract.kind, r.side, r.quantity, r.product) for r in plan] == [
        ("CE", BUY, 130, "MIS"),
        ("CE", BUY, 65, "NRML"),
        ("PE", SELL, 65, "NRML"),  # a long leg is closed by selling
    ]


# ---------------- Selling and exiting through the broker ----------------


def test_sell_places_ce_then_pe_at_market_with_protection(connected):
    b, fake = connected()
    res = asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 650))
    assert [r["ok"] for r in res["results"]] == [True, True]
    assert [p["tradingsymbol"] for p in fake.placed] == [CE, PE]
    p = fake.placed[0]
    assert p["transaction_type"] == "SELL" and p["quantity"] == 650
    assert p["order_type"] == "MARKET" and p["market_protection"] == -1
    assert p["product"] == "NRML" and p["tag"] == "pnf" and p["exchange"] == "NFO"

    asyncio.run(b.refresh())
    [group] = b.state_json()["groups"]
    assert group["key"] == "NIFTY|2026-10-13|22300" and group["open"]
    assert [(leg["kind"], leg["quantity"], leg["averagePrice"]) for leg in group["legs"]] == [
        ("CE", -650, 120.0),
        ("PE", -650, 110.0),
    ]


def test_sell_checks_quantity_and_the_size_cap(connected):
    b, fake = connected()
    with pytest.raises(KiteError, match="multiple of the lot size, 65"):
        asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 100))
    # Never more than twice the algo's default quantity (NIFTY 1,690): 3,380.
    with pytest.raises(KiteError, match=r"3,445 is above the 3,380 limit for NIFTY"):
        asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 3445))
    with pytest.raises(KiteError, match="no NIFTY 22325"):
        asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22325, 65))
    assert fake.placed == []
    assert b.state_json()["maxQty"] == {"NIFTY": 3380, "BANKNIFTY": 1440, "SENSEX": 960}
    res = asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 3380))  # exactly the cap
    assert [r["ok"] for r in res["results"]] == [True, True]


def test_pe_is_held_back_when_the_ce_is_refused(connected):
    b, fake = connected()
    fake.fail_next = [kex.InputException("Insufficient funds", code=400)]
    res = asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 65))
    assert [(r["kind"], r["ok"], r.get("held", False)) for r in res["results"]] == [
        ("CE", False, False),
        ("PE", False, True),
    ]
    assert "Insufficient funds" in res["results"][0]["error"]
    assert res["results"][1]["error"] == "PE not sent: the CE order was not placed."
    assert fake.placed == []  # the PE was never sent


def test_the_held_back_pe_says_why_the_ce_failed(connected, monkeypatch):
    b, fake = connected()
    fake.fail_next = [requests.ReadTimeout("read timed out")]
    res = asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 65))
    assert res["results"][0]["unknown"]
    assert (
        res["results"][1]["error"] == "PE not sent: the CE order's outcome is unknown - check the positions."
    )
    assert fake.placed == []

    async def part_placed(**kwargs):
        return [{"order_id": "1"}, {"error": "Insufficient funds"}]

    monkeypatch.setattr(b.gw, "place_market_order", part_placed)
    res = asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 65))
    assert res["results"][1]["error"] == "PE not sent: only part of the CE was placed - check the positions."


def test_sell_sends_both_legs_at_once_without_waiting_for_fills(connected):
    b, fake = connected()
    fake.calls.clear()
    res = asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 65))
    assert fake.calls == ["place_order", "place_order"]  # nothing in between
    assert [r["ok"] for r in res["results"]] == [True, True]


def test_a_ce_rejected_after_it_was_accepted_is_reported(connected):
    b, fake = connected()
    queue = b.subscribe()
    fake.reject = {CE: "Insufficient funds"}  # Kite's risk check, after the order ID
    asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 65))
    asyncio.run(b.refresh())
    events = []
    while not queue.empty():
        msg = queue.get_nowait()
        if msg["type"] == "order":
            events.append(msg)
    assert [(e["kind"], e["status"]) for e in events] == [("CE", "REJECTED"), ("PE", "COMPLETE")]


def test_a_session_expiring_between_the_legs_reports_the_placed_leg(connected):
    b, fake = connected()

    def expire_on_second_order(f):
        if f.placed:
            raise kex.TokenException("Token expired")

    fake.before["place_order"] = expire_on_second_order
    res = asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 65))
    assert [(r["kind"], r["ok"]) for r in res["results"]] == [("CE", True), ("PE", False)]
    assert res["results"][1]["sessionExpired"]
    assert not b.connected and "expired" in b.message


def test_exit_half_then_full(connected):
    b, fake = connected()
    asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 650))
    key = "NIFTY|2026-10-13|22300"
    asyncio.run(b.exit(key, 0.5))
    assert [(p["tradingsymbol"], p["transaction_type"], p["quantity"]) for p in fake.placed[2:]] == [
        (CE, "BUY", 325),
        (PE, "BUY", 325),
    ]
    asyncio.run(b.exit(key, 1))
    assert [p["quantity"] for p in fake.placed[4:]] == [325, 325]
    asyncio.run(b.refresh())
    [group] = b.state_json()["groups"]
    assert not group["open"]  # closed today: still listed, with its realised P&L
    with pytest.raises(KiteError, match="Nothing to exit"):
        asyncio.run(b.exit(key, 1))


def test_exit_while_an_exit_is_still_working_places_nothing(connected):
    b, fake = connected()
    asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 130))
    fake.hold_open = {CE, PE}  # buy-backs stay OPEN (e.g. outside the protection band)
    key = "NIFTY|2026-10-13|22300"
    asyncio.run(b.exit(key, 1))
    with pytest.raises(KiteError, match="Nothing to exit"):
        asyncio.run(b.exit(key, 1))
    assert len(fake.placed) == 4


def test_an_exit_kite_has_not_validated_yet_still_counts_as_being_exited(connected):
    b, fake = connected()
    asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 130))
    # Just placed, the buy-backs are still being validated, with no pending_quantity yet.
    fake.hold_open = {CE, PE}
    fake.hold_status, fake.hold_unset_pending = "VALIDATION PENDING", True
    key = "NIFTY|2026-10-13|22300"
    asyncio.run(b.exit(key, 1))
    with pytest.raises(KiteError, match="Nothing to exit"):
        asyncio.run(b.exit(key, 1))  # a second Exit Full must not turn the legs long
    assert len(fake.placed) == 4


def test_exit_waits_for_the_order_book_to_list_an_exit_just_placed(connected, monkeypatch):
    monkeypatch.setattr(broker_mod, "REREAD_DELAY", 0)
    b, fake = connected()
    asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 130))
    fake.hold_open = {CE, PE}
    key = "NIFTY|2026-10-13|22300"
    asyncio.run(b.exit(key, 1))
    fake.lag_orders = 2  # Kite's order book does not list the two buy-backs yet

    def catch_up(f):
        if f.calls.count("orders") >= 3:
            f.lag_orders = 0

    fake.before["orders"] = catch_up
    with pytest.raises(KiteError, match="Nothing to exit"):
        asyncio.run(b.exit(key, 1))
    assert len(fake.placed) == 4
    # An order book that never catches up stops the exit rather than doubling it.
    fake.before.clear()
    fake.lag_orders = 2
    with pytest.raises(KiteError, match="not caught up"):
        asyncio.run(b.exit(key, 1))
    assert len(fake.placed) == 4


def test_exit_never_counts_a_fill_twice_when_positions_lag_the_order_book(connected):
    b, fake = connected()
    asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 650))
    key = "NIFTY|2026-10-13|22300"
    asyncio.run(b.exit(key, 0.5))  # buys back 325 of each leg; both fill at once
    # Kite's positions have not caught up with those two fills yet: they still show
    # 650 short while the order book already shows the buy-backs complete.
    fake.lag_fills = 2

    def catch_up(f):
        if f.calls.count("positions") >= 4:
            f.lag_fills = 0

    fake.before["positions"] = catch_up
    asyncio.run(b.exit(key, 1))
    assert [p["quantity"] for p in fake.placed[4:]] == [325, 325]  # not 650: never flips long
    fake.before.clear()
    assert all(r["quantity"] == 0 for r in fake.positions()["net"])


def test_exit_gives_up_if_positions_never_catch_up(connected, monkeypatch):
    monkeypatch.setattr(broker_mod, "REREAD_DELAY", 0)
    b, fake = connected()
    asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 650))
    asyncio.run(b.exit("NIFTY|2026-10-13|22300", 0.5))
    fake.lag_fills = 2
    with pytest.raises(KiteError, match="not caught up"):
        asyncio.run(b.exit("NIFTY|2026-10-13|22300", 1))
    assert len(fake.placed) == 4


def test_a_fill_between_the_two_reads_only_makes_the_exit_smaller(connected):
    b, fake = connected()
    asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 130))
    fake.hold_open = {CE, PE}
    asyncio.run(b.exit("NIFTY|2026-10-13|22300", 0.5))  # 65 of each leg, held open

    def fill_now(f):  # the held buy-backs fill after the orders read, before positions
        for o in f.order_book:
            if o["status"] == "OPEN":
                f.complete(o["order_id"])

    fake.before["positions"] = fill_now
    fake.hold_open = set()
    # Positions show 65 short while the orders read just before still show 65 being
    # bought back: the exit buys nothing more, so no leg goes long.
    with pytest.raises(KiteError, match="already being exited"):
        asyncio.run(b.exit("NIFTY|2026-10-13|22300", 1))
    fake.before.clear()
    assert len(fake.placed) == 4
    assert [r["quantity"] for r in fake.positions()["net"]] == [-65, -65]  # half closed, still short


def test_exit_warns_about_a_sell_still_working(connected):
    b, fake = connected()
    asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 65))
    fake.hold_open = {CE}
    fake.placed_extra = fake.place_order(
        variety="regular", exchange="NFO", tradingsymbol=CE, transaction_type="SELL",
        quantity=65, product="NRML", order_type="MARKET", tag="pnf",
    )  # fmt: skip
    fake.hold_open = set()
    res = asyncio.run(b.exit("NIFTY|2026-10-13|22300", 1))
    assert res["warnings"] == [
        "A CE sell order for 65 is still working and can reopen this position - cancel it in Kite."
    ]


def test_order_outcomes_are_pushed_to_browsers(connected):
    b, fake = connected()
    queue = b.subscribe()
    fake.reject = {PE: "Insufficient funds. Required margin is 95417.84"}
    asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 65))
    asyncio.run(b.refresh())
    events = []
    while not queue.empty():
        msg = queue.get_nowait()
        if msg["type"] == "order":
            events.append(msg)
    assert [(e["kind"], e["status"]) for e in events] == [("CE", "COMPLETE"), ("PE", "REJECTED")]
    assert events[0]["averagePrice"] == 120.0 and events[0]["filled"] == 65
    assert events[1]["message"].startswith("Insufficient funds")
    assert not b._watched


def test_only_contracts_traded_here_are_shown(connected):
    b, fake = connected()
    # A position from elsewhere (e.g. the Kite app) on another strike.
    fake.overnight = [
        {"exchange": "NFO", "tradingsymbol": "NIFTY26O1322000CE", "product": "NRML", "buy_quantity": 0,
         "buy_value": 0.0, "sell_quantity": 65, "sell_value": 6500.0, "multiplier": 1},
    ]  # fmt: skip
    asyncio.run(b.refresh())
    assert b.state_json()["groups"] == []


def test_a_damaged_journal_entry_does_not_lose_the_others(connected, caplog):
    b, _ = connected()
    asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 65))
    data = json.loads(broker_mod.JOURNAL_FILE.read_text())
    data["NFO:BROKEN"] = {"contract": {"exchange": "NFO"}}
    broker_mod.JOURNAL_FILE.write_text(json.dumps(data))
    assert set(KiteBroker(b.gw).journal) == {f"NFO:{CE}", f"NFO:{PE}"}
    assert "Skipping unreadable entry 'NFO:BROKEN'" in caplog.text
    assert not list(broker_mod.JOURNAL_FILE.parent.glob("*.tmp"))  # saved by swapping in a temp file


def test_journal_is_not_pruned_outside_market_hours(connected, clock):
    b, fake = connected()
    asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 65))
    fake.fills, fake.order_book = [], []  # e.g. Kite lists no positions yet at 07:00
    clock["t"] = datetime(2026, 10, 13, 7, 0, tzinfo=IST)
    asyncio.run(b.refresh())
    assert len(b.journal) == 2


def test_journal_survives_restart_and_forgets_old_flat_contracts(connected, clock):
    b, fake = connected()
    asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 65))
    restarted = KiteBroker(b.gw)
    restarted.user = b.user
    assert set(restarted.journal) == {f"NFO:{CE}", f"NFO:{PE}"}

    # Next day the position is still open (NRML carried over): kept.
    clock["t"] = datetime(2026, 10, 13, 9, 20, tzinfo=IST)
    asyncio.run(restarted.refresh())
    assert len(restarted.journal) == 2
    # Closed and a day later: forgotten.
    fake.fills, fake.order_book = [], []
    asyncio.run(restarted.refresh())
    assert restarted.journal == {}
    assert json.loads(broker_mod.JOURNAL_FILE.read_text()) == {}


# ---------------- Session ----------------


def test_login_round_trip_saves_the_session(clock, files):
    fake = FakeKite()
    b = KiteBroker(KiteGateway("key", "secret", client=fake))
    url = b.login_url()
    assert url.startswith(fake.login_url()) and "redirect_params=state%3D" in url
    state = url.rsplit("state%3D", 1)[1]
    asyncio.run(b.complete_login("good-token", state))
    assert b.connected and b.user == {"id": "AB1234", "name": "Kams"}
    saved = json.loads(broker_mod.SESSION_FILE.read_text())
    assert saved["accessToken"] == "access-123"
    assert oct(os.stat(broker_mod.SESSION_FILE).st_mode & 0o777) == "0o600"
    # A state is good for one login only.
    with pytest.raises(KiteError, match="not started here"):
        asyncio.run(b.complete_login("good-token", state))

    # A restart the same day picks the session up again.
    again = KiteBroker(KiteGateway("key", "secret", client=FakeKite()))
    again._restore_session()
    assert again.connected and again.gw.access_token == "access-123"


def test_session_from_before_the_morning_reset_is_dropped(clock, files):
    broker_mod.SESSION_FILE.write_text(
        json.dumps({"accessToken": "old", "userId": "AB1234", "loginTime": "2026-10-11T20:00:00+05:30"})
    )
    b = KiteBroker(KiteGateway("key", "secret", client=FakeKite()))
    b._restore_session()
    assert not b.connected and not broker_mod.SESSION_FILE.exists()


def test_token_expiry_follows_the_6am_reset():
    def at(d, h, m=0):
        return datetime(2026, 10, d, h, m, tzinfo=IST)

    assert not token_expired(at(12, 8), at(12, 23))
    assert not token_expired(at(12, 8), at(13, 5, 59))
    assert token_expired(at(12, 8), at(13, 6))
    assert token_expired(at(12, 5), at(12, 6, 1))  # issued before that morning's reset


def test_an_expired_session_disconnects_and_says_so(connected):
    b, fake = connected()
    queue = b.subscribe()
    fake.session_valid = False
    asyncio.run(b.poll_once())
    assert not b.connected
    assert "expired" in b.message
    assert not broker_mod.SESSION_FILE.exists()
    assert queue.get_nowait()["type"] == "kite"


# ---------------- Gateway ----------------


def test_rate_limited_order_is_retried_but_a_timeout_is_not(files):
    fake = FakeKite()
    fake.access_token = "t"
    gw = KiteGateway("key", "secret", client=fake)
    args = dict(
        exchange="NFO",
        tradingsymbol=CE,
        side="SELL",
        quantity=65,
        product="NRML",
        tag="pnf",
        max_quantity=3510,
    )
    fake.fail_next = [kex.NetworkException("Too many requests", code=429)]
    assert asyncio.run(gw.place_market_order(**args)) == [{"order_id": "1001"}]
    assert fake.calls.count("place_order") == 2

    fake.calls.clear()
    fake.fail_next = [requests.Timeout("read timed out")]
    with pytest.raises(OutcomeUnknown):
        asyncio.run(gw.place_market_order(**args))
    assert fake.calls.count("place_order") == 1  # may have been placed: never resent


def test_reads_are_retried_on_server_errors(files):
    fake = FakeKite()
    fake.access_token = "t"
    fake.fail_next = [kex.NetworkException("Gateway timeout", code=504), requests.ConnectionError("reset")]
    assert asyncio.run(KiteGateway("key", "secret", client=fake).positions()) == {"net": [], "day": []}
    assert fake.calls == ["positions"] * 3


def test_orders_are_sliced_only_above_the_exchanges_maximum(connected):
    b, fake = connected()
    sensex = date(2026, 10, 15)
    # 260 SENSEX (13 lots) is one plain order: Kite refuses slicing below 1,000
    # ("Quantity should be greater than or equal to 1000 for order slicing").
    res = asyncio.run(b.sell("SENSEX", sensex, 72300, 260))
    assert [r["ok"] for r in res["results"]] == [True, True]
    assert fake.calls[-2:] == ["place_order", "place_order"]
    asyncio.run(b.sell("SENSEX", sensex, 72300, 960))  # the size cap: still one order
    assert fake.calls[-2:] == ["place_order", "place_order"]
    # Exactly the exchange's maximum is still one order.
    args = dict(exchange="BFO", side="SELL", product="NRML", tag="pnf", max_quantity=1000)
    asyncio.run(b.gw.place_market_order(tradingsymbol="SENSEX26O1572300CE", quantity=1000, **args))
    assert fake.calls[-1] == "place_order"
    fake.fills.pop(), fake.order_book.pop(), fake.placed.pop()  # not part of the position below
    # Above it (an exit of 1,220 here), Kite slices it into orders of at most 1,000.
    asyncio.run(b.exit("SENSEX|2026-10-15|72300", 1))
    assert fake.calls[-2:] == ["place_autoslice_order", "place_autoslice_order"]
    assert [p["quantity"] for p in fake.placed[-4:]] == [1000, 220, 1000, 220]
    asyncio.run(b.refresh())
    assert all(r["quantity"] == 0 for r in fake.positions()["net"])


def test_banknifty_sells_its_monthly_straddle_and_slices_above_its_maximum(connected):
    b, fake = connected()
    bank = date(2026, 10, 27)
    res = asyncio.run(b.sell("BANKNIFTY", bank, 56000, 1440))  # 48 lots: one order per leg
    assert [r["ok"] for r in res["results"]] == [True, True]
    assert fake.calls[-2:] == ["place_order", "place_order"]
    assert [p["tradingsymbol"] for p in fake.placed[-2:]] == [
        "BANKNIFTY26OCT56000CE",
        "BANKNIFTY26OCT56000PE",
    ]
    asyncio.run(b.sell("BANKNIFTY", bank, 56000, 30))
    asyncio.run(b.exit("BANKNIFTY|2026-10-27|56000", 1))  # 1,470 is above it: sliced
    assert fake.calls[-2:] == ["place_autoslice_order", "place_autoslice_order"]
    assert [p["quantity"] for p in fake.placed[-4:]] == [1440, 30, 1440, 30]


def test_the_maximum_order_quantities_are_the_exchanges():
    from pnf.series import UNDERLYINGS

    assert UNDERLYINGS["NIFTY"].max_order_qty == 3510
    assert UNDERLYINGS["BANKNIFTY"].max_order_qty == 1440  # 48 lots of 30
    assert UNDERLYINGS["SENSEX"].max_order_qty == 1000


def test_autoslice_reply_with_a_failed_slice():
    reply = [{"order_id": "1"}, {"order_id": 2}, {"error": {"code": 400, "message": "Insufficient funds"}}]
    assert parse_slices(reply) == [{"order_id": "1"}, {"order_id": "2"}, {"error": "Insufficient funds"}]
    assert parse_slices({"order_id": "9"}) == [{"order_id": "9"}]
    assert parse_slices("250101000012345") == [{"order_id": "250101000012345"}]  # place_order's reply
    # The shape the SDK's docstring describes: the children are the slices.
    children = {"order_id": "P", "children": [{"order_id": "3"}, {"error": {"message": "Margin"}}]}
    assert parse_slices(children) == [{"order_id": "3"}, {"error": "Margin"}]


# ---------------- Endpoints ----------------


@pytest.fixture
def client(connected):
    b, fake = connected()
    app = FastAPI()
    kite.web.install(app)
    app.state.broker = b
    return TestClient(app, base_url="https://127.0.0.1:8765"), b, fake


def test_requests_from_other_sites_are_refused(client):
    http, _, fake = client
    body = {"underlying": "NIFTY", "expiry": "2026-10-13", "strike": 22300, "quantity": 65}
    assert (
        http.post("/api/kite/sell", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    )
    # DNS rebinding: a foreign host name that resolves to this machine.
    rebound = {"Host": "evil.example:8765", "Origin": "http://evil.example:8765"}
    assert http.post("/api/kite/sell", json=body, headers=rebound).status_code == 403
    assert fake.placed == []
    ok = http.post("/api/kite/sell", json=body, headers={"Origin": "https://127.0.0.1:8765"})
    assert ok.status_code == 200 and len(fake.placed) == 2


def test_sell_errors_are_reported_as_400(client):
    http, _, _ = client
    body = {"underlying": "NIFTY", "expiry": "2026-10-13", "strike": 22300, "quantity": 64}
    res = http.post("/api/kite/sell", json=body)
    assert res.status_code == 400 and "multiple of the lot size" in res.json()["detail"]


def test_login_redirect_and_callback(client):
    http, b, _ = client
    res = http.get("/kite/login", follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"].startswith(
        "https://kite.zerodha.com/connect/login"
    )
    state = res.headers["location"].rsplit("state%3D", 1)[1]
    res = http.get(f"/kite/callback?request_token=bad&status=success&state={state}", follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/"
    assert b.message.startswith("Kite login failed: Token is invalid")
    b.message = ""
    http.get("/kite/callback?status=cancelled", follow_redirects=False)
    assert b.message == ""  # no login pending: another site loading /callback is not reported
    state = http.get("/kite/login", follow_redirects=False).headers["location"].rsplit("state%3D", 1)[1]
    http.get("/kite/callback?status=cancelled", follow_redirects=False)  # another site, mid-login
    assert b.message == "" and state in b._login_states  # the login in progress goes on
    http.get(f"/kite/callback?status=cancelled&state={state}", follow_redirects=False)
    assert "cancelled" in b.message
    for _ in range(100):  # a page hitting /kite/login cannot grow the pending logins
        http.get("/kite/login", follow_redirects=False)
    assert len(b._login_states) == broker_mod.LOGIN_STATES_KEPT

    # The redirect URL registered with Kite: /callback.
    state = http.get("/kite/login", follow_redirects=False).headers["location"].rsplit("state%3D", 1)[1]
    res = http.get(
        f"/callback?request_token=good-token&action=login&status=success&state={state}",
        follow_redirects=False,
    )
    assert res.status_code == 303 and res.headers["location"] == "/"
    assert b.connected and b.message == ""


def test_websocket_sends_the_state_and_refuses_other_sites(client):
    http, _, _ = client
    local = {"Host": "127.0.0.1:8765"}
    with http.websocket_connect("/kite/ws", headers=local) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "kite" and msg["connected"] and msg["lotSizes"]["NIFTY|2026-10-13"] == 65
    with pytest.raises(Exception):  # noqa: B017 - closed with a policy violation before accepting
        with http.websocket_connect("/kite/ws", headers={**local, "Origin": "https://evil.example"}) as ws:
            ws.receive_json()


def test_unconfigured_broker_explains_how_to_set_up(clock, files):
    b = KiteBroker(None)
    state = b.state_json()
    assert not state["configured"] and not state["connected"]
    with pytest.raises(KiteError, match="KITE_API_KEY"):
        asyncio.run(b.sell("NIFTY", NIFTY_EXPIRY, 22300, 65))


def test_settings_come_from_env(monkeypatch, caplog):
    for name in ("KITE_PRODUCT", "KITE_MAX_LOTS", "KITE_ORDER_RELAY", "KITE_RELAY_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KITE_API_KEY", "")  # as in .env until the keys are filled in
    monkeypatch.setenv("KITE_API_SECRET", "")
    assert not kite.web.make_broker().configured

    monkeypatch.setenv("KITE_API_KEY", "key")
    monkeypatch.setenv("KITE_API_SECRET", "secret")
    b = kite.web.make_broker()
    assert b.configured and b.product == "NRML"
    monkeypatch.setenv("KITE_PRODUCT", "mis")
    assert kite.web.make_broker().product == "MIS"
    # The old lot limit is gone (sells are capped at twice the algo's default): said, not fatal.
    monkeypatch.setenv("KITE_MAX_LOTS", "5")
    assert kite.web.make_broker().configured
    assert "KITE_MAX_LOTS in .env is no longer used" in caplog.text
    assert "NIFTY 3,380, BANKNIFTY 1,440, SENSEX 960" in caplog.text
    monkeypatch.setenv("KITE_PRODUCT", "CNC")
    with pytest.raises(RuntimeError, match="NRML or MIS"):
        kite.web.make_broker()
    monkeypatch.setenv("KITE_PRODUCT", "NRML")

    # Orders through the relay: only the order client is rerouted, and only with a sound setup.
    monkeypatch.setenv("KITE_ORDER_RELAY", "https://your-domain.example/kite-route.php")
    monkeypatch.setenv("KITE_RELAY_SECRET", "s" * 64)
    gw = kite.web.make_broker().gw
    assert isinstance(gw.order_client.reqsession.auth, RelayAuth)
    assert gw.order_client.reqsession.auth.url == "https://your-domain.example/kite-route.php"
    assert gw.client.reqsession.auth is None and gw.order_client is not gw.client
    monkeypatch.setenv("KITE_ORDER_RELAY", "http://your-domain.example/kite-route.php")
    with pytest.raises(RuntimeError, match="https"):
        kite.web.make_broker()


def test_a_failed_contract_load_is_not_retried_on_every_poll(clock, files):
    fake = FakeKite()
    fake.access_token = "t"
    b = KiteBroker(KiteGateway("key", "secret", client=fake))
    b.user = {"id": "AB1234", "name": ""}
    fake.fail_next = [kex.InputException("bad exchange", code=400)]
    asyncio.run(b.refresh())
    assert b.state_json()["message"].startswith("Could not load Kite contracts")
    asyncio.run(b.refresh())
    assert fake.calls.count("instruments") == 1 and b.book is None


def test_contracts_are_loaded_again_once_kites_morning_dump_is_out(clock, files):
    fake = FakeKite()
    fake.access_token = "t"
    b = KiteBroker(KiteGateway("key", "secret", client=fake))
    b.user = {"id": "AB1234", "name": ""}
    clock["t"] = datetime(2026, 10, 12, 0, 5, tzinfo=IST)  # just after midnight, yesterday's token
    asyncio.run(b.refresh())
    loads = fake.calls.count("instruments")
    clock["t"] = datetime(2026, 10, 12, 8, 0, tzinfo=IST)
    asyncio.run(b.refresh())
    assert fake.calls.count("instruments") == loads
    clock["t"] = datetime(2026, 10, 12, 8, 31, tzinfo=IST)
    asyncio.run(b.refresh())
    assert fake.calls.count("instruments") == 2 * loads
    clock["t"] = datetime(2026, 10, 12, 14, 0, tzinfo=IST)
    asyncio.run(b.refresh())
    assert fake.calls.count("instruments") == 2 * loads
