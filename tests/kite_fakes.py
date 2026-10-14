"""A stand-in for kiteconnect.KiteConnect: instruments, orders that fill at once,
and positions netted the way Kite nets them. Used by tests/test_kite.py, and
handy for running the app without a Kite account.
"""

from datetime import date

from kiteconnect import exceptions as kex

NIFTY_EXPIRIES = [date(2026, 10, 13), date(2026, 10, 20), date(2026, 10, 27)]
SENSEX_EXPIRIES = [date(2026, 10, 15), date(2026, 10, 22)]


def _instrument_rows(name, exchange, expiries, strikes, lot_size, token_base, code):
    rows = []
    token = token_base
    for expiry in expiries:
        for strike in strikes:
            for kind in ("CE", "PE"):
                token += 1
                rows.append(
                    {
                        "instrument_token": token,
                        "exchange_token": str(token // 256),
                        "tradingsymbol": f"{name}{expiry:%y}{code(expiry)}{strike}{kind}",
                        "name": name,
                        "last_price": 0.0,
                        "expiry": expiry,
                        "strike": float(strike),
                        "tick_size": 0.05,
                        "lot_size": lot_size,
                        "instrument_type": kind,
                        "segment": f"{exchange}-OPT",
                        "exchange": exchange,
                    }
                )
        token += 1
        rows.append(  # a future: not an option, must be ignored
            {
                "instrument_token": token,
                "exchange_token": "1",
                "tradingsymbol": f"{name}{expiry:%y%b}FUT".upper(),
                "name": name,
                "last_price": 0.0,
                "expiry": expiry,
                "strike": 0.0,
                "tick_size": 0.05,
                "lot_size": lot_size,
                "instrument_type": "FUT",
                "segment": f"{exchange}-FUT",
                "exchange": exchange,
            }
        )
    return rows


# The exchanges' maximum quantity per order (freeze quantity), by underlying.
FREEZE_QUANTITY = {"NIFTY": 3510, "SENSEX": 1000, "BANKNIFTY": 600}


def freeze_quantity(tradingsymbol: str) -> int:
    name = next(n for n in sorted(FREEZE_QUANTITY, key=len, reverse=True) if tradingsymbol.startswith(n))
    return FREEZE_QUANTITY[name]


def weekly_code(d: date) -> str:
    return f"{'123456789OND'[d.month - 1]}{d.day:02d}"


INSTRUMENTS = {
    "NFO": _instrument_rows(
        "NIFTY", "NFO", NIFTY_EXPIRIES, range(21000, 23550, 50), 65, 10_000_000, weekly_code
    )
    + _instrument_rows(
        "BANKNIFTY", "NFO", NIFTY_EXPIRIES[:1], range(48000, 48300, 100), 30, 11_000_000, weekly_code
    ),
    "BFO": _instrument_rows(
        "SENSEX", "BFO", SENSEX_EXPIRIES, range(70000, 75100, 100), 20, 20_000_000, weekly_code
    ),
}


class FakeKite:
    """KiteConnect's methods used by kite.gateway, backed by an in-memory account."""

    VARIETY_REGULAR = "regular"
    ORDER_TYPE_MARKET = "MARKET"
    VALIDITY_DAY = "DAY"

    def __init__(self, prices=None):
        self.access_token = None
        self.prices = prices if prices is not None else {}  # tradingsymbol -> last price
        self.default_price = 100.0
        self.placed: list[dict] = []  # every place_autoslice_order call's params
        self.order_book: list[dict] = []
        self.fills: list[dict] = []
        self.overnight: list[dict] = []  # net rows carried from earlier days
        self.fail_next: list[Exception] = []  # raised by the next calls, in order
        self.reject: dict[str, str] = {}  # tradingsymbol -> rejection message
        self.hold_open: set[str] = set()  # tradingsymbols whose orders stay OPEN
        self.calls: list[str] = []
        self.before: dict = {}  # method name -> fn(fake), run as that call starts (e.g. to raise)
        self.lag_fills = 0  # positions() leaves out this many of the newest fills (Kite lagging)
        self.session_valid = True
        self._next_id = 1000

    # ---- session ----

    def set_access_token(self, token):
        self.access_token = token

    def login_url(self):
        return "https://kite.zerodha.com/connect/login?api_key=testkey&v=3"

    def _check(self, name):
        self.calls.append(name)
        if name in self.before:
            self.before[name](self)
        if self.fail_next:
            raise self.fail_next.pop(0)
        if name not in ("generate_session", "login_url", "instruments") and (
            not self.session_valid or not self.access_token
        ):
            raise kex.TokenException("Incorrect `api_key` or `access_token`.")

    def generate_session(self, request_token, api_secret):
        self._check("generate_session")
        if request_token != "good-token":
            raise kex.TokenException("Token is invalid or has expired.")
        self.access_token = "access-123"
        self.session_valid = True
        return {
            "access_token": "access-123",
            "user_id": "AB1234",
            "user_shortname": "Kams",
            "login_time": None,
        }

    def invalidate_access_token(self, access_token=None):
        self._check("invalidate_access_token")
        return True

    def profile(self):
        self._check("profile")
        return {"user_id": "AB1234", "user_shortname": "Kams"}

    # ---- market data ----

    def instruments(self, exchange=None):
        self._check("instruments")
        return [dict(r) for r in INSTRUMENTS.get(exchange, [])]

    def price(self, symbol):
        return self.prices.get(symbol, self.default_price)

    # ---- orders ----

    def place_order(self, **params):
        """One order; Kite refuses it above the exchange's freeze quantity."""
        self._check("place_order")
        limit = freeze_quantity(params["tradingsymbol"])
        if params["quantity"] > limit:
            raise kex.InputException(f"Quantity exceeds the freeze limit of {limit}.", code=400)
        return self._add_order(params, params["quantity"])

    def place_autoslice_order(self, **params):
        """Orders of at most the freeze quantity each; like Kite, slicing is refused for
        a quantity below the freeze quantity."""
        self._check("place_autoslice_order")
        limit = freeze_quantity(params["tradingsymbol"])
        if params["quantity"] < limit:
            raise kex.InputException(
                f"Quantity should be greater than or equal to {limit} for order slicing.", code=400
            )
        slices, left = [], params["quantity"]
        while left > 0:
            slices.append({"order_id": self._add_order(params, min(left, limit))})
            left -= limit
        return slices

    def _add_order(self, params, quantity):
        self.placed.append({**params, "quantity": quantity})
        self._next_id += 1
        order_id = str(self._next_id)
        symbol = params["tradingsymbol"]
        order = {
            "order_id": order_id,
            "exchange": params["exchange"],
            "tradingsymbol": symbol,
            "transaction_type": params["transaction_type"],
            "product": params["product"],
            "quantity": quantity,
            "tag": params.get("tag"),
            "status": "OPEN",
            "status_message": None,
            "filled_quantity": 0,
            "pending_quantity": quantity,
            "average_price": 0.0,
        }
        if symbol in self.reject:
            order.update(status="REJECTED", status_message=self.reject[symbol], pending_quantity=0)
        elif symbol not in self.hold_open:
            fill = self.price(symbol)
            order.update(status="COMPLETE", filled_quantity=quantity, pending_quantity=0, average_price=fill)
            self.fills.append({**params, "quantity": quantity, "price": fill})
        self.order_book.append(order)
        return order_id

    def complete(self, order_id):
        """Fill an order held open."""
        order = next(o for o in self.order_book if o["order_id"] == order_id)
        fill = self.price(order["tradingsymbol"])
        order.update(
            status="COMPLETE", filled_quantity=order["quantity"], pending_quantity=0, average_price=fill
        )
        self.fills.append(
            {
                "exchange": order["exchange"],
                "tradingsymbol": order["tradingsymbol"],
                "transaction_type": order["transaction_type"],
                "product": order["product"],
                "quantity": order["quantity"],
                "price": fill,
            }
        )

    def orders(self):
        self._check("orders")
        return [dict(o) for o in self.order_book]

    def positions(self):
        """Net rows per (exchange, tradingsymbol, product), with Kite's P&L formula."""
        self._check("positions")
        rows = {}
        fills = self.fills[: len(self.fills) - self.lag_fills] if self.lag_fills else self.fills
        for row in self.overnight:
            rows[(row["exchange"], row["tradingsymbol"], row["product"])] = dict(row)
        for f in fills:
            key = (f["exchange"], f["tradingsymbol"], f["product"])
            r = rows.setdefault(
                key,
                {
                    "exchange": f["exchange"],
                    "tradingsymbol": f["tradingsymbol"],
                    "product": f["product"],
                    "buy_quantity": 0,
                    "buy_value": 0.0,
                    "sell_quantity": 0,
                    "sell_value": 0.0,
                    "day_buy_quantity": 0,
                    "day_sell_quantity": 0,
                    "multiplier": 1,
                },
            )
            side = "buy" if f["transaction_type"] == "BUY" else "sell"
            r[f"{side}_quantity"] += f["quantity"]
            r[f"{side}_value"] += f["quantity"] * f["price"]
            r[f"day_{side}_quantity"] = r.get(f"day_{side}_quantity", 0) + f["quantity"]
        net = []
        for r in rows.values():
            qty = r["buy_quantity"] - r["sell_quantity"]
            last = self.price(r["tradingsymbol"])
            if qty < 0:
                avg = r["sell_value"] / r["sell_quantity"]
            elif qty > 0:
                avg = r["buy_value"] / r["buy_quantity"]
            else:
                avg = 0.0
            pnl = r["sell_value"] - r["buy_value"] + qty * last * r["multiplier"]
            net.append({**r, "quantity": qty, "average_price": avg, "last_price": last, "pnl": pnl})
        return {"net": net, "day": []}
