// Prints static/pnf.js results as JSON, for tests/test_pnf_parity.py.
// Reads {"cases": [{"bars": [[ts, close, volume?], ...], "opts": {...}}]} on stdin
// and writes one result per case (null where buildPnf returns null).

import { buildPnf } from '../pnf/static/pnf.js';

let text = '';
for await (const chunk of process.stdin) text += chunk;

const results = JSON.parse(text).cases.map(({ bars, opts }) => {
  const chart = buildPnf(bars, opts ?? {});
  if (!chart) return null;
  return {
    p0: chart.p0,
    box: chart.box,
    hi: chart.hi,
    lo: chart.lo,
    columns: chart.columns.map((c) => ({
      dir: c.dir,
      boxes: c.boxes.map((b) => [b.row, b.ts]),
      hi: c.hi,
      lo: c.lo,
      startTs: c.startTs,
      endTs: c.endTs,
      volume: c.volume,
    })),
    signals: chart.signals.map((s) => [s.type, s.col, s.row, s.level, s.ts]),
    vwma: chart.vwma,
    vwap: chart.vwap,
    vwapAt: bars.map((b) => chart.vwapAt(b[0])),
  };
});
process.stdout.write(JSON.stringify(results));
