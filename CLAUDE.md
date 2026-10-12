# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`pnf/` is a web app on top of the Kotak Neo trading SDK (`kotakneoapi` 3.0.6, imported as `neo_api_client`).
It shows three straddle Point & Figure charts side by side for NIFTY (NSE; ATM - 200, ATM, ATM + 200) or SENSEX (BSE; ATM - 500, ATM, ATM + 500), from 1-min history plus the live WebSocket feed, with VWAP, double top buy / double bottom sell markers, and alerts (double bottom sells below VWAP only).
User-facing documentation is in `README.md`.

## Commands

Always use the project virtualenv (Python 3.12); dependencies are pinned in `requirements.txt` (`.venv/bin/pip install -r requirements.txt`).

```sh
.venv/bin/python -m pnf                 # P&F web app on http://127.0.0.1:8765 (--port to change)
.venv/bin/python -m pytest              # Python tests (tests/test_*.py)
.venv/bin/python -m pytest tests/test_engine.py::test_live_minute_bars_and_reconcile   # one test
node --test tests/*.test.mjs            # P&F engine tests (JS); a bare directory argument fails on Node 22
.venv/bin/ruff check && .venv/bin/ruff format --check   # lint/format (config in pyproject.toml, covers pnf/ and tests/)
```

Credentials come from `.env` (template: `.env.sample`): `CONSUMER_KEY`, `MOBILE_NUMBER`, `UCC`, `MPIN`.
`.env`, `.pnf-config.json`, `.pnf-alerts.json` and `logs/` are gitignored and must never be committed (the repo is public).

The SDK writes its own logs to `logs/neo-api-client.log` (rotated daily).

## P&F web app architecture (`pnf/`)

- `neo.py` - `NeoGateway`: async wrapper over the blocking SDK. Every REST call runs in a thread, serialized with a 1.5s gap (the market-data endpoints 429 on bursts), with backoff retries on 429/5xx.
- `series.py` - `Underlying` and `UNDERLYINGS` hold everything exchange-specific (options segment, index segment and token, strike step, side offset); nothing else may hard-code NIFTY or NSE. Also pure helpers (IST calendar, session filter, CE+PE straddle series with forward-fill and session-anchored VWAP, ATM rounding, expiry roles). No network; unit-tested.
  - Leg candles are `(ts, close, volume)`; straddle bars are `(ts, close, volume)` (CE + PE close, CE + PE volume that minute), sent to the browser as `[ts, close, volume]`. VWAP is computed in the browser (see `pnf.js`).
  - Expiry *roles* (`week1`, `week2`, `month1`, `month2`): the next two expiries and the last expiry of the next two months. The user picks a role, not a date, so the choice rolls forward after expiry; a date shared by a weekly and a monthly is listed once under the monthly role.
- `engine.py` - `Engine`, the state machine:
  - *Build* (no login needed): for the chosen underlying, the expiry for the chosen role, ATM = index open from `quotes()` rounded to the strike step (or that underlying's override), the six option legs via `search_scrip`, then N sessions of 1-min history per leg. Past days are cached; today is re-fetched. `built_u` records which underlying the charts show (the requested one can differ while a switch is loading).
  - The near-month future (`NeoGateway.near_future`: first FUT contract expiring on or after today, via `search_scrip(..., option_type="FUT")`) is quoted at build and subscribed on the feed; it is display-only, so a failed lookup logs a warning and the charts still build.
  - *Legs* are keyed `"<segment>|<token>"` (e.g. `bse_fo|1114892`) everywhere - history, LTP/ATP maps, feed messages, browser JSON - because NSE and BSE token numbers can overlap.
  - *Window*: the N session dates are fixed at build time and include today once 09:15 passes, so the P&F box size (0.5% of the window's first close) never shifts mid-day.
  - *Live*: TOTP from the browser -> `totp_login`/`totp_validate` -> SFeed WebSocket (`subscribe_scrips` for the legs - full packets, because live VWAP needs `volume_traded_today`, which lite packets lack; `subscribe_index` for the built underlying's index). A 250ms clock loop pushes ticks and, at each minute boundary, turns the latest CE+PE LTPs into a live 1-min bar; its volume is the rise in each leg's day volume since the previous minute close (`_volume_mark`). Ticks also carry the forming minute's volume so far (`live.volumes`).
  - *Reconcile*: every 5 min in session (and at 15:36) today's official candles are re-fetched and supersede the live-built bars they cover.
  - *Rollover*: at 09:16 on a new weekday the charts rebuild, re-picking the ATM from that day's open.
  - The underlying, a per-underlying ATM override map, day count and expiry role persist in `.pnf-config.json` (older files with a single `atm` number load as the NIFTY override).
- `app.py` - FastAPI: `/` page, `POST /api/login`, `POST /api/config` (leaving `atm` out keeps the underlying's stored choice), `POST /api/alerts` (a browser reports an alert it raised), `/ws` push channel.
  - Alert log: signals are detected in the browser, which posts each alert; `Engine.log_alert` dedupes by key (several tabs report the same signal), keeps the newest 30 in `.pnf-alerts.json`, and pushes an `alerts` message; snapshots carry `alerts`. Tests must monkeypatch `ALERTS_FILE` (the `make_engine` fixture does) or they write to the real log. Each browser gets its own ordered queue (snapshot first, then `status`/`tick`/`bar` messages); messages carry `generation` so stale bars from a previous build are ignored.
- `static/pnf.js` - the only P&F construction code (fixed box, integer rows anchored at the first close, 3-box reversal) and signal detection: a double top buy is an X column one box above the previous X column's top, a double bottom sell the mirror for O columns. Each column records `endTs` (its last bar), which is where VWAP is sampled. `columnSma` gives the 10-column SMA of column midpoints (`pnf.sma`, one price per column, null before 10 columns). `intradayVwap` gives the intraday VWAP per bar (straddle close x (CE vol + PE vol), accumulated from 09:15, reset each IST session - the AlgoTest straddle-VWAP formula, and what Definedge calls "Intra VWAP" on P&F); `pnf.vwap` samples it at each column's `endTs`. Do not change the definition without the user: CE VWAP + PE VWAP is wrong (each leg weighted by its own volume at its own times; on 2026-10-08 it fell below the straddle's entire traded range), a rolling/moving VWAP was rejected as "looks like an MA", and stepped drawing was rejected - draw it as a line through column centres, broken at session starts. The backend sends close series; the browser builds the chart, including faded "forming" boxes from the live price.
- `static/alerts.js` - signal alerts: toast (bottom-left), panel border pulse, Web Audio chime (rising for buy, falling for sell; mutable via the bell, stored in `localStorage`), and a system notification when the tab is hidden. Alerts fire only for signals confirmed by a closed 1-min `bar` message, never on snapshots, so reloads and reconciles stay silent. `pnf.js` `alertWanted` filters alerts (not markers): only a double bottom sell alerts, and only when the confirming close is below the column's VWAP; double top buys never alert. The live page and the simulator both call it.
- `static/chart.js` - canvas renderer that draws only the visible viewport under a native scroller (spacer sized to the full chart), so long histories stay cheap. A signal is drawn as a solid bar over the two columns before the breakout column, in the empty row just beyond the broken level (green above the old top for a buy, red below the old bottom for a sell). VWAP (yellow, broken at session starts) and SMA (light black / grey) are both lines through column centres. Either overlay can be hidden (`setOverlays`; the choice is per browser, in `localStorage`). Axis tags (price, VWAP, SMA) are laid out by `static/axis.js` `placeTags`: priority order, each lower tag takes the nearest free slot.

To exercise the live path outside market hours, run the app with a fake gateway and a shifted clock: `tests/test_engine.py` has `FakeGateway` and `tick()` helpers (full `SFeedScrip` packets with a running day volume), and `engine.now_ist` is the single clock to monkeypatch.
To restart the server, kill it by port (`kill $(lsof -tiTCP:8765 -sTCP:LISTEN)`): the process name is `Python`, so `pkill -f "python -m pnf"` silently misses it and the new server fails to bind.

## SDK and API behaviour worth knowing

- Market-data calls (`search_scrip`, `historical_data`, `quotes`, `expiries`, `option_chain`) work with only `consumer_key`; trading and the WebSocket need `totp_login()` then `totp_validate(mpin=...)`.
- Scrip names are resolved to instrument tokens with `search_scrip()`, which does a substring match - pick the row whose `pSymbolName` matches exactly (and `pGroup == "EQ"` for cash equity); the token is `pSymbol`, the trading symbol `pTrdSymbol`.
- For options, `search_scrip(exchange_segment="nse_fo", symbol="nifty", expiry="13Oct2026", option_type="CE,PE")` - expiry must be `DDMonYYYY`; `dStrikePrice;` (with the semicolon) is the strike x 100.
- Index tokens are names: NIFTY is `Nifty 50` on `nse_cm` (`nse_cm|26000` is rejected), SENSEX is `SENSEX` on `bse_cm`; the same token works for `quotes`, `historical_data` and `WsToken`.
- SENSEX options: `bse_fo`, 100-point strikes, Thursday expiries; `search_scrip` takes the same `DDMonYYYY` expiry and stores strikes x 100 as on NSE. Feed messages carry `exchange_segment` as the same wire names (`bse_fo`, `nse_fo`).
- `historical_data()` takes `neosymbol="<segment>|<token>"` and whole-date ranges, capped per request: 30 days for 1/3/5min, 60 for 10/15min, 90 for 30/60min, 180 for D/W.
- The backend rejects `fromDate` older than 5 years (422, checked to the second).
- Response `status` casing varies (`SUCCESS`/`success`) and may say "Partial response" while candles are still valid - judge by candle presence.
- Errors arrive in several shapes: `{"code": 429|503}`, `{"fault": {"code": 422}}`, `{"error": [{"message": ...}]}`, `stat == "Not_Ok"`, or `Error`/`errMsg` keys. 429s and 5xx (open circuit breaker) are transient and are retried with backoff; the breaker only resets once the client goes quiet.
- Same-day 1-min candles may be refused while the market is open; the engine then falls back to past days plus live bars.
- When same-day candles are served, they include the minute still in progress as a partial candle. `engine.settled()` drops any candle whose minute ended less than `CANDLE_SETTLE_SECONDS` (60s) ago; storing a partial candle as final froze the chart (the browser ignores live prices for a minute that already has a bar, and the server never closes that minute's real bar) - `test_refresh_serving_the_minute_in_progress_does_not_freeze_the_chart` guards this.
- Equity 1-min candles: 360-361 per day (09:15-15:14 plus an occasional ~15:29 close candle). NIFTY option candles run 09:15-15:39; `pnf` keeps only 09:15-15:29.
