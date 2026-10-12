// Simulated short-straddle trades for the simulator (no DOM; unit-tested in Node).
//
// One trade at a time per chart: Sell opens a fixed number of lots at the
// price on screen; "Exit 50%" buys back half of the open lots (whole lots,
// rounded down); "Exit Full" buys back the rest. A short straddle gains
// when its price falls: points = entry - current, rupees = points x quantity.
// Brokerage and taxes are ignored.

export const LOTS_PER_SELL = 10;

export class ShortStraddleTrade {
  /**
   * @param {number} lotSize units per lot
   * @param {number} entry straddle price sold at
   * @param {number} ts the minute it was sold in (epoch seconds)
   * @param {number} [lots]
   */
  constructor(lotSize, entry, ts, lots = LOTS_PER_SELL) {
    this.lotSize = lotSize;
    this.entry = entry;
    this.ts = ts;
    this.lots = lots; // sold
    this.openLots = lots;
    this.booked = 0; // rupees realised by exits so far
    this.exits = []; // {lots, price, ts}
  }

  get isOpen() {
    return this.openLots > 0;
  }

  /** Lots "Exit 50%" would close now: half of the open lots, rounded down (0 when only 1 is left). */
  get halfLots() {
    return Math.floor(this.openLots / 2);
  }

  exit(lots, price, ts) {
    const n = Math.min(lots, this.openLots);
    if (n <= 0) return 0;
    this.booked += (this.entry - price) * n * this.lotSize;
    this.openLots -= n;
    this.exits.push({ lots: n, price, ts });
    return n;
  }

  exitHalf(price, ts) {
    return this.exit(this.halfLots, price, ts);
  }

  exitFull(price, ts) {
    return this.exit(this.openLots, price, ts);
  }

  /** P&L at `price`: open (points per unit and rupees), booked, and total rupees. */
  pnl(price) {
    const openPts = this.isOpen ? this.entry - price : 0;
    const openRs = openPts * this.openLots * this.lotSize;
    return { openPts, openRs, booked: this.booked, total: openRs + this.booked };
  }

  /** Points per unit over the whole trade (exited lots at their exit prices, the rest at `price`). */
  totalPoints(price) {
    const exited = this.exits.reduce((s, e) => s + (this.entry - e.price) * e.lots, 0);
    return (exited + (this.entry - price) * this.openLots) / this.lots;
  }
}
