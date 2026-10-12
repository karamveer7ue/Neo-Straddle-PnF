# Neo Straddle PnF

Live Point & Figure charts for NIFTY and SENSEX option straddles, built on the [Kotak Neo trade API](https://github.com/Kotak-Neo/kotak-neo-python).

The screen shows three straddles of the chosen index side by side - ATM - 200, ATM and ATM + 200 for NIFTY (NSE), or ATM - 500, ATM and ATM + 500 for SENSEX (BSE) - each as a Point & Figure chart built from 1-minute closes with a 0.5% box and 3-box reversal.
History comes from the Neo historical API, live prices stream over the Neo WebSocket, and every chart carries the intraday VWAP and a 10-column simple moving average (switch either on or off) plus double top buy / double bottom sell signals with on-screen and audio alerts.

![Three NIFTY straddle Point & Figure charts with VWAP and signal markers](docs/screenshot.png)

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup](#setup)
- [Running](#running)
- [Using the screen](#using-the-screen)
- [Alerts](#alerts)
- [Data and timing details](#data-and-timing-details)
- [Development](#development)
- [Security](#security)
- [Limitations](#limitations)

## Features

- **NIFTY or SENSEX:** switch with one click; NIFTY options come from NSE, SENSEX options from BSE.
- **Three straddles at once:** ATM and two side strikes (± 200 for NIFTY, ± 500 for SENSEX), laid out horizontally.
- **Point & Figure on 1-minute closes:** box size fixed at 0.5% of the first straddle close in the window, 3-box reversal.
- **ATM from today's open or your own strike:** the default ATM is the index's open rounded to the nearest strike; type any strike to override it.
- **Expiry picker:** upcoming weekly, next weekly, this month's monthly and next month's monthly.
- **History window:** 1 to 15 trading sessions.
- **Live updates:** after a TOTP login on the page, prices stream in and a new 1-minute bar closes every minute; boxes from the minute still forming are drawn faded.
- **VWAP and SMA 10:** the straddle's intraday VWAP (yellow) and a 10-column simple moving average (light black; soft grey in dark mode), each switchable on or off, also shown in each panel header and on the price axis.
- **Signals:** double top buy and double bottom sell are marked on the charts, with a toast, a panel flash, a chime and (when the tab is in the background) a system notification for each new one.
- **Comfortable charts:** hover any box for its price and the time it printed, zoom in and out, scroll through history, light and dark themes.

## How it works

### Indices

| Index | Options exchange | Strike step | Charts |
| --- | --- | --- | --- |
| NIFTY | NSE (`nse_fo`), index quoted on `nse_cm` as `Nifty 50` | 50 | ATM - 200, ATM, ATM + 200 |
| SENSEX | BSE (`bse_fo`), index quoted on `bse_cm` as `SENSEX` | 100 | ATM - 500, ATM, ATM + 500 |

The NIFTY | SENSEX switch in the top bar changes all three charts.
Each index keeps its own ATM choice; the expiry choice and the number of days apply to both.

### Strikes and expiry

- **ATM:** by default the strike nearest to the day's index open (for example, a NIFTY open of 22,314.95 gives 22,300, and a SENSEX open of 71,776.67 gives 71,800).
  The side charts sit at ATM ± 200 (NIFTY) or ATM ± 500 (SENSEX), and the strikes stay fixed for the whole day.
  At 09:16 on each new trading day the charts rebuild using that day's open.
- **ATM override:** enter any valid strike (a multiple of 50 for NIFTY, 100 for SENSEX) in the ATM box to use it instead.
  The override stays in place for that index, across days and restarts, until you press **Auto**.
- **Expiry:** pick from up to four contracts: the upcoming weekly, the next weekly, this month's monthly and next month's monthly.
  A monthly expiry is the last listed expiry of its calendar month.
  The app remembers which kind of expiry you picked rather than a fixed date, so after a contract expires the choice moves to the next one by itself.
  When a weekly falls on the monthly date it is listed once, as the monthly.

### The straddle series

Each 1-minute straddle close is the CE close plus the PE close for that minute.
Illiquid options can skip minutes, so each leg carries its last close forward, and a chart starts once both legs have traded.
Only regular-session minutes (09:15 to 15:29) are used.

### Point & Figure rules

- **Box size:** 0.5% of the first straddle close in the history window, then fixed for the whole chart.
  The window's dates are fixed for the day (today counts from 09:15), so the box size never changes mid-session.
- **Boxes:** prices are placed on a grid anchored at that first close.
  An X column extends when a close reaches a new box above its top; an O column extends when a close reaches a new box below its bottom.
- **Reversal:** a new column starts only after a move of 3 full boxes against the current column.

### Signals

- **Double top buy:** an X column rises one box above the top of the previous X column.
- **Double bottom sell:** an O column falls one box below the bottom of the previous O column.

Each signal is marked with a solid bar over the previous two columns at the level it broke: a green bar just above the old top for a buy, a red bar just below the old bottom for a sell.
The column that follows the bar is the breakout; hover its first box beyond the bar to see the signal's name.
The latest signal for each chart is shown as a chip in its panel header.

### VWAP (intraday)

The VWAP line is the straddle's intraday VWAP, the standard used by straddle-chart tools (for example AlgoTest's straddle VWAP and Definedge's "Intra VWAP" for Point & Figure charts):

- each minute's straddle price is the CE close plus the PE close (the same prices the chart is built from),
- each minute is weighted by the volume traded in both legs that minute (CE volume + PE volume),
- VWAP = sum of (price x volume) / sum of volume, accumulated from 09:15 and restarted every session.

It is the average price the straddle has actually traded at so far today.
Point & Figure has no time axis, so each column shows the VWAP as it stood when that column last updated, drawn as a line through the column centres and broken where a new session (and a new VWAP) starts.
The newest column updates live, including the minute still forming.

Because Point & Figure compresses time, a busy sideways morning becomes a few columns while a trend prints many, so the VWAP can sit some distance from the most recent columns; that distance is the point of the line (premium above or below its average traded price for the day).

### Simple moving average (SMA 10)

The SMA follows the usual Point & Figure convention: the average of the midpoints of the last 10 columns, where a column's midpoint is halfway between its top and bottom box.
It has one value per column and is drawn as a line through the column centres.
Because it counts columns rather than minutes, it is steady through sideways chop and runs straight across sessions (unlike VWAP, it does not restart each day).
The line starts once a chart has 10 columns, and the newest column's value moves as that column grows.

### Architecture

```
Kotak Neo REST API ──(history, quotes, expiries, scrip search)──┐
                                                               ├─> Python server (FastAPI) ──WebSocket──> Browser
Kotak Neo WebSocket feed ──(live option prices, the index)─────┘    builds 1-min straddle bars          builds P&F, draws charts,
                                                                    and VWAP, pushes updates            detects signals, alerts
```

The server does the data work: picking strikes and expiry, loading and pacing history requests, logging in, turning live ticks into 1-minute bars, and pushing everything to the browser.
The browser builds the Point & Figure charts, detects signals and raises the alerts.

## Requirements

- Python 3.12.
- A Kotak Neo trading account with API access: a consumer key, your registered mobile number, UCC (client code), MPIN, and an authenticator app set up for TOTP.
- Node.js 18+ only if you want to run the JavaScript tests.

## Setup

```sh
git clone https://github.com/karamveer7ue/Neo-Straddle-PnF.git
cd Neo-Straddle-PnF

python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.sample .env
```

Then edit `.env` with your own credentials:

```ini
CONSUMER_KEY=your-consumer-key
MOBILE_NUMBER=+91XXXXXXXXXX
UCC=YOURUCC
MPIN=123456
```

`.env` is listed in `.gitignore`; never commit it.

## Running

```sh
.venv/bin/python -m pnf              # serves http://127.0.0.1:8765
.venv/bin/python -m pnf --port 9000  # any other port
```

Open the address in a browser.
Charts load from history straight away; this needs only the consumer key, not a login.
Loading takes about 10 to 15 seconds, because history requests are spaced out to stay within the API's rate limits.

**Going live:** live prices need a full Neo session.

- During market hours (09:00 to 15:30 on weekdays) a banner asks for the 6-digit TOTP from your authenticator app.
  Enter it in the top bar; the server logs in with the TOTP plus the MPIN from `.env`, and the status pill turns to **Live**.
- Outside market hours the page shows history only, with a **Go live** button if you want to connect anyway.
- The session lives in the server process, so reloading the page does not ask again.
  Restarting the server, or the session expiring (it lasts for the trading day), means entering a fresh TOTP.

## Using the screen

| Control | What it does |
| --- | --- |
| **NIFTY / SENSEX** | Switches all three charts to the other index. |
| **Expiry** | Switches all three charts to another contract. |
| **ATM** | Type a strike (a multiple of 50 for NIFTY, 100 for SENSEX) and press Enter or **Apply**. Esc discards the edit. |
| **Auto** | Goes back to the ATM from today's index open. Hover it to see the open and the strike it gives. |
| **Days** | How many trading sessions of history to show (1 to 15). |
| **VWAP / SMA 10** | Show or hide each overlay on all three charts; both, either or none (remembered by the browser). |
| **- / Fit / +** | Smaller boxes, fit the boxes to the panel height, bigger boxes. |
| **Bell** | Turns alert sounds on or off (remembered by the browser). |
| **Status pill** | **History only**, **Connecting**, **Reconnecting** or **Live**. |

Inside each panel:

- **Header:** straddle price, change since the session's first close, CE and PE prices, VWAP and SMA 10 (when shown), and the latest signal.
- **Meta line:** box size, reversal, number of columns, and the first session in the window.
- **Chart:** the dashed blue line and blue axis tag mark the current price; the yellow and light-black tags mark the current VWAP and SMA 10; dotted vertical lines separate sessions.
- **Hover:** shows the box's price, the minute it printed, the VWAP and SMA 10 at that column, and the signal name if it is a signal box.
- **Scrolling:** charts follow the newest column automatically; scroll back to browse history, and scroll to the right end to resume following.

## Alerts

Alerts fire only when a closed 1-minute bar confirms a new double top buy or double bottom sell.
They never fire for boxes still forming, and reloading the page does not replay old ones.

Each alert gives you:

- a toast in the bottom-left corner (over the oldest history, so it never hides the latest columns),
- a pulsing border on the affected panel,
- a chime: rising notes for a buy, falling notes for a sell,
- a system notification when the tab is in the background, if you have allowed notifications (the browser asks when you go live).

Browsers block sound until you have interacted with the page, so click anywhere once (entering the TOTP counts).
Until then a toast tells you that sound is waiting for a click.

## Data and timing details

- **Rate limits:** the Neo market-data endpoints reject bursts (HTTP 429), so all REST calls go out one at a time, at least 1.5 seconds apart, and are retried with backoff on 429 and 5xx errors.
- **Today's history:** the historical API may refuse same-day candles while the market is open.
  The app then loads the past sessions and fills today from live bars.
- **Refreshing:** every 5 minutes during the session (and once at 15:36), today's official candles are fetched again and replace the live-built bars they cover.
- **Daily rollover:** at 09:16 on each trading day the charts rebuild with the new day's ATM, expiry and history window.
- **Holidays:** a weekday whose candles load fine but come back empty after 09:30 is treated as a market holiday and left out of the window.
- **Saved settings:** the index on screen, each index's ATM override, the day count and the expiry choice are stored in `.pnf-config.json` (gitignored).

## Development

```sh
.venv/bin/python -m pytest                     # Python tests
.venv/bin/python -m pytest tests/test_engine.py::test_live_minute_bars_and_reconcile   # one test
node --test tests/*.test.mjs                   # Point & Figure engine tests
.venv/bin/ruff check && .venv/bin/ruff format --check
```

### Project layout

| Path | Purpose |
| --- | --- |
| `pnf/neo.py` | Async wrapper around the blocking Neo SDK: request pacing, retries, login. |
| `pnf/series.py` | Pure helpers: IST calendar, session filter, straddle series and VWAP, ATM rounding, expiry choices. |
| `pnf/engine.py` | The state machine: building charts, the live feed, minute bars, refreshing, daily rollover. |
| `pnf/app.py` | FastAPI server: the page, `POST /api/login`, `POST /api/config`, and the `/ws` update channel. |
| `pnf/static/pnf.js` | Point & Figure construction and signal detection (the only P&F code). |
| `pnf/static/chart.js` | Canvas chart renderer (draws only the visible area, so long histories stay fast). |
| `pnf/static/axis.js` | Price-axis tag layout, so the price, VWAP and SMA tags never overlap. |
| `pnf/static/alerts.js` | Toasts, sounds and system notifications. |
| `pnf/static/app.js` | Page wiring: controls, server messages, panel headers. |
| `tests/` | Python tests (including the engine against a fake gateway) and JavaScript tests for the P&F engine. |

### Testing the live path outside market hours

`tests/test_engine.py` contains a `FakeGateway` and a `tick()` helper that builds feed packets.
`engine.now_ist` is the only clock the engine reads, so you can run the app against the fake gateway with a shifted clock to watch live bars, VWAP and alerts at any time.

## Security

- The server listens on `127.0.0.1` only, because it holds a live trading session; do not expose it to a network.
- Credentials are read from `.env` and are never sent to the browser.
- The TOTP is used once to log in and is not stored.
- The app only reads market data; it never places orders.

## Limitations

- NIFTY and SENSEX only, with fixed side-strike distances (± 200 and ± 500).
- Box size (0.5%), reversal (3 boxes) and the SMA period (10 columns) are fixed in the code (`pnf/static/pnf.js`).
- Alerts work only while the page is open in a browser tab.
- This is a charting tool, not investment advice.
  Check signals against your own analysis before trading.
