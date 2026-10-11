// One Point & Figure panel drawn on a viewport-sized canvas.
//
// A transparent scroller with a spacer element sized to the full chart
// provides native scrolling; on every scroll or data change only the visible
// part is redrawn. That keeps memory flat however long the chart grows.

import { buildPnf } from './pnf.js';

const HEADER = 24; // date strip height (CSS px)
const AXIS = 72; // price axis width
const PAD_COLS_LEFT = 1;
const PAD_COLS_RIGHT = 5;
const PAD_ROWS = 6;
export const CELL_STEPS = [6, 8, 10, 12, 14, 16, 20, 24];
const AUTO_CELL_MIN = 8;
const AUTO_CELL_MAX = 18;
const LABEL_STEPS = [1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000];

const IST = 'Asia/Kolkata';
const dayFmt = new Intl.DateTimeFormat('en-GB', { timeZone: IST, weekday: 'short', day: '2-digit', month: 'short' });
const dateKeyFmt = new Intl.DateTimeFormat('en-CA', { timeZone: IST });
const timeFmt = new Intl.DateTimeFormat('en-GB', { timeZone: IST, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });

export const fmtPrice = (p) => p.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const dateKey = (ts) => dateKeyFmt.format(ts * 1000);
export const fmtDay = (ts) => dayFmt.format(ts * 1000).replace(',', '');
export const fmtTime = (ts) => timeFmt.format(ts * 1000);

function readTheme() {
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  return {
    bg: v('--plot-bg'),
    grid: v('--plot-grid'),
    gridMajor: v('--plot-grid-major'),
    text: v('--text-2'),
    textStrong: v('--text-1'),
    x: v('--x'),
    o: v('--o'),
    accent: v('--accent'),
    accentText: v('--accent-text'),
    vwap: v('--vwap'),
    vwapText: v('--vwap-text'),
    separator: v('--plot-separator'),
    stripBg: v('--plot-strip'),
    hover: v('--plot-hover'),
    lastCol: v('--plot-last-col'),
    font: v('--font-mono'),
  };
}

let theme = null;
const themeQuery = window.matchMedia('(prefers-color-scheme: dark)');
themeQuery.addEventListener('change', () => {
  theme = null;
  for (const panel of PnfPanel.all) panel.render();
});

export class PnfPanel {
  static all = new Set();

  constructor(plotEl, { onHover } = {}) {
    this.el = plotEl;
    this.canvas = plotEl.querySelector('canvas');
    this.ctx = this.canvas.getContext('2d');
    this.scroller = plotEl.querySelector('.scroller');
    this.spacer = plotEl.querySelector('.spacer');
    this.onHover = onHover;

    this.series = [];
    this.live = null; // {ts, price} of the forming minute
    this.pnf = null;
    this.lastCommittedTs = -Infinity;
    this.dayMarks = [];
    this.cellOverride = null;
    this.cell = 12;
    this.follow = true;
    this.hover = null; // {x, y} in CSS px within the plot
    this.width = 0;
    this.height = 0;
    this.frame = 0;

    this.scroller.addEventListener('scroll', () => {
      const maxLeft = this.scroller.scrollWidth - this.scroller.clientWidth;
      this.follow = this.scroller.scrollLeft >= maxLeft - 2 * this.cell;
      this.render();
    });
    this.scroller.addEventListener('mousemove', (e) => {
      const r = this.el.getBoundingClientRect();
      this.hover = { x: e.clientX - r.left, y: e.clientY - r.top, clientX: e.clientX, clientY: e.clientY };
      this.render();
    });
    this.scroller.addEventListener('mouseleave', () => {
      this.hover = null;
      this.onHover?.(null);
      this.render();
    });
    new ResizeObserver(() => this.resize()).observe(this.el);
    PnfPanel.all.add(this);
  }

  // ---------------- Data ----------------

  setSeries(series) {
    this.series = series;
    this.follow = true;
    this.recompute({ recenter: true });
  }

  /** Append a closed 1-min bar; returns the signals that bar confirmed. */
  addBar(bar) {
    if (this.series.length && bar[0] <= this.series[this.series.length - 1][0]) return [];
    this.series.push(bar);
    if (this.live && this.live.ts <= bar[0]) this.live = null;
    this.recompute();
    return this.pnf?.signals.filter((s) => s.ts === bar[0]) ?? [];
  }

  /** The newest signal confirmed by a closed bar (forming boxes excluded). */
  get lastSignal() {
    const signals = this.pnf?.signals ?? [];
    for (let i = signals.length - 1; i >= 0; i--) if (signals[i].ts <= this.lastCommittedTs) return signals[i];
    return null;
  }

  setLive(live) {
    const last = this.series[this.series.length - 1];
    const next = live && (!last || live.ts > last[0]) ? live : null;
    if (next?.price === this.live?.price && next?.ts === this.live?.ts && next?.vwap === this.live?.vwap) return;
    this.live = next;
    this.recompute();
  }

  setCell(cellOverride) {
    this.cellOverride = cellOverride;
    this.follow = true;
    this.layout({ recenter: true });
  }

  recompute({ recenter = false } = {}) {
    const bars = this.live ? [...this.series, [this.live.ts, this.live.price]] : this.series;
    this.lastCommittedTs = this.series.length ? this.series[this.series.length - 1][0] : -Infinity;
    this.pnf = buildPnf(bars);
    this.vwapByTs = new Map(this.series.map((b) => [b[0], b[2] ?? null]));
    if (this.live) this.vwapByTs.set(this.live.ts, this.live.vwap ?? null);
    this.colDay = this.pnf?.columns.map((col) => dateKey(col.endTs)) ?? [];

    // Mark the column where each new session's first box printed.
    this.dayMarks = [];
    let prev = null;
    this.pnf?.columns.forEach((col, i) => {
      for (const b of col.boxes) {
        const key = dateKey(b.ts);
        if (key !== prev) {
          if (!this.dayMarks.length || this.dayMarks[this.dayMarks.length - 1].col !== i) {
            this.dayMarks.push({ col: i, ts: b.ts });
          }
          prev = key;
        }
      }
    });
    this.layout({ recenter });
  }

  // ---------------- Geometry ----------------

  resize() {
    const dpr = window.devicePixelRatio || 1;
    this.width = this.el.clientWidth;
    this.height = this.el.clientHeight;
    this.canvas.width = Math.round(this.width * dpr);
    this.canvas.height = Math.round(this.height * dpr);
    this.canvas.style.width = `${this.width}px`;
    this.canvas.style.height = `${this.height}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.layout({ recenter: true });
  }

  get topRow() {
    return (this.pnf?.hi ?? 0) + PAD_ROWS;
  }

  layout({ recenter = false } = {}) {
    if (!this.width) return;
    const pnf = this.pnf;
    const rows = pnf ? pnf.hi - pnf.lo + 1 + 2 * PAD_ROWS : 1;
    const cols = pnf ? pnf.columns.length + PAD_COLS_LEFT + PAD_COLS_RIGHT : 1;
    if (this.cellOverride) {
      this.cell = this.cellOverride;
    } else {
      const fit = Math.floor((this.height - HEADER) / rows);
      this.cell = Math.max(AUTO_CELL_MIN, Math.min(AUTO_CELL_MAX, fit));
    }
    const c = this.cell;
    this.spacer.style.width = `${cols * c + AXIS}px`;
    this.spacer.style.height = `${rows * c + HEADER}px`;

    if (this.follow && pnf?.columns.length) {
      this.scroller.scrollLeft = this.scroller.scrollWidth - this.scroller.clientWidth;
      const lastCol = pnf.columns[pnf.columns.length - 1];
      const lastRow = lastCol.dir === 'X' ? lastCol.hi : lastCol.lo;
      const y = HEADER + (this.topRow - lastRow) * c - this.scroller.scrollTop;
      if (recenter || y < HEADER + 2 * c || y > this.height - 3 * c) {
        this.scroller.scrollTop = HEADER + (this.topRow - lastRow) * c - (this.height + HEADER) / 2;
      }
    }
    this.render();
  }

  /** VWAP when column i last updated (null before both legs traded volume). */
  colVwap(i) {
    return this.vwapByTs.get(this.pnf.columns[i].endTs) ?? null;
  }

  get currentVwap() {
    if (this.live) return this.live.vwap ?? null;
    return this.series[this.series.length - 1]?.[2] ?? null;
  }

  colX(i) {
    return (i + PAD_COLS_LEFT) * this.cell - this.scroller.scrollLeft;
  }

  rowY(row) {
    return HEADER + (this.topRow - row) * this.cell - this.scroller.scrollTop;
  }

  // ---------------- Drawing ----------------

  render() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.draw();
    });
  }

  draw() {
    theme ??= readTheme();
    const t = theme;
    const { ctx, width: W, height: H, cell: c, pnf } = this;
    const chartW = W - AXIS;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = t.bg;
    ctx.fillRect(0, 0, W, H);
    if (!pnf) return;

    const labelStep = LABEL_STEPS.find((s) => s * c >= 22) ?? LABEL_STEPS[LABEL_STEPS.length - 1];
    const firstRow = Math.floor(this.topRow - (H - HEADER + this.scroller.scrollTop) / c) - 1;
    const lastRow = Math.ceil(this.topRow - (this.scroller.scrollTop - c) / c) + 1;
    const firstCol = Math.max(0, Math.floor(this.scroller.scrollLeft / c) - PAD_COLS_LEFT - 1);
    const lastCol = Math.min(pnf.columns.length - 1, Math.ceil((this.scroller.scrollLeft + chartW) / c));

    // Chart body
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, HEADER, chartW, H - HEADER);
    ctx.clip();

    if (pnf.columns.length) {
      const x = this.colX(pnf.columns.length - 1);
      ctx.fillStyle = t.lastCol;
      ctx.fillRect(x, HEADER, c, H - HEADER);
    }

    // Grid paper: faint lines per box, stronger at labelled rows.
    ctx.lineWidth = 1;
    for (let row = firstRow; row <= lastRow; row++) {
      const major = row % labelStep === 0;
      if (!major && c < 10) continue;
      const y = Math.round(this.rowY(row) + c / 2) + 0.5;
      ctx.strokeStyle = major ? t.gridMajor : t.grid;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(chartW, y);
      ctx.stroke();
    }
    if (c >= 10) {
      ctx.strokeStyle = t.grid;
      const startCol = Math.floor(this.scroller.scrollLeft / c) - PAD_COLS_LEFT;
      for (let i = startCol; i <= startCol + Math.ceil(chartW / c) + 1; i++) {
        const x = Math.round(this.colX(i)) + 0.5;
        ctx.beginPath();
        ctx.moveTo(x, HEADER);
        ctx.lineTo(x, H);
        ctx.stroke();
      }
    }

    // Session separators
    ctx.strokeStyle = t.separator;
    ctx.setLineDash([3, 3]);
    for (const mark of this.dayMarks) {
      if (mark.col === 0) continue;
      const x = Math.round(this.colX(mark.col)) + 0.5;
      if (x < 0 || x > chartW) continue;
      ctx.beginPath();
      ctx.moveTo(x, HEADER);
      ctx.lineTo(x, H);
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // Hovered cell
    const hit = this.hitTest();
    if (hit) {
      ctx.fillStyle = t.hover;
      ctx.fillRect(this.colX(hit.col), this.rowY(hit.row), c, c);
    }

    // Double top buy / double bottom sell: dashed line at the broken top or
    // bottom (from the previous same-direction column) and a ringed box.
    for (const s of pnf.signals) {
      if (s.col < firstCol - 2 || s.col > lastCol) continue;
      const color = s.type === 'buy' ? t.x : t.o;
      const alpha = s.ts > this.lastCommittedTs ? 0.45 : 1;
      const lineY = Math.round(s.type === 'buy' ? this.rowY(s.level) : this.rowY(s.level) + c) + 0.5;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.globalAlpha = alpha * 0.9;
      ctx.setLineDash([3, 2]);
      ctx.beginPath();
      ctx.moveTo(this.colX(s.col - 2), lineY);
      ctx.lineTo(this.colX(s.col) + c, lineY);
      ctx.stroke();
      ctx.setLineDash([]);
      const x = this.colX(s.col);
      const y = this.rowY(s.row);
      ctx.globalAlpha = alpha * 0.28;
      ctx.fillStyle = color;
      ctx.fillRect(x, y, c, c);
      ctx.globalAlpha = alpha;
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x + 0.75, y + 0.75, c - 1.5, c - 1.5);
    }
    ctx.globalAlpha = 1;

    // X and O boxes
    const inset = Math.max(1.5, c * 0.2);
    ctx.lineWidth = Math.max(1.25, Math.min(2.25, c / 7));
    ctx.lineCap = 'round';
    for (let i = firstCol; i <= lastCol; i++) {
      const col = pnf.columns[i];
      const x = this.colX(i);
      ctx.strokeStyle = col.dir === 'X' ? t.x : t.o;
      for (const b of col.boxes) {
        if (b.row < firstRow || b.row > lastRow) continue;
        const y = this.rowY(b.row);
        ctx.globalAlpha = b.ts > this.lastCommittedTs ? 0.4 : 1;
        ctx.beginPath();
        if (col.dir === 'X') {
          ctx.moveTo(x + inset, y + inset);
          ctx.lineTo(x + c - inset, y + c - inset);
          ctx.moveTo(x + c - inset, y + inset);
          ctx.lineTo(x + inset, y + c - inset);
        } else {
          ctx.arc(x + c / 2, y + c / 2, c / 2 - inset * 0.85, 0, Math.PI * 2);
        }
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;

    // VWAP: a step per column at its value when the column last updated,
    // joined within a session and broken where the session (and VWAP) resets.
    ctx.strokeStyle = t.vwap;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    let pen = false;
    for (let i = Math.max(0, firstCol - 1); i <= Math.min(pnf.columns.length - 1, lastCol + 1); i++) {
      const v = this.colVwap(i);
      if (v === null || (i > 0 && this.colDay[i] !== this.colDay[i - 1])) pen = false;
      if (v === null) continue;
      const x = this.colX(i);
      const y = Math.round(this.priceY(v)) + 0.5;
      if (pen) ctx.lineTo(x, y);
      else ctx.moveTo(x, y);
      ctx.lineTo(x + c, y);
      pen = true;
    }
    ctx.stroke();

    // Current price line
    const current = this.live?.price ?? this.series[this.series.length - 1]?.[1];
    const currentY = current === undefined ? null : this.priceY(current);
    if (currentY !== null) {
      ctx.strokeStyle = t.accent;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(0, Math.round(currentY) + 0.5);
      ctx.lineTo(chartW, Math.round(currentY) + 0.5);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.restore();

    this.drawDateStrip(chartW);
    const vwap = this.currentVwap;
    this.drawAxis(chartW, firstRow, lastRow, labelStep, current, currentY, hit, vwap);
    this.onHover?.(hit && hit.box ? this.describe(hit) : null, this.hover);
  }

  priceY(price) {
    return HEADER + (this.topRow - this.pnf.rowOf(price)) * this.cell - this.scroller.scrollTop + this.cell / 2;
  }

  drawDateStrip(chartW) {
    const { ctx } = this;
    const t = theme;
    ctx.fillStyle = t.stripBg;
    ctx.fillRect(0, 0, chartW, HEADER);
    ctx.fillStyle = t.gridMajor;
    ctx.fillRect(0, HEADER - 1, chartW, 1);

    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, chartW, HEADER);
    ctx.clip();
    ctx.font = `500 11px ${t.font}`;
    ctx.textBaseline = 'middle';
    ctx.fillStyle = t.text;
    // Pin the label of the session in view to the left edge, like a sticky header.
    const marks = this.dayMarks;
    let pinned = -1;
    for (let i = 0; i < marks.length; i++) if (this.colX(marks[i].col) <= 0) pinned = i;
    let minX = 0;
    marks.forEach((mark, i) => {
      if (i < pinned) return;
      const label = fmtDay(mark.ts);
      const w = ctx.measureText(label).width;
      let x = i === pinned ? 6 : this.colX(mark.col) + 4;
      if (i === pinned && i + 1 < marks.length) {
        x = Math.min(x, this.colX(marks[i + 1].col) - w - 10); // pushed off by the next label
      }
      if (x > chartW || x + w < 0 || (i !== pinned && x < minX)) return;
      ctx.fillText(label, x, HEADER / 2);
      minX = x + w + 10;
    });
    ctx.restore();
  }

  drawAxis(chartW, firstRow, lastRow, labelStep, current, currentY, hit, vwap) {
    const { ctx, height: H, cell: c, pnf } = this;
    const t = theme;
    ctx.fillStyle = t.stripBg;
    ctx.fillRect(chartW, 0, AXIS, H);
    ctx.fillStyle = t.gridMajor;
    ctx.fillRect(chartW, 0, 1, H);

    ctx.save();
    ctx.beginPath();
    ctx.rect(chartW, HEADER, AXIS, H - HEADER);
    ctx.clip();
    ctx.font = `11px ${t.font}`;
    ctx.textBaseline = 'middle';
    ctx.fillStyle = t.text;
    // Tags (current price, hovered row) hide the regular labels they would overlap.
    const clampY = (y) => Math.max(HEADER + 9, Math.min(H - 9, y));
    const tagY = currentY === null ? null : clampY(currentY);
    // The VWAP tag yields to the price tag: nudged a tag-height away when they meet.
    let vwapY = vwap === null ? null : clampY(this.priceY(vwap));
    if (vwapY !== null && tagY !== null && Math.abs(vwapY - tagY) < 18) {
      vwapY = clampY(vwapY <= tagY ? tagY - 18 : tagY + 18);
      if (Math.abs(vwapY - tagY) < 18) vwapY = clampY(vwapY <= tagY ? tagY + 18 : tagY - 18);
    }
    let hoverY = hit ? this.rowY(hit.row) + c / 2 : null;
    if (hoverY !== null && [tagY, vwapY].some((ty) => ty !== null && Math.abs(hoverY - ty) < 18)) hoverY = null;
    const covered = (y) => [tagY, vwapY, hoverY].some((ty) => ty !== null && Math.abs(ty - y) < 16);
    for (let row = Math.ceil(firstRow / labelStep) * labelStep; row <= lastRow; row += labelStep) {
      const y = this.rowY(row) + c / 2;
      if (y < HEADER + 7 || y > H - 7) continue; // would be cut in half by the edges
      ctx.fillRect(chartW, Math.round(y), 4, 1);
      if (!covered(y)) ctx.fillText(fmtPrice(pnf.price(row)), chartW + 8, y);
    }
    if (hoverY !== null) this.tag(chartW, hoverY, fmtPrice(pnf.price(hit.row)), t.textStrong, t.bg);
    if (vwapY !== null) this.tag(chartW, vwapY, fmtPrice(vwap), t.vwap, t.vwapText);
    if (tagY !== null) this.tag(chartW, tagY, fmtPrice(current), t.accent, t.accentText);
    ctx.restore();
  }

  tag(x, y, text, bg, fg) {
    const { ctx } = this;
    ctx.fillStyle = bg;
    ctx.beginPath();
    ctx.roundRect(x + 3, y - 9, AXIS - 6, 18, 4);
    ctx.fill();
    ctx.fillStyle = fg;
    ctx.fillText(text, x + 8, y);
  }

  // ---------------- Hover ----------------

  hitTest() {
    const h = this.hover;
    if (!h || !this.pnf || h.x >= this.width - AXIS || h.y < HEADER) return null;
    const c = this.cell;
    const col = Math.floor((h.x + this.scroller.scrollLeft) / c) - PAD_COLS_LEFT;
    const row = this.topRow - Math.floor((h.y - HEADER + this.scroller.scrollTop) / c);
    const column = this.pnf.columns[col];
    const box = column?.boxes.find((b) => b.row === row) ?? null;
    return { col, row, box, column };
  }

  describe(hit) {
    const provisional = hit.box.ts > this.lastCommittedTs;
    const signal = this.pnf.signals.find((s) => s.col === hit.col && s.row === hit.row);
    return {
      dir: hit.column.dir,
      price: this.pnf.price(hit.row),
      when: `${fmtDay(hit.box.ts)} ${fmtTime(hit.box.ts)}`,
      provisional,
      signal: signal?.type ?? null,
      vwap: this.colVwap(hit.col),
    };
  }
}
