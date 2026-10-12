import assert from 'node:assert/strict';
import { test } from 'node:test';

import { buildPnf, columnSma, intradayVwap } from '../pnf/static/pnf.js';

// p0 = 200 with 0.5% boxes gives a box of exactly 1.00, so rows read as rupees.
const bars = (...closes) => closes.map((c, i) => [i, c]);
const shape = (chart) => chart.columns.map((c) => `${c.dir}${c.boxes.map((b) => b.row).join(',')}`);

test('empty series has no chart', () => {
  assert.equal(buildPnf([]), null);
});

test('box size is fixed from the first close', () => {
  const chart = buildPnf(bars(200, 260));
  assert.equal(chart.box, 1);
  assert.equal(chart.price(5), 205);
});

test('no column until price moves a full box from the first close', () => {
  assert.deepEqual(shape(buildPnf(bars(200, 200.9, 199.1))), []);
});

test('first column direction follows the first full-box move', () => {
  assert.deepEqual(shape(buildPnf(bars(200, 203.4))), ['X1,2,3']);
  assert.deepEqual(shape(buildPnf(bars(200, 197.2))), ['O-1,-2']);
});

test('X column extends on new highs and ignores moves smaller than the reversal', () => {
  // 202 -> 201 (1 box) and 200 (2 boxes down) are not a 3-box reversal.
  assert.deepEqual(shape(buildPnf(bars(200, 202, 201, 200, 204.5))), ['X1,2,3,4']);
});

test('3-box reversal starts an O column one box below the X top', () => {
  assert.deepEqual(shape(buildPnf(bars(200, 205, 202))), ['X1,2,3,4,5', 'O4,3,2']);
});

test('O column reverses into X one box above its low', () => {
  assert.deepEqual(shape(buildPnf(bars(200, 205, 201, 204))), ['X1,2,3,4,5', 'O4,3,2,1', 'X2,3,4']);
});

test('exact box boundaries count despite float error', () => {
  // 0.1 + 0.2 style drift must not lose the box at exactly 203.
  const chart = buildPnf(bars(200, 200.1 + 202.9 - 200.0));
  assert.deepEqual(shape(chart), ['X1,2,3']);
});

test('boxes remember the bar that printed them', () => {
  const chart = buildPnf([[10, 200], [20, 202], [30, 203]]);
  assert.deepEqual(chart.columns[0].boxes, [
    { row: 1, ts: 20 }, { row: 2, ts: 20 }, { row: 3, ts: 30 },
  ]);
});

test('chart range spans all columns', () => {
  const chart = buildPnf(bars(200, 205, 196));
  assert.equal(chart.hi, 5);
  assert.equal(chart.lo, -4);
});

test('reversal setting is honoured', () => {
  assert.deepEqual(shape(buildPnf(bars(200, 205, 204), { reversal: 1 })), ['X1,2,3,4,5', 'O4']);
});

// ---------------- Signals ----------------

const sig = (chart) => chart.signals.map((s) => `${s.type}@${s.col}:${s.row}`);

test('no signal without a previous column of the same direction', () => {
  assert.deepEqual(sig(buildPnf(bars(200, 205, 202))), []);
});

test('double top buy when an X column exceeds the previous X top by one box', () => {
  // X up to 205, O down to 202, X back to 205 (equal top: no signal), then 206.
  const chart = buildPnf(bars(200, 205, 202, 205));
  assert.deepEqual(sig(chart), []);
  const breakout = buildPnf(bars(200, 205, 202, 205, 206.2));
  assert.deepEqual(sig(breakout), ['buy@2:6']);
  assert.equal(breakout.signals[0].level, 5);
  assert.equal(breakout.signals[0].ts, 4); // the bar that printed row 6
});

test('double bottom sell when an O column breaks the previous O bottom', () => {
  // X to 205, O to 200, X to 204, O to 199 -> breaks the O bottom at row 0.
  const chart = buildPnf(bars(200, 205, 200, 204, 199));
  assert.deepEqual(sig(chart), ['sell@3:-1']);
  assert.equal(chart.signals[0].level, 0);
});

test('a column that jumps several boxes past the old top still signals at the first box beyond it', () => {
  const chart = buildPnf(bars(200, 205, 202, 209));
  assert.deepEqual(sig(chart), ['buy@2:6']);
});

test('each breakout column signals once, and lower tops do not signal', () => {
  // X to 205, O to 201, X to 204 (lower top: no buy), O to 201 (equal bottom: no sell),
  // then X to 207, which beats the 204 top -> buy at row 5.
  const chart = buildPnf(bars(200, 205, 201, 204, 201, 207));
  assert.deepEqual(shape(chart), ['X1,2,3,4,5', 'O4,3,2,1', 'X2,3,4', 'O3,2,1', 'X2,3,4,5,6,7']);
  assert.deepEqual(sig(chart), ['buy@4:5']);
});

test('columns record the last bar processed while they were current', () => {
  // 201.5 and 204 do not change the chart but still belong to the O column's time span.
  const chart = buildPnf([[1, 200], [2, 205], [3, 202], [4, 201.5], [5, 204], [6, 206]]);
  assert.deepEqual(chart.columns.map((c) => c.endTs), [2, 5, 6]);
});

// ---------------- Moving average ----------------

test('column SMA averages the midpoints of the last N columns', () => {
  const cols = [
    { hi: 5, lo: 1 }, // mid 3
    { hi: 4, lo: 2 }, // mid 3
    { hi: 9, lo: 3 }, // mid 6
    { hi: 8, lo: 0 }, // mid 4
  ];
  assert.deepEqual(columnSma(cols, 3), [null, null, 4, 13 / 3]);
});

test('chart SMA is in price terms, one value per column', () => {
  // p0 = 200, box = 1: rows read as rupees above 200.
  const chart = buildPnf(bars(200, 205, 202, 206, 203), { smaPeriod: 2 });
  assert.deepEqual(shape(chart), ['X1,2,3,4,5', 'O4,3,2', 'X3,4,5,6', 'O5,4,3']);
  // midpoints 3, 3, 4.5, 4 -> SMA(2): -, 3, 3.75, 4.25 rows above 200
  assert.deepEqual(chart.sma, [null, 203, 203.75, 204.25]);
  assert.equal(buildPnf(bars(200, 205)).smaPeriod, 10);
});

// ---------------- Axis tags ----------------

test('axis tags keep their place when free and yield to higher-priority tags', async () => {
  const { placeTags } = await import('../pnf/static/axis.js');
  const ys = (tags) => placeTags(tags, 9, 500).map((t) => t.y);
  assert.deepEqual(ys([{ y: 100 }, { y: 300 }]), [100, 300]); // far apart: untouched
  assert.deepEqual(ys([{ y: 100 }, { y: 105 }]), [100, 118]); // nudged to the nearest free side
  assert.deepEqual(ys([{ y: 100 }, { y: 95 }]), [100, 82]);
  // A third tag squeezed between takes the nearest free slot, right against a placed tag.
  assert.deepEqual(ys([{ y: 100 }, { y: 110 }, { y: 104 }]), [100, 118, 82]);
  // Clamped to the axis: a tag below the bottom edge moves up, never off-screen.
  assert.deepEqual(ys([{ y: 600 }, { y: 600 }]), [500, 482]);
});

// ---------------- Intraday VWAP ----------------

const IST_0915 = Date.UTC(2026, 9, 9, 3, 45) / 1000; // 09:15 IST on 9 Oct 2026, epoch seconds
const at = (min) => IST_0915 + min * 60; // minutes after 09:15

test('intraday VWAP accumulates volume-weighted closes from the session open', () => {
  const bars = [
    [at(0), 100, 10],
    [at(1), 110, 30],
    [at(2), 120, 10],
  ];
  assert.deepEqual(intradayVwap(bars), [100, (100 * 10 + 110 * 30) / 40, (100 * 10 + 110 * 30 + 120 * 10) / 50]);
});

test('intraday VWAP restarts at each session open', () => {
  const nextDay = at(24 * 60); // 09:15 the next day
  assert.deepEqual(intradayVwap([[at(374), 500, 1000], [nextDay, 100, 10], [nextDay + 60, 110, 10]]), [500, 100, 105]);
});

test('intraday VWAP is null until the session trades volume', () => {
  assert.deepEqual(intradayVwap([[at(0), 100, 0], [at(1), 110], [at(2), 120, 5]]), [null, null, 120]);
});

test('each column carries the intraday VWAP from when it last updated', () => {
  // Columns: X opened at 09:16 (bars 1..2), O opened at 09:18 (bars 3..4).
  const chart = buildPnf([
    [at(0), 200, 10],
    [at(1), 203, 10],
    [at(2), 205, 20],
    [at(3), 202, 20],
    [at(4), 201.5, 40],
  ]);
  assert.deepEqual(shape(chart), ['X1,2,3,4,5', 'O4,3,2']);
  const upTo = (n) => {
    const b = [[200, 10], [203, 10], [205, 20], [202, 20], [201.5, 40]].slice(0, n);
    return b.reduce((s, [p, v]) => s + p * v, 0) / b.reduce((s, [, v]) => s + v, 0);
  };
  assert.deepEqual(chart.vwap, [upTo(3), upTo(5)]);
});
