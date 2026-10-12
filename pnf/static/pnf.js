// Point & Figure construction from a close-price series.
//
// Box size is fixed for the whole chart: boxPct of the first close. Rows are
// integer box indexes on a grid anchored at that first close, so row k is the
// price p0 + k * box. An X column's top is the highest row the price has
// reached (floor); an O column's bottom is the lowest (ceil). A new column
// starts only after a move of `reversal` boxes against the current one.

const EPS = 1e-9;

/**
 * @param {Array<[number, number, number?]>} bars  [timestamp, close, volume?], oldest first
 * @param {{boxPct?: number, reversal?: number}} [opts]
 * @returns {null | {
 *   p0: number, box: number, reversal: number, smaPeriod: number, sma: Array<number|null>,
 *   columns: Array<{dir: 'X'|'O', boxes: Array<{row: number, ts: number}>, hi: number, lo: number,
 *     startTs: number, endTs: number}>,
 *   vwap: Array<number|null>, vwapAt: (ts: number) => number|null,
 *   hi: number, lo: number, price: (row: number) => number, rowOf: (price: number) => number,
 * }}
 */
export function buildPnf(bars, { boxPct = 0.5, reversal = 3, smaPeriod = 10 } = {}) {
  if (!bars.length) return null;
  const p0 = bars[0][1];
  const box = (p0 * boxPct) / 100;
  if (!(box > 0)) return null;

  const rowOf = (p) => (p - p0) / box;
  // `|| 0` turns -0 (from ceil of a tiny negative) into a plain 0 row.
  const upRow = (p) => Math.floor(rowOf(p) + EPS) || 0;
  const downRow = (p) => Math.ceil(rowOf(p) - EPS) || 0;

  const columns = [];
  let col = null;

  const open = (dir, from, to, ts) => {
    // startTs / endTs: the first and last bars processed while this was the current column.
    col = { dir, boxes: [], hi: Math.max(from, to), lo: Math.min(from, to), startTs: ts, endTs: ts };
    const step = dir === 'X' ? 1 : -1;
    for (let r = from; dir === 'X' ? r <= to : r >= to; r += step) col.boxes.push({ row: r, ts });
    columns.push(col);
  };

  for (const [ts, p] of bars) {
    if (!col) {
      const u = upRow(p);
      const d = downRow(p);
      if (u >= 1) open('X', 1, u, ts);
      else if (d <= -1) open('O', -1, d, ts);
      continue;
    }
    if (col.dir === 'X') {
      const u = upRow(p);
      if (u > col.hi) {
        for (let r = col.hi + 1; r <= u; r++) col.boxes.push({ row: r, ts });
        col.hi = u;
      } else {
        const d = downRow(p);
        if (d <= col.hi - reversal) open('O', col.hi - 1, d, ts);
      }
    } else {
      const d = downRow(p);
      if (d < col.lo) {
        for (let r = col.lo - 1; r >= d; r--) col.boxes.push({ row: r, ts });
        col.lo = d;
      } else {
        const u = upRow(p);
        if (u >= col.lo + reversal) open('X', col.lo + 1, u, ts);
      }
    }
    col.endTs = ts;
  }

  let hi = 0;
  let lo = 0;
  for (const c of columns) {
    hi = Math.max(hi, c.hi);
    lo = Math.min(lo, c.lo);
  }
  const price = (row) => p0 + row * box;
  const vwapByBar = intradayVwap(bars);
  const vwapByTs = new Map(bars.map((b, i) => [b[0], vwapByBar[i]]));
  return {
    p0, box, reversal, columns, hi, lo, price, rowOf,
    signals: findSignals(columns),
    smaPeriod,
    sma: columnSma(columns, smaPeriod).map((row) => (row === null ? null : price(row))),
    // Intraday VWAP as it stood when each column last updated.
    vwap: columns.map((col) => vwapByTs.get(col.endTs) ?? null),
    /** Intraday VWAP as of the bar at `ts` (null if no such bar or no volume yet). */
    vwapAt: (ts) => vwapByTs.get(ts) ?? null,
  };
}

const IST_OFFSET_S = 5.5 * 3600;
/** IST calendar day number of an epoch-seconds timestamp (identifies the session). */
export const istDay = (ts) => Math.floor((ts + IST_OFFSET_S) / 86400);

/**
 * Intraday VWAP - the standard straddle VWAP: each 1-minute close (CE + PE)
 * weighted by that minute's volume (CE + PE), accumulated from the session
 * open and restarted every session. This is the average price the straddle
 * actually traded at today. Null until the session has traded volume.
 * @param bars [timestamp, close, volume] in time order
 * @returns {Array<number|null>} one value per bar
 */
export function intradayVwap(bars) {
  const out = [];
  let day = null;
  let pv = 0;
  let volume = 0;
  for (const [ts, close, v = 0] of bars) {
    if (istDay(ts) !== day) {
      day = istDay(ts);
      pv = 0;
      volume = 0;
    }
    pv += close * v;
    volume += v;
    out.push(volume > 0 ? pv / volume : null);
  }
  return out;
}

/**
 * Simple moving average of column midpoints - the usual P&F moving average.
 * Element i averages the midpoints ((hi + lo) / 2, in rows) of columns
 * i - period + 1 .. i; it is null until `period` columns exist.
 * @returns {Array<number|null>} one value per column, in rows
 */
export function columnSma(columns, period) {
  const out = [];
  let sum = 0;
  columns.forEach((col, i) => {
    sum += (col.hi + col.lo) / 2;
    if (i >= period) sum -= (columns[i - period].hi + columns[i - period].lo) / 2;
    out.push(i >= period - 1 ? sum / period : null);
  });
  return out;
}

/**
 * Double top buy: an X column rises one box above the top of the previous X
 * column. Double bottom sell: an O column falls one box below the bottom of
 * the previous O column. Each signal points at the breakout box.
 *
 * @returns {Array<{type: 'buy'|'sell', col: number, row: number, level: number, ts: number}>}
 *   `level` is the previous column's top (buy) or bottom (sell) row.
 */
export function findSignals(columns) {
  const signals = [];
  for (let i = 2; i < columns.length; i++) {
    const col = columns[i];
    const prev = columns[i - 2]; // columns alternate, so i-2 has the same direction
    if (col.dir === 'X' && col.hi > prev.hi) {
      const row = prev.hi + 1;
      signals.push({ type: 'buy', col: i, row, level: prev.hi, ts: col.boxes.find((b) => b.row === row).ts });
    } else if (col.dir === 'O' && col.lo < prev.lo) {
      const row = prev.lo - 1;
      signals.push({ type: 'sell', col: i, row, level: prev.lo, ts: col.boxes.find((b) => b.row === row).ts });
    }
  }
  return signals;
}

/**
 * Whether a newly confirmed signal should raise an alert. Only a double
 * bottom sell alerts, and only when the straddle (`price`, the confirming
 * close) is below its VWAP at that moment - unknown VWAP means no alert.
 * A double top buy never alerts. Chart markers are drawn for every signal
 * regardless.
 */
export function alertWanted(signal, price, vwap) {
  return signal.type === 'sell' && vwap != null && price < vwap;
}
