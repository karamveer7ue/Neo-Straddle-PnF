# Neo Straddle PnF

Live Point & Figure charts for NIFTY, BANKNIFTY and SENSEX option straddles, built on the [Kotak Neo trade API](https://github.com/Kotak-Neo/kotak-neo-python).

The screen shows three straddles of the chosen index side by side - ATM - 200, ATM and ATM + 200 for NIFTY (NSE), or ATM - 500, ATM and ATM + 500 for BANKNIFTY (NSE) and SENSEX (BSE) - each as a Point & Figure chart built from 1-minute closes with a 0.5% box and 3-box reversal.
History comes from the Neo historical API, live prices stream over the Neo WebSocket, and every chart carries the intraday VWAP and a 10-column volume-weighted moving average, VWMA 10 (switch either on or off) plus double top buy / double bottom sell markers, with on-screen and audio alerts when a double bottom sell prints below VWAP.

![Three NIFTY straddle Point & Figure charts with VWAP and signal markers](docs/screenshot.png)

Optionally, each chart can sell its straddle through a Zerodha Kite account, with live P&L and Exit 50% / Exit Full buttons for the positions taken (see [Trading through Kite](#trading-through-kite)).
An algo can also trade the three straddles by itself, by fixed rules, with real orders or on paper (see [Algo trading](#algo-trading)).

A separate [simulator](#simulator) replays past sessions on the same charts at 1X to 200X speed, and can run the algo over them.

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup](#setup)
- [Running](#running)
- [Using the screen](#using-the-screen)
- [Alerts](#alerts)
- [Trading through Kite](#trading-through-kite)
- [Algo trading](#algo-trading)
- [Data and timing details](#data-and-timing-details)
- [Simulator](#simulator)
- [Development](#development)
- [Security](#security)
- [Limitations](#limitations)

## Features

- **NIFTY, BANKNIFTY or SENSEX:** pick the index from the menu at the left of the top bar; NIFTY and BANKNIFTY options come from NSE, SENSEX options from BSE.
- **Spot and future:** the index price and the near-month future price, each with its change since the previous close in points and %, live in the top bar.
- **Three straddles at once:** ATM and two side strikes (± 200 for NIFTY, ± 500 for BANKNIFTY and SENSEX), laid out horizontally.
- **Point & Figure on 1-minute closes:** box size fixed at 0.5% of the first straddle close in the window, 3-box reversal.
- **ATM from today's open or your own strike:** the default ATM is the index's open rounded to the nearest strike; type any strike to override it.
- **Expiry picker:** upcoming weekly, next weekly, this month's monthly and next month's monthly (BANKNIFTY has monthlies only, so just the two monthlies).
- **History window:** 1 to 15 trading sessions.
- **Live updates:** after a TOTP login on the page, prices stream in and a new 1-minute bar closes every minute; boxes from the minute still forming are drawn faded.
- **VWAP and VWMA 10:** the straddle's intraday VWAP (yellow) and a 10-column volume-weighted moving average (light black; soft grey in dark mode), each switchable on or off in the **View** menu, also shown in each panel header and on the price axis.
- **Signals:** double top buy and double bottom sell are marked on the charts.
  A new double bottom sell with the straddle below its VWAP raises a toast, a panel flash, a chime and (when the tab is in the background) a system notification; double top buys, and sells at or above VWAP, are marked only.
- **Trading through Kite (optional):** a quantity field and Sell button under each chart, live P&L for the positions taken, and Exit 50% / Exit Full on the charts holding one.
- **Algo trading (optional):** sells a chart's straddle by itself on a double bottom sell below VWAP and VWMA 10, books half at a third of the target and the rest at the target, gets out on an X above VWAP or VWMA 10, and closes everything at a set time; a paper mode and the simulator let you try it first (see [Algo trading](#algo-trading)).
- **Comfortable charts:** hover any box for its price and the time it printed, zoom in and out, scroll through history, light and dark themes.
- **Simulator:** replay past sessions minute by minute at 1X, 10X, 50X, 100X or 200X, with the same charts, overlays and signals, practise selling the straddles with live P&L, and watch the algo trade past days (see [Simulator](#simulator)).

## How it works

### Indices

| Index | Options exchange | Strike step | Charts | Expiries |
| --- | --- | --- | --- | --- |
| NIFTY | NSE (`nse_fo`), index quoted on `nse_cm` as `Nifty 50` | 50 | ATM - 200, ATM, ATM + 200 | weekly and monthly |
| BANKNIFTY | NSE (`nse_fo`), index quoted on `nse_cm` as `Nifty Bank` | 100 | ATM - 500, ATM, ATM + 500 | monthly only |
| SENSEX | BSE (`bse_fo`), index quoted on `bse_cm` as `SENSEX` | 100 | ATM - 500, ATM, ATM + 500 | weekly and monthly |

The index menu in the top bar changes all three charts.
Each index keeps its own ATM and expiry choice; the number of days applies to all of them.

### Strikes and expiry

- **ATM:** by default the strike nearest to the day's index open (for example, a NIFTY open of 22,314.95 gives 22,300, and a SENSEX open of 71,776.67 gives 71,800).
  The side charts sit at ATM ± 200 (NIFTY) or ATM ± 500 (BANKNIFTY, SENSEX), and the strikes stay fixed for the whole day.
  At 09:16 on each new trading day the charts rebuild using that day's open.
- **ATM override:** enter any valid strike (a multiple of 50 for NIFTY, 100 for BANKNIFTY and SENSEX) in the ATM box to use it instead.
  The override stays in place for that index, across days and restarts, until you press **Auto**.
- **Expiry:** pick from up to four contracts: the upcoming weekly, the next weekly, this month's monthly and next month's monthly.
  BANKNIFTY has no weeklies, so it offers only this month's and next month's monthly.
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

### Volume-weighted moving average (VWMA 10)

The VWMA averages the midpoints of the last 10 columns, where a column's midpoint is halfway between its top and bottom box, as the usual Point & Figure moving average does.
Each midpoint is weighted by the CE + PE volume of the minutes that built that column, so busy columns pull the line harder than quiet ones.
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
The [algo](#algo-trading) runs in the server with its own copy of the Point & Figure rules (`pnf/pnf.py`, checked against the browser's by the tests), so it trades the charts exactly as the page draws them.

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

To trade from the charts through Zerodha Kite, also fill in `KITE_API_KEY` and `KITE_API_SECRET` (see [Trading through Kite](#trading-through-kite)); left empty, the app runs without trading.

## Running

### Starting everything with `start.sh`

The easiest way is to start the live app and the [simulator](#simulator) together, from the project folder:

```sh
./start.sh
```

```text
Live app:  https://127.0.0.1:8765
Simulator: https://127.0.0.1:8766
Press Ctrl+C to stop both.
[live] INFO:     Uvicorn running on https://127.0.0.1:8765 (Press CTRL+C to quit)
[sim]  INFO:     Uvicorn running on https://127.0.0.1:8766 (Press CTRL+C to quit)
```

- **Output:** both servers log to the same terminal, each line tagged `[live]` or `[sim]`.
- **Stopping:** Ctrl+C stops both cleanly; if one of them exits (or crashes), the other is stopped too, so you never end up with half the setup running.
- **Checks first:** it stops with a clear message if the virtualenv (`.venv`) or the `.env` file is missing (see [Setup](#setup)).
- **A server already running:** if something is already listening on a port, `start.sh` names it and asks what to do:

  ```text
  The live app is already running on port 8765 (PID 89522).
  Restarting it ends its session: you will need a fresh TOTP.
  [r]estart or [s]kip?
  ```

  - **r (restart)** stops that server and starts a fresh one with the current code.
    Restarting the live app ends its Neo session, so the page asks for a new TOTP.
  - **s (skip)** leaves it running untouched and starts only the other server; Ctrl+C then stops only the one `start.sh` started.
    If both are skipped, it says "Nothing to start." and exits.
  - Any other answer asks again.
  - Without a terminal to ask (for example, when run from another script), it stops instead of guessing; pass `--restart` or `--skip`.

Options:

| Option | What it does |
| --- | --- |
| `--restart` | Restart servers that are already running, without asking. |
| `--skip` | Leave servers that are already running alone, without asking. |
| `--live-port N` | Port for the live app (default 8765). |
| `--sim-port N` | Port for the simulator (default 8766); must differ from the live app's. |
| `-h`, `--help` | Show the usage. |

```sh
./start.sh --restart                         # e.g. after pulling new code
./start.sh --live-port 9000 --sim-port 9001  # other ports
```

Both servers listen on `127.0.0.1` only (see [Security](#security)).

### HTTPS

Both servers are served over HTTPS (Kite only returns to an `https` address after login), so open `https://127.0.0.1:8765`; plain `http://` no longer answers.

- **Certificate:** on first start the app creates a certificate for `127.0.0.1` and `localhost` in `certs/` (gitignored), valid for a year and renewed automatically 30 days before it expires.
- **Browser warning:** the certificate is the app's own, so the first visit shows "Your connection is not private".
  Click **Advanced**, then **Proceed to 127.0.0.1**; the browser remembers the choice for a while.
- **No warning at all (recommended):** let your Mac trust the certificate once (it asks for your password):

  ```sh
  security add-trusted-cert -r trustRoot -k ~/Library/Keychains/login.keychain-db certs/localhost.pem
  ```

  Restart the browser afterwards.
  Repeat this after the yearly renewal (a new `certs/localhost.pem`); to undo it, delete "Straddle P&F (127.0.0.1)" in Keychain Access.
- **Your own certificate:** a valid pair placed at `certs/localhost.pem` and `certs/localhost-key.pem` (for example from [mkcert](https://github.com/FiloSottile/mkcert): `mkcert -cert-file certs/localhost.pem -key-file certs/localhost-key.pem 127.0.0.1 localhost ::1`) is used instead.

### Starting the live app on its own

```sh
.venv/bin/python -m pnf              # serves https://127.0.0.1:8765
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
| **Index** (NIFTY / BANKNIFTY / SENSEX) | Switches all three charts to another index, with that index's own ATM and expiry choice. The expiry menu waits until the new charts have loaded. |
| **Spot / Fut** | The index price and the near-month future (e.g. NIFTY26OCTFUT; hover for the contract and expiry), with their change since the previous close in points and % (just % on windows narrower than 1180px). The future rolls to the next month after expiry. |
| **Expiry** | Switches all three charts to another contract. |
| **ATM** | Type a strike (a multiple of 50 for NIFTY, 100 for BANKNIFTY and SENSEX) and press Enter or **Apply**. Esc discards the edit. |
| **Auto** | Goes back to the ATM from today's index open. Hover it to see the open and the strike it gives. |
| **View** | Opens a menu with the chart settings below; Esc or a click elsewhere closes it. |
| **View > VWAP / VWMA 10** | Show or hide each overlay on all three charts; VWAP is on and VWMA 10 off until you change them (remembered by the browser). |
| **View > Box size** | **-** smaller boxes, **Fit** fits the boxes to the panel height (and makes them square again), **+** bigger boxes. |
| **View > Days** | How many trading sessions of history to show (1 to 15). |
| **Bell** | Turns alert sounds on or off (remembered by the browser). |
| **Alert log** | Opens the last 30 alerts and errors; the badge counts new ones. |
| **Algo** | Shows whether the algo is **Off**, on **Paper** or **Live**, and switches it On or Off; its menu holds the algo's settings (see [Algo trading](#algo-trading)). |
| **Status pill** | **History only**, **Connecting**, **Reconnecting** or **Live**. |

Inside each panel:

- **Header:** straddle price, change since the session's first close, CE and PE prices, VWAP and VWMA 10 (when shown), and the latest signal.
- **Expand** (the icon right of the price): widens that chart over the other two; the top bar stays as it is and the chart keeps its height.
  The icon again, or Esc, brings back all three.
  The other two charts keep updating behind it, and their alerts (toast, sound, notification) still show on top.
  Windows narrower than 1100px stack the charts at full width, so there the icon is hidden.
- **Meta line:** box size, reversal, number of columns, and the first session in the window.
- **Chart:** the dashed blue line and blue axis tag mark the current price; the yellow and light-black tags mark the current VWAP and VWMA 10; dotted vertical lines separate sessions.
- **Hover:** shows the box's price, the minute it printed, the VWAP and VWMA 10 at that column, and the signal name if it is a signal box.
- **Scrolling:** charts follow the newest column automatically; scroll back to browse history, and scroll to the right end to resume following.
  Expanding or restoring a chart also takes it back to the newest column.
- **Vertical stretch:** drag any chart's price axis up to make the boxes of all three charts taller, down to make them shorter; the columns keep their width.
  A chart following its newest column keeps its current price in place; otherwise the price you grabbed (or, on the other charts, the middle) stays put.
  Double-click an axis (or press **Fit**) for square boxes again.
  The stretch carries over when the charts are rebuilt and lasts until the page reloads.

## Alerts

Only double bottom sells raise alerts, and only when a closed 1-minute bar confirms the signal with the straddle below its VWAP at that moment (the confirming minute's close against the VWAP then); the alert says where VWAP was.
Double top buys, and double bottom sells at or above VWAP, are still marked on the charts but raise no alert.
They never fire for boxes still forming, and reloading the page does not replay old ones.

Each alert gives you:

- a toast in the bottom-left corner (over the oldest history, so it never hides the latest columns) that closes itself after 12 seconds, with a thin bar counting down; hovering keeps it open, and the alert log keeps the history,
- a pulsing border on the affected panel,
- a chime: rising notes for a buy, falling notes for a sell,
- a system notification when the tab is in the background, if you have allowed notifications (the browser asks when you go live).

**Alert log:** the list button next to the bell opens the last 30 alerts, newest first, with the index, chart, strike, expiry, the level broken, the breakout price and the bar time.
Errors shown on the page (for example a Kite order not placed or rejected) are saved there too, marked with an amber **!**, with the straddle, the reason and the time.
So are the algo's actions and alarms (see [What the page shows](#what-the-page-shows)).
A red badge on the button counts alerts you have not seen yet; opening the log clears it.
The log is kept by the server in `.pnf-alerts.json` (gitignored), so it survives page reloads and server restarts, and an alert seen by several open tabs is logged once.
Alerts are detected in the browser, so they are only logged while at least one page is open.

Browsers block sound until you have interacted with the page, so click anywhere once (entering the TOTP counts).
Until then a toast tells you that sound is waiting for a click.

## Trading through Kite

With a Zerodha Kite Connect app's key and secret in `.env`, the page can place orders:

- **Under each chart:** enter a quantity (in units, a multiple of the lot size) and press **Sell**; the chart's CE and PE are sold at market at once, with no confirmation.
- **On charts holding a position:** its live P&L and **Exit 50%** / **Exit Full**; charts without a position have no exit buttons.
- **Under the top bar:** every straddle traded from the page with its quantity, sold-at and current straddle price, P&L, and the total.
- **Top bar:** **Connect Kite** to log in (once a day), then **TradeOn**.

Setup (Kite Connect app, redirect URL, static IP registration), how orders are sized and placed, and how positions are tracked are described in [kite/README.md](kite/README.md).

## Algo trading

The algo trades the three charts' straddles by itself, by fixed rules.
It sells a straddle on a double bottom sell below VWAP and VWMA 10, books half the profit at a third of the target and the rest at the full target, and gets out on an X above the lower of VWAP and VWMA 10.
It places its orders through the same Kite connection as the Sell buttons (see [Trading through Kite](#trading-through-kite)), or trades on paper only.

It runs in the server, not in the page.
It therefore keeps working while the page is closed or in a background tab, acts only once however many tabs are open, and picks up its open trades again after a restart.
Keep the page open in a tab anyway: that is where its sounds and pop-up alerts play.

### The Algo menu

The **Algo** button in the top bar shows **Off**, **On · Paper** or **On · Live**, and **Off · managing 1** while it still manages a trade it is no longer taking new ones for (after a restart, for example).
Clicking it switches the algo On or Off; the arrow next to it opens the Algo menu.
The menu holds the settings below, today's algo trades with their P&L (an open one with its own **Exit** button), and an **Exit all algo trades** button that buys back every open algo trade at market at once and switches the algo Off.

| Setting | Default | Meaning |
| --- | --- | --- |
| **Mode** | Paper | **Paper** records the trades the algo would make, at the chart's prices, without placing any order; **Live** places real orders through Kite. It can be changed only while the algo is Off and manages no trade. |
| **Quantity** | NIFTY 1690, BANKNIFTY 720, SENSEX 480 | Units of the CE and of the PE sold per trade, set for each index: whole lots, at least 2 lots (so a trade can be halved), and at most twice the default (see [Size cap](#size-cap)). |
| **Target Points** | NIFTY 30, BANKNIFTY 99, SENSEX 90 | Straddle points (CE + PE) at which the whole trade is booked; half is booked at a third of them. |
| **Simultaneous Trades** | 1 | The most algo trades open at once across the three charts (1 to 3); one chart never has more than one. |
| **Last entry** | 14:45 | No new trades after this time; open trades are still managed. |
| **Exit Time** | 15:12 | Every open algo trade is bought back at this time, in profit or in loss, and the algo switches Off for the day (at the latest 15:25, before Zerodha's 15:26 square-off). |
| **Max loss per trade** | ₹1,00,000 | A trade that has lost this much is bought back at once. |
| **Daily loss limit** | ₹1,00,000 | When the day's algo trades have lost this much in all, every open algo trade is bought back and the algo switches Off for the day. |
| **Max trades per day** | 5 | No new trades after this many in a day. |

Quantity and Target Points are set for the index on screen; the rest apply to every index.
A setting is saved as soon as you leave its field or press Enter (Esc undoes the edit), and a value that cannot be used is refused with the reason under the field.
Quantity, Target Points and Max loss per trade are fixed for a trade when it opens, so changing them affects only new trades; the other settings apply at once.
At the defaults a trade is 26 lots of NIFTY (65 each), 24 lots of BANKNIFTY (30 each) or 24 lots of SENSEX (20 each), about half of what one order may carry on each exchange.

### Entry

At every 1-minute close, the algo sells a chart's straddle when all of these hold:

1. That close confirms a double bottom sell: an O column falls one box below the previous O column's bottom.
2. The close is below VWAP and below VWMA 10.
3. That chart has no open algo trade, and fewer algo trades are open than **Simultaneous Trades** allows.
4. Fewer than **Max trades per day** trades were opened today, the daily loss limit has not been hit, and it is **Last entry** or earlier.
5. No position of your own (from a Sell button or the Kite app) is open on that straddle (checked whenever Kite is connected, in Paper mode too).

In Live mode it sells the CE and then straight away the PE, at market, as MIS, the index's **Quantity** of each, with the trade's own tag on both orders in Kite (`pnfalgo001` for the day's first trade, and so on).
In Paper mode the trade is recorded at that close.
When more charts qualify in the same minute than there are free slots, the ATM chart goes first, then the lower strike, then the higher.
The trade's entry price is the straddle price actually sold at: the CE's average fill plus the PE's.

### Exits

| Rule | When | Then |
| --- | --- | --- |
| **Stop** | At a 1-minute close, the newest column is X and its highest X box is above the lower of VWAP and VWMA 10 | Buy back everything |
| **First target** | A 1-minute close at or below the entry price minus a third of **Target Points** | Buy back half, and add a breakeven stop at the entry price for the rest |
| **Breakeven** | After the first target, a 1-minute close at or above the entry price | Buy back the rest |
| **Full target** | A 1-minute close at or below the entry price minus **Target Points** | Buy back the rest |
| **Max loss** | The trade's loss (booked and open, at the live price) reaches **Max loss per trade** | Buy back everything at once |
| **Daily loss limit** | The day's algo loss (booked and open, at live prices) reaches **Daily loss limit** | Buy back every algo trade; the algo switches Off for the day |
| **Exit Time** | The clock reaches **Exit Time** | Buy back everything; the algo switches Off for the day |

- **Whichever comes first:** after the first target, the stop keeps working next to the breakeven stop, so the rest goes on the first of the two.
- **Prices:** a close is the chart's 1-minute close (CE + PE), the price the chart is built from.
  VWAP and VWMA 10 are taken at the newest column, as the chart draws them, and move every minute.
- **Odd lots:** half of an odd number of lots is rounded up at the first target (25 lots: 13, then 12).

Example: NIFTY 1690 (26 lots) sold at 180.00, with VWAP at 186.00 and VWMA 10 at 184.20.

- An X box above the lower of the two lines (184.20 at first) buys back all 1690.
- A close at 170.00 or below buys back 845 (13 lots); from then on, a close at 180.00 or above buys back the other 845.
- A close at 150.00 or below buys back the last 845.
- A live price of 239.17 (a loss of ₹1,00,000 on 1690) buys back everything at once.
- At 15:12, whatever is still open is bought back.

### Switching it On and Off

- **On:** needs today's charts (rebuilt at 09:16 from the day's open), the live feed (the TOTP login) and, in Live mode, Kite connected.
  Before it starts, the button shows what will be traded (index, expiry, the three strikes, quantity, target and mode) for you to confirm.
- **What it trades:** the index, expiry and strikes on screen.
  While the algo is On or managing a trade, the index, expiry, ATM and Days controls are locked, so the charts keep showing the straddles it trades.
  Should the charts change anyway, it takes no new trades until you switch it On again.
- **Off:** stops everything.
  Open algo trades are left to you, with no targets or stops, not even the Exit Time; if any are open, the button asks before switching Off.
- **Every day starts Off:** after the Exit Time the algo switches itself Off; switch it On again the next day once the charts, the live feed and Kite are ready.
- **After a restart:** the algo is Off, so it opens no new trades, but it goes on managing a trade that was open (targets, stops and the Exit Time) until that trade closes.

### When something goes wrong

- **An entry leg fails** (for example Kite rejects the PE for lack of margin, or a leg does not fill within 10 seconds): the leg already sold is bought back at once and you get an alert.
  The algo then takes no new trades until you switch it On again, since the cause needs you; any other open algo trade is still managed.
- **An exit order that Kite leaves unfilled** for 10 seconds (for example beyond Kite's market-protection band) is cancelled and sent again.
  Every buy-back is sized again from Kite's positions at that moment and never exceeds what Kite shows short, so a short position can never turn long, however often an exit is retried.
- **An exit Kite rejects** is sent again after 5 seconds, then 10, 20 and so on, up to a minute apart, with Kite's reason in the alarm; pressing **Exit** sends it again at once.
- **An order whose reply is lost** (a timeout): the algo looks for it in Kite's order book by its tag, and counts it as not placed if Kite has not listed it after 15 seconds.
  Should Kite list it later after all, the algo still takes it in and buys back what it left short.
- **A crash or restart while orders are on their way:** the algo saves each order before sending it, and every order with a trade's tag in Kite's order book counts as that trade's.
  After the restart it finds the orders that went through; a leg Kite never received counts as not placed, which fails the entry and buys the other leg back.
- **The live feed drops, Kite disconnects, or an exit does not go through while an algo trade is open:** a bar under the top bar says what is wrong, and a sound and an alert repeat every 10 seconds until it is fixed.
  Nothing is bought back because of it (no target or stop can fire without live prices), but the Exit Time still applies.
  A feed connection that dies without a word (the network gone, say) is noticed too: in session, 30 seconds without a packet count as a drop, and the feed reconnects rather than showing stale prices as live.
- **The internet goes down:** the feed and Kite both drop, with the alarms above; the feed reconnects by itself and Kite's polling resumes when the connection is back, and any exit owed meanwhile (the Exit Time, say) is sent then.
  A trade Zerodha squared off at 15:26 during the outage is recognised from its order book afterwards.
- **The server crashes or is restarted:** its state (settings, today's trades and their orders) is saved to `.pnf-algo.json` as it changes, and written whole or not at all.
  Back up, the algo is Off for new trades, finds its orders in Kite's order book (also the ones whose reply the crash cut off), and goes on managing an open trade once you have entered the TOTP (it alarms until then, and Kite must be connected).
- **Two servers at once** (an old one left running on another port, say): only the first holds the `.pnf-algo.lock` file, and the other's algo stays off and says so.
- **A setting that would act at once:** an Exit Time already past is refused while the algo is On or managing a trade, since it would buy everything back that instant; use **Exit all algo trades** for that.
- **You close an algo trade yourself** (with the Exit buttons under the chart or on its card, or in the Kite app): the algo notices from Kite's positions and the order that did it, manages only what is left, and stops once nothing is.
  If Kite's positions show less short than the algo holds and no order explains it, the algo changes nothing and raises an alarm instead.
- **A position from the algo's orders that no open algo trade holds** (for example after its saved state was lost), or a leg bought back past zero: an alarm names the contract, for you to check in Kite.
- **A live trade still open at midnight** (the app could not exit it): Zerodha squared it off at 15:26, so the algo lets it go, with an alert to check Kite's positions.
- **Zerodha's backstop:** algo trades use the MIS product, so if the app cannot exit at all (a crash, the Mac asleep, no connection), Zerodha squares them off at 15:26, charging ₹50 + GST per order.
  The Sell buttons keep using NRML.

### Size cap

Every sell from this app, by the algo or with a Sell button, is capped at twice the algo's default quantity: NIFTY 3,380, BANKNIFTY 1,440 and SENSEX 960 units.
This replaces the earlier `KITE_MAX_LOTS` setting (20 lots).

### Practising first

- **Paper mode** runs the same rules on the live market and records each trade at the chart's prices, without placing orders; slippage and charges are left out.
- **The simulator** replays past days with the same algo (see [The algo in the simulator](#the-algo-in-the-simulator)).

### What the page shows

- **A line on each chart** (under its header): **Algo paper** or **Algo live**, then what the algo does there: watching for a double bottom sell, the open trade (quantity, entry, its stop and targets) with its live P&L and an **Exit** button that buys it back at market at once, or the last trade's result.
- **Marks on the chart** for an open trade: dashed lines at its **Stop** (red) and its entry (**Sold**, grey; it becomes the red **BE** breakeven stop once half is booked), dotted green lines at **T1** and **T2**, each with its price on the axis.
  A red triangle pointing down marks where a trade sold, a green one pointing up where it bought back.
- **Toasts and sounds:** a trade opening (two falling notes), part of it booked (two rising notes), a trade closed (three notes), and the alarms.
- **The alert log** keeps every algo action and alarm and, while the algo is On, every double bottom sell that raised an alert but was not traded, with the reason (for example not below VWMA 10, no free slot, or after Last entry).
- Every 5 minutes the charts replace their live-built minutes with the exchange's official candles (see [Data and timing details](#data-and-timing-details)), which can move a box afterwards; the algo's decisions stay as they were made.

## Data and timing details

- **Rate limits:** the Neo market-data endpoints reject bursts (HTTP 429), so all REST calls go out one at a time, at least 1.5 seconds apart, and are retried with backoff on 429 and 5xx errors.
- **Today's history:** the historical API may refuse same-day candles while the market is open.
  The app then loads the past sessions and fills today from live bars.
- **Refreshing:** every 5 minutes during the session (and once at 15:36), today's official candles are fetched again and replace the live-built bars they cover.
- **Daily rollover:** at 09:16 on each trading day the charts rebuild with the new day's ATM, expiry and history window.
- **Holidays:** a weekday whose candles load fine but come back empty after 09:30 is treated as a market holiday and left out of the window.
- **Saved settings:** the index on screen, each index's ATM override and expiry choice, and the day count are stored in `.pnf-config.json` (gitignored); the alert log in `.pnf-alerts.json`; the algo's settings and today's algo trades in `.pnf-algo.json`.

## Simulator

The simulator replays past NIFTY, BANKNIFTY or SENSEX sessions minute by minute on the same three straddle Point & Figure charts as the live page, so you can watch how the straddles, VWAP, VWMA 10 and the double top / double bottom signals developed on a day that has already traded.

![The straddle simulator replaying a NIFTY session at 50X, mid-afternoon](docs/simulator.png)

It is a separate app (`pnf_sim/`), so it never interferes with the live page, and it reuses the live app's code unchanged: the Neo API gateway, the straddle series rules, the Point & Figure engine, VWAP, VWMA 10, signal detection, the chart renderer, the alerts and the algo.
It reads only market data, so it needs just the consumer key from `.env` and never asks for a TOTP.

### Running the simulator

`./start.sh` starts it together with the live app (see [Starting everything with `start.sh`](#starting-everything-with-startsh)).
To run it on its own:

```sh
.venv/bin/python -m pnf_sim              # https://127.0.0.1:8766
.venv/bin/python -m pnf_sim --port 9001  # any other port
```

It can run at the same time as the live app (port 8765); open each in its own tab.
Setup is the same as for the live app (see [Setup](#setup)); only `CONSUMER_KEY` in `.env` is used.

### Setting up a run

1. Pick **NIFTY**, **BANKNIFTY** or **SENSEX** from the index menu.
   Switching index loads that index's expiries first (a second or two); Load waits until they arrive, so a run always uses the index the menu shows.
2. Pick an **expiry**: the next four listed expiries (for BANKNIFTY, which has monthlies only, the next two).
   NIFTY weeklies expire on Tuesdays and SENSEX weeklies on Thursdays; when the exchange moves an expiry for a holiday, the list shows the actual date (for example, 19 Oct 2026 is a Monday).
3. Pick **From** and **To** dates.
   Both must be before today (the page and the server both check this), a run can cover at most 30 days, and the start can be at most 5 years back (the history API's limit).
4. Optionally type an **ATM** strike (a multiple of 50 for NIFTY, 100 for BANKNIFTY and SENSEX); leave it empty to use the strike nearest to the start day's index open.
5. Press **Load**.
   The history requests are paced to stay within the API's rate limits, so loading takes about 15 seconds.
   While it loads, the previous run is paused and cannot be played; if loading fails, the error is shown and the previous run stays as it was.
   The last 8 runs are kept in memory for the day, so loading one again is instant, even if you reloaded the page while it was being built.
6. Press **Play**.
   Nothing moves until you do, and the panels say so.

### Playback controls

| Control | What it does |
| --- | --- |
| **Play / Pause** | Starts or pauses the replay (keyboard: Space). |
| **Back to the start** | Jumps back to just before the From date's first minute (the clock shows "minute 0"), so the whole run, including its first minute, plays again. |
| **1X / 10X / 50X / 100X / 200X** | Playback speed; hover a button to see how long a full session takes. |
| **Timeline** | Drag to jump to any minute; the left and right arrow keys step exactly one minute (also while the timeline has focus). The thin bar under it shows the time left until the current minute completes. |
| **Clock** | The simulated time, to the second, and the minute count (for example, "minute 296 of 375"). |
| **Spot** | The index at the simulated time, with its change since the previous session's close. |
| **Algo** | Runs the live page's algo over the replay, on paper (see [The algo in the simulator](#the-algo-in-the-simulator)). |
| **View (VWAP / VWMA 10, box size), bell, expand** | The same as on the live page; the simulator remembers its own VWAP/VWMA choice. |

Speeds:

| Speed | One market minute takes | A full session (375 minutes) takes |
| --- | --- | --- |
| 1X | 60 seconds (real time) | 6 h 15 min |
| 10X | 6 seconds | about 38 min |
| 50X | 1.2 seconds | about 7.5 min |
| 100X | 0.6 seconds | about 3.75 min |
| 200X | 0.3 seconds | about 1.9 min |

Changing speed mid-minute keeps the progress already made towards the next minute, and pausing holds the replay exactly where it is.
The replay keeps its pace in a background tab: when the browser delays its timers, the minutes owed are caught up when the tab runs again.
When the run reaches its end, a "Replay finished" banner appears; it goes away once you drag back or press Back to the start.

Keyboard shortcuts work anywhere on the page except while typing in a field (the date, ATM and expiry inputs keep their keys), and combinations with Alt, Ctrl or Cmd are left to the browser (for example, Alt + Left for Back).

### How a replay works

- **One continuous run:** the trading days between the two dates play back-to-back on one chart, like the live page's multi-day view; weekends and holidays are skipped automatically.
- **Context day:** the trading session just before the From date (weekends and holidays skipped) is already drawn when the run loads, and the replay starts at the From date's first minute.
  The charts therefore start with history, as on the live page: VWMA 10 has values from the first minute, signals are judged against the previous day's columns, and the box size comes from the context day's first close.
  VWAP itself restarts at each session's open, so the context day shows its own VWAP line and the From date's VWAP starts fresh at 09:15.
  The context day is not replayed and stays on the chart when you go back along the timeline; if a contract had not traded yet that day, its chart starts at the From date.
- **Strikes** are fixed for the whole run, from the start day's index open (or the ATM you typed): ATM - 200, ATM and ATM + 200 for NIFTY, ATM - 500, ATM and ATM + 500 for BANKNIFTY and SENSEX.
- **Chart rules** are the live page's: box size 0.5% of the first straddle close on the chart (the context day's), 3-box reversal, VWAP restarting at each session's open, VWMA 10 of column midpoints.
- **Movement within each minute:** the history API serves 1-minute candles, so each minute plays along its candle's likely path in four steps: open, the two extremes, then the close (a rising candle is taken to dip first, a falling one to rally first).
  The straddle prices, the CE and PE prices, the forming boxes (drawn faded, as on the live page), VWAP and the spot all move during the minute, and the minute's bar is completed at its close.
  For the straddle, the CE and PE paths are added step by step, which approximates the straddle's true path within the minute.
- **Signals and alerts:** double top buy and double bottom sell are marked on the charts.
  As on the live page, only a double bottom sell below VWAP raises an alert (a toast, a panel flash and a chime, labelled "(simulation)"); double top buys are marked only.
  They fire only while playing; dragging along the timeline rebuilds the charts silently, so jumping around never replays old alerts.
  Toasts close themselves after 12 seconds (hover to keep one open), and simulated alerts are never written to the live page's alert log.

### Simulated trades

Each chart has a trade bar at the bottom for practising a short straddle (selling the CE and PE together) while the replay runs.

| Button | What it does |
| --- | --- |
| **Sell 10 lots** | Sells the straddle at the price on screen, including the minute still forming: 650 qty for NIFTY (65 per lot), 300 qty for BANKNIFTY (30 per lot) or 200 qty for SENSEX (20 per lot), using the lot size from the exchange's contract list. One trade at a time per chart, so Sell is hidden while a trade is open. |
| **Exit 50%** | Buys back half of the open lots, rounded down to whole lots (10, 5, 3, 2, 1); disabled when only 1 lot is left. |
| **Exit Full** | Buys back all open lots and closes the trade; Sell appears again. |

- **P&L** updates live in rupees and points: points = entry price - current price (a short straddle gains when the straddle falls), rupees = points x quantity.
  After a partial exit the P&L includes the booked part and the bar shows the lots still open; after a full exit it shows the closed trade's result, plus the total when a chart has been traded more than once.
  Brokerage, taxes and slippage are ignored.
- Buttons work while paused too, at the price on screen.
  Paused between minutes, the screen already shows the next minute's opening price (its clock reads that minute's start), so a trade before Play, or at a day's first minute, fills at that day's open, not the previous day's close.
- Going back along the timeline clears the simulated trades opened (or exited) after the point you go back to, since they have not happened yet; earlier trades and moving forward keep them, and loading a new run starts afresh.
  Signals after that point alert again when they are replayed.

### The algo in the simulator

The **Algo** button in the playbar runs the [algo](#algo-trading) over the replay with the live page's rules, on paper only.
Its menu is the live page's, without the Mode choice and the Exit buttons.

- **Switching it On:** load a run, then press **Algo**; it trades from the moment on the timeline (from the start of the run at "minute 0"), with no confirmation.
- **Each session:** it switches itself On again at every later session's open, since nobody is there to do it, and goes Off at the Exit Time, after the daily loss limit or once Last entry has passed, as on the live page.
- **Prices:** it trades at the minute closes the charts are built from; Max loss per trade and the Daily loss limit also watch the four price steps within each minute.
  Slippage and charges are left out.
- **What you see:** the line under each chart's header, the lines and triangles on the charts, the day's trades in the Algo menu, and a toast for each algo action (with a sound for a trade opened, booked or closed) while the replay plays.
- **The timeline:** dragging it shows the algo's trades as they stood at that moment, without toasts; a trade after that moment is gone until the replay reaches it again.
- **Settings:** the simulator keeps its own, in the browser, apart from the live page's; Quantity and Target Points are those of the run's index.
  Changing a setting while the algo is On works the run's trades out again with it.
- **Off** takes the algo's trades off the replay; switched On again, it starts afresh from that moment.
  Loading a new run with the algo On replays the algo from that run's start.
- **At the end,** the "Replay finished" banner adds the algo's result, for example "Algo: 3 trades, 2 won, 1 lost, +₹12,480."
- The algo's trades and the trade bars under the charts (Sell 10 lots, Exit 50%, Exit Full) are separate: neither sees the other.

### Which dates can be replayed

- **Only contracts still listed today.** The API cannot fetch option contracts that have expired, so a past day is replayed with one of the next listed expiries, not with the contract that was nearest on that day.
  For example, on 12 Oct 2026 the 13 Oct weekly can replay the days before it, and so can the monthlies further out.
- **How far back:** a weekly contract usually trades properly for only the last two or three weeks before its expiry (it is thin when first listed); monthlies reach further back.
  If the chosen contract had not started trading on the chosen dates, the simulator says so instead of showing empty charts.
- **At most 30 days per run**, the history API's limit for 1-minute candles, and **at most 5 years back**.
- **Context day:** if the chosen contract had not traded yet on the session before From, the run still loads; that chart simply starts at the From date.

The simulator's own [README](pnf_sim/README.md) has the same details next to its code.

## Development

```sh
.venv/bin/python -m pytest                     # Python tests (live app, simulator and algo)
.venv/bin/python -m pytest tests/test_engine.py::test_live_minute_bars_and_reconcile   # one test
node --test tests/*.test.mjs                   # P&F engine, simulator playback, Kite P&L and algo page tests
.venv/bin/ruff check && .venv/bin/ruff format --check
```

### Project layout

| Path | Purpose |
| --- | --- |
| `pnf/neo.py` | Async wrapper around the blocking Neo SDK: request pacing, retries, login. |
| `pnf/series.py` | Pure helpers: IST calendar, session filter, straddle series and VWAP, ATM rounding, expiry choices. |
| `pnf/engine.py` | The state machine: building charts, the live feed, minute bars, refreshing, daily rollover. |
| `pnf/tls.py` | The HTTPS certificate both servers use: created in `certs/` on first start, renewed before it expires. |
| `pnf/app.py` | FastAPI server: the page, `POST /api/login`, `POST /api/config`, and the `/ws` update channel. |
| `pnf/static/pnf.js` | Point & Figure construction and signal detection for the page. |
| `pnf/pnf.py` | The same Point & Figure rules in Python, for the algo; the tests check that both give the same charts. |
| `pnf/static/chart.js` | Canvas chart renderer (draws only the visible area, so long histories stay fast). |
| `pnf/static/axis.js` | Price-axis tag layout, so the price, VWAP and VWMA tags never overlap. |
| `pnf/static/alerts.js` | Toasts, sounds and system notifications. |
| `pnf/static/expand.js` | The expand button that widens one chart over the other two (live page and simulator). |
| `pnf/static/app.js` | Page wiring: controls, server messages, panel headers. |
| `pnf_sim/app.py` | Simulator server: its page, `GET /api/options` (expiries and date limits), `POST /api/simulate` (builds a run) and `POST /api/algo` (the algo's trades over a run); serves the live app's static files read-only. |
| `pnf_sim/replay.py` | Builds a simulator run: date checks, expiries offered, strikes from the start day's open, straddle series with each minute's price path, previous closes for the spot. |
| `kite/` | Order placement and position tracking through Zerodha Kite: gateway, contracts, exit sizing, broker, endpoints and the trading UI (see [kite/README.md](kite/README.md#files)). |
| `algo/core.py` | The algo's rules and bookkeeping, with no I/O: entries, exits, loss limits, Exit Time, settings. |
| `algo/live.py` | Carries out live algo trades through Kite: places, follows, cancels and resends orders. |
| `algo/runner.py` | Runs the algo in the live server: engine and Kite hooks, paper fills, alarms, saving `.pnf-algo.json`, pushing its state. |
| `algo/web.py` | The algo's endpoints (`/algo/...`) and its `/algo/ws` update channel. |
| `algo/sim.py` | Works out the algo's trades over a simulator run. |
| `algo/static/` | The Algo button and menu, chart lines and marks, alarms and confirmations (`ui.js`, shared with the simulator), the live page's link to the server (`algo.js`), and the state's words and numbers (`model.js`). |
| `start.sh` | Starts the live app and the simulator together (see [Running](#running)). |
| `pnf_sim/static/` | The simulator page (`simulator.html`, `simulator.js`, `sim.css`), its replay clock (`player.js`), its simulated trades (`trades.js`) and the algo over the replay (`algo-sim.js`). |
| `tests/` | Python tests (the live engine against a fake gateway, the simulator's run building, Kite trading against a fake Kite in `tests/kite_fakes.py`, the algo's rules, its live runner and its simulator runs, and the Python P&F against the browser's) and JavaScript tests (the P&F engine, the simulator's replay clock, Kite P&L and the algo's page model). |

### Testing the live path outside market hours

`tests/test_engine.py` contains a `FakeGateway` and a `tick()` helper that builds feed packets.
`engine.now_ist` is the only clock the engine reads, so you can run the app against the fake gateway with a shifted clock to watch live bars, VWAP and alerts at any time.

## Security

- The server listens on `127.0.0.1` only, because it holds a live trading session; do not expose it to a network.
- Credentials are read from `.env` and are never sent to the browser.
- The TOTP is used once to log in and is not stored.
- The Neo session is used only for market data.
- Orders are placed only through Kite: from the page's Sell and Exit buttons, and by the algo in Live mode; the Kite access token stays on the server (owner-only `.kite-session.json`), and the order endpoints refuse requests from any other website (see [kite/README.md](kite/README.md#security)).

## Limitations

- NIFTY, BANKNIFTY and SENSEX only, with fixed side-strike distances (± 200, ± 500 and ± 500).
- Box size (0.5%), reversal (3 boxes) and the VWMA period (10 columns) are fixed in the code (`pnf/static/pnf.js`, and `pnf/pnf.py` for the algo).
- Alerts work only while the page is open in a browser tab.
  The algo trades without the page, but its sounds and pop-up alerts also need the page open.
- This is a charting tool, not investment advice.
  Check signals against your own analysis before trading.
