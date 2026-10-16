// Kite trading on the P&F page: a quantity field and Sell button under each
// chart, Exit 50% / Exit Full on the charts holding a position, a strip of
// open positions with live P&L under the top bar, and the Kite connection
// in the top bar.
//
// The page calls update() whenever its charts or live prices change; the
// position data comes from the server over its own WebSocket (/kite/ws).
// P&L is re-priced with the chart's live feed for straddles on screen, and
// shown at Kite's last price for any other.

import { showToast } from '/static/alerts.js';
import { fmtPrice } from '/static/chart.js';
import { checkQuantity, fmtQty, fmtRupees, groupKey, summarize } from './positions.js';

// Orders go out on the first click. A second click on the same chart within this time is
// taken as a double-click and ignored, so it never doubles a sell or turns Exit 50% into 75%.
const REPEAT_GUARD_MS = 1000;
const QTY_KEY = 'kite.qty'; // last quantity sold, per underlying

const k = {
  wsUp: false,
  server: null, // the latest {type: 'kite'} message
  busy: new Map(), // group key -> 'sell' | 'half' | 'full', while a request is in flight
  lastAction: new Map(), // group key -> performance.now() of its last Sell or Exit
  ctx: { underlying: '', expiry: null, panels: [], live: null },
  bars: new WeakMap(), // panel root -> its trade bar elements
};

const offsetLabel = (off) => (off === 0 ? 'ATM' : `ATM ${off > 0 ? '+' : '−'} ${Math.abs(off)}`);
const fmtStrike = (s) => s.toLocaleString('en-IN');
const fmtExpiry = (iso) =>
  new Date(`${iso}T00:00:00+05:30`).toLocaleDateString('en-GB', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short' });
const lotSizeFor = (underlying, expiry) => k.server?.lotSizes?.[`${underlying}|${expiry}`] ?? null;

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text != null) e.textContent = text;
  return e;
}

// ---------------- Stored quantity ----------------

function storedQty(underlying) {
  try {
    const v = Number(JSON.parse(localStorage.getItem(QTY_KEY))?.[underlying]);
    return Number.isInteger(v) && v > 0 ? v : null;
  } catch {
    return null;
  }
}

function storeQty(underlying, qty) {
  try {
    const all = JSON.parse(localStorage.getItem(QTY_KEY)) ?? {};
    localStorage.setItem(QTY_KEY, JSON.stringify({ ...all, [underlying]: qty }));
  } catch {
    // storage unavailable: the field keeps the value for this page only
  }
}

// ---------------- Server ----------------

async function post(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join('; ') : data.detail;
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return data;
}

let retryDelay = 500;
function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/kite/ws`);
  ws.onopen = () => {
    k.wsUp = true;
    retryDelay = 500;
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'kite') {
      k.server = msg;
      render();
    } else if (msg.type === 'order') {
      orderToast(msg);
    }
  };
  ws.onclose = () => {
    if (k.wsUp) {
      k.wsUp = false;
      render();
    }
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 5000);
  };
}

/** Kite is usable for orders right now. */
const ready = () => k.wsUp && !!k.server?.connected;

// ---------------- Toasts ----------------

function describeGroup(key) {
  const [underlying, expiry, strike] = key.split('|');
  return `${underlying} ${fmtStrike(Number(strike))} (${fmtExpiry(expiry)})`;
}

const BATCH_MS = 1500; // the legs of one action fill within a moment: one toast
const batches = new Map(); // "<group>|<side>" -> {events, timer}

function orderToast(o) {
  if (o.status !== 'COMPLETE') {
    const filled = o.filled ? ` (${fmtQty(o.filled)} of ${fmtQty(o.quantity)} filled @ ${fmtPrice(o.averagePrice)})` : '';
    errorToast(
      `${o.kind} ${o.side === 'SELL' ? 'sell' : 'buy'} ${o.status.toLowerCase()}${filled}`,
      o.message || `${o.tradingsymbol}: order ${o.orderId}`,
      // Every open page gets this outcome: one key, so the log keeps it once.
      { group: o.group, key: `order:${o.orderId}:${o.status}` },
    );
    return;
  }
  const key = `${o.group}|${o.side}`;
  const batch = batches.get(key) ?? { events: [] };
  batch.events.push(o);
  clearTimeout(batch.timer);
  batch.timer = setTimeout(() => {
    batches.delete(key);
    const kinds = new Set(batch.events.map((e) => e.kind));
    const what = kinds.size === 2 ? 'straddle' : [...kinds][0];
    showToast({
      kind: 'ok',
      icon: '\u2713',
      title: `${o.side === 'SELL' ? 'Sold' : 'Bought back'} ${describeGroup(o.group)} ${what}`,
      detail: batch.events.map((e) => `${e.kind} ${fmtQty(e.filled)} @ ${fmtPrice(e.averagePrice)}`).join(' \u00b7 '),
    });
  }, BATCH_MS);
  batches.set(key, batch);
}

/**
 * Show an error toast and save it in the alert log (with the signal alerts).
 * @param {{group?: string, key?: string}} [about] the straddle it concerns; a key shared by
 *   every page that shows the same error (others are unique: only the page that hit it logs it)
 */
function errorToast(title, detail, { group, key } = {}) {
  const full = group ? `${describeGroup(group)}: ${title}` : title;
  showToast({ kind: 'error', icon: '!', title: full, detail });
  const id = key ?? `error:${Date.now()}:${Math.random().toString(36).slice(2, 10)}`;
  post('/api/alerts', { type: 'error', key: id.slice(0, 200), title: full.slice(0, 200), detail: String(detail ?? '').slice(0, 600) })
    .catch(() => {}); // logging must never get in the way of the toast
}

/** Report the legs that were not placed (or held back), and any warnings; placed legs report when they fill. */
function reportPlacement(action, res) {
  const about = { group: res.group };
  for (const r of res.results.filter((x) => !x.ok)) {
    if (r.held) errorToast(`${r.kind} not sent`, r.error, about);
    else errorToast(`${r.kind} ${action} ${r.unknown ? 'outcome unknown' : 'not placed'}`, r.error, about);
  }
  for (const w of res.warnings ?? []) errorToast('Order still working', w, about);
}

// ---------------- Actions ----------------

/** Claim a chart for an order request; false while one is in flight or just went out. */
function claim(key, action) {
  const last = k.lastAction.get(key);
  if (k.busy.has(key) || !ready() || (last != null && performance.now() - last < REPEAT_GUARD_MS)) return false;
  k.busy.set(key, action);
  k.lastAction.set(key, performance.now());
  return true;
}

async function sell(target, input) {
  const qty = Number(input.value);
  const check = checkQuantity(qty, target.lotSize, k.server?.maxLots);
  if (!check.ok) {
    input.setCustomValidity(check.error);
    input.reportValidity();
    return;
  }
  if (!claim(target.key, 'sell')) return;
  render();
  try {
    const res = await post('/api/kite/sell', { underlying: target.underlying, expiry: target.expiry, strike: target.strike, quantity: qty });
    storeQty(target.underlying, qty);
    reportPlacement('sell', res);
  } catch (err) {
    errorToast('Sell not placed', err.message, { group: target.key });
  } finally {
    k.busy.delete(target.key);
    render();
  }
}

async function exit(key, fraction) {
  if (!claim(key, fraction < 1 ? 'half' : 'full')) return;
  render();
  try {
    reportPlacement('buy', await post('/api/kite/exit', { group: key, fraction }));
  } catch (err) {
    errorToast(fraction < 1 ? 'Exit 50% not placed' : 'Exit not placed', err.message, { group: key });
  } finally {
    k.busy.delete(key);
    render();
  }
}

// ---------------- Top bar ----------------

const account = el('div', 'kite-account');
const connectBtn = el('a', 'btn kite-connect', 'Connect Kite');
connectBtn.href = '/kite/login';
const userPill = el('button', 'pill kite-user');
userPill.type = 'button';
userPill.append(el('i'), el('span'));
const offChip = el('span', 'pill kite-off', 'TradeOff');
account.append(connectBtn, userPill, offChip);

userPill.addEventListener('click', async () => {
  if (!window.confirm('Disconnect Kite? Orders stop until you connect again.')) return;
  try {
    await post('/api/kite/logout', {});
  } catch (err) {
    errorToast('Could not disconnect Kite', err.message);
  }
});

function renderAccount() {
  const s = k.server;
  const configured = !!s?.configured;
  offChip.hidden = !s || configured;
  offChip.title = 'Kite trading is not set up: add KITE_API_KEY and KITE_API_SECRET to .env and restart (see kite/README.md).';
  connectBtn.hidden = !configured || s.connected;
  connectBtn.title = 'Log in to Zerodha Kite to place orders from the charts';
  userPill.hidden = !configured || !s.connected;
  userPill.classList.toggle('live', k.wsUp);
  userPill.classList.toggle('pending', !k.wsUp);
  userPill.querySelector('span').textContent = 'TradeOn';
  userPill.title = k.wsUp
    ? `Connected to Kite · ${s?.product} orders · click to disconnect`
    : 'Reconnecting to the local server...';
}

// ---------------- Positions strip ----------------

const strip = el('section', 'kite-strip');
strip.setAttribute('aria-label', 'Kite positions');
const stripGroups = el('div', 'kite-groups');
const stripTotal = el('div', 'kite-total');
const totalValue = el('span', 'value');
const asOf = el('span', 'kite-asof');
stripTotal.append(el('span', 'label', 'Total P&L'), totalValue, asOf);
const stripMsg = el('div', 'kite-msg');
strip.append(stripMsg, stripGroups, stripTotal);

/** The chart showing a group's straddle, if any. */
function panelFor(group) {
  const { underlying, expiry, panels } = k.ctx;
  if (group.underlying !== underlying || group.expiry !== expiry) return null;
  return panels.find((p) => !p.def.error && p.def.strike === group.strike) ?? null;
}

/** Fresher prices for a group's legs: the chart's live feed, when it is on screen and live. */
function pricer(panel) {
  const live = k.ctx.live?.legs;
  if (!panel || !live) return () => null;
  return (leg) => live[(leg.kind === 'CE' ? panel.def.ce : panel.def.pe)?.key] ?? null;
}

function groupCard(group) {
  const card = el('div', 'kite-group');
  card.dataset.key = group.key;
  const title = el('div', 'kg-title');
  title.append(el('b'), el('span', 'kg-expiry'), el('span', 'kg-chart'));
  const actions = el('div', 'kg-actions');
  const half = el('button', 'btn kite-half', 'Exit 50%');
  const full = el('button', 'btn kite-full', 'Exit Full');
  half.type = full.type = 'button';
  half.addEventListener('click', () => exit(group.key, 0.5));
  full.addEventListener('click', () => exit(group.key, 1));
  actions.append(half, full);
  card.append(title, el('div', 'kg-detail'), el('div', 'kg-pnl'), actions);
  return card;
}

function renderStrip() {
  const s = k.server;
  const groups = s?.connected ? s.groups : [];
  const message = s?.message ?? '';
  strip.hidden = !groups.length && !message;
  stripMsg.hidden = !message;
  stripMsg.textContent = message;

  const keys = groups.map((g) => g.key).join();
  if (stripGroups.dataset.keys !== keys) {
    stripGroups.replaceChildren(...groups.map(groupCard));
    stripGroups.dataset.keys = keys;
  }
  let total = 0;
  groups.forEach((group, i) => {
    const card = stripGroups.children[i];
    const panel = panelFor(group);
    const sum = summarize(group, pricer(panel));
    total += sum.pnl;
    card.classList.toggle('closed', !sum.open);
    card.querySelector('b').textContent = `${group.underlying} ${fmtStrike(group.strike)}`;
    card.querySelector('.kg-expiry').textContent = fmtExpiry(group.expiry);
    const chip = card.querySelector('.kg-chart');
    chip.hidden = !panel;
    chip.textContent = panel ? offsetLabel(panel.def.offset) : '';
    chip.title = 'On the chart below; its exit buttons are under that chart';
    card.querySelector('.kg-detail').textContent = groupDetail(group, sum);
    card.querySelector('.kg-detail').title = legsTitle(sum);
    const pnl = card.querySelector('.kg-pnl');
    pnl.textContent = fmtRupees(sum.pnl);
    pnl.className = `kg-pnl ${sum.pnl >= 0 ? 'up' : 'down'}`;
    pnl.title = panel && k.ctx.live ? 'At the live price on the chart' : 'At Kite’s last price';
    // Exits live under the chart; a straddle not on screen gets them here.
    const actions = card.querySelector('.kg-actions');
    actions.hidden = !!panel || !sum.open;
    renderExitButtons(actions, group, sum);
  });
  totalValue.textContent = fmtRupees(total);
  totalValue.className = `value ${total >= 0 ? 'up' : 'down'}`;
  stripTotal.hidden = !groups.length;
  asOf.textContent = s?.updatedAt ? `as of ${new Date(s.updatedAt * 1000).toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata' })}` : '';
  asOf.title = 'When Kite positions were last read (every 2 s in market hours)';
}

function groupDetail(group, sum) {
  const pending = group.pending.length ? ` · ${group.pending.length} order${group.pending.length > 1 ? 's' : ''} pending` : '';
  if (!sum.open) return `Closed${pending}`;
  if (sum.short != null) return `Short ${fmtQty(sum.short)} · ${fmtPrice(sum.entry)} → ${fmtPrice(sum.now)}${pending}`;
  // Unequal legs (e.g. one leg exited elsewhere): list them.
  const legs = sum.legs.filter((l) => l.quantity).map((l) => `${l.kind} ${l.quantity > 0 ? '+' : '−'}${fmtQty(l.quantity)}`);
  return `${legs.join(' · ')}${pending}`;
}

function legsTitle(sum) {
  return sum.legs
    .map((l) => `${l.tradingsymbol} ${l.product}: ${l.quantity > 0 ? '+' : ''}${l.quantity} @ ${fmtPrice(l.averagePrice)}, now ${fmtPrice(l.price)}, ${fmtRupees(l.pnl)}`)
    .join('\n');
}

function renderExitButtons(box, group, sum) {
  const busy = k.busy.get(group.key);
  const half = box.querySelector('.kite-half');
  const full = box.querySelector('.kite-full');
  half.disabled = !ready() || !!busy || !sum.canHalf;
  full.disabled = !ready() || !!busy || !sum.open;
  half.textContent = busy === 'half' ? 'Exiting...' : 'Exit 50%';
  full.textContent = busy === 'full' ? 'Exiting...' : 'Exit Full';
  half.title = sum.canHalf
    ? 'Buy back half of each leg (whole lots, rounded down) at market'
    : 'Only 1 lot is open; it cannot be halved';
  full.title = 'Buy back the whole position at market';
}

// ---------------- Trade bar under each chart ----------------

function makeBar(root) {
  const bar = el('footer', 'kite-bar');
  const order = el('div', 'kite-order');
  const field = el('label', 'field kite-qty');
  const input = el('input');
  input.type = 'number';
  input.inputMode = 'numeric';
  input.setAttribute('aria-label', 'Quantity to sell (units, a multiple of the lot size)');
  input.title = 'Quantity to sell, in units (a multiple of the lot size); arrow keys step one lot';
  field.append(el('span', null, 'Qty'), input);
  const sellBtn = el('button', 'btn kite-sell', 'Sell');
  sellBtn.type = 'button';
  const lots = el('span', 'kite-lots');
  order.append(field, sellBtn, lots);

  const position = el('div', 'kite-position kg-actions');
  const summary = el('div', 'kite-pos');
  summary.append(el('b', 'kite-pos-pnl'), el('span', 'kite-pos-qty'));
  const half = el('button', 'btn kite-half', 'Exit 50%');
  const full = el('button', 'btn kite-full', 'Exit Full');
  half.type = full.type = 'button';
  position.append(summary, half, full);
  bar.append(order, position);
  root.append(bar);

  const parts = { bar, input, sellBtn, lots, position, target: null, group: null };
  input.addEventListener('input', () => {
    input.setCustomValidity('');
    render();
  });
  input.addEventListener('keydown', (e) => {
    // A held-down Enter repeats: only a fresh press sells.
    if (e.key === 'Enter' && !e.repeat && parts.target) sell(parts.target, input);
  });
  sellBtn.addEventListener('click', () => parts.target && sell(parts.target, input));
  half.addEventListener('click', () => parts.group && exit(parts.group.key, 0.5));
  full.addEventListener('click', () => parts.group && exit(parts.group.key, 1));
  return parts;
}

function renderBar(panel) {
  let parts = k.bars.get(panel.root);
  if (!parts) {
    parts = makeBar(panel.root);
    k.bars.set(panel.root, parts);
  }
  const { underlying, expiry } = k.ctx;
  const { def } = panel;
  const s = k.server;
  const lotSize = expiry ? lotSizeFor(underlying, expiry) : null;
  const key = expiry ? groupKey(underlying, expiry, def.strike) : null;
  parts.target = key && !def.error ? { key, underlying, expiry, strike: def.strike, lotSize } : null;

  // Quantity: the last one sold on this index, else one lot. It is filled in only when the
  // field is empty (page load, index switch), so it never changes under the user.
  const { input } = parts;
  if (parts.underlying !== underlying) {
    parts.underlying = underlying;
    input.value = '';
  }
  if (lotSize) {
    input.step = String(lotSize);
    input.min = String(lotSize);
  }
  if (!input.value && document.activeElement !== input) {
    const want = storedQty(underlying) ?? lotSize;
    if (want) input.value = String(want);
  }
  const check = checkQuantity(Number(input.value), lotSize, s?.maxLots);
  input.setAttribute('aria-invalid', String(!!input.value && !check.ok && !!lotSize));

  const busy = key ? k.busy.get(key) : null;
  const { sellBtn, lots } = parts;
  sellBtn.disabled = !ready() || !parts.target || !lotSize || !!busy;
  sellBtn.textContent = busy === 'sell' ? 'Selling...' : 'Sell';
  sellBtn.title = !s?.configured
    ? 'Kite trading is not set up (see kite/README.md)'
    : !s.connected
      ? 'Connect Kite (top bar) to trade'
      : !parts.target
        ? 'No straddle on this chart'
        : `Sell ${def.ce?.symbol ?? 'CE'} and ${def.pe?.symbol ?? 'PE'} at market (${s.product}), at once`;

  if (!s?.configured) lots.textContent = 'Kite not set up';
  else if (!s.connected) lots.textContent = 'Kite not connected';
  else if (!lotSize) lots.textContent = '';
  else lots.textContent = check.ok ? `${check.lots} lot${check.lots > 1 ? 's' : ''} × ${lotSize}` : check.error;
  lots.classList.toggle('invalid', !!s?.connected && !!lotSize && !check.ok && !!input.value);
  lots.title = lots.textContent;

  // Position and exits: only on a chart whose straddle has a position from this app.
  const group = key && s?.connected ? s.groups.find((g) => g.key === key) : null;
  const sum = group ? summarize(group, pricer(panel)) : null;
  parts.group = group;
  parts.position.hidden = !sum?.open;
  panel.root.classList.toggle('kite-holding', !!sum?.open);
  if (sum?.open) {
    const pnl = parts.position.querySelector('.kite-pos-pnl');
    pnl.textContent = fmtRupees(sum.pnl);
    pnl.className = `kite-pos-pnl ${sum.pnl >= 0 ? 'up' : 'down'}`;
    const qty = parts.position.querySelector('.kite-pos-qty');
    qty.textContent = sum.short != null ? `Short ${fmtQty(sum.short)}` : groupDetail(group, sum);
    parts.position.querySelector('.kite-pos').title = legsTitle(sum);
    renderExitButtons(parts.position, group, sum);
  }
}

// ---------------- Entry points ----------------

function render() {
  renderAccount();
  renderStrip();
  for (const panel of k.ctx.panels) renderBar(panel);
}

/**
 * Mount the trading UI.
 * @param {{topbar: Element, before: Element, banner: Element}} where
 *   the account control goes into `topbar` before `before`; the positions strip after `banner`.
 */
export function mountTrading({ topbar, before, banner }) {
  topbar.insertBefore(account, before);
  banner.after(strip);
  render();
  connect();
}

/**
 * Tell the trading UI what the charts show now.
 * @param {{underlying: string, expiry: string|null, panels: {root: Element, def: object}[], live: object|null}} ctx
 */
export function updateTrading(ctx) {
  k.ctx = ctx;
  render();
}
