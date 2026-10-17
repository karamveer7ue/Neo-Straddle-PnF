"""The algo on the live page: wires the rules (core.py) to the charts' engine, to Kite
(live.py) or paper fills, to the browsers, and to disk.

- The engine calls on_minute_closed at every live minute close and on_loop every
  250 ms (Engine.listeners); the broker calls the live executor after every Kite
  refresh. The algo runs here in the server, so it works with the page closed or
  in a background tab, and acts once however many tabs are open.
- Paper trades fill at once at the live prices; live ones go through Kite.
- Everything the algo does goes into the alert log and is pushed to every open
  page on /algo/ws, with the full state whenever it changes. Alarms (the feed
  down, Kite disconnected, an exit not going through...) are part of that state;
  the page repeats them every 10 seconds while they last.
- The state (settings and today's trades) is saved to STATE_FILE, so a restart
  picks the trades up again. A restart always leaves the algo Off for new trades;
  trades it was managing go on being managed.
"""

import asyncio
import fcntl
import json
import logging
import os
import secrets
import time
from datetime import datetime
from pathlib import Path

from pnf import engine as engine_mod
from pnf.pnf import build_pnf, tip_of
from pnf.series import UNDERLYINGS

from .core import (
    FAILED,
    LIVE,
    PAPER,
    AlgoCore,
    AlgoError,
    ChartView,
    Enter,
    SettingsError,
    quantity_problem,
    trade_json,
)
from .live import EXIT_TIMEOUT, LiveExecutor

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / ".pnf-algo.json"
SAVE_PRICES_EVERY = 30.0  # seconds: a change of prices alone is saved this often, anything else at once
EXIT_ALARM_AFTER = EXIT_TIMEOUT + 5  # seconds an exit may take before it raises an alarm
ALARM_EVERY = 10.0  # seconds between rings while an alarm lasts (the user's choice)
CLIENT_QUEUE_LIMIT = 500


def _without_prices(data: dict) -> dict:
    """The saved state minus the legs' last prices (they change on every tick)."""
    trades = [
        {**t, "legs": {k: {**leg, "last": None} for k, leg in t["legs"].items()}} for t in data["trades"]
    ]
    return {**data, "trades": trades}


class AlgoRunner:
    def __init__(self, engine, broker, state_file: Path | None = None):
        self.engine = engine
        self.broker = broker  # a KiteBroker (unconfigured without the Kite keys)
        self.state_file = state_file or STATE_FILE
        self._lock_fd: int | None = None
        self.disabled = self._lock()  # why this process must not run the algo ("" when it may)
        self.core = AlgoCore() if self.disabled else self._load()
        kite = broker is not None and broker.configured and not self.disabled
        self.live = LiveExecutor(self) if kite else None
        self.clients: set[asyncio.Queue] = set()
        self._tasks: set[asyncio.Task] = set()
        self._event_seq = 0
        self._saved = ("", "")  # (state without prices, full state) last written
        self._saved_at = 0.0
        self._pushed = ""
        self._version = 0  # of the state pushed last
        self._boot = secrets.token_hex(4)  # this run's: a restart numbers its states from 1 again
        self._latest: dict = {}
        self._alarm_keys: set[str] = set()
        self._rang_at = 0.0  # monotonic time of the last alarm ring
        self._lots: tuple = (None, None, {})  # (contract book, expiry, lot sizes) last worked out
        self._confirmed: tuple | None = None  # the charts (index, expiry, strikes) switched On for

    # ---------------- Lifecycle ----------------

    def start(self) -> None:
        if self.disabled:
            log.error("Algo disabled: %s", self.disabled)
            self.changed()
            return
        self.engine.listeners.append(self)
        if self.live:
            self.broker.listeners.append(self.live.reconcile)
        self.changed()

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._save(force=True)
        self._unlock()

    def _lock(self) -> str:
        """One process at a time runs the algo on an account: two servers (an old one left
        running on another port, say) would each trade, with the same order tags. The lock
        goes with the process, so a crash or a kill frees it."""
        path = self.state_file.with_name(self.state_file.stem + ".lock")
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            return f"The algo cannot lock {path.name} ({exc}); it stays off in this server."
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return (
                f"Another server is already running the algo on this account (it holds {path.name}): "
                "stop that one first."
            )
        self._lock_fd = fd
        return ""

    def _unlock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing releases the flock
            self._lock_fd = None

    def now(self) -> datetime:
        return engine_mod.now_ist()  # the engine's clock (tests shift it)

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Algo task failed", exc_info=task.exception())

    # ---------------- Engine hooks ----------------

    def on_minute_closed(self, minute: datetime, now: datetime) -> None:
        if self.core.on and self._confirmed is not None and self._charts() != self._confirmed:
            self.core.stop_entries(
                now, "The charts changed (index, expiry or strikes) after it was switched On."
            )
        intents = self.core.on_minute(now, self.views(minute), self.engine.ltp)
        self._execute(intents, now)
        self.changed()

    def on_loop(self, now: datetime) -> None:
        prices = self.engine.ltp
        intents = self.core.on_prices(now, prices) if self.feed_live() else []
        intents += self.core.on_clock(now, prices)
        self._execute(intents, now)
        self.changed()

    def _execute(self, intents: list, now: datetime) -> None:
        live_exits = False
        for intent in intents:
            t = intent.trade
            if t.mode == PAPER:
                self.core.fill_paper(intent, self.engine.ltp, now)
            elif self.live is None:
                if isinstance(intent, Enter):
                    self.core.entry_refused(t, "Kite is not set up", now)
            elif isinstance(intent, Enter):
                self.live.prepare(t)  # its placeholders are saved with it, before anything is sent
                self.spawn(self.live.enter(t))
            else:
                live_exits = True
        if live_exits:
            self.spawn(self.live.sync())

    # ---------------- What the algo sees ----------------

    def feed_live(self) -> bool:
        return self.engine.feed_state == "live" and self.engine.gw.logged_in

    def views(self, minute: datetime) -> list[ChartView]:
        """The charts on screen as the rules see them at a minute close. Charts are only
        worked out while the algo has something to decide."""
        eng = self.engine
        ts = int(minute.timestamp())
        needed = self.core.on or bool(self.core.managed())
        out = []
        for chart in eng.charts:
            if chart.error or not chart.ce or not chart.pe:
                continue
            try:
                series = chart.series()
                bar = series[-1] if needed and series and series[-1][0] == ts else None
                pair = self._kite_pair(chart.strike)
                out.append(
                    ChartView(
                        key=chart.key,
                        offset=chart.offset,
                        strike=chart.strike,
                        underlying=eng.built_u.key,
                        expiry=eng.expiry,
                        ce=chart.ce,
                        pe=chart.pe,
                        lot_size=pair[0].lot_size if pair else chart.ce.get("lotSize"),
                        bar=bar,
                        tip=tip_of(build_pnf(series), ts) if bar else None,
                        blocked=self._blocked(pair),
                    )
                )
            except Exception:  # odd data on one chart must not stop the others' exits
                log.exception("Algo could not read the %s chart this minute", chart.key)
        return out

    def _charts(self) -> tuple:
        eng = self.engine
        return eng.built_u.key, eng.expiry, tuple(c.strike for c in eng.charts)

    def _kite_pair(self, strike: int):
        if self.broker is None or not self.broker.configured or self.engine.expiry is None:
            return None
        return self.broker.straddle_of(self.engine.built_u.key, self.engine.expiry, strike)

    def _blocked(self, pair) -> str:
        connected = self.broker is not None and self.broker.connected
        if self.core.mode == LIVE:
            if not connected:
                return "Kite is not connected"
            if pair is None:
                return "Kite does not list this straddle"
        if pair and connected and any(self.broker.held(c) for c in pair):
            return "your own position is open on this straddle"
        return ""

    def lot_sizes(self) -> dict[str, int]:
        """Lot size per index: Kite's (for the expiry on screen, or the nearest), else Neo's."""
        eng = self.engine
        book = self.broker.book if self.broker is not None else None
        if self._lots[0] is not book or self._lots[1] != eng.expiry:
            lots = {}
            if book is not None:
                for key, lot in book.lot_sizes().items():  # sorted: each index's nearest expiry first
                    lots.setdefault(key.split("|")[0], lot)
                if eng.expiry is not None:
                    on_screen = book.lot_sizes().get(f"{eng.built_u.key}|{eng.expiry.isoformat()}")
                    if on_screen:
                        lots[eng.built_u.key] = on_screen
            self._lots = (book, eng.expiry, lots)
        lots = dict(self._lots[2])
        if eng.built_u.key not in lots:
            neo = next((c.ce["lotSize"] for c in eng.charts if c.ce and c.ce.get("lotSize")), None)
            if neo:
                lots[eng.built_u.key] = neo
        return lots

    def summary(self) -> dict:
        """What switching On would trade (shown for confirmation, and checked again)."""
        eng, s = self.engine, self.core.settings
        u = eng.built_u.key
        return {
            "underlying": u,
            "expiry": eng.expiry.isoformat() if eng.expiry else None,
            "strikes": [c.strike for c in eng.charts],
            "mode": s.mode,
            "qty": s.qty[u],
            "lotSize": self.lot_sizes().get(u),
            "target": s.target[u],
        }

    def start_problem(self) -> str:
        """Why the algo cannot be switched On now ("" when it can)."""
        eng, s = self.engine, self.core.settings
        if self.disabled:
            return self.disabled
        if eng.phase != "ready" or not eng.charts or eng.expiry is None:
            return "The charts are not ready yet."
        if eng.underlying_key != eng.built_u.key:
            return "The charts are switching to another index."
        if eng.built_for != (self.now().date(), True):
            # Before then the strikes are yesterday's, or today's before the 09:16 rebuild.
            return "Today's charts are not built yet: they are rebuilt at 09:16 from today's open."
        if not self.feed_live():
            return "Go live first: the algo needs the live feed (enter the TOTP)."
        u = eng.built_u.key
        if s.mode == LIVE:
            if self.broker is None or not self.broker.configured:
                return "Kite is not set up (see kite/README.md), so only Paper mode can run."
            if not self.broker.connected:
                return "Connect Kite first (top bar), or switch the mode to Paper."
            if self.broker.book is None:
                return "Kite's contracts are still loading."
            missing = [c.strike for c in eng.charts if not c.error and self._kite_pair(c.strike) is None]
            if missing:
                return f"Kite does not list the {', '.join(map(str, missing))} straddle for this expiry."
        lot = self.lot_sizes().get(u)
        if not lot:
            return "The lot size is not known yet."
        problem = quantity_problem(u, s.qty[u], lot)
        if problem:
            return f"{u} quantity {s.qty[u]}: {problem}."
        return ""

    def locked(self) -> bool:
        """The index, expiry, ATM and days stay as they are while the algo is On or managing
        a trade: its charts are the straddles it trades."""
        return self.core.on or bool(self.core.managed())

    # ---------------- Actions (from the page) ----------------

    def switch_on(self, expect: dict | None = None) -> None:
        problem = self.start_problem()
        if problem:
            raise AlgoError(problem)
        summary = self.summary()
        if expect is not None and any(summary.get(k) != v for k, v in expect.items()):
            raise AlgoError("The charts or the settings changed meanwhile: look again, then switch On.")
        self.core.switch_on(self.now())
        self._confirmed = self._charts()
        log.info("Algo switched On (%s): %s", self.core.on_mode, summary)
        self.changed()

    def switch_off(self) -> None:
        released = self.core.switch_off(self.now())
        log.info("Algo switched Off; %d open trade(s) handed over", len(released))
        self.changed()

    def exit_all(self) -> None:
        now = self.now()
        intents = self.core.exit_all(now)
        self._retry_now([i.trade for i in intents])
        self._execute(intents, now)
        self.changed()

    def exit_trade(self, trade_id: str) -> None:
        now = self.now()
        intent = self.core.exit_trade(now, trade_id)
        self._retry_now([intent.trade])
        self._execute([intent], now)
        self.changed()

    def _retry_now(self, trades: list) -> None:
        """An exit you asked for is tried at once, whatever the pause after Kite's refusals."""
        if self.live:
            for t in trades:
                self.live.retry_now(t)

    def update_settings(self, changes: dict) -> None:
        s = self.core.settings
        if "mode" in changes and changes["mode"] != s.mode and self.locked():
            raise SettingsError(
                {"mode": "Switch the algo Off (and let its trades close) before changing the mode"}
            )
        new = s.updated(changes)
        if new.exit_time != s.exit_time and self.locked() and self.now().time() >= new.exit_at:
            # Applied at once, it would buy everything back this instant: a typo must not.
            raise SettingsError(
                {"exitTime": "Already past: that would buy everything back now (Exit all does that)"}
            )
        # Lots are checked for the quantities being changed (others are checked at switch On).
        changed_qty = {f"qty.{k}" for k in (changes.get("qty") or {})}
        problems = {f: m for f, m in new.problems(self.lot_sizes()).items() if f in changed_qty}
        if problems:
            raise SettingsError(problems)
        self.core.settings = new
        self.changed()

    # ---------------- Alarms ----------------

    def alarms(self) -> list[dict]:
        """What needs you now: the pages ring every 10 seconds while it lasts."""
        out = []

        def alarm(key: str, title: str, detail: str) -> None:
            out.append({"key": key, "title": title, "detail": detail})

        managed = self.core.managed()
        if managed and not self.feed_live():
            n = len(managed)
            what = f"No target or stop can fire for {n} open algo trade{'s' if n > 1 else ''}"
            if self.engine.gw.logged_in:  # dropped: the engine reconnects by itself
                detail = f"{what} until it is back; it is reconnecting by itself."
            else:  # the session ended: only a fresh TOTP brings it back
                detail = f"{what}: enter a fresh TOTP in the top bar to go live again."
            alarm("feed", "Live feed is down", f"{detail} The Exit Time still applies.")
        live = [t for t in managed if t.mode == LIVE]
        if live and (self.broker is None or not self.broker.connected):
            alarm(
                "kite",
                "Kite is disconnected",
                "The algo cannot place or follow orders for its open trades: connect Kite (top bar).",
            )
        on_screen = {(c.ce["key"], c.pe["key"]) for c in self.engine.charts if c.ce and c.pe}
        for t in managed:
            if (t.legs["CE"].key, t.legs["PE"].key) not in on_screen:
                alarm(
                    f"chart:{t.id}",
                    f"{t.name} is not on the charts",
                    "Its 1-minute rules cannot run; only the loss limits and the Exit Time can close it.",
                )
        now = self.now().timestamp()  # the clock the exit decisions were stamped with
        for t in live:
            if self.live and self.live.unknown(t):
                alarm(
                    f"unknown:{t.id}",
                    f"Order outcome unknown: {t.name}",
                    "Kite did not answer an order; the algo is looking for it in the order book.",
                )
            elif t.state == FAILED and t.holding:
                alarm(
                    f"leg:{t.id}",
                    f"Unhedged leg: {t.name}",
                    "The entry failed and a leg is still short alone; the algo is buying it back.",
                )
            elif t.holding > t.want and t.exits and now - t.exits[-1].at > EXIT_ALARM_AFTER:
                why = self.live.problems.get(t.id) if self.live else ""
                alarm(
                    f"exit:{t.id}",
                    f"Exit not going through: {t.name}",
                    why or "Waiting for Kite to fill the buy-back; the algo keeps trying.",
                )
        if self.live:
            for t in self.core.active():
                gap = self.live.mismatch.get(t.id)
                if gap:
                    alarm(f"gap:{t.id}", f"Kite does not match the algo: {t.name}", gap)
            for kind, _exchange, symbol, units in self.live.orphans:
                if kind == "long":
                    alarm(
                        f"long:{symbol}",
                        f"{symbol} is long {units:,} (MIS)",
                        "An algo buy-back filled past zero (Kite listed an order late): "
                        "sell it in Kite if it is not yours.",
                    )
                else:
                    alarm(
                        f"orphan:{symbol}",
                        f"Algo position not managed: {symbol}",
                        f"Kite shows {units:,} short (MIS) from the algo's orders that no open algo trade "
                        "holds: buy it back in Kite, or check Kite's positions.",
                    )
        return out

    # ---------------- State: events, saving, pushing ----------------

    def changed(self) -> None:
        """After anything that may have changed the algo: log and push its events and new
        alarms, save its state, and push the state if it differs from the last one sent."""
        for ev in self.core.take_events():
            self._event_seq += 1
            self.engine.log_alert(
                {
                    "type": "algo",
                    "key": f"algo:{ev['at']}:{self._event_seq}",
                    "kind": ev["kind"],
                    "title": ev["title"],
                    "detail": ev["detail"],
                    "pnl": ev["pnl"],
                }
            )
            self._push({"type": "algo-event", **ev})
            log.info("Algo: %s - %s", ev["title"], ev["detail"])
        alarms = self.alarms()
        new = [a for a in alarms if a["key"] not in self._alarm_keys]
        for a in new:
            log.warning("Algo alarm: %s - %s", a["title"], a["detail"])
            self.engine.log_alert(
                {
                    "type": "algo",
                    "key": f"alarm:{a['key']}:{int(time.time())}",
                    "kind": "alarm",
                    "title": a["title"],
                    "detail": a["detail"],
                }
            )
        self._alarm_keys = {a["key"] for a in alarms}
        # The pages sound and show the alarms on each ring: at once, then every ALARM_EVERY
        # seconds. The ring comes from here, as background tabs slow their own timers down.
        if alarms and (new or time.monotonic() - self._rang_at >= ALARM_EVERY):
            self._rang_at = time.monotonic()
            self._push({"type": "algo-ring", "alarms": alarms})
        self._save()
        state = self._state(alarms)
        text = json.dumps(state, sort_keys=True)
        if text != self._pushed:
            self._pushed = text
            # Numbered, so a page never puts an older state over a newer one (the answer to
            # an Exit can reach it after the push of the fills that closed the trade).
            self._version += 1
            self._latest = {**state, "boot": self._boot, "version": self._version}
            self._push(self._latest)

    def state_json(self) -> dict:
        """The algo's latest state, numbered as pushed (the endpoints answer with it)."""
        self.changed()
        return self._latest

    def _state(self, alarms: list[dict]) -> dict:
        core = self.core
        return {
            "type": "algo",
            "on": core.on,
            "mode": core.mode,
            "settings": core.settings.to_json(),
            "halted": core.halted,
            "exitTimeDone": core.exit_time_done,
            "startProblem": "" if core.on else self.start_problem(),
            "locked": self.locked(),
            "kite": self.broker is not None and self.broker.configured,
            "summary": self.summary(),
            "lotSizes": self.lot_sizes(),
            "caps": {k: u.max_sell_qty for k, u in UNDERLYINGS.items()},
            "defaults": {k: {"qty": u.algo_qty, "target": u.algo_target} for k, u in UNDERLYINGS.items()},
            "day": core.day.isoformat() if core.day else None,
            "entries": len(core.todays()),
            "trades": [trade_json(t) for t in core.trades],
            "alarms": alarms,
        }

    def _save(self, force: bool = False) -> None:
        data = self.core.to_json()
        structural = json.dumps(_without_prices(data), sort_keys=True)
        full = json.dumps(data, indent=1, sort_keys=True)
        if full == self._saved[1]:
            return
        if (
            structural == self._saved[0]
            and not force
            and time.monotonic() - self._saved_at < SAVE_PRICES_EVERY
        ):
            return
        if self.disabled:
            return  # the file is the other server's
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        try:
            tmp.write_text(full)
            os.replace(tmp, self.state_file)  # a crash mid-write never loses the trades
        except OSError as exc:
            log.warning("Could not save %s: %s", self.state_file, exc)
            return
        self._saved = (structural, full)
        self._saved_at = time.monotonic()

    def _load(self) -> AlgoCore:
        try:
            data = json.loads(self.state_file.read_text())
            return AlgoCore.from_json(data)
        except FileNotFoundError:
            return AlgoCore()
        except Exception as exc:  # whatever shape it is in, the server must still come up
            # Kept aside, not overwritten: it may describe open trades (Kite still lists them).
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            bad = self.state_file.with_name(f"{self.state_file.name}.unreadable-{stamp}")
            log.error("Could not read %s (%s); kept as %s, the algo starts afresh", self.state_file, exc, bad)
            try:
                os.replace(self.state_file, bad)
            except OSError:
                pass
            return AlgoCore()

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
                queue.put_nowait(self._latest)
