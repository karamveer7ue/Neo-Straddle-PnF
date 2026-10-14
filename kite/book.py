"""Positions placed through this app, grouped into straddles, and exit sizing.

Pure functions over Kite's position and order rows; nothing here talks to
the network, so it is unit-tested directly (tests/test_kite.py).

A *group* is one straddle: the CE and PE of an underlying, expiry and strike.
Kite nets positions per instrument and product, so a group's legs are the
net rows of its two contracts (one per product used).
"""

from dataclasses import dataclass
from datetime import date

from .contracts import Contract, group_key

TERMINAL = {"COMPLETE", "CANCELLED", "REJECTED"}  # every other order status is still working
BUY, SELL = "BUY", "SELL"


def contract_to_json(c: Contract) -> dict:
    return {
        "exchange": c.exchange,
        "tradingsymbol": c.tradingsymbol,
        "instrumentToken": c.instrument_token,
        "underlying": c.underlying,
        "expiry": c.expiry.isoformat(),
        "strike": c.strike,
        "kind": c.kind,
        "lotSize": c.lot_size,
    }


def contract_from_json(d: dict) -> Contract:
    return Contract(
        exchange=d["exchange"],
        tradingsymbol=d["tradingsymbol"],
        instrument_token=int(d["instrumentToken"]),
        underlying=d["underlying"],
        expiry=date.fromisoformat(d["expiry"]),
        strike=int(d["strike"]),
        kind=d["kind"],
        lot_size=int(d["lotSize"]),
    )


@dataclass(frozen=True)
class OrderRequest:
    contract: Contract
    side: str  # BUY | SELL
    quantity: int
    product: str


def is_working(order: dict) -> bool:
    return str(order.get("status", "")).upper() not in TERMINAL


def pending_quantity(orders: list[dict], contract: Contract, product: str, side: str) -> int:
    """Quantity still to fill on working orders for this contract, product and side."""
    total = 0
    for o in orders:
        if (
            is_working(o)
            and o.get("exchange") == contract.exchange
            and o.get("tradingsymbol") == contract.tradingsymbol
            and o.get("product") == product
            and o.get("transaction_type") == side
        ):
            total += int(o.get("pending_quantity") or 0)
    return total


def position_rows(net: list[dict], contract: Contract) -> list[dict]:
    """Kite's net rows for one contract (one per product)."""
    return [
        p
        for p in net
        if p.get("exchange") == contract.exchange and p.get("tradingsymbol") == contract.tradingsymbol
    ]


def plan_exit(
    contracts: list[Contract], net: list[dict], orders: list[dict], fraction: float
) -> list[OrderRequest]:
    """Orders that close `fraction` (0.5 or 1) of each open leg of a straddle.

    Each leg is closed with its own product (an exit in another product would
    open a new position instead). Quantity already on its way out - working
    exit orders - is left out, so pressing Exit twice never flips a short
    into a long. A half exit closes half of the lots still to exit, rounded
    down to whole lots; a leg with a single lot left is not halved.
    """
    plan = []
    for contract in contracts:
        for row in position_rows(net, contract):
            qty = int(row.get("quantity") or 0)
            if not qty:
                continue
            product = row["product"]
            side = BUY if qty < 0 else SELL
            available = abs(qty) - pending_quantity(orders, contract, product, side)
            if available <= 0:
                continue
            if fraction >= 1:
                n = available
            else:
                n = (available // contract.lot_size // 2) * contract.lot_size
            if n > 0:
                plan.append(OrderRequest(contract, side, n, product))
    return plan


def positions_behind(contracts: list[Contract], net: list[dict], orders: list[dict]) -> bool:
    """True when Kite's positions do not yet include every fill its order book shows.

    Positions can lag the order book for a moment after a fill. Sizing an exit
    from positions that miss an earlier exit's fill would exit that quantity
    twice and turn the leg long, so such a pair of replies must be re-read.
    Today's fills are compared with the position rows' day quantities; rows
    without those fields are not judged.
    """
    for c in contracts:
        filled: dict[tuple[str, str], int] = {}
        for o in orders:
            if o.get("exchange") == c.exchange and o.get("tradingsymbol") == c.tradingsymbol:
                key = (o.get("product"), o.get("transaction_type"))
                filled[key] = filled.get(key, 0) + int(o.get("filled_quantity") or 0)
        rows = {r.get("product"): r for r in position_rows(net, c)}
        for (product, side), qty in filled.items():
            if not qty:
                continue
            row = rows.get(product)
            if row is None:
                return True  # filled, yet no position row at all
            field = "day_buy_quantity" if side == BUY else "day_sell_quantity"
            if field in row and int(row[field] or 0) < qty:
                return True
    return False


def working_entries(contracts: list[Contract], net: list[dict], orders: list[dict]) -> list[dict]:
    """Working orders that add to an open leg (e.g. a Sell still open outside the
    market-protection band): they can reopen a position after it is exited."""
    found = []
    for c in contracts:
        for row in position_rows(net, c):
            qty = int(row.get("quantity") or 0)
            if not qty:
                continue
            side = SELL if qty < 0 else BUY  # the side that grows this leg
            pending = pending_quantity(orders, c, row["product"], side)
            if pending:
                found.append({"kind": c.kind, "side": side, "quantity": pending})
    return found


def group_json(contracts: list[Contract], net: list[dict], orders: list[dict]) -> dict:
    """One straddle's legs and working orders, as sent to the browser.

    Per leg, the browser gets what it needs to re-price the P&L live:
    Kite's `pnl` is linear in `last_price` with slope quantity x multiplier.
    """
    first = contracts[0]
    legs, pending = [], []
    for contract in contracts:
        for row in position_rows(net, contract):
            legs.append(
                {
                    "kind": contract.kind,
                    "exchange": contract.exchange,
                    "tradingsymbol": contract.tradingsymbol,
                    "product": row.get("product"),
                    "quantity": int(row.get("quantity") or 0),
                    "averagePrice": float(row.get("average_price") or 0),
                    "lastPrice": float(row.get("last_price") or 0),
                    "pnl": float(row.get("pnl") or 0),
                    "multiplier": float(row.get("multiplier") or 1),
                    "lotSize": contract.lot_size,
                }
            )
        for o in orders:
            if (
                is_working(o)
                and o.get("exchange") == contract.exchange
                and o.get("tradingsymbol") == contract.tradingsymbol
            ):
                pending.append(
                    {
                        "orderId": str(o.get("order_id")),
                        "kind": contract.kind,
                        "side": o.get("transaction_type"),
                        "quantity": int(o.get("pending_quantity") or 0),
                        "status": o.get("status"),
                    }
                )
    legs.sort(key=lambda leg: (leg["kind"], leg["product"] or ""))
    return {
        "key": group_key(first.underlying, first.expiry, first.strike),
        "underlying": first.underlying,
        "expiry": first.expiry.isoformat(),
        "strike": first.strike,
        "legs": legs,
        "pending": pending,
        "open": any(leg["quantity"] for leg in legs),
    }


def straddles(tracked: list[Contract]) -> dict[str, list[Contract]]:
    """Tracked contracts by straddle group key (a group may know only one leg)."""
    groups: dict[str, list[Contract]] = {}
    for c in tracked:
        groups.setdefault(group_key(c.underlying, c.expiry, c.strike), []).append(c)
    for legs in groups.values():
        legs.sort(key=lambda c: c.kind)
    return groups
