// Straddle simulator page: replays past sessions minute by minute.
//
// Reuses the live page's modules unchanged (P&F engine, chart renderer,
// alerts). The server returns a whole run at once; this page reveals it one
// market minute at a time through the Player clock.

import { raiseAlert, setSoundEnabled, soundEnabled } from '/static/alerts.js';
import { CELL_STEPS, PnfPanel, dateKey, fmtDay, fmtPrice, fmtTime } from '/static/chart.js';
import { alertWanted } from '/static/pnf.js';
import { loadOverlays, mountMenu, saveOverlays } from '/static/view.js';
import { Player, SPEEDS } from './player.js';
import { LOTS_PER_SELL, ShortStraddleTrade } from './trades.js';

const $ = (id) => document.getElementById(id);
const els = {
  underlyings: $('underlyings'),
  setup: $('setup'),
  expiry: $('expiry'),
  start: $('start'),
  end: $('end'),
  atm: $('atm'),
  load: $('load'),
  play: $('play'),
  playLabel: $('play-label'),
  restart: $('restart'),
  speeds: $('speeds'),
  seek: $('seek'),
  nextMinute: $('next-minute-bar'),
  clock: $('clock'),
  spotBox: $('spot-box'),
  simTime: $('sim-time'),
  simCount: $('sim-count'),
  spot: $('spot'),
  spotStat: $('spot-stat'),
  spotChg: $('spot-chg'),
  overlays: $('overlays'),
  viewBtn: $('view-btn'),
  viewPanel: $('view-panel'),
  zoomIn: $('zoom-in'),
  zoomOut: $('zoom-out'),
  zoomFit: $('zoom-fit'),
  sound: $('sound'),
  banner: $('banner'),
  bannerText: $('banner-text'),
  charts: $('charts'),
  empty: $('sim-empty'),
  template: $('panel-template'),
  tooltip: $('tooltip'),
};

const SIGNAL_NAMES = { buy: 'Double top buy', sell: 'Double bottom sell' };
const FLASH_MS = 8000;
const SPEED_KEY = 'pnf.sim.speed';

const state = {
  underlying: 'NIFTY',
  pendingUnderlying: null, // an index switch waiting for its expiries
  options: null, // /api/options for the selected index
  run: null, // /api/simulate result
  minutes: [], // every market minute in the run, in order
  bars: new Map(), // chart key -> Map(ts -> bar)
  spot: new Map(), // ts -> index price
  panels: new Map(), // chart key -> {root, panel, def}
  player: null,
  speed: readStored(SPEED_KEY, 1, (v) => SPEEDS.includes(v)),
  overlays: loadOverlays(),
  cellOverride: null,
  alerted: new Set(),
  started: false, // Play pressed at least once since the run loaded
  loading: false, // a new run is loading: the old one cannot be played meanwhile
  bannerTag: '', // what put the current banner up, so only that can take it down
  formingKey: '', // which in-minute step is on screen, so each is drawn once
  formingSpot: null,
};

// ---------------- Small helpers ----------------

function readStored(key, fallback, valid) {
  try {
    const v = JSON.parse(localStorage.getItem(key));
    return valid(v) ? v : fallback;
  } catch {
    return fallback;
  }
}

function store(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage unavailable: the choice lasts for this page only
  }
}

const signed = (v, digits = 2) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(digits)}`;
const offsetLabel = (off) => (off === 0 ? 'ATM' : `ATM ${off > 0 ? '+' : '−'} ${Math.abs(off)}`);
const fmtStrike = (s) => s.toLocaleString('en-IN');
const fmtDate = (iso, opts = { day: '2-digit', month: 'short' }) =>
  new Date(`${iso}T00:00:00+05:30`).toLocaleDateString('en-GB', { timeZone: 'Asia/Kolkata', ...opts });

function setChange(el, value, base, suffix = '') {
  if (value == null || base == null || !base) {
    el.replaceChildren();
    el.classList.remove('up', 'down');
    return;
  }
  const diff = value - base;
  const abs = document.createElement('span');
  abs.className = 'abs';
  abs.textContent = `${signed(diff)} `;
  el.replaceChildren(abs, `(${signed((diff / base) * 100)}%)${suffix}`);
  el.classList.toggle('up', diff >= 0);
  el.classList.toggle('down', diff < 0);
}

function showBanner(text, kind = '', tag = '') {
  state.bannerTag = text ? tag : '';
  els.banner.hidden = !text;
  els.banner.className = `banner ${kind}`;
  els.bannerText.textContent = text;
}

async function api(url, body) {
  const res = await fetch(url, body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : undefined);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join('; ') : data.detail;
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return data;
}

// ---------------- Setup row ----------------

function renderUnderlyings(keys = state.options?.underlyings ?? []) {
  // The pressed button is the latest choice, even while its expiries are loading;
  // Load waits for them, or it would load the previous index under the new button.
  const chosen = state.pendingUnderlying ?? state.underlying;
  renderLoadButton();
  els.underlyings.replaceChildren(...keys.map((key) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = key;
    btn.setAttribute('aria-pressed', String(key === chosen));
    btn.addEventListener('click', () => {
      if (key === (state.pendingUnderlying ?? state.underlying)) return;
      if (key === state.underlying) {
        // Back to the index already loaded: cancel the pending switch.
        optionsRequest += 1;
        state.pendingUnderlying = null;
        renderUnderlyings();
        return;
      }
      loadOptions(key);
    });
    return btn;
  }));
}

function renderLoadButton() {
  els.load.disabled = state.loading || !!state.pendingUnderlying;
  els.load.textContent = state.loading ? 'Loading...' : 'Load';
  els.load.title = state.pendingUnderlying ? `Loading the ${state.pendingUnderlying} expiries...` : '';
}

let optionsRequest = 0;

/**
 * Load the form's choices for an index. The index only changes once its
 * expiries have arrived, and a reply overtaken by a later click is ignored,
 * so the index, its expiries and its strike step always belong together.
 */
async function loadOptions(underlying = state.underlying) {
  const request = ++optionsRequest;
  if (state.options && underlying !== state.underlying) {
    state.pendingUnderlying = underlying;
    renderUnderlyings();
  }
  try {
    const opts = await api(`/api/options?underlying=${underlying}`);
    if (request !== optionsRequest) return; // a later switch is under way
    if (underlying !== state.underlying) els.atm.value = '';
    state.underlying = underlying;
    state.pendingUnderlying = null;
    state.options = opts;
    renderUnderlyings(opts.underlyings);
    els.expiry.replaceChildren(...opts.expiries.map((iso) =>
      new Option(`${fmtDate(iso)} · ${fmtDate(iso, { weekday: 'short' })}`, iso)));
    for (const input of [els.start, els.end]) {
      input.max = opts.latestDate; // only past sessions
      if (!input.value || input.value > opts.latestDate) {
        input.value = input === els.start ? opts.defaultStart : opts.defaultEnd;
      }
    }
    els.atm.step = String(opts.underlying.strikeStep);
    els.atm.placeholder = 'Auto';
    if (state.bannerTag === 'options') showBanner(''); // never another message (e.g. a run loading)
  } catch (err) {
    if (request !== optionsRequest) return;
    state.pendingUnderlying = null; // stay on the index that is loaded
    renderUnderlyings();
    showBanner(`Could not load the ${underlying} expiries: ${err.message}`, 'error', 'options');
  }
}

function validate() {
  const { start, end } = { start: els.start.value, end: els.end.value };
  const opts = state.options;
  if (!opts) return 'The expiries have not loaded yet; check the server and reload the page.';
  if (!start || !end) return 'Pick a start and an end date.';
  if (end >= opts.today) return `The end date must be before today (${fmtDate(opts.today, { day: '2-digit', month: 'short', year: 'numeric' })}).`;
  if (start > end) return 'The start date must be on or before the end date.';
  const days = (new Date(end) - new Date(start)) / 86_400_000 + 1;
  if (days > opts.maxRangeDays) return `Pick at most ${opts.maxRangeDays} days.`;
  const atm = els.atm.value ? Number(els.atm.value) : null;
  if (atm !== null && (!Number.isInteger(atm) || atm % opts.underlying.strikeStep)) {
    return `${state.underlying} ATM must be a multiple of ${opts.underlying.strikeStep}.`;
  }
  return null;
}

els.setup.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (state.loading || state.pendingUnderlying) return;
  const problem = validate();
  if (problem) {
    showBanner(problem, 'error');
    return;
  }
  state.player?.pause();
  setLoading(true); // the old run cannot be played while the new one loads
  const body = {
    underlying: state.underlying,
    expiry: els.expiry.value,
    start: els.start.value,
    end: els.end.value,
    atm: els.atm.value ? Number(els.atm.value) : null,
  };
  showBanner(`Loading ${state.underlying} straddles for ${fmtDate(body.start)} – ${fmtDate(body.end)} ` +
    '(history requests are paced, so this takes about 15 seconds)...');
  let run = null;
  let error = null;
  try {
    run = await api('/api/simulate', body);
  } catch (err) {
    error = err;
  }
  setLoading(false);
  if (run) startRun(run);
  else showBanner(error.message, 'error'); // the previous run stays playable
});

function setLoading(on) {
  state.loading = on;
  renderLoadButton();
  renderPlayback();
}

// ---------------- Run ----------------

function startRun(run) {
  state.run = run;
  state.alerted.clear();
  state.bars = new Map(run.charts.map((c) => [c.key, new Map(c.series.map((b) => [b[0], b]))]));
  state.spot = new Map(run.spot.map((s) => [s[0], s[1]]));
  state.spotRows = new Map(run.spot.map((s) => [s[0], s]));
  state.started = false;
  const all = new Set(run.spot.map((s) => s[0]));
  for (const c of run.charts) for (const b of c.series) all.add(b[0]);
  // Minutes before replayStart belong to the context day: drawn at load, not replayed.
  state.minutes = [...all].filter((ts) => ts >= run.replayStart).sort((a, b) => a - b);

  buildPanels(run.charts);
  state.player?.pause(); // the previous run's clock must never drive these charts
  // start: -1 - nothing of the run is revealed yet, so its first minute is played too.
  state.player = new Player(state.minutes.length, { onStep: (i) => reveal(i, true), onState: renderPlayback, start: -1 });
  state.player.setSpeed(state.speed);
  els.seek.min = '-1';
  els.seek.max = String(state.minutes.length - 1);
  renderPlayback();
  els.clock.hidden = false;
  els.spotBox.hidden = false;
  reveal(-1, false);

  const days = run.sessions.map((d) => fmtDate(d)).join(', ');
  const atmFrom = run.atm === run.atmAuto
    ? `ATM ${fmtStrike(run.atm)} from the ${fmtDate(run.sessions[0])} open (${fmtPrice(run.indexOpen)})`
    : `ATM ${fmtStrike(run.atm)} (set by you)`;
  const context = run.contextDate
    ? ` · ${fmtDate(run.contextDate, { weekday: 'short', day: '2-digit', month: 'short' })} already drawn as context`
    : '';
  showBanner(`Loaded ${run.sessions.length} session${run.sessions.length > 1 ? 's' : ''} (${days})${context} · ` +
    `${fmtDate(run.expiry)} expiry · ${atmFrom}. Press Play.`, 'ok');
}

function buildPanels(charts) {
  for (const { panel } of state.panels.values()) panel.destroy();
  els.charts.replaceChildren();
  state.panels.clear();
  for (const def of charts) {
    const root = els.template.content.firstElementChild.cloneNode(true);
    root.classList.toggle('atm', def.offset === 0);
    els.charts.append(root);
    const panel = new PnfPanel(root.querySelector('.plot'), { onHover: showTooltip });
    panel.setCell(state.cellOverride);
    panel.setOverlays(state.overlays);
    const entry = { root, panel, def, trade: null, closed: [] };
    state.panels.set(def.key, entry);
    wireTrades(entry);
  }
}

/**
 * Show the run as of minute i. While playing, each new minute's bar is added
 * to the charts (and new signals alert); a jump rebuilds the charts silently.
 */
/** The last revealed moment: minute i's start, or just before the run (i = -1). */
const revealedTs = (i) => (i >= 0 ? state.minutes[i] : state.run.replayStart - 1);

function reveal(i, stepping) {
  const ts = revealedTs(i);
  for (const entry of state.panels.values()) {
    const bar = state.bars.get(entry.def.key).get(ts);
    entry.liveLegs = null;
    if (stepping) {
      if (bar) alertSignals(entry, entry.panel.addBar(bar));
    } else {
      entry.panel.setLive(null);
      entry.panel.setSeries(entry.def.series.filter((b) => b[0] <= ts));
    }
    renderPanelHead(entry);
  }
  state.formingKey = '';
  state.formingSpot = null;
  renderClock();
}

/**
 * Play the minute in progress along its price path (open, extremes, close):
 * the charts show it as forming boxes, like live ticks on the live page.
 * Step k of PATH_STEPS is shown once the minute is k / PATH_STEPS done.
 */
function showFormingMinute() {
  const p = state.player;
  const next = p ? p.index + 1 : 0;
  if (!p || next >= state.minutes.length || !(p.playing || p.progress > 0)) return;
  const steps = state.run.pathSteps;
  const k = Math.min(steps - 1, Math.floor(p.progress * steps));
  const key = `${next}:${k}`;
  if (key === state.formingKey) return;
  state.formingKey = key;
  const ts = state.minutes[next];
  for (const entry of state.panels.values()) {
    const bar = state.bars.get(entry.def.key).get(ts);
    if (bar) {
      const [price, ce, pe] = bar[5][k];
      // volume traded so far this minute, spread evenly over the steps
      entry.panel.setLive({ ts, price, volume: Math.round((bar[2] * (k + 1)) / steps) });
      entry.liveLegs = { ce, pe };
    }
    renderPanelHead(entry);
  }
  const spot = state.spotRows.get(ts);
  state.formingSpot = spot ? spot[2][k] : null;
  renderClock();
}

function seekTo(i) {
  const p = state.player;
  if (!p) return;
  const target = Math.max(-1, Math.min(state.minutes.length - 1, i));
  if (target === p.index && p.progress === 0) return; // nowhere to go (e.g. Left at the start)
  p.seek(target);
  const t = revealedTs(p.index);
  // Anything that happened after the new moment has not happened yet: trades opened or
  // exited later are removed, and signals after it may alert again when played.
  const later = (trade) => trade.ts > t || trade.exits.some((x) => x.ts > t);
  let cleared = false;
  for (const entry of state.panels.values()) {
    if (entry.trade && later(entry.trade)) {
      entry.trade = null;
      cleared = true;
    }
    const kept = entry.closed.filter((trade) => !later(trade));
    cleared ||= kept.length !== entry.closed.length;
    entry.closed = kept;
  }
  for (const key of [...state.alerted]) if (Number(key.split(':')[2]) > t) state.alerted.delete(key);
  if (cleared) showBanner('Simulated trades after this point were cleared: the replay went back before them.', 'warn');
  reveal(p.index, false);
}

// ---------------- Rendering ----------------

function renderPanelHead(entry) {
  const { root, def, panel } = entry;
  const q = (sel) => root.querySelector(sel);
  q('.ph-offset').textContent = offsetLabel(def.offset);
  q('.ph-strike').textContent = `${fmtStrike(def.strike)} straddle`;
  const last = panel.series[panel.series.length - 1];
  const legs = entry.liveLegs ?? (last ? { ce: last[3], pe: last[4] } : null);
  const price = panel.live?.price ?? last?.[1];

  const parts = [legs ? `CE ${fmtPrice(legs.ce)} · PE ${fmtPrice(legs.pe)}` : ''];
  const overlay = (cls, label, value) => {
    const el = document.createElement('span');
    el.className = cls;
    el.textContent = `${label} ${value != null ? fmtPrice(value) : '–'}`;
    parts.push(' · ', el);
  };
  if (last && state.overlays.vwap) overlay('ph-vwap', 'VWAP', panel.currentVwap);
  if (last && state.overlays.sma) overlay('ph-sma', 'SMA10', panel.currentSma);
  q('.ph-legs').replaceChildren(...parts);
  q('.ph-legs').title = def.ce ? `${def.ce} + ${def.pe}` : '';

  q('.ph-price').textContent = price != null ? fmtPrice(price) : '–';
  const day = dateKey(panel.live?.ts ?? last?.[0] ?? 0);
  const open = panel.series.find((b) => dateKey(b[0]) === day) ?? (panel.live ? [0, panel.live.price] : null);
  setChange(q('.ph-chg'), price, open?.[1], ' vs open');

  const pnf = panel.pnf;
  q('.ph-meta-text').textContent = pnf && panel.series.length
    ? `Box ${fmtPrice(pnf.box)} (0.5% of ${fmtPrice(pnf.p0)}) · ${pnf.reversal}-box · ` +
      `${pnf.columns.length} cols · since ${fmtDay(panel.series[0][0])}`
    : ' ';
  const s = panel.lastSignal;
  const chip = q('.ph-signal');
  chip.hidden = !s;
  if (s) {
    chip.className = `ph-signal ${s.type}`;
    chip.textContent = `${s.type === 'buy' ? '▲ DT buy' : '▼ DB sell'} ${fmtTime(s.ts)}`;
    chip.title = `Last signal: ${SIGNAL_NAMES[s.type]}, ${fmtDay(s.ts)} ${fmtTime(s.ts)}`;
  }

  renderTrade(entry);

  const msg = q('.plot-msg');
  let text = def.error ?? '';
  if (!text && !state.started && !pnf?.columns.length) text = 'Press Play to start the replay.';
  else if (!text && !panel.series.length && !panel.live) text = 'No trades yet at this point in the run.';
  else if (!text && !pnf?.columns.length) {
    text = `Waiting for the first ${fmtPrice(pnf?.box ?? 0)}-point (0.5%) box move...`;
  }
  msg.hidden = !text;
  msg.textContent = text;
  msg.classList.toggle('error', !!def.error);
}

function spotAt(ts) {
  if (state.spot.has(ts)) return state.spot.get(ts);
  let price = null; // carry the last known price through minutes without an index candle
  for (const [t, p] of state.spot) {
    if (t > ts) break;
    price = p;
  }
  return price;
}

function renderClock() {
  const p = state.player;
  if (!p) return;
  const ts = state.minutes[p.index]; // undefined before the first minute
  // Simulated wall time: the revealed minute has closed, and the next one is under way.
  const nextStart = state.minutes[p.index + 1] ?? ts + 60;
  const now = p.atEnd ? ts + 60 : nextStart + Math.floor(p.progress * 60);
  const secs = String(new Date(now * 1000).getUTCSeconds()).padStart(2, '0');
  els.simTime.textContent = `${fmtDay(now)} ${fmtTime(now)}:${secs}`;
  els.simCount.textContent = `minute ${p.index + 1} of ${state.minutes.length}`;
  els.seek.value = String(p.index);
  // The change is against the previous close of the session being simulated now.
  const day = dateKey(p.atEnd ? ts : nextStart);
  const spot = state.formingSpot ?? (ts !== undefined ? spotAt(ts) : state.run.spotPrevCloses[day]);
  els.spot.textContent = spot != null ? fmtPrice(spot) : '–';
  setChange(els.spotChg, spot, state.run.spotPrevCloses[day]);
  els.spotStat.title = `${state.run.underlying.key} index (spot) at the simulated time`;
}

function renderPlayback() {
  const p = state.player;
  els.play.setAttribute('aria-pressed', String(!!p?.playing));
  els.play.title = p?.playing ? 'Pause (Space)' : p?.atEnd ? 'Replay finished' : 'Play (Space)';
  els.playLabel.textContent = p?.playing ? 'Pause' : 'Play';
  const off = state.loading || !p;
  els.play.disabled = off || (p.atEnd && !p.playing);
  els.seek.disabled = off;
  els.restart.disabled = off;
  for (const btn of els.speeds.children) btn.setAttribute('aria-pressed', String(Number(btn.dataset.speed) === state.speed));
  if (p?.atEnd && !state.loading) {
    showBanner('Replay finished. Drag the timeline or press Back to the start to watch again.', 'ok', 'finished');
  } else if (state.bannerTag === 'finished') showBanner('');
}

// The thin bar under the timeline: time left until the next minute is revealed.
function animateNextMinute() {
  const p = state.player;
  els.nextMinute.style.width = `${p && !p.atEnd ? Math.round(p.progress * 1000) / 10 : 0}%`;
  if (p?.playing) {
    showFormingMinute();
    renderClock();
  }
  requestAnimationFrame(animateNextMinute);
}
requestAnimationFrame(animateNextMinute);

// ---------------- Alerts ----------------

function alertSignals(entry, signals) {
  for (const s of signals) {
    const key = `${entry.def.key}:${s.type}:${s.ts}:${s.row}`;
    if (state.alerted.has(key)) continue;
    const pnf = entry.panel.pnf;
    // Judged on the closed bar that confirmed it (not marked until that bar exists).
    const close = entry.panel.series.findLast((b) => b[0] === s.ts)?.[1];
    if (close == null) continue;
    state.alerted.add(key);
    // A double bottom sell alerts only below VWAP (its marker is drawn regardless).
    const vwap = pnf.vwapAt(s.ts);
    if (!alertWanted(s, close, vwap)) continue;
    raiseAlert({
      type: s.type,
      title: `${SIGNAL_NAMES[s.type]} · ${offsetLabel(entry.def.offset)} (simulation)`,
      detail: `${state.run.underlying.key} ${fmtStrike(entry.def.strike)} straddle broke ` +
        `${s.type === 'buy' ? 'above' : 'below'} ${fmtPrice(pnf.price(s.level))} → ` +
        `${fmtPrice(pnf.price(s.row))} at ${fmtDay(s.ts)} ${fmtTime(s.ts)}` +
        (s.type === 'sell' ? ` · below VWAP ${fmtPrice(vwap)}` : ''),
    });
    flashPanel(entry.root, s.type);
  }
}

function flashPanel(root, type) {
  root.classList.remove('flash-buy', 'flash-sell');
  void root.offsetWidth; // restart the animation if it is already running
  root.classList.add(`flash-${type}`);
  clearTimeout(root.flashTimer);
  root.flashTimer = setTimeout(() => root.classList.remove(`flash-${type}`), FLASH_MS);
}

// ---------------- Simulated trades ----------------

const fmtRupees = (v) =>
  `${v >= 0 ? '+' : '\u2212'}\u20b9${Math.abs(Math.round(v)).toLocaleString('en-IN')}`;

/** The price and minute on screen for this chart (the forming minute included). */
function priceNow(entry) {
  const { panel } = entry;
  if (panel.live) return { price: panel.live.price, ts: panel.live.ts };
  const last = panel.series[panel.series.length - 1];
  return last ? { price: last[1], ts: last[0] } : null;
}

function wireTrades(entry) {
  const q = (sel) => entry.root.querySelector(sel);
  q('.trade-sell').textContent = `Sell ${LOTS_PER_SELL} lots`;
  q('.trade-sell').addEventListener('click', () => {
    const now = priceNow(entry);
    if (!now || entry.trade) return;
    entry.trade = new ShortStraddleTrade(state.run.lotSize, now.price, now.ts);
    renderTrade(entry);
  });
  q('.trade-half').addEventListener('click', () => {
    const now = priceNow(entry);
    if (!now || !entry.trade) return;
    entry.trade.exitHalf(now.price, now.ts);
    renderTrade(entry);
  });
  q('.trade-full').addEventListener('click', () => {
    const now = priceNow(entry);
    if (!now || !entry.trade) return;
    entry.trade.exitFull(now.price, now.ts);
    entry.closed.push(entry.trade);
    entry.trade = null;
    renderTrade(entry);
  });
}

function renderTrade(entry) {
  const q = (sel) => entry.root.querySelector(sel);
  const now = priceNow(entry);
  const pnlEl = q('.trade-pnl');
  const detail = q('.trade-detail');
  const sell = q('.trade-sell');
  const half = q('.trade-half');
  const full = q('.trade-full');
  const t = entry.trade;
  const qty = (lots) => `${lots} lot${lots > 1 ? 's' : ''} (${(lots * state.run.lotSize).toLocaleString('en-IN')} qty)`;

  sell.hidden = !!t;
  half.hidden = !t;
  full.hidden = !t;
  sell.disabled = !now || !!entry.def.error;
  const showPnl = (rupees, points) => {
    pnlEl.className = `trade-pnl ${rupees >= 0 ? 'up' : 'down'}`;
    const pts = document.createElement('span');
    pts.className = 'pts';
    pts.textContent = `${points >= 0 ? '+' : '\u2212'}${Math.abs(points).toFixed(2)} pts`;
    pnlEl.replaceChildren(fmtRupees(rupees), pts);
  };

  if (t && now) {
    const p = t.pnl(now.price);
    showPnl(p.total, t.totalPoints(now.price));
    const parts = [`Short ${qty(t.openLots)} @ ${fmtPrice(t.entry)} since ${fmtTime(t.ts)}`];
    if (t.exits.length) parts.push(`booked ${fmtRupees(p.booked)}`);
    detail.textContent = parts.join(' \u00b7 ');
    detail.title = detail.textContent; // shown in full on hover when the bar is narrow
    half.disabled = t.halfLots < 1;
    half.title = t.halfLots ? `Buy back ${qty(t.halfLots)} at the price on screen` : 'Only 1 lot is open; it cannot be halved';
    full.title = `Buy back all ${qty(t.openLots)} at the price on screen`;
    return;
  }

  const last = entry.closed[entry.closed.length - 1];
  if (last) {
    const total = entry.closed.reduce((s, x) => s + x.booked, 0);
    showPnl(last.booked, last.totalPoints(0));
    detail.textContent = `Closed: sold ${fmtPrice(last.entry)} at ${fmtTime(last.ts)}` +
      (entry.closed.length > 1 ? ` \u00b7 ${entry.closed.length} trades, total ${fmtRupees(total)}` : '');
    detail.title = detail.textContent;
  } else {
    pnlEl.replaceChildren();
    detail.textContent = now ? `No trade \u00b7 straddle ${fmtPrice(now.price)}` : 'No trade';
  }
}

// ---------------- Tooltip ----------------

function showTooltip(desc, hover) {
  const tip = els.tooltip;
  if (!desc || !hover) {
    tip.hidden = true;
    return;
  }
  const dir = desc.dir.toLowerCase();
  const signal = desc.signal
    ? `<div class="tip-signal ${desc.signal}">${desc.signal === 'buy' ? '▲' : '▼'} ${SIGNAL_NAMES[desc.signal]}</div>`
    : '';
  const extra = (desc.vwap != null ? `<div class="tip-vwap">VWAP ${fmtPrice(desc.vwap)}</div>` : '') +
    (desc.sma != null ? `<div class="tip-sma">SMA 10 ${fmtPrice(desc.sma)}</div>` : '');
  tip.innerHTML = `${signal}<b class="${dir}">${desc.dir}</b> ${fmtPrice(desc.price)} ` +
    `<span class="muted">· ${desc.when}</span>${extra}`;
  tip.hidden = false;
  const pad = 14;
  const r = tip.getBoundingClientRect();
  let x = hover.clientX + pad;
  let y = hover.clientY + pad;
  if (x + r.width > window.innerWidth - 8) x = hover.clientX - r.width - pad;
  if (y + r.height > window.innerHeight - 8) y = hover.clientY - r.height - pad;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}

// ---------------- Playback controls ----------------

els.speeds.replaceChildren(...SPEEDS.map((speed) => {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.dataset.speed = String(speed);
  btn.textContent = `${speed}X`;
  const secs = (375 * 60) / speed; // a full session
  const len = secs >= 3600 ? `${(secs / 3600).toFixed(2).replace(/\.?0+$/, '')} h`
    : secs >= 60 ? `${Math.round(secs / 60)} min` : `${Math.round(secs)} s`;
  btn.title = `${speed === 1 ? 'Real time' : `${speed}x real time`}: a full session takes about ${len}`;
  btn.addEventListener('click', () => {
    state.speed = speed;
    store(SPEED_KEY, speed);
    state.player?.setSpeed(speed);
    renderPlayback();
  });
  return btn;
}));

function togglePlay() {
  const p = state.player;
  if (!p || state.loading || (p.atEnd && !p.playing)) return; // e.g. Space while a new run loads
  if (p.playing) p.pause();
  else {
    showBanner('');
    if (!state.started) {
      state.started = true;
      for (const entry of state.panels.values()) renderPanelHead(entry);
    }
    p.play();
  }
}

els.play.addEventListener('click', togglePlay);
els.restart.addEventListener('click', () => seekTo(-1));
els.seek.addEventListener('input', () => seekTo(Number(els.seek.value)));

document.addEventListener('keydown', (e) => {
  // Text fields keep their keys; the timeline slider does not need Space. Browser
  // shortcuts (e.g. Alt + Left for Back) are left alone.
  if (!state.player || e.altKey || e.ctrlKey || e.metaKey) return;
  if (e.target.closest('input:not([type="range"]), select, textarea')) return;
  if (e.key === ' ') {
    e.preventDefault();
    togglePlay();
  } else if ((e.key === 'ArrowRight' || e.key === 'ArrowLeft') && !els.seek.disabled) {
    e.preventDefault(); // a focused timeline would step a second minute on its own
    seekTo(state.player.index + (e.key === 'ArrowRight' ? 1 : -1));
  }
});

// ---------------- Overlays, zoom, sound ----------------

mountMenu(els.viewBtn, els.viewPanel);

function renderOverlayToggles() {
  for (const box of els.overlays.querySelectorAll('input[data-overlay]')) {
    box.checked = state.overlays[box.dataset.overlay];
  }
}

els.overlays.addEventListener('change', (e) => {
  const box = e.target.closest('input[data-overlay]');
  if (!box) return;
  state.overlays = { ...state.overlays, [box.dataset.overlay]: box.checked };
  saveOverlays(state.overlays);
  for (const entry of state.panels.values()) {
    entry.panel.setOverlays(state.overlays);
    renderPanelHead(entry);
  }
  renderOverlayToggles();
});

function setZoom(cellOverride) {
  state.cellOverride = cellOverride;
  for (const { panel } of state.panels.values()) panel.setCell(cellOverride);
  els.zoomFit.setAttribute('aria-pressed', String(cellOverride == null));
}

function stepZoom(dir) {
  const current = state.cellOverride ?? state.panels.values().next().value?.panel.cell ?? 12;
  setZoom(dir > 0
    ? CELL_STEPS.find((s) => s > current) ?? CELL_STEPS[CELL_STEPS.length - 1]
    : CELL_STEPS.findLast((s) => s < current) ?? CELL_STEPS[0]);
}

els.zoomIn.addEventListener('click', () => stepZoom(1));
els.zoomOut.addEventListener('click', () => stepZoom(-1));
els.zoomFit.addEventListener('click', () => setZoom(null));

function renderSound() {
  const on = soundEnabled();
  els.sound.setAttribute('aria-pressed', String(on));
  els.sound.title = on ? 'Alert sounds on - click to mute' : 'Alert sounds muted - click to unmute';
}

els.sound.addEventListener('click', () => {
  setSoundEnabled(!soundEnabled());
  renderSound();
});

renderOverlayToggles();
renderSound();
setZoom(null);
renderPlayback();
loadOptions();
