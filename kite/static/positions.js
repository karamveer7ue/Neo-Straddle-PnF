// Position maths for the Kite trading UI (no DOM; unit-tested in Node).
//
// The server sends each straddle traded from this app as a group of legs,
// one per contract and product, with Kite's own P&L at Kite's last price.
// Kite's P&L is linear in the price, with slope quantity x multiplier, so
// moving it to a fresher price (the live feed's) is exact:
//   pnl(live) = pnl + quantity x multiplier x (live - lastPrice)

/** The group key of a chart's straddle, as the server builds it. */
export const groupKey = (underlying, expiry, strike) => `${underlying}|${expiry}|${strike}`;

/** A leg's P&L in rupees at `price` (Kite's own figure when there is no fresher price). */
export function legPnl(leg, price) {
  if (price == null || !Number.isFinite(price)) return leg.pnl;
  return leg.pnl + leg.quantity * leg.multiplier * (price - leg.lastPrice);
}

/**
 * Summary of one group at the latest prices.
 * @param {object} group from the server: {legs, pending, open, ...}
 * @param {(leg) => number|null} priceOf a fresher price for a leg, or null
 * @returns {{pnl: number, open: boolean, short: number|null, entry: number|null, now: number|null,
 *            lots: number, canHalf: boolean, legs: object[]}}
 *   `short`: units short per leg when both legs are equally short (a plain short straddle);
 *   `entry`/`now`: straddle price sold at and now (CE + PE), for such a straddle.
 */
export function summarize(group, priceOf = () => null) {
  let pnl = 0;
  const legs = group.legs.map((leg) => {
    const live = priceOf(leg);
    const price = live ?? leg.lastPrice;
    const legValue = legPnl(leg, live);
    pnl += legValue;
    return { ...leg, price, pnl: legValue };
  });
  const open = legs.filter((l) => l.quantity !== 0);
  const ce = open.filter((l) => l.kind === 'CE');
  const pe = open.filter((l) => l.kind === 'PE');
  let short = null;
  let entry = null;
  let now = null;
  if (ce.length === 1 && pe.length === 1 && ce[0].quantity < 0 && ce[0].quantity === pe[0].quantity) {
    short = -ce[0].quantity;
    entry = ce[0].averagePrice + pe[0].averagePrice;
    now = ce[0].price + pe[0].price;
  }
  const lotsOf = (l) => Math.floor(Math.abs(l.quantity) / l.lotSize);
  return {
    pnl,
    open: open.length > 0,
    short,
    entry,
    now,
    lots: open.reduce((m, l) => Math.max(m, lotsOf(l)), 0),
    // Exit 50% closes whole lots: a leg needs at least 2 to be halved.
    canHalf: open.some((l) => lotsOf(l) >= 2),
    legs,
  };
}

/**
 * Check a quantity typed for a sell.
 * @returns {{ok: boolean, lots?: number, error?: string}}
 */
export function checkQuantity(quantity, lotSize, maxLots) {
  if (!lotSize) return { ok: false, error: 'Lot size unknown until Kite is connected' };
  if (!Number.isInteger(quantity) || quantity <= 0) return { ok: false, error: 'Enter a quantity' };
  if (quantity % lotSize) return { ok: false, error: `Must be a multiple of the lot size, ${lotSize}` };
  const lots = quantity / lotSize;
  if (maxLots && lots > maxLots) return { ok: false, lots, error: `Above the ${maxLots}-lot limit (KITE_MAX_LOTS)` };
  return { ok: true, lots };
}

export const fmtRupees = (v) =>
  `${v >= 0 ? '+' : '−'}₹${Math.abs(Math.round(v)).toLocaleString('en-IN')}`;

export const fmtQty = (q) => Math.abs(q).toLocaleString('en-IN');
