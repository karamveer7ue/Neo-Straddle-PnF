"""State machine behind the three straddle charts.

Owns the chart data (official 1-min history plus live-built minutes),
the Neo WebSocket feed, and the browser connections it pushes to.

Lifecycle:
- build: for the chosen underlying (NIFTY on NSE or SENSEX on BSE), pick
  the expiry and the ATM, resolve the six option legs and load N sessions
  of 1-min history. Needs only the consumer key.
- login (TOTP from the browser) starts the live feed. Every minute close,
  the latest CE + PE LTPs become a live 1-min bar.
- during the session, today's official candles are re-fetched every few
  minutes and replace the live-built bars they cover.
- at 09:16 on a new trading day the charts are rebuilt, which re-picks
  the ATM from that day's open (unless an ATM override is set).
"""

import asyncio
import json
import logging
import time as clock
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from neo_api_client.websocket.feed import SFeedIndex, SFeedScrip, SFeedScripLite, WsToken
from neo_api_client.websocket.feed.exceptions import AuthenticationError

from .neo import NeoError, NeoGateway
from .series import (
    DEFAULT_EXPIRY_ROLE,
    DEFAULT_UNDERLYING,
    EXPIRY_ROLES,
    IST,
    MAX_DAYS,
    MIN_DAYS,
    UNDERLYINGS,
    Bar,
    Candle,
    expiry_choices,
    expiry_roles,
    history_start,
    in_session,
    is_market_window,
    last_sessions,
    merge_tail,
    minute_start,
    now_ist,
    parse_candles,
    round_to_strike,
    session_date,
    straddle_series,
)

log = logging.getLogger(__name__)

DEFAULT_DAYS = 3
LOOP_INTERVAL = 0.25  # seconds; also the live-tick push rate to browsers
RECONCILE_EVERY_MIN = 5  # refresh today's official candles this often
FEED_RECONNECT_DELAY = 5
REBUILD_RETRY_DELAY = 60  # after a failed automatic rebuild
CONFIG_FILE = Path(__file__).resolve().parent.parent / ".pnf-config.json"
ALERTS_FILE = Path(__file__).resolve().parent.parent / ".pnf-alerts.json"
ALERTS_KEPT = 30  # most recent signal alerts kept in the log
CLIENT_QUEUE_LIMIT = 5000  # pending messages per browser before a resync
# The history API serves the minute still in progress as a partial candle.
# A candle is used only once its minute ended this long ago; the newest
# minutes come from live-built bars until a later refresh has final ones.
CANDLE_SETTLE_SECONDS = 60
KEEP = object()  # configure(): leave the underlying's ATM override as it is


@dataclass
class Chart:
    key: str
    offset: int
    strike: int
    ce: dict | None  # {"key": "<segment>|<token>", "segment", "token", "symbol"}
    pe: dict | None
    history: list[Bar] = field(default_factory=list)
    live: list[Bar] = field(default_factory=list)
    error: str | None = None

    @property
    def legs(self) -> list[dict]:
        return [leg for leg in (self.ce, self.pe) if leg]

    @property
    def leg_keys(self) -> list[str]:
        return [leg["key"] for leg in self.legs]

    def series(self) -> list[Bar]:
        return merge_tail(self.history, self.live)

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "offset": self.offset,
            "strike": self.strike,
            "ce": self.ce,
            "pe": self.pe,
            "series": self.series(),
            "error": self.error,
        }


def settled(candles: list[Candle], now: datetime) -> list[Candle]:
    """Drop candles whose minute has not been over for CANDLE_SETTLE_SECONDS.

    The history API includes the minute in progress (and the one just ended
    may still be filling in). Treating such a candle as final froze the
    chart: the browser ignores live prices for a minute it already has a
    bar for, and the server never closes that minute's real bar.
    """
    cutoff = now.timestamp() - 60 - CANDLE_SETTLE_SECONDS
    return [c for c in candles if c[0] <= cutoff]


class Engine:
    def __init__(self, gateway: NeoGateway):
        self.gw = gateway
        self.clients: set[asyncio.Queue] = set()

        # User config. Each underlying keeps its own ATM override; the day
        # count and expiry role (a generic "weekly"/"monthly") are shared.
        self.underlying_key = DEFAULT_UNDERLYING
        self.atm_overrides: dict[str, int | None] = dict.fromkeys(UNDERLYINGS)
        self.days = DEFAULT_DAYS
        self.expiry_role = DEFAULT_EXPIRY_ROLE
        self._load_config()

        # Build output
        self.phase = "idle"  # idle | loading | ready | error
        self.message = ""
        self.expiry: date | None = None
        self.expiry_options: list[dict] = []
        self.built_u = UNDERLYINGS[self.underlying_key]  # what the current charts show
        self.index_open: float | None = None
        self.atm_auto: int | None = None
        self.atm: int | None = None
        self.window_dates: list[date] = []
        self.charts: list[Chart] = []
        self.leg_bars: dict[str, list[Candle]] = {}  # leg key -> official 1-min candles
        self.built_for: tuple[date, bool] | None = None  # (day, built after the open)
        self._recheck_at: datetime | None = None  # rebuild no earlier than this (provisional holiday)
        self.generation = 0

        # Live state
        self.spot: float | None = None
        self.spot_prev_close: float | None = None
        self.future: dict | None = None  # near-month index future leg, plus "expiry"
        self.future_ltp: float | None = None
        self.future_prev_close: float | None = None
        self.ltp: dict[str, float] = {}
        # Column VWAPs need each minute's traded volume. The feed reports a leg's
        # running day volume; a minute's volume is the rise since the last close.
        self.day_volume: dict[str, int] = {}  # leg key -> latest volume_traded_today
        self._volume_mark: dict[str, int] = {}  # leg key -> day volume at the last minute close
        self.feed_state = "offline"  # offline | connecting | live | reconnecting
        self.feed_message = ""
        self._tick_dirty = False

        self.alerts: list[dict] = self._load_alerts()  # newest first

        self._past_cache: dict[tuple[str, date, date], list[Candle]] = {}
        self._build_task: asyncio.Task | None = None
        self._feed_task: asyncio.Task | None = None
        self._loop_task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None
        self._last_auto_rebuild = 0.0

    # ---------------- Lifecycle ----------------

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop())
        self.rebuild()

    async def stop(self) -> None:
        for task in (self._build_task, self._feed_task, self._loop_task, self._reconcile_task):
            if task:
                task.cancel()
        if self.gw.logged_in:
            try:
                await asyncio.to_thread(self.gw.client.logout)
            except Exception as exc:  # best effort on shutdown
                log.warning("Logout failed: %s", exc)

    @property
    def atm_override(self) -> int | None:
        return self.atm_overrides[self.underlying_key]

    def configure(self, underlying: str, days: int, expiry_role: str, atm=KEEP) -> None:
        """Apply new settings and rebuild. `atm` (an override or None for the
        day's open) applies to `underlying`; KEEP leaves its stored override."""
        self.underlying_key = underlying
        if atm is not KEEP:
            self.atm_overrides[underlying] = atm
        self.days = days
        self.expiry_role = expiry_role
        self._save_config()
        self.rebuild()

    def _load_config(self) -> None:
        """Restore the settings from an earlier run."""
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            atm = saved.get("atm")
            if not isinstance(atm, dict):  # older files held one NIFTY override
                atm = {"NIFTY": atm}
            overrides = {k: atm.get(k) for k in UNDERLYINGS}
            underlying = saved.get("underlying", DEFAULT_UNDERLYING)
            days, role = saved.get("days", DEFAULT_DAYS), saved.get("expiry", DEFAULT_EXPIRY_ROLE)
            if (
                all(v is None or isinstance(v, int) for v in overrides.values())
                and underlying in UNDERLYINGS
                and MIN_DAYS <= days <= MAX_DAYS
                and role in EXPIRY_ROLES
            ):
                self.atm_overrides, self.underlying_key = overrides, underlying
                self.days, self.expiry_role = days, role
        except (OSError, ValueError, AttributeError, TypeError):
            pass  # first run or unreadable file: defaults

    def _save_config(self) -> None:
        config = {
            "underlying": self.underlying_key,
            "atm": self.atm_overrides,
            "days": self.days,
            "expiry": self.expiry_role,
        }
        try:
            CONFIG_FILE.write_text(json.dumps(config))
        except OSError as exc:
            log.warning("Could not save %s: %s", CONFIG_FILE, exc)

    # ---------------- Alert log ----------------

    def log_alert(self, alert: dict) -> bool:
        """Record a signal alert raised in a browser; returns False for a duplicate.

        Every open page raises the same alert, so entries are deduplicated by
        their key. Newest first, capped at ALERTS_KEPT, saved to disk.
        """
        if any(a["key"] == alert["key"] for a in self.alerts):
            return False
        entry = {**alert, "loggedAt": int(now_ist().timestamp())}
        self.alerts = [entry, *self.alerts][:ALERTS_KEPT]
        try:
            ALERTS_FILE.write_text(json.dumps(self.alerts))
        except OSError as exc:
            log.warning("Could not save %s: %s", ALERTS_FILE, exc)
        self._push({"type": "alerts", "alerts": self.alerts})
        return True

    def _load_alerts(self) -> list[dict]:
        try:
            alerts = json.loads(ALERTS_FILE.read_text())
            if isinstance(alerts, list) and all(isinstance(a, dict) and "key" in a for a in alerts):
                return alerts[:ALERTS_KEPT]
        except (OSError, ValueError):
            pass  # no log yet, or unreadable: start empty
        return []

    def rebuild(self) -> None:
        if self._build_task and not self._build_task.done():
            self._build_task.cancel()
        self._build_task = asyncio.create_task(self._build())

    # ---------------- Browser push ----------------

    def subscribe(self) -> asyncio.Queue:
        """Register a browser connection; its queue starts with a snapshot.

        One queue per connection keeps messages in order (a bar never
        overtakes the snapshot it extends).
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE_LIMIT)
        queue.put_nowait(self.snapshot())
        self.clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.clients.discard(queue)

    def _push(self, payload: dict) -> None:
        for queue in list(self.clients):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # A stalled browser: drop its backlog and resync it from scratch.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(self.snapshot())

    def status_json(self) -> dict:
        return {
            "type": "status",
            "phase": self.phase,
            "message": self.message,
            "feed": {
                "state": self.feed_state,
                "message": self.feed_message,
                "loggedIn": self.gw.logged_in,
                "totpRequired": not self.gw.logged_in and is_market_window(now_ist()),
            },
        }

    def snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "generation": self.generation,
            "status": self.status_json(),
            "config": {
                "underlying": self.underlying_key,  # requested; may still be loading
                "underlyings": list(UNDERLYINGS),
                "built": self.built_u.to_json(),  # what the charts below show
                "atm": self.atm,
                "atmAuto": self.atm_auto,
                "atmOverride": self.atm_overrides[self.built_u.key],
                "indexOpen": self.index_open,
                "days": self.days,
                "expiry": self.expiry.isoformat() if self.expiry else None,
                "expiryRole": self.expiry_role,
                "expiryOptions": self.expiry_options,
                "windowDates": [d.isoformat() for d in self.window_dates],
            },
            "spot": self.spot,
            "spotPrevClose": self.spot_prev_close,
            "future": self._future_json(),
            "charts": [c.to_json() for c in self.charts],
            "legs": {k: self.ltp.get(k) for c in self.charts for k in c.leg_keys},
            "live": self._live_json(),
            "alerts": self.alerts,
        }

    def _future_json(self) -> dict | None:
        if not self.future:
            return None
        return {
            "symbol": self.future["symbol"],
            "expiry": self.future["expiry"],
            "ltp": self.future_ltp,
            "prevClose": self.future_prev_close,
        }

    def _set_phase(self, phase: str, message: str = "") -> None:
        self.phase, self.message = phase, message
        self._push(self.status_json())

    def _set_feed(self, state: str, message: str = "") -> None:
        self.feed_state, self.feed_message = state, message
        self._push(self.status_json())

    # ---------------- Build ----------------

    async def _build(self) -> None:
        started = now_ist()
        today = started.date()
        # Past-day history cached under an earlier "today" can never be asked for again.
        self._past_cache = {k: v for k, v in self._past_cache.items() if k[2] == today}
        u = UNDERLYINGS[self.underlying_key]
        self._set_phase("loading", f"Loading {u.key} expiries...")
        try:
            roles = expiry_roles(await self.gw.expiries(u), today)
            expiry = roles.get(self.expiry_role, roles[DEFAULT_EXPIRY_ROLE])

            self._set_phase("loading", f"Loading {u.key} open...")
            quote = await self.gw.index_quote(u)
            atm_auto = round_to_strike(quote["open"], u.strike_step)
            atm = self.atm_overrides[u.key] or atm_auto

            future, future_quote = None, None
            try:  # the future is shown for reference only: never fail the charts over it
                future = await self.gw.near_future(u, today)
                future_quote = await self.gw.quote(future["segment"], future["token"], future["symbol"])
            except Exception as exc:  # any failure, not just API errors (e.g. an odd expiry field)
                log.warning("Near-month %s future unavailable: %s", u.key, str(exc)[:200])
                future, future_quote = None, None

            self._set_phase("loading", f"Resolving {u.key} {expiry:%d %b} option contracts...")
            strikes = {key: atm + off for key, off in u.chart_offsets.items()}
            contracts = await self.gw.option_contracts(u, expiry, list(strikes.values()))
            charts = []
            for key, off in u.chart_offsets.items():
                strike = strikes[key]
                ce, pe = contracts.get((strike, "CE")), contracts.get((strike, "PE"))
                chart = Chart(key=key, offset=off, strike=strike, ce=ce, pe=pe)
                if not (ce and pe):
                    chart.error = f"No {u.key} {strike} straddle listed for {expiry:%d %b %Y}"
                charts.append(chart)

            from_date = history_start(today, self.days)
            leg_keys = [k for c in charts for k in c.leg_keys]
            leg_bars, today_known_empty = {}, True
            for i, leg_key in enumerate(leg_keys, 1):
                self._set_phase("loading", f"Loading {u.key} 1-min history {i}/{len(leg_keys)}...")
                bars, today_ok, traded = await self._leg_history(leg_key, from_date, today, started)
                leg_bars[leg_key] = bars
                if not today_ok or traded:
                    today_known_empty = False

            window = self._window_dates(leg_bars, today, started, today_known_empty)
            # Before 09:30 an empty today cannot yet be told apart from a holiday: build
            # now with today in the window, and check again once at 09:31.
            provisional = today_known_empty and started.time() < time(9, 30) and today in window

            # Commit atomically - nothing above touched live state.
            old_leg_keys = {k for c in self.charts for k in c.leg_keys}
            switched = u is not self.built_u
            self.built_u = u
            self.expiry, self.atm, self.atm_auto = expiry, atm, atm_auto
            self.expiry_options = expiry_choices(roles)
            self.index_open = quote["open"]
            if switched or self.feed_state != "live" or self.spot is None:
                self.spot, self.spot_prev_close = quote["ltp"], quote["prev_close"]
            old_future_key = self.future["key"] if self.future else None
            self.future = future
            if future_quote and (
                future["key"] != old_future_key or self.feed_state != "live" or self.future_ltp is None
            ):
                self.future_ltp, self.future_prev_close = future_quote["ltp"], future_quote["prev_close"]
            elif not future:
                self.future_ltp = self.future_prev_close = None
            future_changed = (future["key"] if future else None) != old_future_key
            # Live-built bars are today's only data while same-day history is refused,
            # and bars closed during the build landed on the old charts: keep them for
            # any straddle that is still on screen.
            old_live = {tuple(c.leg_keys): c.live for c in self.charts if c.leg_keys}
            for chart in charts:
                chart.live = list(old_live.get(tuple(chart.leg_keys), []))
            self.leg_bars = leg_bars
            self.window_dates = window
            self.charts = charts
            # The browser learns the new generation from this build's snapshot, so it is
            # raised only now: until then ticks and bars keep flowing for the old charts.
            self.generation += 1
            self._recompute_history()
            for leg_key, bars in leg_bars.items():
                if bars and (leg_key not in self.ltp or self.feed_state != "live"):
                    self.ltp[leg_key] = bars[-1][1]
            self.built_for = (today, started.time() >= time(9, 16) and not provisional)
            # A provisional build before 09:16 still takes the 09:16 rebuild (the ATM is
            # re-picked from the confirmed open); after that, it waits for 09:31.
            recheck = time(9, 16) if started.time() < time(9, 16) else time(9, 31)
            self._recheck_at = datetime.combine(today, recheck, IST) if provisional else None
            self.phase, self.message = "ready", ""
            self._push(self.snapshot())

            if self.gw.logged_in and (switched or future_changed or set(leg_keys) != old_leg_keys):
                self._start_feed()  # resubscribe to the new legs, future and index
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surfaced to the UI
            log.exception("Build failed")
            self._set_phase("error", str(exc)[:400])

    async def _leg_history(
        self, leg_key: str, from_date: date, today: date, now: datetime
    ) -> tuple[list[Candle], bool, bool]:
        """Official 1-min closes for one leg, whether today's fetch worked, and
        whether the reply showed any trading today (judged before `settled`
        drops the newest candles, so a build at 09:16 sees the 09:15 one).

        Past days are cached; today is always re-fetched. Same-day candles
        may be refused while the market is open, in which case the past
        days are still returned and live bars fill today in.
        """
        today_started = now.weekday() < 5 and now.time() >= time(9, 15)
        key = (leg_key, from_date, today)
        past = self._past_cache.get(key)
        if past is None:
            try:
                raw = parse_candles(await self.gw.history(leg_key, from_date, today))
                bars, today_ok = settled(raw, now_ist()), True
                traded = any(session_date(c[0]) == today for c in raw)
            except NeoError:
                if not today_started:
                    raise
                log.warning("History incl. today failed for %s - loading past days only", leg_key)
                yesterday = today - timedelta(days=1)
                bars = parse_candles(await self.gw.history(leg_key, from_date, yesterday))
                today_ok, traded = False, False
            past = [b for b in bars if session_date(b[0]) < today]
            self._past_cache[key] = past
            return bars, today_ok, traded
        if not today_started:
            return past, True, False
        today_bars, today_ok, traded = await self._today_history(leg_key, today)
        return past + today_bars, today_ok, traded

    async def _today_history(self, leg_key: str, today: date) -> tuple[list[Candle], bool, bool]:
        """Today's settled candles, whether the fetch worked, and whether it showed trading."""
        try:
            raw = parse_candles(await self.gw.history(leg_key, today, today))
        except NeoError as exc:
            log.warning("Today's history unavailable for %s: %s", leg_key, str(exc)[:200])
            return [], False, False
        return settled(raw, now_ist()), True, any(session_date(c[0]) == today for c in raw)

    def _window_dates(
        self, leg_bars: dict[str, list[Candle]], today: date, now: datetime, today_known_empty: bool
    ) -> list[date]:
        """The N session dates the charts cover, fixed for the whole day.

        Today counts as a session once 09:15 has passed on a weekday, even
        before its candles can be fetched, so the window (and with it the
        box size) does not shift when today's data starts arriving. A
        weekday whose candles fetched fine but came back empty after 09:30
        is treated as a market holiday.
        """
        seen = {session_date(c[0]) for bars in leg_bars.values() for c in bars}
        holiday = today_known_empty and now.time() >= time(9, 30)
        if now.weekday() < 5 and now.time() >= time(9, 15) and not holiday:
            seen.add(today)
        return sorted(seen)[-self.days :]

    def _recompute_history(self) -> None:
        for chart in self.charts:
            if chart.error:
                continue
            full = straddle_series(
                self.leg_bars.get(chart.ce["key"], []), self.leg_bars.get(chart.pe["key"], [])
            )
            chart.history = last_sessions(full, self.window_dates)
            # Live bars outside the window go (e.g. a provisional today found to be a
            # holiday), and so do those now covered by official ones.
            window = set(self.window_dates)
            last = chart.history[-1][0] if chart.history else None
            chart.live = [
                b for b in chart.live if session_date(b[0]) in window and (last is None or b[0] > last)
            ]

    # ---------------- Reconcile today's official candles ----------------

    async def _reconcile(self) -> None:
        try:
            await self._reconcile_today()
        except asyncio.CancelledError:
            raise
        except Exception:  # never let the refresh die silently in an unawaited task
            log.exception("Refreshing today's candles failed")

    async def _reconcile_today(self) -> None:
        today = now_ist().date()
        generation = self.generation
        fresh = {}
        for leg_key in [k for c in self.charts for k in c.leg_keys]:
            bars, ok, _ = await self._today_history(leg_key, today)
            if ok:
                fresh[leg_key] = bars
        if generation != self.generation or not fresh:
            return  # a rebuild happened meanwhile, or nothing new
        changed = False
        for leg_key, bars in fresh.items():
            stored = self.leg_bars.get(leg_key, [])
            past = [b for b in stored if session_date(b[0]) < today]
            have = [b for b in stored if session_date(b[0]) == today]
            # A reply covering less of today than we already hold (e.g. an empty
            # "success") must not erase official bars that superseded live ones.
            if have and (not bars or bars[-1][0] < have[-1][0]):
                continue
            self.leg_bars[leg_key] = past + bars
            changed = True
        if changed:
            self._recompute_history()
            self._push(self.snapshot())

    # ---------------- Live feed ----------------

    async def login(self, totp: str) -> None:
        await self.gw.login(totp)
        self._start_feed()

    def _start_feed(self) -> None:
        if self._feed_task and not self._feed_task.done():
            self._feed_task.cancel()
        self._feed_task = asyncio.create_task(self._run_feed())

    async def _run_feed(self) -> None:
        while self.gw.logged_in:
            u = self.built_u
            legs = [leg for c in self.charts for leg in c.legs] + ([self.future] if self.future else [])
            tokens = [WsToken(leg["segment"], leg["token"]) for leg in legs]
            # Volume while not subscribed (a drop, another index, yesterday) is not this
            # minute's: each leg's first packet sets a fresh baseline.
            for leg in legs:
                self._volume_mark.pop(leg["key"], None)
                self.day_volume.pop(leg["key"], None)
            self._set_feed("connecting", "Connecting to the live feed...")
            try:
                async with self.gw.create_feed() as ws:
                    if tokens:  # full scrip packets carry the day volume that VWAP needs
                        await ws.subscribe_scrips(tokens)
                    await ws.subscribe_index([WsToken(u.index_segment, u.index_token)])
                    self._set_feed("live")
                    async for message in ws:
                        self._on_feed(message)
            except asyncio.CancelledError:
                raise
            except AuthenticationError as exc:
                log.warning("Feed authentication failed: %s", exc)
                self.gw.logged_in = False
                self._set_feed("offline", "Session expired - enter a fresh TOTP to go live.")
                return
            except Exception as exc:  # reconnect on anything else
                log.exception("Feed error")
                self.feed_message = str(exc)[:200]
            self._set_feed("reconnecting", f"Feed dropped, reconnecting in {FEED_RECONNECT_DELAY}s...")
            await asyncio.sleep(FEED_RECONNECT_DELAY)

    def _on_feed(self, message) -> None:
        if isinstance(message, (SFeedScrip, SFeedScripLite)):
            leg_key = f"{message.exchange_segment}|{message.instrument_token}"
            if message.last_traded_price > 0:
                self.ltp[leg_key] = message.last_traded_price
                if self.future and leg_key == self.future["key"]:
                    self.future_ltp = message.last_traded_price
                self._tick_dirty = True
            if isinstance(message, SFeedScrip):
                self.day_volume[leg_key] = message.volume_traded_today
                self._volume_mark.setdefault(leg_key, message.volume_traded_today)
        elif isinstance(message, SFeedIndex):
            if message.last_traded_price > 0:
                self.spot = message.last_traded_price
                if message.close_price > 0:
                    self.spot_prev_close = message.close_price
                self._tick_dirty = True

    def _straddle_ltp(self, chart: Chart) -> float | None:
        if chart.error:
            return None
        ce, pe = self.ltp.get(chart.ce["key"]), self.ltp.get(chart.pe["key"])
        return round(ce + pe, 2) if ce is not None and pe is not None else None

    def _minute_volume(self, chart: Chart) -> int:
        """CE + PE volume traded since the last minute close."""
        total = 0
        for k in chart.leg_keys:
            day = self.day_volume.get(k)
            if day is None:
                continue
            mark = self._volume_mark.get(k, day)
            # The exchange's day volume restarts at zero each session.
            total += day - mark if day >= mark else day
        return total

    def _live_json(self) -> dict | None:
        """The forming minute's straddle prices, only while the feed is live."""
        if self.feed_state != "live":
            return None
        minute = int(minute_start(now_ist()).timestamp())
        return {
            "minute": minute,
            "straddles": {c.key: self._straddle_ltp(c) for c in self.charts},
            "volumes": {c.key: self._minute_volume(c) for c in self.charts},  # so far this minute
            "legs": {k: self.ltp.get(k) for c in self.charts for k in c.leg_keys},
        }

    def _close_minute(self, minute: datetime) -> None:
        """Turn the LTPs at a minute boundary into that minute's bar."""
        if self.feed_state != "live":
            return
        try:
            if minute.weekday() >= 5 or not in_session(minute):
                return
            # A window without this day is final only when built for this day after the
            # open (a holiday). One from an earlier build (overnight, the 09:15 minute
            # before the 09:16 rollover) still takes the bar: the rebuild carries it over.
            if minute.date() not in self.window_dates and self.built_for == (minute.date(), True):
                return
            ts = int(minute.timestamp())
            for chart in self.charts:
                price = self._straddle_ltp(chart)
                series = chart.series()
                if price is None or (series and series[-1][0] >= ts):
                    continue
                bar = (ts, price, self._minute_volume(chart))
                chart.live.append(bar)
                self._push({"type": "bar", "generation": self.generation, "key": chart.key, "bar": list(bar)})
        finally:
            # Every minute starts counting volume afresh, bar or not.
            self._volume_mark.update(self.day_volume)

    # ---------------- Clock ----------------

    async def _loop(self) -> None:
        prev_minute = minute_start(now_ist())
        last_reconcile: datetime | None = None
        while True:
            await asyncio.sleep(LOOP_INTERVAL)
            try:
                now = now_ist()
                minute = minute_start(now)
                if minute > prev_minute:
                    if minute - prev_minute == timedelta(minutes=1):
                        self._close_minute(prev_minute)
                    else:
                        # The loop stalled (e.g. the machine slept): the prices and volume
                        # now on hand are not that minute's. Skip it; the refresh brings
                        # the official candles.
                        log.warning("Clock stalled from %s to %s; skipping that minute", prev_minute, minute)
                        self._volume_mark.update(self.day_volume)
                    prev_minute = minute
                    self._push(self.status_json())  # keeps totpRequired current

                if self._tick_dirty and self.feed_state == "live":
                    self._tick_dirty = False
                    self._push(
                        {
                            "type": "tick",
                            "generation": self.generation,
                            "spot": self.spot,
                            "spotPrevClose": self.spot_prev_close,
                            "future": self._future_json(),
                            "live": self._live_json(),
                        }
                    )

                building = self._build_task and not self._build_task.done()
                if (
                    not building
                    and now.weekday() < 5
                    and now.time() >= time(9, 16)
                    and self.built_for != (now.date(), True)
                    and (
                        self._recheck_at is None
                        or self._recheck_at.date() != now.date()
                        or now >= self._recheck_at
                    )
                    and clock.monotonic() - self._last_auto_rebuild > REBUILD_RETRY_DELAY
                ):
                    log.info("New session - rebuilding charts")
                    self._last_auto_rebuild = clock.monotonic()
                    self.rebuild()
                    continue

                due = (
                    time(9, 17) <= now.time() <= time(15, 31) and now.minute % RECONCILE_EVERY_MIN == 2
                ) or now.time().replace(second=0, microsecond=0) == time(15, 36)
                reconciling = self._reconcile_task and not self._reconcile_task.done()
                if (
                    due
                    and minute != last_reconcile
                    and not building
                    and not reconciling
                    and self.charts
                    and self.phase in ("ready", "error")  # a failed rebuild keeps the old charts live
                    and now.date() in self.window_dates
                ):
                    last_reconcile = minute
                    self._reconcile_task = asyncio.create_task(self._reconcile())
            except Exception:  # the clock must never die
                log.exception("Clock loop error")
