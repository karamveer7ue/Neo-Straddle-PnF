// The algo's numbers and words for the page (no DOM, so it is unit-tested in Node:
// tests/algo.test.mjs). The algo itself runs in the server (algo/core.py); this only
// describes the state it pushes on /algo/ws.

export const MINUS = '−';
const IST = 'Asia/Kolkata';
const clockFmt = new Intl.DateTimeFormat('en-GB', { timeZone: IST, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const dateFmt = new Intl.DateTimeFormat('en-GB', { timeZone: IST, day: '2-digit', month: 'short' });

// The word joiner (U+2060) keeps the sign with the amount: a line may otherwise break after a "+".
export const fmtRupees = (v) =>
  `${Math.round(v) >= 0 ? '+' : MINUS}⁠₹${Math.abs(Math.round(v)).toLocaleString('en-IN')}`;
export const fmtInr = (v) => `₹${Math.round(v).toLocaleString('en-IN')}`;
export const fmtQty = (q) => Math.abs(q).toLocaleString('en-IN');
export const fmtPrice = (p) =>
  p == null ? '–' : p.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const fmtClock = (epoch) => clockFmt.format(epoch * 1000);
export const fmtExpiry = (iso) => (iso ? dateFmt.format(new Date(`${iso}T00:00:00+05:30`)) : '');
export const chartLabel = (offset) => (offset === 0 ? 'ATM' : `ATM ${offset > 0 ? '+' : MINUS} ${Math.abs(offset)}`);

/** Whole rupees typed with or without separators ("1,00,000"); NaN when not a number. */
export function parseRupees(text) {
  const digits = String(text).replace(/[,\s₹]/g, '');
  return /^\d+$/.test(digits) ? Number(digits) : NaN;
}

/**
 * Whether a state from the server may replace the one shown. States are numbered per server
 * run (`boot`), and an older one never replaces a newer one: the answer to an Exit can arrive
 * after the push of the fills that closed the trade.
 */
export const supersedes = (shown, incoming) =>
  !shown || !incoming.boot || shown.boot !== incoming.boot || incoming.version >= shown.version;

export const openTrades = (s) => (s ? s.trades.filter((t) => t.active) : []);
export const managedTrades = (s) => openTrades(s).filter((t) => t.managed);

/** The algo trades (today's, and any still open) on the straddle a chart shows. */
export const tradesOn = (s, def) =>
  s && def?.ce && def?.pe ? s.trades.filter((t) => t.legs.CE.key === def.ce.key && t.legs.PE.key === def.pe.key) : [];

/**
 * A trade's P&L at the live prices: what it booked, plus each open leg at its live
 * price. Without a live price for an open leg it is the server's figure (at the last
 * prices it saw).
 */
export function tradePnl(trade, livePrice = () => null) {
  let total = trade.booked;
  for (const leg of Object.values(trade.legs)) {
    if (!leg.open) continue;
    const price = livePrice(leg.key);
    if (price == null || leg.avg == null) return trade.pnl;
    total += (leg.avg - price) * leg.open;
  }
  return total;
}

/** Today's algo trades in the current mode: how many, and their P&L at the live prices. */
export function daySummary(s, livePrice) {
  const trades = s.trades.filter((t) => t.day === s.day && t.mode === s.mode);
  return { count: trades.length, pnl: trades.reduce((sum, t) => sum + tradePnl(t, livePrice), 0) };
}

/** The Algo button: data-state for its colours, and its words. */
export function switchLabel(s) {
  if (!s) return { state: 'off', text: 'Off' };
  if (s.on) return { state: s.mode, text: `On · ${s.mode === 'live' ? 'Live' : 'Paper'}` };
  const managing = managedTrades(s).length;
  if (managing) return { state: 'managing', text: `Off · managing ${managing}` };
  return { state: 'off', text: 'Off' };
}

/** What a trade is doing (`what`) and the levels that close it (`levels`). */
export function describeTrade(t) {
  const short = `Short ${fmtQty(t.holding)} @ ${fmtPrice(t.entry)}`;
  if (t.state === 'entering') return { what: `Selling ${fmtQty(t.qty)} at market…`, levels: '' };
  if (t.state === 'failed') {
    return { what: t.active ? 'Entry failed: buying back what was sold' : 'Entry failed', levels: t.reason };
  }
  if (t.state === 'closed') return { what: `Closed: ${t.reasonText}`, levels: exitsText(t) };
  if (!t.managed) return { what: `Yours: ${short}`, levels: 'Algo Off: no targets or stops' };
  if (t.want < t.holding) return { what: `Buying back ${fmtQty(t.holding - t.want)}…`, levels: short };
  // The stop first: on a narrow chart the end of the line is cut off, and the targets are drawn.
  const stop = `Stop: X above ${t.stop ? fmtPrice(t.stop.level) : 'VWAP / VWMA 10'}`;
  if (t.halfDone) {
    return { what: `${short} · half booked`, levels: `${stop} or back at ${fmtPrice(t.entry)} · T2 ${fmtPrice(t.target2)}` };
  }
  return { what: short, levels: `${stop} · T1 ${fmtPrice(t.target1)} · T2 ${fmtPrice(t.target2)}` };
}

/** "bought back 1,690 @ 211.00", or per exit: "First target 845 @ 198.00 · Full target 845 @ 178.00" */
export function exitsText(t) {
  const done = t.exits.filter((e) => e.units);
  if (done.length === 1) return `bought back ${fmtQty(done[0].units)} @ ${fmtPrice(done[0].price)}`;
  return done.map((e) => `${e.text.split(':')[0]} ${fmtQty(e.units)} @ ${fmtPrice(e.price)}`).join(' · ');
}

/**
 * Marks for one chart: lines at an open, managed trade's stop, entry (its breakeven stop
 * once half is booked) and targets, most important first (the chart labels them in this
 * order); points where trades sold and bought back.
 */
export function chartMarks(trades) {
  const lines = [];
  const points = [];
  for (const t of trades) {
    if (t.state !== 'failed' && t.entry != null) points.push({ ts: t.signal.ts, price: t.entry, kind: 'sell' });
    for (const e of t.exits) {
      if (e.units && e.price != null) points.push({ ts: e.at - 60, price: e.price, kind: 'buy' });
    }
    if (!t.active || t.state !== 'open' || t.entry == null) continue;
    if (!t.managed) {
      lines.push({ price: t.entry, kind: 'entry', label: 'Sold' });
      continue;
    }
    if (t.stop) lines.push({ price: t.stop.level, kind: 'stop', label: 'Stop' });
    if (t.halfDone) {
      lines.push({ price: t.entry, kind: 'stop', label: 'BE' });
    } else {
      lines.push({ price: t.entry, kind: 'entry', label: 'Sold' });
      lines.push({ price: t.target1, kind: 'target', label: 'T1' });
    }
    lines.push({ price: t.target2, kind: 'target', label: 'T2' });
  }
  return { lines, points };
}

/** How an algo event shows: toast style, icon and sound. */
export function eventStyle(ev) {
  switch (ev.kind) {
    case 'opened':
      return { kind: 'sell', icon: '▼', tone: 'open' };
    case 'booked':
      return { kind: 'ok', icon: '✓', tone: 'book' };
    case 'closed':
      return ev.pnl != null && ev.pnl < 0
        ? { kind: 'loss', icon: '■', tone: 'close' }
        : { kind: 'ok', icon: '✓', tone: 'close' };
    case 'failed':
      return { kind: 'error', icon: '!', tone: 'alarm' };
    default: // skipped, state
      return { kind: 'info', icon: 'i', tone: null };
  }
}

/** The rows of the confirmation shown before switching On. */
export function onSummary(s) {
  const m = s.summary;
  const st = s.settings;
  const lots = m.lotSize ? m.qty / m.lotSize : null;
  const third = Math.round((m.target / 3) * 100) / 100;
  return [
    ['Mode', m.mode === 'live' ? 'Live: real orders through Kite (MIS)' : 'Paper: no orders, trades recorded at chart prices'],
    ['Charts', `${m.underlying} · ${fmtExpiry(m.expiry)} expiry · ${m.strikes.map((k) => k.toLocaleString('en-IN')).join(' / ')}`],
    ['Quantity', `${fmtQty(m.qty)} per trade${lots ? ` (${lots} lots of ${m.lotSize})` : ''}`],
    ['Target', `${m.target} points; half booked at ${third}`],
    ['At once', `${st.maxOpen} trade${st.maxOpen > 1 ? 's' : ''} across the three charts`],
    ['Times', `entries until ${st.lastEntry}; everything closed at ${st.exitTime}`],
    ['Limits', `${fmtInr(st.maxLoss)} per trade, ${fmtInr(st.dailyLoss)} per day, ${st.maxTrades} trades a day`],
  ];
}

/** A quantity's problem for an index ("" when fine); the server checks it again. */
export function qtyProblem(qty, lot, cap) {
  if (!Number.isInteger(qty) || qty < 1) return 'Enter a quantity';
  if (cap && qty > cap) return `At most ${fmtQty(cap)} (twice the default)`;
  if (lot && qty % lot) return `A multiple of the lot size, ${lot}`;
  if (lot && qty < 2 * lot) return `At least 2 lots (${2 * lot})`;
  return '';
}
