import { raiseAlert, requestNotifications, setSoundEnabled, soundEnabled } from './alerts.js';
import { CELL_STEPS, PnfPanel, dateKey, fmtDay, fmtPrice, fmtTime } from './chart.js';

const MAX_DAYS = 15;
const STRIKE_STEP = 50;

const $ = (id) => document.getElementById(id);
const els = {
  spot: $('spot'),
  spotChg: $('spot-chg'),
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
  legs: {},
  live: null,
  panels: new Map(), // key -> {root, panel, def}
  cellOverride: null,
  wantLogin: false,
  loginError: '',
  atmDirty: false,
  alerted: new Set(), // signal keys already alerted, so a re-sent bar never alerts twice
};

const SIGNAL_NAMES = { buy: 'Double top buy', sell: 'Double bottom sell' };
const FLASH_MS = 8000;

for (let d = 1; d <= MAX_DAYS; d++) els.days.add(new Option(String(d), String(d)));

// ---------------- Formatting ----------------

const signed = (v, digits = 2) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(digits)}`;
const offsetLabel = (off) => (off === 0 ? 'ATM' : `ATM ${off > 0 ? '+' : '−'} ${Math.abs(off)}`);
const fmtStrike = (s) => s.toLocaleString('en-IN');

function setChange(el, value, base, suffix = '') {
  if (value == null || base == null || !base) {
    el.textContent = '';
    el.className = el.className.replace(/\b(up|down)\b/g, '').trim();
    return;
  }
  const diff = value - base;
  el.textContent = `${signed(diff)} (${signed((diff / base) * 100)}%)${suffix}`;
  el.classList.toggle('up', diff >= 0);
  el.classList.toggle('down', diff < 0);
}

// ---------------- Panels ----------------

function buildPanels(charts) {
  const keys = charts.map((c) => c.key).join();
  if ([...state.panels.keys()].join() === keys) return;
  els.charts.replaceChildren();
  state.panels.clear();
  for (const def of charts) {
    const root = els.template.content.firstElementChild.cloneNode(true);
    root.classList.toggle('atm', def.offset === 0);
    els.charts.append(root);
    const panel = new PnfPanel(root.querySelector('.plot'), { onHover: showTooltip });
    panel.setCell(state.cellOverride);
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

  const legPrice = (leg) => (leg && state.legs[leg.token] != null ? fmtPrice(state.legs[leg.token]) : '–');
  const legs = q('.ph-legs');
  if (def.error) {
    legs.replaceChildren();
  } else {
    const vwap = panel.currentVwap;
    const vwapEl = document.createElement('span');
    vwapEl.className = 'ph-vwap';
    vwapEl.textContent = `VWAP ${vwap != null ? fmtPrice(vwap) : '–'}`;
    legs.replaceChildren(`CE ${legPrice(def.ce)} · PE ${legPrice(def.pe)} · `, vwapEl);
  }
  q('.ph-legs').title = def.error ? '' : `${def.ce.symbol} + ${def.pe.symbol}`;

  const cur = currentPrice(entry);
  q('.ph-price').textContent = cur ? fmtPrice(cur.price) : '–';
  // Change against the first close of the session the current price belongs to.
  const day = cur && dateKey(cur.ts);
  const open = cur && panel.series.find((b) => dateKey(b[0]) === day);
  setChange(q('.ph-chg'), cur?.price, open?.[1], ' vs open');

  const pnf = panel.pnf;
  const first = panel.series[0];
  renderSignalChip(q('.ph-signal'), panel);
  q('.ph-meta-text').textContent = pnf
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
    const key = `${entry.def.ce?.token}:${s.type}:${s.ts}:${s.row}`;
    if (state.alerted.has(key)) continue;
    state.alerted.add(key);
    const price = entry.panel.pnf.price(s.row);
    raiseAlert({
      type: s.type,
      title: `${SIGNAL_NAMES[s.type]} \u00b7 ${offsetLabel(entry.def.offset)}`,
      detail: `${fmtStrike(entry.def.strike)} straddle broke ${s.type === 'buy' ? 'above' : 'below'} ` +
        `${fmtPrice(entry.panel.pnf.price(s.level))} \u2192 ${fmtPrice(price)} at ${fmtTime(s.ts)}`,
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

function renderPanels() {
  for (const entry of state.panels.values()) renderPanelHead(entry);
}

// ---------------- Top bar ----------------

function renderTopbar() {
  els.spot.textContent = state.spot != null ? fmtPrice(state.spot) : '–';
  setChange(els.spotChg, state.spot, state.spotPrevClose);

  const cfg = state.config;
  if (cfg) {
    renderExpiryOptions(cfg);
    if (!state.atmDirty && document.activeElement !== els.atm && cfg.atm != null) els.atm.value = cfg.atm;
    els.days.value = String(cfg.days);
    const auto = cfg.atmOverride == null && !state.atmDirty;
    els.atmAuto.setAttribute('aria-pressed', String(auto));
    els.atmAuto.title = cfg.niftyOpen != null
      ? `Use the strike nearest to today's NIFTY open (${fmtPrice(cfg.niftyOpen)} → ${fmtStrike(cfg.atmAuto)})`
      : "Use the strike nearest to today's NIFTY open";
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
  els.pill.hidden = showLogin;
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
  els.expiry.disabled = !options.length;
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
  const vwap = desc.vwap != null ? `<div class="tip-vwap">VWAP ${fmtPrice(desc.vwap)}</div>` : '';
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
    const vwap = live?.vwaps?.[entry.def.key] ?? null;
    entry.panel.setLive(price != null ? { ts: live.minute, price, vwap } : null);
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
    state.legs = { ...msg.legs };
    buildPanels(msg.charts);
    for (const def of msg.charts) {
      const entry = state.panels.get(def.key);
      const changed = entry.def.strike !== def.strike || entry.def.ce?.token !== def.ce?.token;
      entry.def = def;
      if (changed || !entry.panel.series.length) entry.panel.setSeries(def.series);
      else {
        // Same strikes (e.g. a reconcile): keep the user's scroll position.
        const follow = entry.panel.follow;
        entry.panel.series = def.series;
        entry.panel.recompute();
        entry.panel.follow = follow;
      }
    }
    applyLive(msg.live);
    renderAll();
  },
  status(msg) {
    state.status = msg;
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

/** Post the current controls, with `overrides` taking precedence. */
async function submitConfig(overrides = {}) {
  const body = {
    atm: state.atmDirty ? Number(els.atm.value) : state.config?.atmOverride ?? null,
    days: Number(els.days.value),
    expiry: els.expiry.value || state.config?.expiryRole || 'week1',
    ...overrides,
  };
  try {
    await post('/api/config', body);
    state.atmDirty = false;
  } catch (err) {
    state.status = { ...state.status, phase: 'error', message: err.message };
    renderAll();
  }
}

els.config.addEventListener('submit', (e) => {
  e.preventDefault();
  const atm = Number(els.atm.value);
  if (!Number.isInteger(atm) || atm % STRIKE_STEP) {
    els.atm.setCustomValidity(`ATM must be a multiple of ${STRIKE_STEP}`);
    els.atm.reportValidity();
    return;
  }
  els.atm.blur();
  submitConfig({ atm }).then(renderTopbar);
});

els.atm.addEventListener('input', () => {
  els.atm.setCustomValidity('');
  state.atmDirty = true;
  renderTopbar();
});

els.atmAuto.addEventListener('click', () => {
  state.atmDirty = false;
  submitConfig({ atm: null }).then(renderTopbar);
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
els.expiry.addEventListener('change', () => submitConfig());

function renderSound() {
  const on = soundEnabled();
  els.sound.setAttribute('aria-pressed', String(on));
  els.sound.title = on ? 'Alert sounds on - click to mute' : 'Alert sounds muted - click to unmute';
}

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

connect();
