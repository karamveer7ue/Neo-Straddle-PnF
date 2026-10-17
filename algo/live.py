"""Carries the algo's live trades out through Kite (kite/broker.py).

The core (core.py) decides what each trade should hold (its `want`); this module
makes Kite's positions match, and decides nothing itself:

- a new trade: sell the CE, then at once the PE (as MIS, tagged with the trade's
  own tag); a leg refused, rejected or left unfilled fails the entry, and the
  core then asks for whatever was sold to be bought back;
- an exit: buy back what the trade holds above its `want`. Every buy-back is
  sized again from a fresh read of Kite's positions and order book and can never
  exceed what Kite shows short (KiteBroker.algo_buy_back), so a leg cannot turn
  long however often an exit is retried. An exit Kite refuses or rejects is tried
  again after a pause that doubles each time in a row;
- before anything is sent, each leg gets a placeholder order, saved with the
  trade; it becomes the order Kite lists. An order cut short by a crash or a
  restart, or whose reply was lost, is thus found later by its tag;
- following orders: after every Kite refresh, every order in Kite's order book
  with a trade's tag is that trade's. Its progress is copied in, and one the
  trade does not know yet (placed while a reply was on its way, before a
  restart, or listed late) is taken in - which reopens a closed trade it leaves
  short. An order with a lost reply that Kite's order book, read 15 seconds on,
  does not list counts as never placed. An order Kite leaves unfilled is
  cancelled (an entry leg) or cancelled and sent again (an exit);
- Kite showing less short than a trade holds, on two refreshes in a row and
  explained by filled buy-backs that are not the algo's (an Exit button, the
  Kite app, Zerodha's square-off), means it was bought back outside the algo:
  the core is told, and manages only what is left. A gap nothing explains is
  only reported (`mismatch`), as are positions from the algo's own orders that
  no open trade accounts for (`orphans`).
"""

import asyncio
import logging
import re
import time
from datetime import date, datetime

from kite.book import positions_behind
from kite.broker import ALGO_PRODUCT
from kite.gateway import KiteError
from pnf.series import IST

from .core import BUY, ENTERING, EXTERNAL_ID, FAILED, KINDS, LIVE, SELL, Order, Trade

log = logging.getLogger(__name__)

TAG_PREFIX = "pnfalgo"  # Kite's order tag: alphanumeric, at most 20 characters
TAG_RE = re.compile(rf"{TAG_PREFIX}(\d+)")
PENDING = "pending-"  # the id of a placeholder order: not sent yet, or its reply was lost
ENTRY_TIMEOUT = 10.0  # seconds an entry leg may stay unfilled before it is cancelled (the entry fails)
EXIT_TIMEOUT = 10.0  # seconds an exit order may stay unfilled before it is cancelled and sent again
UNKNOWN_SETTLE = 15.0  # seconds until an order with a lost reply that Kite does not list counts as lost
RETRY_DELAY = 5.0  # seconds before trying again an exit Kite refused or rejected ...
RETRY_MAX = 60.0  # ... doubling each time in a row, up to this
CANCEL_AGAIN = 10.0  # seconds before cancelling again an order Kite still shows working
CLOCK_SLACK = 5.0  # seconds Kite's order timestamps may run behind this machine's clock


def order_tag(t: Trade) -> str:
    """The tag on every Kite order of a trade (unique within the day, as Kite's order book is)."""
    return f"{TAG_PREFIX}{t.seq:03d}"


def algo_tag(order: dict) -> str | None:
    """The algo's tag on a Kite order ("tag", or among "tags"); None on anyone else's."""
    for tag in (order.get("tag"), *(order.get("tags") or ())):
        if tag and TAG_RE.fullmatch(str(tag)):
            return str(tag)
    return None


def _side(order: dict) -> str:
    return str(order.get("transaction_type") or "").upper()


def _place_of(order: dict) -> tuple:
    return order.get("exchange"), order.get("tradingsymbol")


def _placed_when(order: dict) -> float | None:
    """When Kite took an order (epoch seconds; the SDK hands a naive IST datetime, the raw
    API a "YYYY-MM-DD HH:MM:SS" string); None when not known."""
    when = order.get("order_timestamp")
    if isinstance(when, str):
        try:
            when = datetime.strptime(when[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if not isinstance(when, datetime):
        return None
    return (when if when.tzinfo else when.replace(tzinfo=IST)).timestamp()


def _copy(o: Order, k: dict) -> bool:
    """Kite's view of an order into ours; True when anything changed."""
    update = {
        "qty": int(k.get("quantity") or 0) or o.qty,
        "filled": int(k.get("filled_quantity") or 0),
        "avg": float(k.get("average_price") or 0),
        "status": str(k.get("status") or o.status).upper(),
        "message": k.get("status_message") or "",
    }
    if all(getattr(o, f) == v for f, v in update.items()):
        return False
    for f, v in update.items():
        setattr(o, f, v)
    return True


class LiveExecutor:
    def __init__(self, runner):
        self.runner = runner
        self.broker = runner.broker
        self.problems: dict[str, str] = {}  # trade id -> why its exit is not going through
        self.mismatch: dict[str, str] = {}  # trade id -> a gap with Kite's positions nothing explains
        self.orphans: list[tuple] = []  # (kind, exchange, symbol, units): see _unaccounted
        self._suspects: list[tuple] = []  # ... as seen on the refresh before
        self._next_try: dict[str, float] = {}  # trade id -> monotonic time of the next exit attempt
        self._refusals: dict[str, int] = {}  # trade id -> exits refused or rejected in a row
        self._cancels: dict[str, float] = {}  # order id -> monotonic time a cancel was sent
        self._placing: dict[str, list] = {}  # trade id -> what is being sent: exit decision, or None (entry)
        self._gaps: dict[str, dict[str, int]] = {}  # trade id -> {kind: units Kite shows less short}
        self._lock = asyncio.Lock()
        self._again = False
        self._strays: set[str] = set()  # order ids already warned about
        self._prepared: dict[str, list] = {}  # trade id -> its entry's mark, set by prepare()

    @property
    def core(self):
        return self.runner.core

    def _contracts(self, t: Trade):
        pair = self.broker.straddle_of(t.underlying, date.fromisoformat(t.expiry), t.strike)
        return dict(zip(KINDS, pair, strict=True)) if pair else None

    def _todays(self) -> list[Trade]:
        """Today's live trades, open or not (an order Kite lists late can reopen one)."""
        day = self.core.day.isoformat() if self.core.day else ""
        return [t for t in self.core.trades if t.mode == LIVE and t.day == day]

    def _live_trades(self) -> list[Trade]:
        return [t for t in self._todays() if t.active]

    def unknown(self, t: Trade) -> list[Order]:
        """Orders of `t` whose placement is over but whose outcome is not known yet."""
        if self._placing.get(t.id):
            return []
        return [o for o in t.orders if o.status == "UNKNOWN"]

    def _begin(self, t: Trade, decision: int | None) -> list:
        mark = [decision]
        self._placing.setdefault(t.id, []).append(mark)
        return mark

    def _end(self, t: Trade, mark: list) -> None:
        marks = [m for m in self._placing.get(t.id, []) if m is not mark]
        if marks:
            self._placing[t.id] = marks
        else:
            self._placing.pop(t.id, None)

    def retry_now(self, t: Trade) -> None:
        """You asked for an exit: try at once, whatever the pause after refusals."""
        self._next_try.pop(t.id, None)
        self._refusals.pop(t.id, None)

    # ---------------- Placeholders ----------------

    def _hold(self, t: Trade, kind: str, side: str, qty: int, exit_index: int | None) -> Order:
        """A placeholder for an order about to be sent: it counts as working, and becomes the
        order Kite lists (by the trade's tag), or counts as never placed."""
        o = Order(
            f"{PENDING}{t.id}-{kind}-{side}-{len(t.orders)}",
            kind,
            side,
            qty,
            status="UNKNOWN",
            exit=exit_index,
            placed_at=time.time(),
            message="being sent",
        )
        t.orders.append(o)
        return o

    def prepare(self, t: Trade) -> None:
        """Before an entry is sent (and before the trade is saved): a placeholder per leg, so
        an entry cut short by a crash or a restart leaves its orders to be found."""
        for kind in KINDS:
            self._hold(t, kind, SELL, t.qty, None)
        self._prepared[t.id] = self._begin(t, None)

    def _record(self, t: Trade, result: dict, holder: Order | None, side: str, exit_index: int | None):
        """What one placement did: its order takes the placeholder's place (unless Kite's order
        book showed it first), an outcome not known keeps the placeholder to look up by tag,
        and the placeholder goes when nothing was sent."""
        kind = result["kind"]
        ids = result.get("orderIds") or []
        known = {o.id for o in t.orders}
        for order_id in ids:
            if order_id in known:
                continue  # taken in from Kite's order book meanwhile
            qty = result["quantity"] if len(ids) == 1 else 0  # slices get their size from the book
            if holder is not None and holder.id.startswith(PENDING):
                holder.id, holder.status, holder.message = order_id, "OPEN", ""
                holder.placed_at = time.time()  # its age (for the timeouts) counts from now
                holder.qty = qty or holder.qty
            else:
                t.orders.append(Order(order_id, kind, side, qty, placed_at=time.time(), exit=exit_index))
        if holder is not None and holder.id.startswith(PENDING):
            if result.get("unknown") and not ids:
                # Kite may still list it: it counts as working until found, or 15 s on, lost.
                holder.placed_at, holder.message = time.time(), result.get("error", "")
                holder.status = "UNKNOWN"  # counted as working until found, or 15 s on, lost
                holder.qty = result["quantity"]
            else:
                t.orders.remove(holder)  # nothing was sent for this leg
        self.core.settle(t, self.runner.now())

    # ---------------- Entry ----------------

    async def enter(self, t: Trade) -> None:
        """Sell the trade's CE and PE; what Kite refuses fails the entry."""
        mark = self._prepared.pop(t.id, None) or self._begin(t, None)
        holders = {o.kind: o for o in t.orders if o.id.startswith(PENDING) and o.side == SELL}
        for kind in KINDS:
            if kind not in holders:  # not prepared (prepare() runs before the trade is saved)
                holders[kind] = self._hold(t, kind, SELL, t.qty, None)
        try:
            try:
                results = await self.broker.algo_sell(
                    t.underlying, date.fromisoformat(t.expiry), t.strike, t.qty, order_tag(t)
                )
            except KiteError as exc:  # nothing was sent (not connected, contracts, size)
                for o in holders.values():
                    if o.id.startswith(PENDING):
                        t.orders.remove(o)
                self.core.entry_refused(t, str(exc), self.runner.now())
                return
            refused = []
            for r in results:
                self._record(t, r, holders.get(r["kind"]), SELL, None)
                if r.get("held"):
                    refused.append(r["error"])  # "PE not sent: ..."
                elif not r["ok"] and not r.get("unknown"):
                    refused.append(f"{r['kind']}: {r.get('error') or 'not placed'}")
            if refused:
                self.core.entry_refused(t, "; ".join(refused), self.runner.now())
        finally:
            self._end(t, mark)
            self.runner.changed()
        await self.sync()

    # ---------------- Following Kite ----------------

    def reconcile(self) -> None:
        """After a Kite refresh: bring today's live trades up to date with Kite's order book,
        then notice positions bought back outside the algo. Called by the broker after
        every refresh (no awaiting)."""
        now = self.runner.now()
        self.core.roll(now)
        book = self.broker.orders
        self._note_seq(book)
        changed = False
        for t in self._todays():
            contracts = self._contracts(t)
            if contracts is None:
                continue  # Kite's contracts load first: the legs cannot be told apart yet
            found = self._merge(t, book, contracts)
            if found is not None:
                self.core.settle(t, now)
                if t.closed_at is not None and not t.flat:
                    self.core.reopen(t, now, f"Kite listed {', '.join(found) or 'an order'} late")
                changed = True
            if t.active and self._follow_positions(t, contracts, now):
                changed = True
            if not t.active:  # done: nothing of it is followed any more
                for state in (self._gaps, self.mismatch, self.problems, self._refusals, self._next_try):
                    state.pop(t.id, None)
            for o in t.orders:
                if o.done:
                    self._cancels.pop(o.id, None)
        current = self._unaccounted()
        self.orphans = [x for x in current if x[:3] in {s[:3] for s in self._suspects}]
        self._suspects = current
        if changed:
            self.runner.changed()
        self.runner.spawn(self.sync())

    def _note_seq(self, book: list[dict]) -> None:
        """New trades never reuse a tag Kite's order book shows today (had the saved state been
        lost): they number on from the highest."""
        top = max((int(TAG_RE.fullmatch(tag).group(1)) for k in book if (tag := algo_tag(k))), default=0)
        if top > self.core.seq:
            log.warning("Kite's order book shows algo trade %d: new trades number on from it", top)
            self.core.seq = top

    def _merge(self, t: Trade, book: list[dict], contracts: dict) -> list[str] | None:
        """Copy Kite's order book into the trade: every order with its tag is its own. Returns
        None when nothing changed, else what was found that had counted as not placed."""
        tag = order_tag(t)
        legs = {(c.exchange, c.tradingsymbol): kind for kind, c in contracts.items()}
        by_id = {o.id: o for o in t.orders}
        changed, found = False, []
        for k in book:
            if algo_tag(k) != tag:
                continue
            kind, side, order_id = legs.get(_place_of(k)), _side(k), str(k.get("order_id"))
            if kind is None or side not in (SELL, BUY):
                if order_id not in self._strays:  # once, not at every refresh
                    self._strays.add(order_id)
                    log.warning("Order %s has the tag of %s but is not one of its legs", order_id, t.name)
                continue
            o = by_id.get(order_id)
            if o is None:
                o = next(
                    (x for x in t.orders if x.id.startswith(PENDING) and (x.kind, x.side) == (kind, side)),
                    None,
                )
                if o is not None:
                    lost = o.status == "LOST"
                    o.id = order_id  # the order its placeholder stood for
                else:
                    lost = True  # never recorded here: sent before a restart, or listed late
                    o = Order(order_id, kind, side, 0, placed_at=time.time(), exit=self._exit_index(t, side))
                    t.orders.append(o)
                log.info("%s: order %s found in Kite's order book", t.name, order_id)
                by_id[order_id] = o
                changed = True
            else:
                lost = False
            was = o.status
            if _copy(o, k):
                changed = True
                self._note_refusal(t, o, was)
            if lost and o.filled:
                found.append(f"the {o.kind} {o.side.lower()} of {o.filled:,}")
        if not self._placing.get(t.id):
            for o in t.orders:
                if o.status == "UNKNOWN" and self.broker.updated_at - o.placed_at > UNKNOWN_SETTLE:
                    o.status, o.message = "LOST", "Kite never listed it: not placed"
                    log.warning("%s: %s %s order counted as not placed", t.name, o.kind, o.side.lower())
                    changed = True
        return found if changed else None

    def _exit_index(self, t: Trade, side: str) -> int | None:
        if side != BUY:
            return None
        sending = [m[0] for m in self._placing.get(t.id, []) if m[0] is not None]
        if sending:
            return sending[-1]
        return len(t.exits) - 1 if t.exits else None

    def _note_refusal(self, t: Trade, o: Order, was: str) -> None:
        """An exit order Kite rejects (or someone else cancels) is a refusal: the next try
        waits. The run of refusals ends once nothing more is owed (_sync_once)."""
        if o.side != BUY:
            return
        ours = o.id in self._cancels or time.time() - o.placed_at > EXIT_TIMEOUT  # cancelled as stale
        if o.status != was and (o.status == "REJECTED" or (o.status == "CANCELLED" and not ours)):
            self._refused(
                t, f"Kite {o.status.lower()} the {o.kind} buy-back" + (f": {o.message}" if o.message else "")
            )

    def _follow_positions(self, t: Trade, contracts: dict, now) -> bool:
        """Kite showing less short than the trade holds, on two refreshes in a row and
        explained by filled buy-backs that are not the algo's: bought back outside it."""
        if (
            self._placing.get(t.id)
            or not self.broker.connected
            or any(o.status == "UNKNOWN" for o in t.orders)
            or positions_behind(list(contracts.values()), self.broker.net, self.broker.orders)
        ):
            self._gaps.pop(t.id, None)  # (any mismatch stays as it was until a clear refresh)
            return False
        gaps = {}
        for kind, c in contracts.items():
            # Kite's MIS short, less what other open algo trades on the contract hold ...
            short = max(0, -self.broker.held(c, ALGO_PRODUCT) - self._held_by_others(t, c))
            # ... against what this one holds beyond its own buy-backs on their way (Kite's
            # positions can show those filled before its order book does).
            held = max(0, t.open(kind)) - t.working(kind, BUY)
            if held > short:
                gaps[kind] = held - short
        before, self._gaps[t.id] = self._gaps.get(t.id, {}), gaps
        fills, unexplained = {}, []
        for kind, gap in gaps.items():
            seen = min(gap, before.get(kind, 0))  # the same gap on the refresh before
            if not seen:
                continue
            units, price = self._outside(contracts[kind], t.opened_at)
            if min(seen, units):
                fills[kind] = (min(seen, units), price)
            if seen > units:
                unexplained.append(f"{seen - units:,} {kind}")
        if unexplained:
            self.mismatch[t.id] = (
                f"Kite shows {' and '.join(unexplained)} less short than the algo holds, and no buy-back "
                "order explains it: the algo leaves it alone. Check Kite's positions."
            )
        else:
            self.mismatch.pop(t.id, None)
        if not fills:
            return False
        log.warning("%s was bought back outside the algo: %s", t.name, fills)
        self.core.external_fills(t, fills, now)
        return True

    def _held_by_others(self, t: Trade, c) -> int:
        """Units short on contract `c` held by today's other open algo trades."""
        held = 0
        for other in self._live_trades():
            if other is t:
                continue
            for kind, oc in (self._contracts(other) or {}).items():
                if (oc.exchange, oc.tradingsymbol) == (c.exchange, c.tradingsymbol):
                    held += max(0, other.open(kind))
        return held

    def _outside(self, c, since: float) -> tuple[int, float | None]:
        """Units of a contract bought back as MIS, since `since` (a trade's opening), by orders
        that are not the algo's - not a round trip of yours earlier in the day - less what is
        already booked as bought back outside since then; and their price."""
        since -= CLOCK_SLACK
        units, value = 0, 0.0
        for k in self.broker.orders:
            when = _placed_when(k)
            if (
                _place_of(k) == (c.exchange, c.tradingsymbol)
                and k.get("product") == ALGO_PRODUCT
                and _side(k) == BUY
                and algo_tag(k) is None
                and (when is None or when >= since)
            ):
                filled = int(k.get("filled_quantity") or 0)
                units += filled
                value += filled * float(k.get("average_price") or 0)
        booked = 0
        for t in self._todays():
            contracts = self._contracts(t) or {}
            for kind, tc in contracts.items():
                if (tc.exchange, tc.tradingsymbol) == (c.exchange, c.tradingsymbol):
                    booked += sum(
                        o.filled
                        for o in t.orders
                        if o.kind == kind and o.id.startswith(EXTERNAL_ID) and o.placed_at >= since
                    )
        return max(0, units - booked), (round(value / units, 2) if units else None)

    def _unaccounted(self) -> list[tuple]:
        """Positions from the algo's own orders that no open algo trade accounts for:
        ("short", exchange, symbol, units) - what Kite shows short (as far as the algo's
        filled orders explain it) beyond what open trades hold, e.g. after the saved state
        was lost - or ("long", ...): a leg bought back past zero."""
        if not self.broker.connected:
            return []
        net = {}  # (exchange, symbol) -> units the algo's filled orders leave short
        for k in self.broker.orders:
            if algo_tag(k) is None or k.get("product") != ALGO_PRODUCT:
                continue
            filled = int(k.get("filled_quantity") or 0)
            net[_place_of(k)] = net.get(_place_of(k), 0) + (filled if _side(k) == SELL else -filled)
        if not net:
            return []
        held, outside = {}, {}
        for t in self._todays():
            for kind, c in (self._contracts(t) or {}).items():
                place = (c.exchange, c.tradingsymbol)
                if t.active:
                    held[place] = held.get(place, 0) + max(0, t.open(kind))
                bought = sum(o.filled for o in t.orders if o.kind == kind and o.id.startswith(EXTERNAL_ID))
                outside[place] = outside.get(place, 0) + bought
        kite = {
            _place_of(r): int(r.get("quantity") or 0)
            for r in self.broker.net
            if r.get("product") == ALGO_PRODUCT
        }
        out = []
        for place, short in net.items():
            if kite.get(place, 0) > 0:
                if short < 0:  # the algo's own orders bought back more than they sold
                    out.append(("long", *place, min(kite[place], -short)))
                continue
            units = min(-kite.get(place, 0), short - outside.get(place, 0)) - held.get(place, 0)
            if units > 0:
                out.append(("short", *place, units))
        return out

    # ---------------- Making Kite match ----------------

    async def sync(self) -> None:
        """Cancel orders that should not fill, and send the exits owed. One pass at a time;
        a call while one runs asks for another pass after it."""
        if self._lock.locked():
            self._again = True
            return
        async with self._lock:
            while True:
                self._again = False
                try:
                    await self._sync_once()
                except Exception:  # never let a pass die silently; the next refresh tries again
                    log.exception("Algo order sync failed")
                if not self._again:
                    break

    async def _sync_once(self) -> None:
        if not self.broker.connected:
            return
        for t in self._live_trades():
            await self._cancel_stale(t)
            if not t.managed:
                continue
            owed = {k: max(0, t.open(k)) - t.working(k, BUY) - t.want for k in KINDS}
            if any(v > 0 for v in owed.values()):
                if time.monotonic() >= self._next_try.get(t.id, 0):
                    await self._buy_back(t, owed)
            else:
                self.problems.pop(t.id, None)
                self._refusals.pop(t.id, None)
                self._next_try.pop(t.id, None)

    async def _cancel_stale(self, t: Trade) -> None:
        now = time.time()
        for o in list(t.orders):
            if o.done or o.status == "UNKNOWN":
                continue
            sent = self._cancels.get(o.id)
            if sent is not None and time.monotonic() - sent < CANCEL_AGAIN:
                continue  # cancelled a moment ago: the next refreshes show how it ended
            if o.side == SELL:
                # An entry leg is not wanted once the entry failed or an exit came first, and
                # not left working past the timeout either (the entry then fails).
                unwanted = t.state == FAILED or (t.state == ENTERING and t.want < t.qty)
                if unwanted or now - o.placed_at > ENTRY_TIMEOUT:
                    await self._cancel(t, o)
            elif now - o.placed_at > EXIT_TIMEOUT:
                # Left unfilled (e.g. beyond Kite's market-protection band): cancel it; once Kite
                # confirms, the rest is owed again and sent as a fresh order.
                await self._cancel(t, o)

    async def _cancel(self, t: Trade, o: Order) -> None:
        self._cancels[o.id] = time.monotonic()  # cancelled again later if Kite still shows it working
        try:
            await self.broker.cancel(o.id)
            log.info("Cancelled %s %s order %s of %s", o.kind, o.side.lower(), o.id, t.name)
        except KiteError as exc:  # e.g. it filled meanwhile: the next refresh shows how it ended
            log.warning("Could not cancel order %s of %s: %s", o.id, t.name, exc)

    def _refused(self, t: Trade, why: str) -> None:
        """An exit Kite refused or rejected: tried again after 5 s, 10 s, 20 s... (at most a
        minute), with Kite's reason in the alarm."""
        n = self._refusals.get(t.id, 0) + 1
        self._refusals[t.id] = n
        self.problems[t.id] = why
        self._next_try[t.id] = time.monotonic() + min(RETRY_DELAY * 2 ** (n - 1), RETRY_MAX)
        log.warning("Algo exit for %s refused (%d in a row): %s", t.name, n, why)

    async def _buy_back(self, t: Trade, owed: dict[str, int]) -> None:
        contracts = self._contracts(t)
        if not contracts:
            self._refused(t, "Kite does not list this straddle")
            self.runner.changed()
            return
        decision = len(t.exits) - 1 if t.exits else None
        legs = [(k, v) for k, v in owed.items() if v > 0]
        holders = {k: self._hold(t, k, BUY, v, decision) for k, v in legs}
        mark = self._begin(t, decision)
        try:
            try:
                results = await self.broker.algo_buy_back(
                    t.group, [(contracts[k], v) for k, v in legs], order_tag(t)
                )
            except KiteError as exc:  # nothing was sent
                for o in holders.values():
                    if o.id.startswith(PENDING):
                        t.orders.remove(o)
                self._refused(t, str(exc))
                return
            problems = []
            for r in results:
                holder = holders.pop(r["kind"], None)
                if r["quantity"] == 0 and r["ok"]:  # Kite shows nothing (more) to buy back
                    if holder is not None and holder.id.startswith(PENDING):
                        t.orders.remove(holder)
                    continue
                self._record(t, r, holder, BUY, decision)
                if not r["ok"] and not r.get("unknown"):
                    problems.append(f"{r['kind']}: {r.get('error') or 'not placed'}")
            for o in holders.values():  # no result: not sent (the session expired before it)
                if o.id.startswith(PENDING):
                    t.orders.remove(o)
            if problems:
                self._refused(t, "; ".join(problems))
            elif all(r["quantity"] == 0 for r in results):
                # Kite shows nothing short to buy back: what was bought back elsewhere is
                # noticed on the next refreshes; look again in a moment.
                self._next_try[t.id] = time.monotonic() + RETRY_DELAY
        finally:
            self._end(t, mark)
            self.core.settle(t, self.runner.now())
            self.runner.changed()
