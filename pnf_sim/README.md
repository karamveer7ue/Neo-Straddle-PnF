# Straddle simulator

Replays past NIFTY or SENSEX sessions minute by minute on the same three straddle Point & Figure charts as the live page (ATM - 200, ATM, ATM + 200 for NIFTY; ATM - 500, ATM, ATM + 500 for SENSEX), at 1X, 10X, 50X, 100X or 200X.

It is a separate app, so the live page is never affected, and it reuses the live app's code unchanged: the Neo API gateway, the straddle series rules, the Point & Figure engine, VWAP, SMA 10, signal detection, the chart renderer and the alerts.
It needs only the consumer key from `.env` (market data), so it never asks for a TOTP.

## Running

From the project folder, `./start.sh` starts the simulator together with the live app (port 8765); if either is already running, it asks whether to restart or skip it.
See [Running](../README.md#running) in the main README for its options.
To run the simulator on its own:

```sh
.venv/bin/python -m pnf_sim              # https://127.0.0.1:8766
.venv/bin/python -m pnf_sim --port 9001  # any other port
```

## Using it

1. Pick **NIFTY** or **SENSEX** (Load waits until that index's expiries have loaded).
2. Pick an **expiry**: the next four listed expiries (NIFTY: Tuesdays, SENSEX: Thursdays; the exchange moves an expiry for a holiday, e.g. 19 Oct 2026 is a Monday).
3. Pick **From** and **To** dates.
   Both must be before today, the range can be at most 30 days, and the start at most 5 years back.
4. Optionally set an **ATM** strike; leave it empty to use the strike nearest to the start day's open.
5. Press **Load** (about 15 seconds: history requests are paced to stay within the API's rate limits), then **Play**.
   The previous run is paused while loading and stays as it was if loading fails.
   The last 8 runs are kept in memory for the day, so loading one again is instant.

| Control | What it does |
| --- | --- |
| **Play / Pause** | Starts or pauses the replay (keyboard: Space). Nothing moves until you press Play. |
| **Back to the start** | Jumps back to just before the From date's first minute (the clock shows "minute 0"), so the whole run, including its first minute, plays again. |
| **1X / 10X / 50X / 100X / 200X** | Speed. 1X is real time: one market minute per minute (a full session takes 6 h 15 min). 10X: about 38 min per session; 50X: about 8 min; 100X: about 4 min; 200X: a minute every 0.3 seconds, about 2 min per session. |
| **Timeline** | Drag to jump to any minute (keyboard: left and right arrows step one minute). The thin bar below it shows the time left until the next minute appears. |
| **Clock** | The simulated time (to the second) and the minute count. |
| **Spot** | The index at the simulated time, with its change since the previous session's close. |
| **VWAP / SMA 10, zoom, bell** | As on the live page. |

## How a run works

- **One continuous run:** the trading days between the two dates play back-to-back on one chart, like the live page's multi-day view; weekends and holidays are skipped automatically.
- **Context day:** the trading session just before the From date (weekends and holidays skipped) is already drawn when the run loads, and the replay starts at the From date's first minute.
  The charts therefore start with history, as on the live page: SMA 10 has values from the first minute, signals are judged against the previous day's columns, and the box size comes from the context day's first close.
  VWAP itself restarts at each session's open, so the context day shows its own VWAP line and the From date's VWAP starts fresh at 09:15.
  The context day is not replayed and stays on the chart when you go back along the timeline; if a contract had not traded yet that day, its chart starts at the From date.
- **Strikes** are fixed for the whole run, from the start day's index open (or the ATM you set).
- **Box size** is 0.5% of the first straddle close on the chart (the context day's), with a 3-box reversal; VWAP restarts at each session's open.
- **Prices move within each minute:** the history API serves 1-minute candles, so each minute is played along its candle's likely path in four steps (open, the two extremes, close; a rising candle dips first, a falling one rallies first). The straddles, the forming boxes on the charts and the spot all move, and the minute's bar is completed at its close. For the straddle, the CE and PE paths are added step by step, which approximates the straddle's true path within the minute.
- **Signals:** double top buy and double bottom sell are marked on the charts. As on the live page, only a double bottom sell below VWAP raises an alert (a toast, a panel flash and a chime, marked "simulation"); double top buys are marked only. Alerts fire only while playing: jumping along the timeline rebuilds the charts silently. Toasts close themselves after 12 seconds (hover to keep one open). Simulated alerts are never written to the live page's alert log.

## Simulated trades

Each chart has a trade bar at the bottom for practising a short straddle (selling the CE and PE together) during the replay.

- **Sell 10 lots** sells the straddle at the price on screen, including the minute still forming (650 qty for NIFTY at 65 per lot, 200 qty for SENSEX at 20 per lot; the lot size comes from the exchange's contract list).
  One trade at a time per chart: Sell is hidden while a trade is open.
- **Exit 50%** buys back half of the open lots, rounded down to whole lots (10, 5, 3, 2, 1); it is disabled when only 1 lot is left.
- **Exit Full** buys back all open lots and closes the trade; Sell then appears again.
- **P&L** is shown in rupees and points, live as the replay moves: points = entry price - current price (a short straddle gains when the straddle falls), rupees = points x quantity.
  After a partial exit the P&L includes what was booked, and the bar shows the lots still open; after a full exit it shows the closed trade's result, and the total when a chart has been traded more than once.
  Brokerage, taxes and slippage are ignored.
- Trades can be placed and exited while paused, at the price on screen.
  Going back along the timeline clears the simulated trades opened (or exited) after the point you go back to, since they have not happened yet; earlier trades and moving forward keep them, and loading a new run starts afresh.
  Signals after that point alert again when they are replayed.

## Limits

- **Only contracts still listed today can be replayed.** The API cannot fetch expired option contracts, so a past day is replayed with one of the next four listed expiries.
  A weekly contract usually has data for only the last two or three weeks before its expiry (it trades thinly when first listed); monthlies reach further back.
  If the chosen contract had not started trading on the chosen dates, the simulator says so.
- At most 30 days per run (the history API's limit for 1-minute candles).

## Files

| Path | Purpose |
| --- | --- |
| `pnf_sim/app.py` | FastAPI server: the page, `GET /api/options`, `POST /api/simulate`; serves the live app's static files read-only at `/static`. |
| `pnf_sim/replay.py` | Builds a run: date checks, expiries offered, the context day before the run, strikes from the start day's open, the straddle series with CE and PE prices and each minute's price path, previous closes for the spot. |
| `pnf_sim/static/simulator.html`, `simulator.js`, `sim.css` | The page. |
| `pnf_sim/static/player.js` | The replay clock (speed, pause, seek); no DOM, unit-tested in Node. |
| `pnf_sim/static/trades.js` | Simulated short-straddle trades and their P&L; no DOM, unit-tested in Node. |
| `tests/test_sim.py`, `tests/sim.test.mjs` | Tests. |

```sh
.venv/bin/python -m pytest tests/test_sim.py
node --test tests/sim.test.mjs
```
