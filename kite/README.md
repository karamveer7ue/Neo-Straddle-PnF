# Kite trading

Sell a straddle from any chart on the live P&F page, and track and exit it, through your Zerodha Kite account ([Kite Connect API](https://kite.trade/docs/pykiteconnect/v4/)).
The [algo](../README.md#algo-trading) places its live orders through the same connection (see [The algo's orders](#the-algos-orders)).
Charts and prices still come from Kotak Neo; Kite is used only to place orders and read your positions.

## What it adds to the page

- **Under every chart:** a **Qty** field and a **Sell** button.
  Sell sells the chart's CE and PE at market, the quantity entered of each.
  One click (or Enter in the Qty field) sends the orders at once; there is no confirmation step.
  While they are being sent the button shows **Selling...**, and a second click on the same chart within a second is ignored, so a double-click or a held-down Enter sells only once.
  Exit 50% and Exit Full work the same way: instant, and a double-click acts once (Exit 50% never becomes 75%).
- **On charts holding a position from this page:** the position's live P&L and **Exit 50%** / **Exit Full** buttons.
  Charts without a position show no exit buttons.
- **Under the top bar:** one card per straddle traded from here, with its quantity, the straddle price sold at and now, and the P&L, plus the total.
  A straddle closed today stays listed (muted, "Closed") with its booked P&L until the next day.
  A position on a strike that is no longer on screen (e.g. after the ATM moved, or on another expiry) gets its own Exit buttons on its card, so it can always be closed from the page.
- **In the top bar:** **Connect Kite**, or **TradeOn** once connected (click it to disconnect).

Every error toast (an order not placed, rejected or left working) is also saved in the alert log next to the bell, so it can be read later.
Each order's outcome appears as a toast: "Sold NIFTY 22,300 (13 Oct) straddle - CE 650 @ 100.80 · PE 650 @ 51.20", or the rejection reason from Kite (e.g. insufficient margin).

## Setup

1. Create a Kite Connect app at [developers.kite.trade](https://developers.kite.trade) (a paid subscription).
   Set its **Redirect URL** to `https://127.0.0.1:8765/callback` (use your port if you run the app on another one).
   That is where Kite returns after you log in; the app finishes the login there and sends you back to the charts.
   The app is served over HTTPS for this (see [HTTPS](../README.md#https) for the one-time certificate step).
   The postback URL is not used and can stay empty; the app reads order status from Kite instead.
2. Register your **static IP** in the developer profile ("IP Whitelist").
   Since April 2026 Kite refuses API orders from unregistered IPs; positions and other reads work from anywhere.
   Without a static IP on this machine, send the orders through the relay on your web host (see [Orders through a relay](#orders-through-a-relay-static-ip)) and register the relay's IP instead.
   If orders fail with "IP ... is not allowed to place orders", the IP the request left from (IPv4 or IPv6) is not the one registered.
3. Put the app's key and secret in `.env` (the entries are already there, empty):

   ```ini
   KITE_API_KEY=your-api-key
   KITE_API_SECRET=your-api-secret
   ```

4. Restart the app, then click **Connect Kite** in the top bar and log in on Kite's page; it returns you to the charts.

Once connected, the top bar shows **TradeOn** (click it to disconnect).
With the keys empty, the page shows **TradeOff** and the Sell buttons stay disabled; the charts work as before.

Optional settings in `.env`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `KITE_PRODUCT` | `NRML` | The Sell buttons' product: `NRML` carries positions overnight until you exit; `MIS` is intraday (Zerodha squares it off before the close). The algo always uses `MIS`. |
| `KITE_ORDER_RELAY` | empty | The relay's URL, e.g. `https://your-domain.example/kite-route.php`; empty sends orders straight to Kite. |
| `KITE_RELAY_SECRET` | empty | The secret shared with the relay (64 random hex characters). |

The login lasts until about 06:00 the next morning (Kite's daily reset), including across app restarts; after that, click **Connect Kite** again.

## Orders through a relay (static IP)

Kite takes orders only from the static IP registered in the developer console.
`kite/relay/kite-route.php`, uploaded to a web host with a fixed IP, sends the orders on to Kite from there:

```text
this app ──(sign)──> https://your-domain.example/kite-route.php ──> api.kite.trade/orders/...
         <──────── Kite's reply, unchanged ────────
```

- **Only orders go through it:** placing, modifying and cancelling (the only calls Kite checks the IP of).
  Positions, the order book and the login go straight to Kite, as before.
- **Signed:** every relayed request carries an HMAC-SHA256 signature made with `KITE_RELAY_SECRET` and a timestamp, over everything the relay forwards (the order, the Kite authorization and the other headers).
  The relay refuses anything unsigned, signed with another secret, changed after signing, older than 60 seconds, already sent once, or for any other Kite endpoint, so nobody else can send orders from your IP and a captured request cannot place the same order twice.
  To refuse repeats it keeps the hashes of recent signatures (for 2 minutes) in `.kite-route-seen.json` in the folder above `kite-route.php` (outside `public_html`), or in the system temp folder if that one is not writable.
- **After updating the app, upload a fresh copy:** the signature format can change between versions, and an older `kite-route.php` then refuses every order ("bad signature"; nothing is sent).
  Run step 2 below again and upload the new copy.
- **Nothing stored:** the Kite access token passes through with each order and is neither stored nor logged on the host.
- **Errors:** the relay's own refusals come back as Kite-style errors ("Order relay: ..." in the toast).
  If the relay cannot reach Kite, the order counts as "outcome unknown" (check the position before retrying), like any other network failure.
  If the host answers 404 or 403 itself, the toast asks whether `kite-route.php` is uploaded and not blocked by the host's firewall.

Setting it up:

1. In `.env`, set `KITE_ORDER_RELAY` to the relay's URL (e.g. `https://your-domain.example/kite-route.php`) and `KITE_RELAY_SECRET` to 64 random hex characters (`python3 -c "import secrets; print(secrets.token_hex(32))"`).
   To use another secret, change it in `.env` and do step 2 again.
2. Write the upload copy, with the secret filled in:

   ```sh
   .venv/bin/python -m kite.order_relay php    # writes relay-upload/kite-route.php (gitignored)
   ```

3. Upload `relay-upload/kite-route.php` to the host as `public_html/kite-route.php` (Hostinger: File Manager), so it is served at `https://your-domain.example/kite-route.php`.
   The host needs PHP 7.4 or later with the curl extension (standard on Hostinger).
4. Check it and find the IP to register:

   ```sh
   .venv/bin/python -m kite.order_relay check
   ```

   ```text
   Relay OK: https://your-domain.example/kite-route.php
   Its outgoing IP (register this in the Kite developer console): 203.0.113.7
   ```

   Register **that** IP in the Kite developer console: it is the address the host's requests leave from, which can differ from the IP your domain points to.
   A host with both IPv4 and IPv6 (Hostinger has both) could reach Kite over either, so the relay always uses one: IPv4, or IPv6 if you set `KITE_IP_VERSION = 6` in `kite-route.php`.
   `check` shows the address of that version (and the other, unused one); an "IP (2a02:...) is not allowed" error means Kite saw an address you have not registered.
5. Restart the app (`./start.sh --restart`).

**Shared hosting and Kite's IP rules:** on shared hosting (such as Hostinger's web hosting plans), the outgoing IP is shared with the other sites on the same server.
Kite links an IP to one account only ("already linked to another account" when registering means another Kite user on that server got there first), SEBI's rules allow sharing an IP only within a family, and the host may move your site to another server (and IP) without notice - orders then fail with "IP ... is not allowed" until you register the new one (`check` shows it).
A VPS or a hosting plan with a dedicated IP avoids all three.

Each order takes one extra hop (this machine -> the host -> Kite), typically 50 to 150 ms.

## How orders are placed

- **Quantity** is in units and must be a multiple of the contract's lot size (65 for NIFTY, 30 for BANKNIFTY, 20 for SENSEX at the time of writing; the page reads it from Kite's contract list for each expiry and shows the lot count next to the field).
  The field starts at the last quantity you sold on that index, or one lot.
- **Size cap:** a sell is at most twice the algo's default quantity for its index: 3,380 for NIFTY, 1,440 for BANKNIFTY, 960 for SENSEX (`max_sell_qty` in `pnf/series.py` `UNDERLYINGS`), as a guard against a mistyped quantity.
  A larger quantity is refused before anything is sent, by the page and again by the server; the cap applies to the algo too.
- **Orders** are MARKET orders with Kite's automatic market protection (`market_protection=-1`, required for API market orders since April 2026), DAY validity, tagged `pnf` (visible in Kite's order book).
  An order is a single order up to the exchange's maximum quantity per order (the freeze quantity: 3,510 for NIFTY, 1,440 for BANKNIFTY, 1,000 for SENSEX); above it, Kite splits it into orders of at most that size (autoslice).
  Kite refuses to slice anything smaller ("Quantity should be greater than or equal to 1000 for order slicing"), so slicing is asked for only when needed.
  The maximums are `max_order_qty` in `pnf/series.py` `UNDERLYINGS`; update them there when the exchanges revise them.
- **Sell** places the CE order and then straight away the PE, without waiting for fills.
  If Kite refuses the CE outright, the PE is not sent and a toast says why.
  Kite runs its risk checks (margin above all) after accepting an order, so a CE can still be rejected a moment later while the PE fills: the rejection toast then shows it, and the position card shows the PE alone - exit it there if you do not want it.
- **Exit 50%** buys back half of each leg's open lots, rounded down to whole lots (10 lots -> 5, 5 -> 2); it is disabled while each leg has only 1 lot.
  **Exit Full** buys back everything.
  Both size the order from Kite's positions and orders read at that moment, leave out quantity already being exited by a working order (so pressing Exit twice never turns a short into a long), and use each leg's own product.
  Orders are read before positions, and if Kite's positions do not yet include every fill its order book shows (they can lag for a moment), they are read again; an exit is never sized from positions that miss an earlier exit's fill.
  If a sell order on the same contracts is still working, a toast warns that it can reopen the position.
- **Retries:** a request refused with "too many requests" (429) is retried; any other failure that may have reached Kite (a timeout above all) is never resent, and the toast says the outcome is unknown - check the position before trying again.
- Orders use their own connection to Kite, so they never wait behind a position refresh or the daily contract download.
- If the Kite session expires between the two legs, the toast reports the leg that was placed and the one that was not; connect Kite again to see and exit the position.

## How positions are tracked

- Positions and orders are read from Kite every 2 seconds in market hours (every minute outside them), and every half second while an order just placed has not completed.
- Only contracts ordered from this page are shown.
  They are remembered in `.kite-journal.json`, so overnight NRML positions are still shown and exitable the next day; a contract is forgotten once it has expired, or when Kite shows no position or working order for it during market hours and it was last traded on an earlier day.
  The file is replaced atomically on every change, and an unreadable entry is skipped (and logged) without losing the others.
- **P&L** is Kite's own figure, re-priced with the live Neo price for the straddles on screen (exact, since Kite's P&L moves by quantity x price change), and at Kite's last price for any other.
  Brokerage and taxes are not included.
- Positions on the same contracts taken elsewhere (e.g. in the Kite app) are netted by Kite into the same position, and Exit closes them too.

## The algo's orders

In Live mode the [algo](../README.md#algo-trading) trades through this connection, with the same order rules as the Sell buttons (market orders with market protection, the size cap, slicing above the freeze quantity, never resending an order whose outcome is unknown), and these differences:

- **Product:** always `MIS`, whatever `KITE_PRODUCT` says, so Zerodha squares its positions off at 15:26 if the app cannot.
  Kite keeps MIS and NRML positions on the same contract apart.
- **Tag:** each trade's orders carry their own tag in Kite's order book, `pnfalgo001` for the day's first algo trade, `pnfalgo002` for the next, and so on.
  Every order in Kite's order book with a trade's tag counts as that trade's, so an order whose reply was lost, or that went out just before a restart, is still found.
  One Kite has not listed 15 seconds on counts as not placed (and is taken in after all should Kite list it later).
- **Buy-backs** are sized from a fresh read of the order book and positions (re-read while the positions lag the order book), and never exceed the MIS position Kite shows short minus the MIS buys still working, so a leg can never turn long.
- **Cancelling:** an entry leg still unfilled after 10 seconds is cancelled (the entry has then failed, and whatever was sold is bought back); an exit unfilled after 10 seconds is cancelled and sent again.
  A cancel Kite does not act on is sent again 10 seconds later; an exit Kite rejects is sent again after a pause that doubles each time (5 seconds up to a minute).
- **Following:** the algo reads its orders' progress from the broker's regular refreshes (every half second while one of its orders is working).
  Its positions show in the positions strip and on the charts like any other, and Exit 50% / Exit Full act on them too.
  The algo counts a position as bought back outside it only when Kite's positions show it on two refreshes in a row and a filled MIS buy order that is not the algo's (an Exit button, the Kite app, Zerodha's square-off) explains it; then it manages only what is left.

## Files

| Path | Purpose |
| --- | --- |
| `kite/gateway.py` | Async wrapper over the blocking `kiteconnect` SDK: one call at a time, retries for reads, order placement that is never resent after an uncertain failure. |
| `kite/contracts.py` | Kite's index-option contracts (from the instrument dump, loaded once a day) by underlying, expiry, strike and CE/PE. |
| `kite/book.py` | Pure helpers: straddle groups from positions and orders, and exit sizing. |
| `kite/broker.py` | Session (login, saved token), sell and exit, polling, the journal, order outcomes, and the algo's orders (`algo_sell`, `algo_buy_back`, `cancel`). |
| `kite/web.py` | The endpoints (`/kite/login`, `/callback`, `/api/kite/sell`, `/api/kite/exit`, `/api/kite/logout`, `/kite/ws`) and the static files, added to the P&F app by `install()`. |
| `kite/static/trading.js`, `trading.css` | The trading UI on the page. |
| `kite/static/positions.js` | P&L and quantity checks (no DOM; unit-tested in Node). |
| `kite/order_relay.py` | Signs order calls and sends them to the relay; `check` and `php` commands. |
| `kite/relay/kite-route.php` | The relay to upload to a host with a static IP (the template; `php` writes the copy with the secret). |

Local files (gitignored): `.kite-session.json` (the access token, readable by your user only), `.kite-journal.json`, and `relay-upload/` (the relay with your secret in it).

## Security

- The access token can place orders: it is stored only in `.kite-session.json` with owner-only permissions, and never sent to the browser.
- The server listens on `127.0.0.1` only, and every Kite endpoint accepts requests only from the app's own page (checked by Host and Origin), so another website open in the same browser cannot place or exit orders, even through DNS rebinding.
- The login callback accepts only a login started from this page (a one-time state value passed through Kite).
- The order relay accepts only signed, fresh requests for Kite's order endpoints (see [Orders through a relay](#orders-through-a-relay-static-ip)); keep `KITE_RELAY_SECRET` private, and change it (and re-upload) if it leaks.
