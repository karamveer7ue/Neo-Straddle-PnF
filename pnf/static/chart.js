// One Point & Figure panel drawn on a viewport-sized canvas.
//
// A transparent scroller with a spacer element sized to the full chart
// provides native scrolling; on every scroll or data change only the visible
// part is redrawn. That keeps memory flat however long the chart grows.

import { TAG_H, placeTags } from './axis.js';
import { buildPnf, istDay } from './pnf.js';

const HEADER = 24; // date strip height (CSS px)
const AXIS = 72; // price axis width
const PAD_COLS_LEFT = 1;
const PAD_COLS_RIGHT = 5;
const PAD_ROWS = 6;
export const CELL_STEPS = [6, 8, 10, 12, 14, 16, 20, 24];
const AUTO_CELL_MIN = 8;
const AUTO_CELL_MAX = 18;
// Box height can be stretched or squeezed apart from the width by dragging the price axis.
const ROW_MIN = 2;
const ROW_MAX = 64;
const DRAG_DOUBLING_PX = 120; // dragging this far up doubles the box height (down halves it)
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
    vwma: v('--vwma'),
    vwmaText: v('--vwma-text'),
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

// Moving the window to a screen with another pixel density does not change any
// CSS size, so no ResizeObserver fires: watch the density itself and re-arm.
function watchPixelRatio() {
  matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`).addEventListener('change', () => {
    for (const panel of PnfPanel.all) panel.resize();
    watchPixelRatio();
  }, { once: true });
}
watchPixelRatio();

export class PnfPanel {
  static all = new Set();

  /** `onRowScale(scale)` is told when a drag or double-click on the price axis sets the box height. */
  constructor(plotEl, { onHover, onRowScale } = {}) {
    this.el = plotEl;
    this.canvas = plotEl.querySelector('canvas');
    this.ctx = this.canvas.getContext('2d');
    this.scroller = plotEl.querySelector('.scroller');
    this.spacer = plotEl.querySelector('.spacer');
    this.onHover = onHover;
    this.onRowScale = onRowScale;

    this.series = [];
    this.live = null; // {ts, price} of the forming minute
    this.pnf = null;
    this.lastCommittedTs = -Infinity;
    this.dayMarks = [];
    this.cellOverride = null;
    this.overlays = { vwap: true, vwma: true };
    this.cell = 12; // box width (CSS px)
    this.rowScale = 1; // box height / width, set by dragging the price axis
    this.rowH = 12; // box height (CSS px)
    this.axisDrag = null; // {startY, startScale, anchorY} while the price axis is dragged
    this.follow = true;
    this.hover = null; // {x, y} in CSS px within the plot
    this.width = 0;
    this.originX = 0; // blank space left of the first column when all columns fit
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
      this.scroller.style.cursor = this.axisDrag || this.onAxis(this.hover) ? 'ns-resize' : '';
      this.render();
    });
    // Drag the price axis up to stretch the boxes vertically, down to squeeze them;
    // a double-click there goes back to square boxes.
    this.scroller.addEventListener('pointerdown', (e) => {
      const r = this.el.getBoundingClientRect();
      const y = e.clientY - r.top;
      if (e.button !== 0 || !this.pnf || !this.onAxis({ x: e.clientX - r.left, y })) return;
      e.preventDefault();
      this.scroller.setPointerCapture(e.pointerId);
      this.axisDrag = { startY: e.clientY, startScale: this.rowScale, anchorY: this.scaleAnchor(y) };
    });
    this.scroller.addEventListener('pointermove', (e) => {
      if (!this.axisDrag) return;
      const { startY, startScale, anchorY } = this.axisDrag;
      this.setRowScale(startScale * 2 ** ((startY - e.clientY) / DRAG_DOUBLING_PX), anchorY);
      this.onRowScale?.(this.rowScale);
    });
    const endDrag = () => {
      this.axisDrag = null;
    };
    this.scroller.addEventListener('pointerup', endDrag);
    this.scroller.addEventListener('pointercancel', endDrag);
    this.scroller.addEventListener('dblclick', (e) => {
      const r = this.el.getBoundingClientRect();
      const y = e.clientY - r.top;
      if (!this.onAxis({ x: e.clientX - r.left, y })) return;
      this.setRowScale(1, this.scaleAnchor(y));
      this.onRowScale?.(1);
    });
    this.scroller.addEventListener('mouseleave', () => {
      this.hover = null;
      this.onHover?.(null);
      this.render();
    });
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.el);
    PnfPanel.all.add(this);
  }

  /** Release the panel for good (its observer, pending frame and theme/density hooks). */
  destroy() {
    if (this.hover) this.onHover?.(null, null); // a removed panel never gets its mouseleave
    this.hover = null;
    this.resizeObserver.disconnect();
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = 0;
    PnfPanel.all.delete(this);
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
    const same = next?.price === this.live?.price && next?.ts === this.live?.ts && next?.volume === this.live?.volume;
    if (same) return;
    this.live = next;
    this.recompute();
  }

  setOverlays(overlays) {
    this.overlays = { ...overlays };
    this.render();
  }

  setCell(cellOverride) {
    this.cellOverride = cellOverride;
    if (cellOverride === null) this.rowScale = 1; // Fit: square boxes, sized to the panel height
    this.follow = true;
    this.layout({ recenter: true });
  }

  /** Back to the newest column (and its price), e.g. after the panel changed size. */
  followLatest() {
    this.follow = true;
    this.layout({ recenter: true });
  }

  /**
   * Box height as a multiple of the width, keeping the price at plot y `anchorY`
   * where it is; columns keep their width.
   */
  setRowScale(scale, anchorY) {
    if (!this.width) {
      this.rowScale = scale; // not laid out yet: the first resize applies it
      return;
    }
    const fromTop = (this.scroller.scrollTop + anchorY - HEADER) / this.rowH; // rows above the anchor
    const rowH = Math.min(ROW_MAX, Math.max(ROW_MIN, this.cell * scale));
    this.rowScale = rowH / this.cell;
    this.layout({ keepTop: true });
    this.scroller.scrollTop = fromTop * this.rowH - (anchorY - HEADER);
    this.render();
  }

  /** Take the box height another panel's axis drag set (around the current price while following). */
  matchRowScale(scale) {
    if (scale !== this.rowScale) this.setRowScale(scale, this.scaleAnchor(this.height / 2));
  }

  /**
   * The plot y that stays put while the box height changes: the current price
   * while following the newest column (so it stays in view), else `y`.
   */
  scaleAnchor(y) {
    const current = this.live?.price ?? this.series[this.series.length - 1]?.[1];
    if (!this.follow || current === undefined) return y;
    return Math.min(this.height, Math.max(HEADER, this.priceY(current)));
  }

  /** Whether a plot point (CSS px) is on the price axis, below the date strip. */
  onAxis(p) {
    return p.x >= this.width - AXIS && p.y >= HEADER;
  }

  recompute({ recenter = false } = {}) {
    // The forming minute joins as a bar with the volume traded so far, so the
    // newest column's VWAP moves in real time.
    const bars = this.live ? [...this.series, [this.live.ts, this.live.price, this.live.volume ?? 0]] : this.series;
    this.lastCommittedTs = this.series.length ? this.series[this.series.length - 1][0] : -Infinity;
    this.pnf = buildPnf(bars);

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

  layout({ recenter = false, keepTop = false } = {}) {
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
    this.rowH = Math.min(ROW_MAX, Math.max(ROW_MIN, c * this.rowScale));
    const rh = this.rowH;
    this.spacer.style.width = `${cols * c + AXIS}px`;
    this.spacer.style.height = `${rows * rh + HEADER}px`;
    // Columns that fit without scrolling sit against the price axis, as they do once they scroll.
    this.originX = Math.max(0, Math.floor(this.width - AXIS - cols * c));
    // With nothing to scroll back to, the newest column is in view: keep following it.
    if (this.originX > 0) this.follow = true;

    if (this.follow && pnf?.columns.length) {
      this.scroller.scrollLeft = this.scroller.scrollWidth - this.scroller.clientWidth;
      const lastCol = pnf.columns[pnf.columns.length - 1];
      const lastRow = lastCol.dir === 'X' ? lastCol.hi : lastCol.lo;
      const y = HEADER + (this.topRow - lastRow) * rh - this.scroller.scrollTop;
      if (!keepTop && (recenter || y < HEADER + 2 * rh || y > this.height - 3 * rh)) {
        this.scroller.scrollTop = HEADER + (this.topRow - lastRow) * rh - (this.height + HEADER) / 2;
      }
    }
    this.render();
  }


  get currentVwma() {
    return this.pnf?.vwma.at(-1) ?? null;
  }

  get currentVwap() {
    return this.pnf?.vwap.at(-1) ?? null;
  }

  /** Chart x of the plot's left edge: the scroll offset, less the blank space when all columns fit. */
  get viewLeft() {
    return this.scroller.scrollLeft - this.originX;
  }

  colX(i) {
    return (i + PAD_COLS_LEFT) * this.cell - this.viewLeft;
  }

  rowY(row) {
    return HEADER + (this.topRow - row) * this.rowH - this.scroller.scrollTop;
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
    const { ctx, width: W, height: H, cell: c, rowH: rh, pnf } = this;
    const chartW = W - AXIS;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = t.bg;
    ctx.fillRect(0, 0, W, H);
    if (!pnf) return;

    const labelStep = LABEL_STEPS.find((s) => s * rh >= 22) ?? LABEL_STEPS[LABEL_STEPS.length - 1];
    const firstRow = Math.floor(this.topRow - (H - HEADER + this.scroller.scrollTop) / rh) - 1;
    const lastRow = Math.ceil(this.topRow - (this.scroller.scrollTop - rh) / rh) + 1;
    const firstCol = Math.max(0, Math.floor(this.viewLeft / c) - PAD_COLS_LEFT - 1);
    const lastCol = Math.min(pnf.columns.length - 1, Math.ceil((this.viewLeft + chartW) / c));

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
      if (!major && rh < 10) continue;
      const y = Math.round(this.rowY(row) + rh / 2) + 0.5;
      ctx.strokeStyle = major ? t.gridMajor : t.grid;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(chartW, y);
      ctx.stroke();
    }
    if (c >= 10) {
      ctx.strokeStyle = t.grid;
      const startCol = Math.floor(this.viewLeft / c) - PAD_COLS_LEFT;
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
      ctx.fillRect(this.colX(hit.col), this.rowY(hit.row), c, rh);
    }

    // Double top buy / double bottom sell: a solid bar over the previous two
    // columns, sitting just beyond the level the signal broke - green above
    // the old top for a buy, red below the old bottom for a sell. It lives in
    // the row next to that level, which is empty in both columns.
    const barH = Math.max(2, Math.min(Math.round(c * 0.3), Math.round(rh * 0.3)));
    const gap = Math.max(1, Math.round(Math.min(c, rh) * 0.12));
    for (const s of pnf.signals) {
      if (s.col - 1 < firstCol || s.col - 2 > lastCol) continue;
      const x0 = this.colX(s.col - 2) + 1;
      const x1 = this.colX(s.col) - 1;
      const y = s.type === 'buy' ? this.rowY(s.level) - gap - barH : this.rowY(s.level) + rh + gap;
      ctx.globalAlpha = s.ts > this.lastCommittedTs ? 0.45 : 1; // forming: faded
      ctx.fillStyle = s.type === 'buy' ? t.x : t.o;
      ctx.beginPath();
      ctx.roundRect(x0, Math.round(y), x1 - x0, barH, barH / 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;

    // X and O boxes
    const insetX = Math.max(1.5, c * 0.2);
    const insetY = Math.min(Math.max(1.5, rh * 0.2), rh / 2 - 0.5);
    ctx.lineWidth = Math.max(1, Math.min(2.25, Math.min(c, rh) / 7));
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
          ctx.moveTo(x + insetX, y + insetY);
          ctx.lineTo(x + c - insetX, y + rh - insetY);
          ctx.moveTo(x + c - insetX, y + insetY);
          ctx.lineTo(x + insetX, y + rh - insetY);
        } else {
          const rx = c / 2 - insetX * 0.85;
          const ry = Math.max(0.5, rh / 2 - insetY * 0.85);
          ctx.ellipse(x + c / 2, y + rh / 2, rx, ry, 0, 0, Math.PI * 2);
        }
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;

    const fromCol = Math.max(0, firstCol - 1);
    const toCol = Math.min(pnf.columns.length - 1, lastCol + 1);
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';

    // VWMA of the last N column midpoints: a line through the column centres.
    // It is column-based, so unlike VWAP it runs straight across sessions.
    if (this.overlays.vwma) {
      ctx.strokeStyle = t.vwma;
      ctx.beginPath();
      let started = false;
      for (let i = fromCol; i <= toCol; i++) {
        const v = pnf.vwma[i];
        if (v === null) continue;
        const x = this.colX(i) + c / 2;
        const y = this.priceY(v);
        if (started) ctx.lineTo(x, y);
        else ctx.moveTo(x, y);
        started = true;
      }
      ctx.stroke();
    }

    // Intraday VWAP: a line through the column centres, each at the value it
    // had when that column last updated, broken where a new session starts
    // (VWAP restarts at 09:15).
    if (this.overlays.vwap) {
      ctx.strokeStyle = t.vwap;
      ctx.beginPath();
      let pen = false;
      for (let i = fromCol; i <= toCol; i++) {
        const v = pnf.vwap[i];
        const newSession = i > 0 && istDay(pnf.columns[i].endTs) !== istDay(pnf.columns[i - 1].endTs);
        if (v === null || newSession) pen = false;
        if (v === null) continue;
        const x = this.colX(i) + c / 2;
        const y = this.priceY(v);
        if (pen) ctx.lineTo(x, y);
        else ctx.moveTo(x, y);
        pen = true;
      }
      ctx.stroke();
    }

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
    // Axis tags in priority order: the price keeps its place, overlays yield.
    const tags = [];
    if (currentY !== null) tags.push({ y: currentY, value: current, bg: t.accent, fg: t.accentText });
    const vwap = this.currentVwap;
    if (this.overlays.vwap && vwap !== null) {
      tags.push({ y: this.priceY(vwap), value: vwap, bg: t.vwap, fg: t.vwapText });
    }
    const vwma = this.currentVwma;
    if (this.overlays.vwma && vwma !== null) tags.push({ y: this.priceY(vwma), value: vwma, bg: t.vwma, fg: t.vwmaText });
    this.drawAxis(chartW, firstRow, lastRow, labelStep, tags, hit);
    // Only the hovered panel drives the shared tooltip; the others redrawing on a tick
    // must not hide it (mouseleave clears it when the pointer goes).
    if (this.hover) this.onHover?.(hit && hit.box ? this.describe(hit) : null, this.hover);
  }

  priceY(price) {
    return HEADER + (this.topRow - this.pnf.rowOf(price)) * this.rowH - this.scroller.scrollTop + this.rowH / 2;
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
      // A session only a few columns old would run under the price axis: end it at the edge.
      if (i === marks.length - 1) x = Math.min(x, chartW - w - 6);
      if (x > chartW || x + w < 0 || (i !== pinned && x < minX)) return;
      ctx.fillText(label, x, HEADER / 2);
      minX = x + w + 10;
    });
    ctx.restore();
  }

  drawAxis(chartW, firstRow, lastRow, labelStep, tags, hit) {
    const { ctx, height: H, rowH: rh, pnf } = this;
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

    const placed = placeTags(tags, HEADER + TAG_H / 2, H - TAG_H / 2);
    // The hovered-row readout is dropped where it would cover a tag.
    let hoverY = hit ? this.rowY(hit.row) + rh / 2 : null;
    if (hoverY !== null && placed.some((p) => Math.abs(hoverY - p.y) < TAG_H)) hoverY = null;
    // Tags hide the regular labels they would overlap.
    const covered = (y) => [...placed.map((p) => p.y), hoverY].some((ty) => ty !== null && Math.abs(ty - y) < 16);
    for (let row = Math.ceil(firstRow / labelStep) * labelStep; row <= lastRow; row += labelStep) {
      const y = this.rowY(row) + rh / 2;
      if (y < HEADER + 7 || y > H - 7) continue; // would be cut in half by the edges
      ctx.fillRect(chartW, Math.round(y), 4, 1);
      if (!covered(y)) ctx.fillText(fmtPrice(pnf.price(row)), chartW + 8, y);
    }
    if (hoverY !== null) this.tag(chartW, hoverY, fmtPrice(pnf.price(hit.row)), t.textStrong, t.bg);
    // Lowest priority first, so the price tag is drawn on top.
    for (const p of [...placed].reverse()) this.tag(chartW, p.y, fmtPrice(p.value), p.bg, p.fg);
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
    const col = Math.floor((h.x + this.viewLeft) / this.cell) - PAD_COLS_LEFT;
    const row = this.topRow - Math.floor((h.y - HEADER + this.scroller.scrollTop) / this.rowH);
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
      vwap: this.overlays.vwap ? this.pnf.vwap[hit.col] : null,
      vwma: this.overlays.vwma ? this.pnf.vwma[hit.col] : null,
    };
  }
}
