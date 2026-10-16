import { raiseAlert, requestNotifications, setSoundEnabled, soundEnabled } from './alerts.js';
import { CELL_STEPS, PnfPanel, dateKey, fmtDay, fmtPrice, fmtTime } from './chart.js';
import { alertWanted, istDay } from './pnf.js';
import { loadOverlays, mountMenu, saveOverlays } from './view.js';
import { mountTrading, updateTrading } from '/kite/static/trading.js';

const MAX_DAYS = 15;

const $ = (id) => document.getElementById(id);
const els = {
  brandTitle: $('brand-title'),
  underlying: $('underlying'),
  spotStat: $('spot-stat'),
  spot: $('spot'),
  spotChg: $('spot-chg'),
  futStat: $('fut-stat'),
  fut: $('fut'),
  futChg: $('fut-chg'),
  expiry: $('expiry'),
  config: $('config'),
  atm: $('atm'),
  atmAuto: $('atm-auto'),
  apply: $('apply'),
  days: $('days'),
  zoomIn: $('zoom-in'),
  zoomOut: $('zoom-out'),
  zoomFit: $('zoom-fit'),
  sound: $('sound'),
  overlays: $('overlays'),
  viewBtn: $('view-btn'),
  viewPanel: $('view-panel'),
  alertLogBtn: $('alert-log-btn'),
  alertBadge: $('alert-badge'),
  alertLog: $('alert-log'),
  alertLogList: $('alert-log-list'),
  alertLogEmpty: $('alert-log-empty'),
  alertLogClose: $('alert-log-close'),
  pill: $('feed-pill'),
  login: $('login'),
  totp: $('totp'),
  loginBtn: $('login-btn'),
  goLive: $('go-live'),
  banner: $('banner'),
  bannerText: $('banner-text'),
  bannerAction: $('banner-action'),
  charts: $('charts'),
  template: $('panel-template'),
  tooltip: $('tooltip'),
};

const state = {
  connected: false,
  generation: null,
  config: null,
  status: null,
  spot: null,
  spotPrevClose: null,
  future: null, // {symbol, expiry, ltp, prevClose} of the near-month future
  legs: {},
  live: null,
  panels: new Map(), // key -> {root, panel, def}
  cellOverride: null,
  wantLogin: false,
  loginError: '',
  atmDirty: false,
  alerted: new Set(), // signal keys already alerted, so a re-sent bar never alerts twice
  alerts: [], // the server's alert log, newest first
};

const SIGNAL_NAMES = { buy: 'Double top buy', sell: 'Double bottom sell' };
state.overlays = loadOverlays();
const FLASH_MS = 8000;
const CATCHUP_ALERT_S = 5 * 60; // after a reconnect, signals at most this old still alert

for (let d = 1; d <= MAX_DAYS; d++) els.days.add(new Option(String(d), String(d)));

// ---------------- Formatting ----------------

const signed = (v, digits = 2) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(digits)}`;
const offsetLabel = (off) => (off === 0 ? 'ATM' : `ATM ${off > 0 ? '+' : '−'} ${Math.abs(off)}`);
const fmtStrike = (s) => s.toLocaleString('en-IN');

/** Show "+12.30 (+1.25%)" (the absolute part in its own span, so CSS can hide it). */
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

// ---------------- Panels ----------------

function buildPanels(charts) {
  const keys = charts.map((c) => c.key).join();
  if ([...state.panels.keys()].join() === keys) return;
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
    state.panels.set(def.key, { root, panel, def });
  }
}

function currentPrice(entry) {
  const live = state.live?.straddles?.[entry.def.key];
  if (live != null) return { price: live, ts: state.live.minute };
  const last = entry.panel.series[entry.panel.series.length - 1];
  return last ? { price: last[1], ts: last[0] } : null;
}

function renderPanelHead(entry) {
  const { root, def, panel } = entry;
  const q = (sel) => root.querySelector(sel);
  q('.ph-offset').textContent = offsetLabel(def.offset);
  q('.ph-strike').textContent = `${fmtStrike(def.strike)} straddle`;

  const legPrice = (leg) => (leg && state.legs[leg.key] != null ? fmtPrice(state.legs[leg.key]) : '–');
  const legs = q('.ph-legs');
  if (def.error) {
    legs.replaceChildren();
  } else {
    const parts = [`CE ${legPrice(def.ce)} · PE ${legPrice(def.pe)}`];
    const overlayValue = (cls, label, value) => {
      const el = document.createElement('span');
      el.className = cls;
      el.textContent = `${label} ${value != null ? fmtPrice(value) : '–'}`;
      parts.push(' · ', el);
    };
    if (state.overlays.vwap) overlayValue('ph-vwap', 'VWAP', panel.currentVwap);
    if (state.overlays.sma) overlayValue('ph-sma', `SMA${panel.pnf?.smaPeriod ?? 10}`, panel.currentSma);
    legs.replaceChildren(...parts);
  }
  q('.ph-legs').title = def.error ? '' : `${def.ce.symbol} + ${def.pe.symbol}`;

  const cur = currentPrice(entry);
  q('.ph-price').textContent = cur ? fmtPrice(cur.price) : '–';
  // Change against the first close of the session the current price belongs to.
  const open = cur && sessionOpen(panel.series, cur.ts);
  setChange(q('.ph-chg'), cur?.price, open?.[1], ' vs open');

  const pnf = panel.pnf;
  const first = panel.series[0];
  renderSignalChip(q('.ph-signal'), panel);
  q('.ph-meta-text').textContent = pnf && first // a live price alone, before any closed bar, has no history yet
    ? `Box ${fmtPrice(pnf.box)} (0.5% of ${fmtPrice(pnf.p0)}) · ${pnf.reversal}-box · ` +
      `${pnf.columns.length} cols · since ${fmtDay(first[0])}`
    : ' ';

  const msg = q('.plot-msg');
  let text = '';
  let error = false;
  if (def.error) {
    text = def.error;
    error = true;
  } else if (!panel.series.length) {
    text = state.status?.phase === 'loading' ? state.status.message : 'No 1-min data for this straddle yet.';
  } else if (!pnf?.columns.length) {
    text = 'Waiting for a full 0.5% box move...';
  }
  msg.hidden = !text;
  msg.textContent = text;
  msg.classList.toggle('error', error);
}

/** First bar of the session `ts` belongs to (scanned back from the newest bar). */
function sessionOpen(series, ts) {
  const day = istDay(ts);
  let open = null;
  for (let i = series.length - 1; i >= 0 && istDay(series[i][0]) >= day; i--) {
    if (istDay(series[i][0]) === day) open = series[i];
  }
  return open;
}

function renderSignalChip(el, panel) {
  const s = panel.lastSignal;
  el.hidden = !s;
  if (!s) return;
  const last = panel.series[panel.series.length - 1];
  const sameDay = dateKey(s.ts) === dateKey(last[0]);
  const when = sameDay ? fmtTime(s.ts) : `${fmtDay(s.ts).slice(4)} ${fmtTime(s.ts)}`;
  el.className = `ph-signal ${s.type}`;
  el.textContent = `${s.type === 'buy' ? '\u25b2 DT buy' : '\u25bc DB sell'} ${when}`;
  el.title = `Last signal: ${SIGNAL_NAMES[s.type]} at ${fmtPrice(panel.pnf.price(s.row))}, ` +
    `${fmtDay(s.ts)} ${fmtTime(s.ts)}`;
}

function alertSignals(entry, signals) {
  for (const s of signals) {
    const key = `${entry.def.ce?.key}:${s.type}:${s.ts}:${s.row}`;
    if (state.alerted.has(key)) continue;
    // Judged on the closed bar that confirmed it: a signal from the forming minute
    // is not marked, so it can still alert when its bar closes.
    const close = closeAt(entry.panel.series, s.ts);
    if (close == null) continue;
    state.alerted.add(key);
    // A double bottom sell alerts only below VWAP (its marker is drawn regardless).
    const vwap = entry.panel.pnf.vwapAt(s.ts);
    if (!alertWanted(s, close, vwap)) continue;
    const price = entry.panel.pnf.price(s.row);
    const level = entry.panel.pnf.price(s.level);
    // Record it in the shared log (the server ignores duplicates from other tabs).
    post('/api/alerts', {
      key: `${builtKey()}|${key}`,
      type: s.type,
      underlying: builtKey(),
      offset: entry.def.offset,
      strike: entry.def.strike,
      expiry: state.config?.expiry ?? null,
      level: Math.round(level * 100) / 100,
      price: Math.round(price * 100) / 100,
      barTs: s.ts,
    }).catch(() => {}); // logging must never get in the way of the alert itself
    raiseAlert({
      type: s.type,
      title: `${SIGNAL_NAMES[s.type]} \u00b7 ${offsetLabel(entry.def.offset)}`,
      detail: `${builtKey()} ${fmtStrike(entry.def.strike)} straddle broke ${s.type === 'buy' ? 'above' : 'below'} ` +
        `${fmtPrice(entry.panel.pnf.price(s.level))} \u2192 ${fmtPrice(price)} at ${fmtTime(s.ts)}` +
        (s.type === 'sell' ? ` \u00b7 below VWAP ${fmtPrice(vwap)}` : ''),
    });
    flashPanel(entry.root, s.type);
  }
}

/** Close of the bar at `ts` (searched from the newest end, where signals are). */
function closeAt(series, ts) {
  for (let i = series.length - 1; i >= 0 && series[i][0] >= ts; i--) if (series[i][0] === ts) return series[i][1];
  return null;
}

function flashPanel(root, type) {
  root.classList.remove('flash-buy', 'flash-sell');
  void root.offsetWidth; // restart the animation if it is already running
  root.classList.add(`flash-${type}`);
  clearTimeout(root.flashTimer);
  root.flashTimer = setTimeout(() => root.classList.remove(`flash-${type}`), FLASH_MS);
}

/** The index the charts on screen were built for (may lag a just-clicked switch). */
const builtKey = () => state.config?.built.key ?? '';

function renderUnderlyings(cfg) {
  const keys = cfg.underlyings ?? [];
  if (els.underlying.dataset.keys !== keys.join()) {
    els.underlying.replaceChildren(...keys.map((key) => new Option(key, key)));
    els.underlying.dataset.keys = keys.join();
  }
  // The latest choice, even while its charts are still loading.
  els.underlying.value = cfg.underlying;
}

// ---------------- Alert log ----------------

const SEEN_KEY = 'pnf.alertsSeen'; // loggedAt of the newest alert the viewer has seen

let seenInMemory = null; // fallback when the browser blocks storage

function readSeen() {
  try {
    const v = Number(localStorage.getItem(SEEN_KEY));
    // The in-memory value wins when newer: storage may read but refuse writes (quota).
    if (Number.isFinite(v) && v > 0) return Math.max(v, seenInMemory ?? 0);
  } catch {
    // storage unavailable: use the in-memory value
  }
  return seenInMemory;
}

function writeSeen(v) {
  seenInMemory = v;
  try {
    localStorage.setItem(SEEN_KEY, String(v));
  } catch {
    // storage unavailable: kept in memory for this page only
  }
}

let seenAtOpen = 0; // alerts newer than this are highlighted while the log is open

function setAlerts(alerts) {
  state.alerts = alerts ?? [];
  // First visit: count only alerts that arrive from now on, not the backlog.
  if (readSeen() === null) writeSeen(state.alerts[0]?.loggedAt ?? Math.floor(Date.now() / 1000));
  if (!els.alertLog.hidden) markAlertsSeen();
  renderAlertLog();
}

function markAlertsSeen() {
  if (state.alerts.length) writeSeen(Math.max(readSeen() ?? 0, state.alerts[0].loggedAt));
}

function renderAlertLog() {
  const seen = readSeen() ?? 0;
  const unseen = state.alerts.filter((a) => a.loggedAt > seen).length;
  els.alertBadge.hidden = !unseen;
  els.alertBadge.textContent = unseen > 9 ? '9+' : String(unseen);
  els.alertLogBtn.title = unseen ? `Alert log (last 30) - ${unseen} new` : 'Alert log (last 30)';

  els.alertLogEmpty.hidden = state.alerts.length > 0;
  els.alertLogList.replaceChildren(...state.alerts.map((a) => {
    const li = document.createElement('li');
    li.className = `${a.type}${a.loggedAt > seenAtOpen ? ' unseen' : ''}`;
    const isError = a.type === 'error'; // an error a page showed (e.g. a Kite order not placed)
    const icon = document.createElement('span');
    icon.className = 'icon';
    icon.textContent = isError ? '!' : a.type === 'buy' ? '\u25b2' : '\u25bc';
    const title = document.createElement('b');
    title.textContent = isError ? a.title : `${SIGNAL_NAMES[a.type]} \u00b7 ${offsetLabel(a.offset)}`;
    const when = isError ? a.loggedAt : a.barTs;
    const time = document.createElement('time');
    time.dateTime = new Date(when * 1000).toISOString();
    time.textContent = `${fmtDay(when)} ${fmtTime(when)}`;
    const detail = document.createElement('span');
    detail.className = 'detail';
    if (isError) {
      detail.textContent = a.detail;
      li.append(icon, title, time, detail);
      return li;
    }
    const expiry = a.expiry
      ? ` (${new Date(`${a.expiry}T00:00:00+05:30`).toLocaleDateString('en-GB', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short' })})`
      : '';
    detail.textContent = `${a.underlying} ${fmtStrike(a.strike)}${expiry} straddle broke ` +
      `${a.type === 'buy' ? 'above' : 'below'} ${fmtPrice(a.level)} \u2192 ${fmtPrice(a.price)}`;
    li.append(icon, title, time, detail);
    return li;
  }));
}

function toggleAlertLog(open = els.alertLog.hidden) {
  if (open) {
    seenAtOpen = readSeen() ?? 0;
    markAlertsSeen();
  }
  els.alertLog.hidden = !open;
  els.alertLogBtn.setAttribute('aria-expanded', String(open));
  renderAlertLog();
}

els.alertLogBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  toggleAlertLog();
});
els.alertLogClose.addEventListener('click', () => toggleAlertLog(false));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !els.alertLog.hidden) toggleAlertLog(false);
});
document.addEventListener('click', (e) => {
  if (!els.alertLog.hidden && !els.alertLog.contains(e.target)) toggleAlertLog(false);
});

function renderPanels() {
  for (const entry of state.panels.values()) renderPanelHead(entry);
  renderTrading();
}

/** Tell the Kite trading UI (order bars, positions strip) what the charts show. */
function renderTrading() {
  updateTrading({
    underlying: builtKey(),
    expiry: state.config?.expiry ?? null,
    panels: [...state.panels.values()].map(({ root, def }) => ({ root, def })),
    live: state.live,
  });
}

// ---------------- Top bar ----------------

function renderTopbar() {
  els.spot.textContent = state.spot != null ? fmtPrice(state.spot) : '–';
  setChange(els.spotChg, state.spot, state.spotPrevClose);
  const fut = state.future;
  els.fut.textContent = fut?.ltp != null ? fmtPrice(fut.ltp) : '–';
  setChange(els.futChg, fut?.ltp, fut?.prevClose);
  els.futStat.title = fut
    ? `${fut.symbol}, near-month future expiring ${new Date(`${fut.expiry}T00:00:00+05:30`).toLocaleDateString('en-GB', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric' })}`
    : 'Near-month future unavailable';

  const cfg = state.config;
  if (cfg) {
    const u = cfg.built;
    renderUnderlyings(cfg);
    renderExpiryOptions(cfg);
    els.brandTitle.textContent = `${u.key} straddle`;
    document.title = `${u.key} Straddle P&F`;
    els.spotStat.title = `${u.key} index (spot)`;
    els.atm.step = String(u.strikeStep);
    if (!state.atmDirty && document.activeElement !== els.atm && cfg.atm != null) els.atm.value = cfg.atm;
    els.days.value = String(cfg.days);
    const auto = cfg.atmOverride == null && !state.atmDirty;
    els.atmAuto.setAttribute('aria-pressed', String(auto));
    els.atmAuto.title = cfg.indexOpen != null
      ? `Use the strike nearest to today's ${u.key} open (${fmtPrice(cfg.indexOpen)} → ${fmtStrike(cfg.atmAuto)})`
      : `Use the strike nearest to today's ${u.key} open`;
  }

  els.apply.hidden = !state.atmDirty;
  els.zoomFit.setAttribute('aria-pressed', String(state.cellOverride == null));

  // Feed status
  const feed = state.status?.feed;
  const pillText = { live: 'Live', connecting: 'Connecting', reconnecting: 'Reconnecting', offline: 'History only' };
  const fs = state.connected ? feed?.state ?? 'offline' : 'offline';
  els.pill.className = `pill ${fs === 'live' ? 'live' : fs === 'offline' ? '' : 'pending'}`;
  els.pill.querySelector('span').textContent = state.connected ? pillText[fs] : 'Disconnected';

  const loggedIn = !!feed?.loggedIn;
  const showLogin = state.connected && !loggedIn && (feed?.totpRequired || state.wantLogin);
  els.login.hidden = !showLogin;
  // "Go live" already says the feed is off, so the pill only reports other states.
  els.pill.hidden = showLogin || (state.connected && !loggedIn && fs === 'offline');
  els.goLive.hidden = !state.connected || loggedIn || showLogin;
  els.totp.setAttribute('aria-invalid', String(!!state.loginError));
  els.totp.title = state.loginError;
}

function renderExpiryOptions(cfg) {
  const options = cfg.expiryOptions ?? [];
  const signature = options.map((o) => `${o.role}=${o.date}`).join();
  if (els.expiry.dataset.signature !== signature) {
    els.expiry.replaceChildren(...options.map((o) => new Option(o.label, o.role)));
    els.expiry.dataset.signature = signature;
  }
  // Select by date: a role folded into another (same contract) still shows as chosen.
  const current = options.find((o) => o.date === cfg.expiry);
  if (current) els.expiry.value = current.role;
  // The options belong to the charts on screen: while another index loads, they
  // are not that index's to pick from (BANKNIFTY has monthlies only).
  els.expiry.disabled = !options.length || cfg.underlying !== cfg.built.key;
}

function renderBanner() {
  const st = state.status;
  let text = '';
  let kind = '';
  let action = null;
  if (!state.connected) {
    text = 'Connection to the local server lost - reconnecting...';
    kind = 'error';
  } else if (st?.phase === 'error') {
    text = `Could not load the charts: ${st.message}`;
    kind = 'error';
    action = { label: 'Retry', run: () => submitConfig() };
  } else if (state.loginError) {
    text = state.loginError;
    kind = 'error';
  } else if (st?.phase === 'loading') {
    text = st.message;
  } else if (!st?.feed.loggedIn && st?.feed.totpRequired) {
    text = 'Market is open. Enter the 6-digit TOTP from your authenticator app to stream live prices.';
  } else if (st?.feed.message && st.feed.state !== 'live') {
    text = st.feed.message;
    kind = 'warn';
  }
  els.banner.hidden = !text;
  els.banner.className = `banner ${kind}`;
  els.bannerText.textContent = text;
  els.bannerAction.hidden = !action;
  if (action) {
    els.bannerAction.textContent = action.label;
    els.bannerAction.onclick = action.run;
  }
}

function renderAll() {
  renderTopbar();
  renderBanner();
  renderPanels();
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
    ? `<div class="tip-signal ${desc.signal}">${desc.signal === 'buy' ? '\u25b2' : '\u25bc'} ${SIGNAL_NAMES[desc.signal]}</div>`
    : '';
  const vwap = (desc.vwap != null ? `<div class="tip-vwap">VWAP ${fmtPrice(desc.vwap)}</div>` : '') +
    (desc.sma != null ? `<div class="tip-sma">SMA 10 ${fmtPrice(desc.sma)}</div>` : '');
  tip.innerHTML = `${signal}<b class="${dir}">${desc.dir}</b> ${fmtPrice(desc.price)} <span class="muted">· ${desc.when}${
    desc.provisional ? ' · forming' : ''
  }</span>${vwap}`;
  tip.hidden = false;
  const pad = 14;
  const { innerWidth: vw, innerHeight: vh } = window;
  const r = tip.getBoundingClientRect();
  let x = hover.clientX + pad;
  let y = hover.clientY + pad;
  if (x + r.width > vw - 8) x = hover.clientX - r.width - pad;
  if (y + r.height > vh - 8) y = hover.clientY - r.height - pad;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}

// ---------------- Server messages ----------------

function applyLive(live) {
  state.live = live;
  for (const entry of state.panels.values()) {
    const price = live?.straddles?.[entry.def.key];
    const volume = live?.volumes?.[entry.def.key] ?? 0;
    entry.panel.setLive(price != null ? { ts: live.minute, price, volume } : null);
  }
  if (live?.legs) Object.assign(state.legs, live.legs);
}

const handlers = {
  snapshot(msg) {
    state.generation = msg.generation;
    state.config = msg.config;
    state.status = msg.status;
    state.spot = msg.spot;
    state.spotPrevClose = msg.spotPrevClose;
    state.future = msg.future ?? null;
    state.legs = { ...msg.legs };
    setAlerts(msg.alerts);
    buildPanels(msg.charts);
    for (const def of msg.charts) {
      const entry = state.panels.get(def.key);
      const changed = entry.def.strike !== def.strike || entry.def.ce?.key !== def.ce?.key;
      entry.def = def;
      if (changed || !entry.panel.series.length) entry.panel.setSeries(def.series);
      else {
        // Same strikes (e.g. a reconcile or a reconnect): keep the user's scroll position.
        const lastSeen = entry.panel.series[entry.panel.series.length - 1]?.[0] ?? Infinity;
        const follow = entry.panel.follow;
        entry.panel.series = def.series;
        entry.panel.recompute();
        entry.panel.follow = follow;
        // Bars that closed while this page was disconnected arrive only here: their
        // recent signals must still alert (already-alerted ones are skipped by key).
        // Older ones stay silent, as on a page load: a backlog after a long sleep is noise.
        const recent = Date.now() / 1000 - CATCHUP_ALERT_S;
        alertSignals(entry, entry.panel.pnf?.signals.filter((s) => s.ts > lastSeen && s.ts >= recent) ?? []);
      }
    }
    applyLive(msg.live);
    renderAll();
  },
  alerts(msg) {
    setAlerts(msg.alerts);
  },
  status(msg) {
    state.status = msg;
    if (state.config) state.config.underlying = msg.underlying; // a switch, in every tab
    if (msg.feed.loggedIn) state.wantLogin = false;
    if (msg.feed.state !== 'live') applyLive(null);
    renderAll();
  },
  bar(msg) {
    if (msg.generation !== state.generation) return;
    const entry = state.panels.get(msg.key);
    if (!entry) return;
    const confirmed = entry.panel.addBar(msg.bar);
    renderPanelHead(entry);
    alertSignals(entry, confirmed);
  },
  tick(msg) {
    if (msg.generation !== state.generation) return;
    state.spot = msg.spot;
    state.spotPrevClose = msg.spotPrevClose;
    state.future = msg.future ?? null;
    applyLive(msg.live);
    renderTopbar();
    renderPanels();
  },
};

let retryDelay = 500;
function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = () => {
    state.connected = true;
    retryDelay = 500;
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    handlers[msg.type]?.(msg);
  };
  ws.onclose = () => {
    if (state.connected) {
      state.connected = false;
      applyLive(null);
      renderAll();
    }
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 5000);
  };
}

// ---------------- Actions ----------------

async function post(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join('; ') : data.detail;
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

/**
 * Post the current controls, with `overrides` taking precedence. `atm` and
 * `expiry` are sent only when the user set them (`atm`: a strike, or null for
 * Auto); otherwise the server keeps the stored choice for that index.
 */
async function submitConfig(overrides = {}) {
  const body = {
    underlying: state.config?.underlying ?? 'NIFTY',
    days: Number(els.days.value),
    ...overrides,
  };
  try {
    await post('/api/config', body);
    state.atmDirty = false;
    return true;
  } catch (err) {
    state.status = { ...state.status, phase: 'error', message: err.message };
    renderAll();
    return false;
  }
}

els.config.addEventListener('submit', (e) => {
  e.preventDefault();
  const atm = Number(els.atm.value);
  const u = state.config?.built;
  if (!u) return;
  if (!Number.isInteger(atm) || atm % u.strikeStep) {
    els.atm.setCustomValidity(`${u.key} ATM must be a multiple of ${u.strikeStep}`);
    els.atm.reportValidity();
    return;
  }
  els.atm.blur();
  // The strike typed applies to the index on screen.
  submitConfig({ underlying: u.key, atm }).then(renderTopbar);
});

els.atm.addEventListener('input', () => {
  els.atm.setCustomValidity('');
  state.atmDirty = true;
  renderTopbar();
});

els.atmAuto.addEventListener('click', () => {
  state.atmDirty = false;
  submitConfig({ ...(builtKey() && { underlying: builtKey() }), atm: null }).then(renderTopbar);
});

els.atm.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  state.atmDirty = false; // discard the draft
  els.atm.value = state.config?.atm ?? '';
  els.atm.setCustomValidity('');
  els.atm.blur();
  renderTopbar();
});

els.days.addEventListener('change', () => submitConfig());
els.expiry.addEventListener('change', () => submitConfig({ expiry: els.expiry.value }));

els.underlying.addEventListener('change', () => {
  if (!state.config) return;
  const previous = state.config.underlying;
  // Shown as chosen at once (the server's status confirms it), so a tick
  // re-rendering the menu before then does not flip it back.
  state.config.underlying = els.underlying.value;
  state.atmDirty = false;
  // Neither `atm` nor `expiry`: the server restores that index's own choices.
  submitConfig({ underlying: els.underlying.value }).then((ok) => {
    if (!ok) state.config.underlying = previous;
    renderTopbar();
  });
});

function renderSound() {
  const on = soundEnabled();
  els.sound.setAttribute('aria-pressed', String(on));
  els.sound.title = on ? 'Alert sounds on - click to mute' : 'Alert sounds muted - click to unmute';
}

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
  for (const { panel } of state.panels.values()) panel.setOverlays(state.overlays);
  renderOverlayToggles();
  renderPanels();
});
renderOverlayToggles();

els.sound.addEventListener('click', () => {
  setSoundEnabled(!soundEnabled());
  renderSound();
});
renderSound();

function setZoom(cellOverride) {
  state.cellOverride = cellOverride;
  for (const { panel } of state.panels.values()) panel.setCell(cellOverride);
  renderTopbar();
}

function stepZoom(dir) {
  const current = state.cellOverride ?? state.panels.values().next().value?.panel.cell ?? 12;
  const next = dir > 0
    ? CELL_STEPS.find((s) => s > current) ?? CELL_STEPS[CELL_STEPS.length - 1]
    : CELL_STEPS.findLast((s) => s < current) ?? CELL_STEPS[0];
  setZoom(next);
}

els.zoomIn.addEventListener('click', () => stepZoom(1));
els.zoomOut.addEventListener('click', () => stepZoom(-1));
els.zoomFit.addEventListener('click', () => setZoom(null));

els.goLive.addEventListener('click', () => {
  state.wantLogin = true;
  renderTopbar();
  els.totp.focus();
});

els.totp.addEventListener('input', () => {
  els.totp.value = els.totp.value.replace(/\D/g, '').slice(0, 6);
  if (state.loginError) {
    state.loginError = '';
    renderAll();
  }
});

els.login.addEventListener('submit', async (e) => {
  e.preventDefault();
  requestNotifications(); // going live is when signal alerts start to matter
  els.loginBtn.disabled = true;
  els.loginBtn.textContent = 'Logging in...';
  try {
    await post('/api/login', { totp: els.totp.value });
    state.loginError = '';
    els.totp.value = '';
  } catch (err) {
    state.loginError = `Login failed: ${err.message}`;
    els.totp.select();
  } finally {
    els.loginBtn.disabled = false;
    els.loginBtn.textContent = 'Go live';
    renderAll();
  }
});

mountTrading({ topbar: document.querySelector('.topbar'), before: document.querySelector('.feed'), banner: els.banner });
connect();
