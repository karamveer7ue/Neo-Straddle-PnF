import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  chartLabel,
  chartMarks,
  daySummary,
  describeTrade,
  eventStyle,
  fmtPrice,
  fmtRupees,
  onSummary,
  parseRupees,
  qtyProblem,
  supersedes,
  switchLabel,
  tradePnl,
  tradesOn,
} from '../algo/static/model.js';

// A trade as the server sends it (algo/runner.py _trade_json): NIFTY 1690 sold at 208.
const trade = (over = {}) => ({
  id: '2026-10-12-1',
  day: '2026-10-12',
  mode: 'paper',
  name: 'NIFTY 22,300 ATM',
  offset: 0,
  strike: 22300,
  qty: 1690,
  state: 'open',
  active: true,
  managed: true,
  want: 1690,
  holding: 1690,
  halfDone: false,
  entry: 208,
  target1: 198,
  target2: 178,
  stop: { level: 210.68, at: 1791779040 },
  legs: { CE: { key: 'nse_fo|22300CE', open: 1690, avg: 124.8 }, PE: { key: 'nse_fo|22300PE', open: 1690, avg: 83.2 } },
  booked: 0,
  pnl: -1690,
  signal: { ts: 1791778980, close: 208 },
  exits: [],
  reason: '',
  reasonText: '',
  ...over,
});

const state = (over = {}) => ({
  on: false,
  mode: 'paper',
  day: '2026-10-12',
  trades: [],
  summary: { underlying: 'NIFTY', expiry: '2026-10-13', strikes: [22100, 22300, 22500], mode: 'paper', qty: 1690, lotSize: 65, target: 30 },
  settings: { maxOpen: 1, lastEntry: '14:45', exitTime: '15:12', maxLoss: 100000, dailyLoss: 100000, maxTrades: 5 },
  ...over,
});

test('live P&L re-prices the open legs; without a live price it is the server figure', () => {
  const t = trade({ booked: 8450, legs: { CE: { key: 'c', open: 845, avg: 124.8 }, PE: { key: 'p', open: 845, avg: 83.2 } } });
  const prices = { c: 120, p: 80 }; // 200: 8 points on 845
  assert.equal(tradePnl(t, (k) => prices[k]), 8450 + 845 * 8);
  assert.equal(tradePnl(t, () => null), t.pnl);
  assert.equal(tradePnl(trade({ state: 'closed', booked: 33800, pnl: 33800, legs: { CE: { open: 0 }, PE: { open: 0 } } })), 33800);
});

test('the Algo button says Off, On with its mode, or how many trades it still manages', () => {
  assert.deepEqual(switchLabel(null), { state: 'off', text: 'Off' });
  assert.deepEqual(switchLabel(state()), { state: 'off', text: 'Off' });
  assert.deepEqual(switchLabel(state({ on: true })), { state: 'paper', text: 'On · Paper' });
  assert.deepEqual(switchLabel(state({ on: true, mode: 'live' })), { state: 'live', text: 'On · Live' });
  assert.deepEqual(switchLabel(state({ trades: [trade()] })), { state: 'managing', text: 'Off · managing 1' });
  assert.equal(switchLabel(state({ trades: [trade({ managed: false })] })).state, 'off');
});

test('a trade is described by what it does and what closes it', () => {
  assert.deepEqual(describeTrade(trade()), {
    what: 'Short 1,690 @ 208.00',
    levels: 'Stop: X above 210.68 · T1 198.00 · T2 178.00',
  });
  assert.deepEqual(describeTrade(trade({ halfDone: true, holding: 845, want: 845 })), {
    what: 'Short 845 @ 208.00 · half booked',
    levels: 'Stop: X above 210.68 or back at 208.00 · T2 178.00',
  });
  assert.equal(describeTrade(trade({ want: 845 })).what, 'Buying back 845…');
  assert.equal(describeTrade(trade({ managed: false })).what, 'Yours: Short 1,690 @ 208.00');
  assert.equal(describeTrade(trade({ state: 'entering', entry: null })).what, 'Selling 1,690 at market…');
  const closed = trade({
    state: 'closed', active: false, holding: 0, reasonText: 'Full target',
    exits: [
      { text: 'First target', units: 845, price: 198 },
      { text: 'Full target', units: 845, price: 178 },
    ],
  }); // prettier-ignore
  assert.deepEqual(describeTrade(closed), {
    what: 'Closed: Full target',
    levels: 'First target 845 @ 198.00 · Full target 845 @ 178.00',
  });
  assert.equal(describeTrade(trade({ state: 'failed', active: false, reason: 'PE rejected' })).levels, 'PE rejected');
});

test('chart marks: entry and targets, then breakeven after the half; sells and buy-backs as points', () => {
  const open = chartMarks([trade()]);
  assert.deepEqual(open.lines, [
    { price: 210.68, kind: 'stop', label: 'Stop' },
    { price: 208, kind: 'entry', label: 'Sold' },
    { price: 198, kind: 'target', label: 'T1' },
    { price: 178, kind: 'target', label: 'T2' },
  ]);
  assert.deepEqual(open.points, [{ ts: 1791778980, price: 208, kind: 'sell' }]);
  const half = chartMarks([trade({ halfDone: true, exits: [{ at: 1791779280, units: 845, price: 198 }] })]);
  assert.deepEqual(half.lines.map((l) => l.label), ['Stop', 'BE', 'T2']);
  assert.deepEqual(half.points[1], { ts: 1791779220, price: 198, kind: 'buy' }); // the bar that closed
  // A trade you took over shows only where it was sold; a closed one only its points.
  assert.deepEqual(chartMarks([trade({ managed: false })]).lines, [{ price: 208, kind: 'entry', label: 'Sold' }]);
  assert.deepEqual(chartMarks([trade({ state: 'closed', active: false })]).lines, []);
});

test('only trades on the chart straddle, and the day in the current mode, are counted', () => {
  const s = state({
    trades: [trade(), trade({ id: 'x', mode: 'live', booked: 999, pnl: 999 }), trade({ id: 'y', day: '2026-10-09' })],
  });
  assert.equal(tradesOn(s, { ce: { key: 'nse_fo|22300CE' }, pe: { key: 'nse_fo|22300PE' } }).length, 3);
  assert.equal(tradesOn(s, { ce: { key: 'other' }, pe: { key: 'nse_fo|22300PE' } }).length, 0);
  assert.deepEqual(daySummary(s, () => null), { count: 1, pnl: -1690 });
});

test('toasts: a sale, a booking, a close in profit or in loss, a failure', () => {
  assert.deepEqual(eventStyle({ kind: 'opened' }), { kind: 'sell', icon: '▼', tone: 'open' });
  assert.equal(eventStyle({ kind: 'booked' }).tone, 'book');
  assert.equal(eventStyle({ kind: 'closed', pnl: 33800 }).kind, 'ok');
  assert.equal(eventStyle({ kind: 'closed', pnl: -5070 }).kind, 'loss');
  assert.deepEqual(eventStyle({ kind: 'failed' }), { kind: 'error', icon: '!', tone: 'alarm' });
  assert.equal(eventStyle({ kind: 'skipped' }).tone, null);
});

test('the On confirmation says exactly what will trade', () => {
  const rows = Object.fromEntries(onSummary(state()));
  assert.equal(rows.Mode, 'Paper: no orders, trades recorded at chart prices');
  assert.equal(rows.Charts, 'NIFTY · 13 Oct expiry · 22,100 / 22,300 / 22,500');
  assert.equal(rows.Quantity, '1,690 per trade (26 lots of 65)');
  assert.equal(rows.Target, '30 points; half booked at 10');
  assert.equal(rows.Times, 'entries until 14:45; everything closed at 15:12');
  assert.equal(rows.Limits, '₹1,00,000 per trade, ₹1,00,000 per day, 5 trades a day');
  const live = Object.fromEntries(onSummary(state({ summary: { ...state().summary, mode: 'live' } })));
  assert.equal(live.Mode, 'Live: real orders through Kite (MIS)');
});

test('quantities and rupees typed in the menu', () => {
  assert.equal(qtyProblem(1690, 65, 3380), '');
  assert.equal(qtyProblem(3445, 65, 3380), 'At most 3,380 (twice the default)');
  assert.equal(qtyProblem(1700, 65, 3380), 'A multiple of the lot size, 65');
  assert.equal(qtyProblem(65, 65, 3380), 'At least 2 lots (130)');
  assert.equal(qtyProblem(0, 65, 3380), 'Enter a quantity');
  assert.equal(parseRupees('1,00,000'), 100000);
  assert.equal(parseRupees('₹ 50000'), 50000);
  assert.ok(Number.isNaN(parseRupees('1L')));
});

test('formatting matches the page', () => {
  assert.equal(fmtRupees(33800), '+\u2060₹33,800');
  assert.equal(fmtRupees(-5070), '−\u2060₹5,070');
  assert.equal(fmtPrice(208), '208.00');
  assert.equal(fmtPrice(null), '–');
  assert.equal(chartLabel(-200), 'ATM − 200');
  assert.equal(chartLabel(500), 'ATM + 500');
});

test('a single buy-back is described once, several one by one', () => {
  const one = trade({ state: 'closed', active: false, reasonText: 'Exit (by you)', exits: [{ text: 'Exit (by you)', units: 1690, price: 202 }] });
  assert.deepEqual(describeTrade(one), { what: 'Closed: Exit (by you)', levels: 'bought back 1,690 @ 202.00' });
});

test('an older state never replaces a newer one from the same server run', () => {
  const at = (version, boot = 'a1') => ({ boot, version });
  assert.equal(supersedes(null, at(3)), true);
  assert.equal(supersedes(at(5), at(6)), true);
  assert.equal(supersedes(at(5), at(5)), true); // the same state again
  assert.equal(supersedes(at(5), at(4)), false); // e.g. the answer to an Exit, after the fills' push
  assert.equal(supersedes(at(5), at(1, 'b2')), true); // the server restarted: it counts from 1 again
  assert.equal(supersedes(at(5), { trades: [] }), true); // the simulator's states are not numbered
});
