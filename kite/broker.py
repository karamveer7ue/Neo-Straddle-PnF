"""Order placement and position tracking through Zerodha Kite.

Owns the Kite session, the contract book, the journal of contracts traded
from this app, and the browser connections it pushes position updates to.

- login: Kite's redirect flow. /kite/login sends the browser to Kite, which
  returns to /kite/callback with a request token; that is exchanged for an
  access token, saved to SESSION_FILE so a restart the same day stays logged
  in (Kite expires tokens at about 06:00 the next morning).
- sell: a straddle is sold as two MARKET orders, CE then PE. The PE is sent
  only once the CE order was accepted in full, so a refusal never leaves a
  single naked leg from this app.
- exit: half or all of each open leg is bought back, sized from fresh
  positions and orders (see book.plan_exit).
- tracking: positions and orders are polled; only contracts in the journal
  (those ordered from here) are shown. Orders just placed are watched until
  they complete or are rejected, and each outcome is pushed to the browsers.
"""

import asyncio
import json
import logging
import os
import secrets
import time as clock
from datetime import date, datetime, time, timedelta
from pathlib import Path

from pnf.series import IST, UNDERLYINGS, now_ist

from .book import (
    SELL,
    OrderRequest,
    contract_from_json,
    contract_to_json,
    group_json,
    is_working,
    plan_exit,
    position_rows,
    positions_behind,
    straddles,
    working_entries,
)
from .contracts import ContractBook, kite_exchange
from .gateway import KiteError, KiteGateway, OutcomeUnknown, SessionExpired

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE = ROOT / ".kite-session.json"
JOURNAL_FILE = ROOT / ".kite-journal.json"

ORDER_TAG = "pnf"  # shown on the orders in Kite, so they can be told apart there
DEFAULT_PRODUCT = "NRML"
DEFAULT_MAX_LOTS = 20
POLL_MARKET = 2.0  # seconds between position refreshes in market hours
POLL_IDLE = 60.0  # ... and outside them
POLL_WATCHING = 0.5  # while an order just placed has not completed
WATCH_TIMEOUT = 30.0  # stop chasing an order's outcome after this long
REREAD_DELAY = 0.2  # seconds before re-reading positions that lag the order book
CONSISTENT_READS = 4  # attempts at positions that include every fill, before an exit gives up
BOOK_RETRY_DELAY = 60.0  # seconds before retrying a failed contract load
LOGIN_STATE_TTL = 15 * 60  # seconds a /kite/login round trip may take
TOKEN_RESET = time(6, 0)  # Kite expires access tokens around 06:00 IST
MARKET_HOURS = (time(9, 0), time(15, 45))
CLIENT_QUEUE_LIMIT = 500


def token_expired(login_time: datetime, now: datetime) -> bool:
    """Kite tokens last until about 06:00 IST after the day they were issued."""
    reset = datetime.combine(login_time.date(), TOKEN_RESET, IST)
    if login_time >= reset:
        reset += timedelta(days=1)
    return now >= reset


def in_market_hours(now: datetime) -> bool:
    return now.weekday() < 5 and MARKET_HOURS[0] <= now.time() <= MARKET_HOURS[1]


class KiteBroker:
    def __init__(
        self, gateway: KiteGateway | None, product: str = DEFAULT_PRODUCT, max_lots: int = DEFAULT_MAX_LOTS
    ):
        self.gw = gateway  # None: Kite is not set up in .env
        self.product = product
        self.max_lots = max_lots
        self.clients: set[asyncio.Queue] = set()

        self.user: dict | None = None  # {"id", "name"} while logged in
        self.message = ""  # why trading is unavailable (e.g. the session expired)
        self.book: ContractBook | None = None
        self.book_error = ""  # the last failure to load the contract book
        self._book_retry_at = 0.0  # monotonic time before which a failed load is not retried
        self.journal: dict[str, dict] = self._load_journal()  # contract key -> {"contract", "lastTraded"}
        self.net: list[dict] = []
        self.orders: list[dict] = []
        self.updated_at: float | None = None  # epoch seconds of the last positions refresh

        self._login_states: dict[str, float] = {}
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._watched: dict[str, dict] = {}  # order id -> {"since", "kind", "group"} until it finishes
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ---------------- Lifecycle ----------------

    @property
    def configured(self) -> bool:
        return self.gw is not None

    @property
    def connected(self) -> bool:
        return self.user is not None

    def start(self) -> None:
        if self.configured:
            self._restore_session()
            self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def _restore_session(self) -> None:
        try:
            saved = json.loads(SESSION_FILE.read_text())
            login_time = datetime.fromisoformat(saved["loginTime"])
            token, user = saved["accessToken"], {"id": saved["userId"], "name": saved.get("userName", "")}
        except (OSError, ValueError, KeyError, TypeError):
            return  # never logged in, or unreadable: log in from the page
        if token_expired(login_time, now_ist()):
            self._forget_session()
            return
        self.gw.set_access_token(token)
        self.user = user  # confirmed (or dropped) by the first refresh

    def _save_session(self, session: dict) -> None:
        data = {
            "accessToken": session["access_token"],
            "userId": session.get("user_id", ""),
            "userName": session.get("user_shortname") or session.get("user_name") or "",
            "loginTime": now_ist().isoformat(),
        }
        try:
            # Owner-only from the first byte: the token can place orders.
            fd = os.open(SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)  # an existing file keeps its mode through O_CREAT
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(data))
        except OSError as exc:
            log.warning("Could not save %s: %s", SESSION_FILE, exc)

    def _forget_session(self) -> None:
        try:
            SESSION_FILE.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not remove %s: %s", SESSION_FILE, exc)

    def _session_expired(self, reason: str) -> None:
        log.warning("Kite session ended: %s", reason)
        self.user = None
        self.gw.set_access_token(None)
        self._forget_session()
        self.net, self.orders, self._watched = [], [], {}
        self.message = "Kite session expired - connect Kite again to trade."
        self._publish()

    # ---------------- Login ----------------

    def login_url(self) -> str:
        now = clock.monotonic()
        self._login_states = {s: t for s, t in self._login_states.items() if now - t < LOGIN_STATE_TTL}
        state = secrets.token_urlsafe(16)
        self._login_states[state] = now
        return self.gw.login_url(state)

    async def complete_login(self, request_token: str, state: str) -> None:
        """Finish the redirect flow. Raises KiteError with a message for the page."""
        started = self._login_states.pop(state, None)
        if started is None or clock.monotonic() - started > LOGIN_STATE_TTL:
            raise KiteError("This Kite login was not started here, or took too long - connect again.")
        session = await self.gw.generate_session(request_token)
        self._save_session(session)
        self.user = {
            "id": session.get("user_id", ""),
            "name": session.get("user_shortname") or session.get("user_name") or "",
        }
        self.message = ""
        log.info("Kite connected as %s", self.user["id"])
        self._publish()
        self._wake.set()  # load contracts and positions now

    def report(self, message: str) -> None:
        """Show `message` on every open page (e.g. why a login failed)."""
        self.message = message[:400]
        self._publish()

    async def logout(self) -> None:
        if not self.connected:
            return
        try:
            await self.gw.invalidate()
        except KiteError as exc:  # best effort: the token is dropped here either way
            log.warning("Kite logout failed: %s", exc)
        self.user = None
        self.gw.set_access_token(None)
        self._forget_session()
        self.net, self.orders, self._watched = [], [], {}
        self.message = ""
        self._publish()

    # ---------------- Browser push ----------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE_LIMIT)
        queue.put_nowait(self.state_json())
        self.clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.clients.discard(queue)

    def _push(self, payload: dict) -> None:
        for queue in list(self.clients):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:  # a stalled browser: resync it with the full state
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(self.state_json())

    def _publish(self) -> None:
        self._push(self.state_json())

    def state_json(self) -> dict:
        return {
            "type": "kite",
            "configured": self.configured,
            "connected": self.connected,
            "user": self.user,
            "message": self.message or self.book_error,
            "product": self.product,
            "maxLots": self.max_lots,
            "lotSizes": self.book.lot_sizes() if self.book else {},
            "groups": self.groups_json(),
            "updatedAt": self.updated_at,
        }

    def groups_json(self) -> list[dict]:
        """Straddles traded from here that Kite still lists (open, or closed today)."""
        if not self.connected:
            return []
        tracked = [contract_from_json(e["contract"]) for e in self.journal.values()]
        groups = []
        for contracts in straddles(tracked).values():
            g = group_json(contracts, self.net, self.orders)
            if g["legs"] or g["pending"]:
                groups.append(g)
        # Open positions first, then by underlying, expiry and strike.
        groups.sort(key=lambda g: (not g["open"], g["underlying"], g["expiry"], g["strike"]))
        return groups

    # ---------------- Journal ----------------

    def _load_journal(self) -> dict[str, dict]:
        """The saved journal; an unreadable entry is skipped, never the whole file."""
        try:
            data = json.loads(JOURNAL_FILE.read_text())
        except FileNotFoundError:
            return {}  # nothing traded yet
        except (OSError, ValueError) as exc:
            log.error("Could not read %s (%s): positions from earlier days are not shown", JOURNAL_FILE, exc)
            return {}
        journal = {}
        for key, entry in data.items() if isinstance(data, dict) else []:
            try:
                contract_from_json(entry["contract"])
                date.fromisoformat(entry["lastTraded"])
                journal[key] = entry
            except (KeyError, TypeError, ValueError) as exc:
                log.error("Skipping unreadable entry %r in %s: %s", key, JOURNAL_FILE, exc)
        return journal

    def _save_journal(self) -> None:
        # Write a temporary file and swap it in: a crash mid-write must not lose the
        # contracts whose positions the page shows (and exits).
        tmp = JOURNAL_FILE.with_name(JOURNAL_FILE.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.journal, indent=1, sort_keys=True))
            os.replace(tmp, JOURNAL_FILE)
        except OSError as exc:
            log.warning("Could not save %s: %s", JOURNAL_FILE, exc)

    def _track(self, contracts) -> None:
        today = now_ist().date().isoformat()
        for c in contracts:
            self.journal[c.key] = {"contract": contract_to_json(c), "lastTraded": today}
        self._save_journal()

    def _prune_journal(self) -> None:
        """Forget contracts that expired, or that have neither a position nor a working
        order and were last traded before today.

        The second judgement trusts Kite's positions to be complete, so it is made
        only in market hours (early in the morning Kite may list none yet).
        """
        now = now_ist()
        today = now.date()
        keep = {}
        for key, entry in self.journal.items():
            c = contract_from_json(entry["contract"])
            if c.expiry < today:
                continue
            if not in_market_hours(now):
                keep[key] = entry
                continue
            listed = position_rows(self.net, c) or any(
                is_working(o)
                and o.get("exchange") == c.exchange
                and o.get("tradingsymbol") == c.tradingsymbol
                for o in self.orders
            )
            if listed or date.fromisoformat(entry["lastTraded"]) >= today:
                keep[key] = entry
        if keep != self.journal:
            self.journal = keep
            self._save_journal()

    # ---------------- Refresh ----------------

    async def poll_once(self) -> None:
        """One refresh, if logged in; an expired session logs out."""
        if not self.connected:
            return
        try:
            await self.refresh()
        except SessionExpired as exc:
            self._session_expired(str(exc))
        except KiteError as exc:
            log.warning("Kite refresh failed: %s", exc)

    async def _poll_loop(self) -> None:
        while True:
            self._wake.clear()  # before polling: a wake-up asked for meanwhile must not be lost
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # the poller must never die
                log.exception("Kite refresh error")
            if self._watched:
                interval = POLL_WATCHING
            elif in_market_hours(now_ist()):
                interval = POLL_MARKET
            else:
                interval = POLL_IDLE
            try:
                await asyncio.wait_for(self._wake.wait(), interval)
            except TimeoutError:
                pass

    async def _ensure_book(self) -> ContractBook:
        today = now_ist().date()
        if self.book is None or self.book.loaded_on != today:
            rows = {}
            for exchange in sorted({kite_exchange(u) for u in UNDERLYINGS.values()}):
                rows[exchange] = await self.gw.instruments(exchange)
            self.book = ContractBook(rows, today)
            log.info("Loaded %d Kite index-option contracts", len(self.book))
        return self.book

    async def refresh(self) -> None:
        """Re-read contracts (once a day), positions and orders, then push them."""
        if clock.monotonic() >= self._book_retry_at:
            try:
                await self._ensure_book()
                self.book_error = ""
            except SessionExpired:
                raise
            except KiteError as exc:  # the dump is large: do not fetch it on every poll
                self.book_error = f"Could not load Kite contracts: {exc}"
                self._book_retry_at = clock.monotonic() + BOOK_RETRY_DELAY
        positions = await self.gw.positions()
        orders = await self.gw.orders()
        self.net = positions.get("net", []) if isinstance(positions, dict) else []
        self.orders = orders if isinstance(orders, list) else []
        self.updated_at = clock.time()
        self._prune_journal()
        self._report_outcomes()
        self._publish()

    def _report_outcomes(self) -> None:
        """Push the outcome of each watched order that has finished (or gone quiet)."""
        by_id = {str(o.get("order_id")): o for o in self.orders}
        now = clock.monotonic()
        for order_id, w in list(self._watched.items()):
            o = by_id.get(order_id)
            if o is not None and not is_working(o):
                self._watched.pop(order_id)
                self._push(
                    {
                        "type": "order",
                        "orderId": order_id,
                        "group": w["group"],
                        "kind": w["kind"],
                        "side": o.get("transaction_type"),
                        "tradingsymbol": o.get("tradingsymbol"),
                        "status": o.get("status"),
                        "quantity": int(o.get("quantity") or 0),
                        "filled": int(o.get("filled_quantity") or 0),
                        "averagePrice": float(o.get("average_price") or 0),
                        "message": o.get("status_message") or "",
                    }
                )
            elif now - w["since"] > WATCH_TIMEOUT:
                self._watched.pop(order_id)  # still open (e.g. outside the protection band): shown as pending

    # ---------------- Orders ----------------

    def _require_session(self) -> None:
        if not self.configured:
            raise KiteError("Kite is not set up: add KITE_API_KEY and KITE_API_SECRET to .env.")
        if not self.connected:
            raise KiteError("Connect Kite first.")

    def _require_book(self) -> ContractBook:
        self._require_session()
        if self.book is None:
            raise KiteError(self.book_error or "Kite contracts are still loading - try again in a moment.")
        return self.book

    def _lock(self, key: str) -> asyncio.Lock:
        return self._group_locks.setdefault(key, asyncio.Lock())

    async def _place(self, req: OrderRequest, group: str) -> dict:
        """Place one leg's order; returns a result line for the browser (never raises)."""
        c = req.contract
        result = {
            "kind": c.kind,
            "side": req.side,
            "quantity": req.quantity,
            "tradingsymbol": c.tradingsymbol,
        }
        unknown = " - check the positions before retrying"
        try:
            slices = await self.gw.place_market_order(
                exchange=c.exchange,
                tradingsymbol=c.tradingsymbol,
                side=req.side,
                quantity=req.quantity,
                product=req.product,
                tag=ORDER_TAG,
                max_quantity=UNDERLYINGS[c.underlying].max_order_qty,
            )
        except SessionExpired as exc:  # nothing was placed for this leg; the caller stops here
            return {**result, "ok": False, "sessionExpired": True, "error": f"Kite session expired: {exc}"}
        except OutcomeUnknown as exc:
            log.error("Order outcome unknown for %s %s %d: %s", req.side, c.tradingsymbol, req.quantity, exc)
            return {**result, "ok": False, "unknown": True, "error": f"{exc}{unknown}"}
        except KiteError as exc:
            return {**result, "ok": False, "error": str(exc)}
        except Exception as exc:  # e.g. an SDK bug: the request may have gone out
            log.exception("Order for %s failed", c.tradingsymbol)
            return {**result, "ok": False, "unknown": True, "error": f"{exc}{unknown}"}
        order_ids = [s["order_id"] for s in slices if "order_id" in s]
        errors = [s["error"] for s in slices if "error" in s]
        for order_id in order_ids:
            self._watched[order_id] = {"since": clock.monotonic(), "kind": c.kind, "group": group}
        log.info(
            "%s %s %d (%s): orders %s %s",
            req.side,
            c.tradingsymbol,
            req.quantity,
            req.product,
            order_ids,
            errors,
        )
        return {**result, "ok": not errors, "orderIds": order_ids, "error": "; ".join(errors)}

    async def sell(self, underlying: str, expiry: date, strike: int, quantity: int) -> dict:
        """Sell the straddle at once: `quantity` of the CE, then straight away the PE."""
        book = self._require_book()
        pair = book.straddle(underlying, expiry, strike)
        if not pair:
            raise KiteError(f"Kite lists no {underlying} {strike} CE and PE for {expiry:%d %b %Y}.")
        lot = pair[0].lot_size
        if quantity <= 0 or quantity % lot:
            raise KiteError(f"Quantity must be a multiple of the lot size, {lot}.")
        if quantity > self.max_lots * lot:
            raise KiteError(
                f"{quantity} is above the {self.max_lots}-lot limit ({self.max_lots * lot}); "
                "raise KITE_MAX_LOTS in .env to allow more."
            )
        key = f"{underlying}|{expiry.isoformat()}|{strike}"
        ce, pe = pair
        async with self._lock(key):
            self._track(pair)  # before placing: an order with an unknown outcome is still tracked
            first = await self._place(OrderRequest(ce, SELL, quantity, self.product), key)
            results = [first]
            # The PE follows without waiting for the CE to fill (entries are instant), but
            # not after Kite refused the CE outright: that would leave a naked PE. A CE
            # rejected later by Kite's risk checks is reported when its outcome arrives.
            if not first["ok"]:
                results.append(
                    {"kind": "PE", "side": SELL, "quantity": quantity, "tradingsymbol": pe.tradingsymbol,
                     "ok": False, "held": True, "error": "PE not sent: the CE order was not placed."}
                )  # fmt: skip
            else:
                results.append(await self._place(OrderRequest(pe, SELL, quantity, self.product), key))
        self._after_orders(results)
        return {"group": key, "results": results, "warnings": []}

    async def exit(self, group: str, fraction: float) -> dict:
        """Buy back half (0.5) or all (1) of each open leg of a straddle traded here."""
        self._require_session()
        tracked = [contract_from_json(e["contract"]) for e in self.journal.values()]
        contracts = straddles(tracked).get(group)
        if not contracts:
            raise KiteError("No position from this app on that straddle.")
        async with self._lock(group):
            net, orders = await self._consistent_book(contracts)
            self.net, self.orders = net, orders
            plan = plan_exit(contracts, net, orders, fraction)
            if not plan:
                raise KiteError(
                    "Nothing to exit: no leg has 2 or more lots left that are not already being exited."
                    if fraction < 1
                    else "Nothing to exit: the position is closed, or already being exited."
                )
            warnings = [
                f"A {w['kind']} {w['side'].lower()} order for {w['quantity']} is still working "
                "and can reopen this position - cancel it in Kite."
                for w in working_entries(contracts, net, orders)
            ]
            results = []
            for req in plan:
                results.append(await self._place(req, group))
                if results[-1].get("sessionExpired"):
                    break
        self._after_orders(results)
        return {"group": group, "results": results, "warnings": warnings}

    async def _consistent_book(self, contracts) -> tuple[list[dict], list[dict]]:
        """Kite's net positions and orders, read so that an exit is never oversized.

        Orders are read first: a fill landing between the two reads then shows in
        the positions while its order still counts as working, which can only make
        the exit smaller. Positions that miss fills the order book already shows
        are read again.
        """
        for _ in range(CONSISTENT_READS):
            orders = await self._fresh(lambda: self.gw.orders(urgent=True))
            positions = await self._fresh(lambda: self.gw.positions(urgent=True))
            orders = orders if isinstance(orders, list) else []
            net = positions.get("net", []) if isinstance(positions, dict) else []
            if not positions_behind(contracts, net, orders):
                return net, orders
            log.info("Kite positions are behind the order book; reading again")
            await asyncio.sleep(REREAD_DELAY)
        raise KiteError("Kite's positions have not caught up with the latest fills - try again in a moment.")

    def _after_orders(self, results: list[dict]) -> None:
        if any(r.get("sessionExpired") for r in results):
            self._session_expired("expired while placing orders")
        self._wake.set()

    async def _fresh(self, fetch):
        try:
            return await fetch()
        except SessionExpired as exc:
            self._session_expired(str(exc))
            raise
