# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`pnf/` is a web app on top of the Kotak Neo trading SDK (`kotakneoapi` 3.0.6, imported as `neo_api_client`).
It shows three NIFTY straddle Point & Figure charts (ATM - 200, ATM, ATM + 200) side by side, from 1-min history plus the live WebSocket feed, with VWAP, double top buy / double bottom sell markers, and alerts.
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
`.env`, `.pnf-config.json` and `logs/` are gitignored and must never be committed (the repo is public).

The SDK writes its own logs to `logs/neo-api-client.log` (rotated daily).

## P&F web app architecture (`pnf/`)

- `neo.py` - `NeoGateway`: async wrapper over the blocking SDK. Every REST call runs in a thread, serialized with a 1.5s gap (the market-data endpoints 429 on bursts), with backoff retries on 429/5xx.
- `series.py` - pure helpers (IST calendar, session filter, CE+PE straddle series with forward-fill and session-anchored VWAP, ATM rounding, expiry roles). No network; unit-tested.
  - Leg candles are `(ts, close, typical, volume)`; straddle bars are `(ts, close, vwap)`, sent to the browser as `[ts, close, vwap]`.
  - Straddle VWAP = CE VWAP + PE VWAP, each from 1-min typical price x volume, reset at 09:15.
  - Expiry *roles* (`week1`, `week2`, `month1`, `month2`): the next two expiries and the last expiry of the next two months. The user picks a role, not a date, so the choice rolls forward after expiry; a date shared by a weekly and a monthly is listed once under the monthly role.
- `engine.py` - `Engine`, the state machine:
  - *Build* (no login needed): the expiry for the chosen role, ATM = NIFTY open from `quotes()` rounded to 50 (or the user's override), the six option tokens via `search_scrip`, then N sessions of 1-min history per leg. Past days are cached; today is re-fetched.
  - *Window*: the N session dates are fixed at build time and include today once 09:15 passes, so the P&F box size (0.5% of the window's first close) never shifts mid-day.
  - *Live*: TOTP from the browser -> `totp_login`/`totp_validate` -> SFeed WebSocket (`subscribe_scrips` for the legs - full packets, because live VWAP needs `average_trade_price`; `subscribe_index` for `Nifty 50`). A 250ms clock loop pushes ticks and, at each minute boundary, turns the latest CE+PE LTPs (and CE+PE ATP as VWAP) into a live 1-min bar.
  - *Reconcile*: every 5 min in session (and at 15:36) today's official candles are re-fetched and supersede the live-built bars they cover.
  - *Rollover*: at 09:16 on a new weekday the charts rebuild, re-picking the ATM from that day's open.
  - ATM override, day count and expiry role persist in `.pnf-config.json`.
- `app.py` - FastAPI: `/` page, `POST /api/login`, `POST /api/config`, `/ws` push channel. Each browser gets its own ordered queue (snapshot first, then `status`/`tick`/`bar` messages); messages carry `generation` so stale bars from a previous build are ignored.
- `static/pnf.js` - the only P&F construction code (fixed box, integer rows anchored at the first close, 3-box reversal) and signal detection: a double top buy is an X column one box above the previous X column's top, a double bottom sell the mirror for O columns. Each column records `endTs` (its last bar), which is where VWAP is sampled. The backend sends close series; the browser builds the chart, including faded "forming" boxes from the live price.
- `static/alerts.js` - signal alerts: toast (bottom-left), panel border pulse, Web Audio chime (rising for buy, falling for sell; mutable via the bell, stored in `localStorage`), and a system notification when the tab is hidden. Alerts fire only for signals confirmed by a closed 1-min `bar` message, never on snapshots, so reloads and reconciles stay silent.
- `static/chart.js` - canvas renderer that draws only the visible viewport under a native scroller (spacer sized to the full chart), so long histories stay cheap. VWAP is a yellow step line (one step per column at its `endTs` value), broken at session changes.

To exercise the live path outside market hours, run the app with a fake gateway and a shifted clock: `tests/test_engine.py` has `FakeGateway` and `tick()` helpers (full `SFeedScrip` packets with ATP), and `engine.now_ist` is the single clock to monkeypatch.
To restart the server, kill it by port (`kill $(lsof -tiTCP:8765 -sTCP:LISTEN)`): the process name is `Python`, so `pkill -f "python -m pnf"` silently misses it and the new server fails to bind.

## SDK and API behaviour worth knowing

- Market-data calls (`search_scrip`, `historical_data`, `quotes`, `expiries`, `option_chain`) work with only `consumer_key`; trading and the WebSocket need `totp_login()` then `totp_validate(mpin=...)`.
- Scrip names are resolved to instrument tokens with `search_scrip()`, which does a substring match - pick the row whose `pSymbolName` matches exactly (and `pGroup == "EQ"` for cash equity); the token is `pSymbol`, the trading symbol `pTrdSymbol`.
- For options, `search_scrip(exchange_segment="nse_fo", symbol="nifty", expiry="13Oct2026", option_type="CE,PE")` - expiry must be `DDMonYYYY`; `dStrikePrice;` (with the semicolon) is the strike x 100.
- The NIFTY index is `Nifty 50` everywhere: `quotes` token, `historical_data(neosymbol="nse_cm|Nifty 50")`, and `WsToken("nse_cm", "Nifty 50")`; `nse_cm|26000` is rejected.
- `historical_data()` takes `neosymbol="<segment>|<token>"` and whole-date ranges, capped per request: 30 days for 1/3/5min, 60 for 10/15min, 90 for 30/60min, 180 for D/W.
- The backend rejects `fromDate` older than 5 years (422, checked to the second).
- Response `status` casing varies (`SUCCESS`/`success`) and may say "Partial response" while candles are still valid - judge by candle presence.
- Errors arrive in several shapes: `{"code": 429|503}`, `{"fault": {"code": 422}}`, `{"error": [{"message": ...}]}`, `stat == "Not_Ok"`, or `Error`/`errMsg` keys. 429s and 5xx (open circuit breaker) are transient and are retried with backoff; the breaker only resets once the client goes quiet.
- Same-day 1-min candles may be refused while the market is open; the engine then falls back to past days plus live bars.
- Equity 1-min candles: 360-361 per day (09:15-15:14 plus an occasional ~15:29 close candle). NIFTY option candles run 09:15-15:39; `pnf` keeps only 09:15-15:29.
