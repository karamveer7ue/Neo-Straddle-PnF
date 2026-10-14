import assert from 'node:assert/strict';
import { test } from 'node:test';

import { checkQuantity, fmtRupees, groupKey, legPnl, summarize } from '../kite/static/positions.js';

// A short NIFTY straddle as the server sends it: 10 lots of 65, sold at 120 + 110.
const leg = (kind, quantity, averagePrice, lastPrice, extra = {}) => ({
  kind,
  quantity,
  averagePrice,
  lastPrice,
  multiplier: 1,
  lotSize: 65,
  product: 'NRML',
  tradingsymbol: `NIFTY26O1322300${kind}`,
  // Kite's formula for a position opened today: sell value - buy value + quantity x last price.
  pnl: -quantity * averagePrice + quantity * lastPrice,
  ...extra,
});

const group = (legs) => ({ key: 'NIFTY|2026-10-13|22300', legs, pending: [], open: true });

test('leg P&L moves to a fresher price exactly', () => {
  const ce = leg('CE', -650, 120, 118); // Kite: +1,300
  assert.equal(ce.pnl, 1300);
  assert.equal(legPnl(ce, null), 1300);
  assert.equal(legPnl(ce, 115), 650 * 5); // short: a lower price is a gain
  assert.equal(legPnl(ce, 125), -650 * 5);
});

test('a short straddle summary uses live prices where given', () => {
  const g = group([leg('CE', -650, 120, 118), leg('PE', -650, 110, 112)]);
  const atKite = summarize(g);
  assert.equal(atKite.pnl, 1300 - 1300);
  assert.deepEqual([atKite.short, atKite.entry, atKite.now], [650, 230, 230]);
  assert.equal(atKite.lots, 10);
  assert.ok(atKite.canHalf && atKite.open);

  const live = summarize(g, (l) => (l.kind === 'CE' ? 100 : null)); // only the CE has a live price
  assert.equal(live.pnl, 650 * 20 - 1300);
  assert.equal(live.now, 100 + 112);
});

test('unequal legs are not summed into one straddle price', () => {
  const s = summarize(group([leg('CE', -650, 120, 118), leg('PE', -325, 110, 112)]));
  assert.equal(s.short, null);
  assert.equal(s.entry, null);
  assert.ok(s.canHalf);
});

test('one lot left cannot be halved; a closed group is not open', () => {
  assert.equal(summarize(group([leg('CE', -65, 120, 118), leg('PE', -65, 110, 112)])).canHalf, false);
  const closed = summarize(group([leg('CE', 0, 0, 118, { pnl: 975 }), leg('PE', 0, 0, 112, { pnl: -325 })]));
  assert.equal(closed.open, false);
  assert.equal(closed.canHalf, false);
  assert.equal(closed.pnl, 650); // realised only: a fresh price changes nothing at quantity 0
  assert.equal(summarize(group(closed.legs), () => 50).pnl, 650);
});

test('quantity must be whole lots within the limit', () => {
  assert.deepEqual(checkQuantity(650, 65, 20), { ok: true, lots: 10 });
  assert.match(checkQuantity(600, 65, 20).error, /multiple of the lot size, 65/);
  assert.match(checkQuantity(65 * 21, 65, 20).error, /20-lot limit/);
  assert.match(checkQuantity(0, 65, 20).error, /Enter a quantity/);
  assert.match(checkQuantity(Number(''), 65, 20).error, /Enter a quantity/);
  assert.match(checkQuantity(65, null, 20).error, /Lot size unknown/);
});

test('formatting and keys', () => {
  assert.equal(fmtRupees(123456.4), '+₹1,23,456');
  assert.equal(fmtRupees(-1300), '−₹1,300');
  assert.equal(groupKey('NIFTY', '2026-10-13', 22300), 'NIFTY|2026-10-13|22300');
});
