// The algo on a page: the Algo button and its menu in the top bar, a line on each chart
// about its trade there, the trade's marks on the chart, the alarm bar, confirmations
// and toasts. Shared by the live page (algo.js: the server's algo) and the simulator
// (pnf_sim/static/algo-sim.js: the same algo over a replay). It only shows a state and
// passes on what you set or switch, through an adapter; the algo itself decides nothing
// here.

import { notifyHidden, playTone, showToast, soundWaiting } from '/static/alerts.js';
import { mountMenu } from '/static/view.js';
import {
  chartLabel,
  chartMarks,
  daySummary,
  describeTrade,
  eventStyle,
  fmtClock,
  fmtExpiry,
  fmtQty,
  fmtRupees,
  managedTrades,
  onSummary,
  openTrades,
  parseRupees,
  qtyProblem,
  supersedes,
  switchLabel,
  tradePnl,
  tradesOn,
} from './model.js';

const REPEAT_GUARD_MS = 1000; // a second click within this is a double-click: ignored
const SOUND_HINT = 'Click anywhere on the page to enable alert sounds.';

// The menu's settings; `index` ones are set for the index on screen.
const FIELDS = [
  { name: 'qty', index: true, label: 'Quantity', input: 'numeric' },
  { name: 'target', index: true, label: 'Target Points', input: 'decimal' },
  { name: 'maxOpen', label: 'Simultaneous Trades', options: [1, 2, 3] },
  { name: 'lastEntry', label: 'Last entry', input: 'time' },
  { name: 'exitTime', label: 'Exit Time', input: 'time' },
  { name: 'maxLoss', label: 'Max loss per trade', input: 'rupees' },
  { name: 'dailyLoss', label: 'Daily loss limit', input: 'rupees' },
  { name: 'maxTrades', label: 'Max trades per day', input: 'numeric' },
];

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text != null) e.textContent = text;
  return e;
}

/**
 * @param {{adapter: {
 *   confirm: boolean, trades: boolean, modeSwitch: boolean,
 *   switchOn: (expect: object) => Promise<object>, switchOff: () => Promise<object>,
 *   saveSettings: (changes: object) => Promise<object>,
 *   exitAll?: () => Promise<object>, exitTrade?: (id: string) => Promise<object>,
 *   logError?: (title: string, detail: string) => void,
 * }, onLock?: (locked: boolean) => void}} options
 *   `confirm`: ask before switching On (and Off with trades open); `trades`: Exit buttons;
 *   `modeSwitch`: the Paper / Live choice. A failed action rejects with an Error that may
 *   carry `errors` ({field: message}) for the settings.
 */
export function createAlgoUI({ adapter, onLock = () => {} }) {
  const ui = {
    s: null, // the latest state
    up: false, // connected to whatever feeds the state
    ctx: { panels: [], live: null }, // the charts on screen and their live prices
    busy: false,
    lastAction: 0,
    locked: null,
    errors: {}, // field -> message shown under it
    rows: new WeakMap(), // panel root -> its algo row
    alarmToast: null,
    tradesKey: '',
  };

  // ---------------- Top bar: the button and its menu ----------------

  const menu = el('div', 'algo-menu');
  const control = el('div', 'algo-control');
  const toggle = el('button', 'algo-toggle');
  toggle.type = 'button';
  toggle.append(el('i', 'algo-dot'), el('b', 'algo-name', 'Algo'), el('span', 'algo-state', 'Off'));
  const caret = el('button', 'algo-caret');
  caret.type = 'button';
  caret.setAttribute('aria-haspopup', 'true');
  caret.setAttribute('aria-expanded', 'false');
  caret.setAttribute('aria-label', 'Algo settings and trades');
  caret.title = 'Algo settings and today’s algo trades';
  caret.innerHTML =
    '<svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" ' +
    'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';
  control.append(toggle, caret);

  const panel = el('div', 'view-panel algo-panel');
  panel.setAttribute('role', 'group');
  panel.setAttribute('aria-label', 'Algo');
  panel.hidden = true;
  menu.append(control, panel);

  const head = el('header', 'ap-head');
  const modes = el('div', 'segmented ap-mode');
  modes.setAttribute('role', 'group');
  modes.setAttribute('aria-label', 'Mode');
  for (const [mode, text, title] of [
    ['paper', 'Paper', 'Records the trades the algo would make, at the chart prices: no orders'],
    ['live', 'Live', 'Real orders through Kite (MIS)'],
  ]) {
    const b = el('button', null, text);
    b.type = 'button';
    b.dataset.mode = mode;
    b.title = title;
    b.addEventListener('click', () => saveSetting('mode', mode));
    modes.append(b);
  }
  head.append(el('h2', null, 'Algo'), modes);
  const status = el('p', 'ap-status');

  const indexSection = el('section', 'ap-fields');
  const indexTitle = el('h3', 'ap-section');
  const allSection = el('section', 'ap-fields');
  allSection.append(el('h3', 'ap-section', 'All indexes'));
  indexSection.append(indexTitle);
  const fields = new Map(); // name -> {row, input, hint, error}
  for (const f of FIELDS) {
    const row = el('label', 'ap-field');
    row.dataset.name = f.name;
    let input;
    if (f.options) {
      input = el('select', 'ap-select');
      for (const v of f.options) input.append(new Option(String(v), String(v)));
    } else {
      input = el('input', 'ap-input');
      input.type = 'text';
      input.autocomplete = 'off';
      input.spellcheck = false;
      input.inputMode = f.input === 'decimal' ? 'decimal' : 'numeric';
      if (f.input === 'time') {
        input.maxLength = 5;
        input.placeholder = 'HH:MM';
      }
    }
    const box = el('span', `ap-box${f.input === 'rupees' ? ' rupees' : ''}`);
    if (f.input === 'rupees') box.append(el('i', null, '₹'));
    box.append(input);
    const hint = el('span', 'ap-hint');
    row.append(el('span', 'ap-label', f.label), box, hint);
    input.addEventListener('change', () => saveSetting(f.name, input.value, f));
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') input.blur();
      if (e.key === 'Escape') {
        e.stopPropagation(); // the menu stays open; only the edit is dropped
        delete ui.errors[f.name];
        input.value = valueOf(f);
        input.blur();
        renderPanel();
      }
    });
    (f.index ? indexSection : allSection).append(row);
    fields.set(f.name, { row, input, hint });
  }

  const today = el('section', 'ap-today');
  const todayTitle = el('h3', 'ap-section');
  const tradeList = el('ol', 'ap-trades');
  const empty = el('p', 'ap-empty', 'No algo trades today.');
  today.append(todayTitle, tradeList, empty);

  const foot = el('footer', 'ap-foot');
  const exitAllBtn = el('button', 'btn ap-exit-all', 'Exit all algo trades');
  exitAllBtn.type = 'button';
  exitAllBtn.title = 'Buy back every open algo trade at market now, and switch the algo Off';
  exitAllBtn.addEventListener('click', () => {
    if (claim()) act(adapter.exitAll, 'Exit all algo trades failed');
  });
  foot.append(exitAllBtn);
  panel.append(head, status, indexSection, allSection, today);
  if (adapter.trades) panel.append(foot);
  if (!adapter.modeSwitch) modes.hidden = true;

  mountMenu(caret, panel);
  toggle.addEventListener('click', onToggle);

  // ---------------- Alarm bar and dialog ----------------

  const alarmBar = el('div', 'algo-alarm');
  alarmBar.setAttribute('role', 'alert');
  alarmBar.hidden = true;

  const dialog = el('dialog', 'algo-dialog');
  dialog.setAttribute('aria-labelledby', 'algo-dialog-title');

  // ---------------- Actions ----------------

  /** A second action within the repeat guard, or while one is in flight, is ignored. */
  function claim() {
    const now = performance.now();
    if (ui.busy || !ui.up || now - ui.lastAction < REPEAT_GUARD_MS) return false;
    ui.lastAction = now;
    return true;
  }

  async function act(fn, failTitle) {
    ui.busy = true;
    render();
    try {
      const state = await fn();
      if (state?.type === 'algo') setState(state);
      return true;
    } catch (err) {
      errorToast(failTitle, err.message);
      return false;
    } finally {
      ui.busy = false;
      render();
    }
  }

  async function onToggle() {
    const s = ui.s;
    if (!s || !claim()) return;
    if (s.on) {
      const yours = managedTrades(s);
      if (adapter.confirm && yours.length && !(await confirmOff(yours))) return;
      await act(adapter.switchOff, 'The algo did not switch Off');
      return;
    }
    if (s.startProblem) {
      errorToast('The algo cannot switch On', s.startProblem);
      return;
    }
    const expect = s.summary;
    if (adapter.confirm && !(await confirmOn(s))) return;
    await act(() => adapter.switchOn(expect), 'The algo did not switch On');
  }

  async function saveSetting(name, raw, f = null) {
    const s = ui.s;
    if (!s) return;
    let value = raw;
    if (f) {
      value = parseField(f, raw);
      if (Number.isNaN(value)) {
        ui.errors[f.name] = f.input === 'time' ? 'Enter a time like 14:45' : 'Enter a number';
        renderPanel();
        return;
      }
      if (value === currentValue(f)) {
        delete ui.errors[f.name];
        renderPanel();
        return;
      }
    } else if (value === s.settings.mode) {
      return;
    }
    const u = s.summary.underlying;
    const changes = f?.index ? { [name]: { [u]: value } } : { [name]: value };
    try {
      setState(await adapter.saveSettings(changes));
      delete ui.errors[name];
    } catch (err) {
      const key = f?.index ? `${name}.${u}` : name;
      const message = err.errors?.[key] ?? err.errors?.[name] ?? err.message;
      if (f) ui.errors[name] = message;
      else errorToast('Mode not changed', message); // no field to show it under
    }
    renderPanel();
  }

  function parseField(f, raw) {
    const text = String(raw).trim();
    if (f.input === 'time') return /^\d{1,2}:\d{2}$/.test(text) ? text.padStart(5, '0') : NaN;
    if (f.input === 'rupees') return parseRupees(text);
    if (f.input === 'decimal') return text && Number.isFinite(Number(text)) ? Number(text) : NaN;
    const digits = text.replace(/,/g, '');
    return /^\d+$/.test(digits) ? Number(digits) : NaN;
  }

  function currentValue(f) {
    const st = ui.s.settings;
    return f.index ? st[f.name][ui.s.summary.underlying] : st[f.name];
  }

  function valueOf(f) {
    const v = currentValue(f);
    if (v == null) return '';
    return f.input === 'rupees' ? v.toLocaleString('en-IN') : String(v);
  }

  // ---------------- Confirmations ----------------

  function ask({ title, lead, rows = [], items = [], note = '', confirm, danger = false }) {
    dialog.replaceChildren();
    const form = el('form');
    form.method = 'dialog';
    const h = el('h2', null, title);
    h.id = 'algo-dialog-title';
    form.append(h);
    if (lead) form.append(el('p', 'ad-lead', lead));
    if (rows.length) {
      const dl = el('dl', 'ad-rows');
      for (const [k, v] of rows) dl.append(el('dt', null, k), el('dd', null, v));
      form.append(dl);
    }
    if (items.length) {
      const ul = el('ul', 'ad-items');
      for (const item of items) ul.append(el('li', null, item));
      form.append(ul);
    }
    if (note) form.append(el('p', 'ad-note', note));
    const actions = el('div', 'ad-actions');
    const cancel = el('button', 'btn', 'Cancel');
    cancel.value = 'cancel';
    const ok = el('button', `btn ${danger ? 'danger' : 'primary'}`, confirm);
    ok.value = 'ok';
    actions.append(cancel, ok);
    form.append(actions);
    dialog.append(form);
    dialog.classList.toggle('live', danger);
    return new Promise((resolve) => {
      dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
      dialog.returnValue = '';
      dialog.showModal();
      cancel.focus(); // Enter alone never switches anything: the choice takes a click
    });
  }

  function confirmOn(s) {
    const live = s.summary.mode === 'live';
    return ask({
      title: live ? 'Switch the algo On with real orders?' : 'Switch the algo On (paper)?',
      lead: live
        ? 'It will sell and buy back straddles through Kite by itself, at market, without asking again.'
        : 'It will record the trades it would make; no orders are sent.',
      rows: onSummary(s),
      note: 'Switching Off later leaves any open algo trade to you: no targets, no stops, no Exit Time.',
      confirm: live ? 'Switch On: real orders' : 'Switch On',
      danger: live,
    });
  }

  function confirmOff(trades) {
    const n = trades.length;
    return ask({
      title: 'Switch the algo Off?',
      lead: `${n === 1 ? 'This open algo trade' : `These ${n} open algo trades`} will be left to you, with no targets, no stops and no Exit Time:`,
      items: trades.map((t) => `${t.name}: ${describeTrade(t).what}`),
      note: n === 1
        ? 'Exit it with Exit on its chart, or with Exit all algo trades in the Algo menu.'
        : 'Exit them with Exit on their charts, or with Exit all algo trades in the Algo menu.',
      confirm: 'Switch Off',
    });
  }

  // ---------------- Toasts ----------------

  function errorToast(title, detail) {
    showToast({ kind: 'error', icon: '!', title, detail: detail ?? '' });
    adapter.logError?.(title, detail ?? '');
  }

  /** An algo event (a trade opened, booked, closed, failed, a signal skipped...). */
  function event(ev) {
    const style = eventStyle(ev);
    if (style.tone) playTone(style.tone);
    showToast({
      kind: style.kind,
      icon: style.icon,
      title: ev.title,
      detail: ev.detail,
      hint: style.tone && soundWaiting() ? SOUND_HINT : '',
    });
    if (style.tone) notifyHidden(ev.title, ev.detail, `algo:${ev.at}:${ev.kind}:${ev.trade ?? ''}`);
  }

  /** The alarms ring (at once, then every 10 seconds while they last): sound and one toast. */
  function ring(alarms) {
    if (!alarms?.length) return;
    playTone('alarm');
    ui.alarmToast?.remove();
    const [first, ...more] = alarms;
    ui.alarmToast = showToast({
      kind: 'error',
      icon: '!',
      title: `Algo: ${first.title}${more.length ? ` (+${more.length} more)` : ''}`,
      detail: first.detail,
      hint: soundWaiting() ? SOUND_HINT : '',
    });
    notifyHidden(`Algo: ${first.title}`, first.detail, 'algo-alarm');
  }

  // ---------------- Rendering ----------------

  const livePrice = (key) => ui.ctx.live?.legs?.[key] ?? null;

  function render() {
    renderControl();
    renderPanel();
    renderRows();
    renderAlarmBar();
    const locked = !!ui.s?.locked;
    if (locked !== ui.locked) {
      ui.locked = locked;
      onLock(locked);
    }
  }

  function renderControl() {
    const s = ui.s;
    const label = switchLabel(s);
    control.dataset.state = label.state;
    control.classList.toggle('alarm', !!s?.alarms?.length);
    control.classList.toggle('busy', ui.busy);
    toggle.querySelector('.algo-state').textContent = label.text;
    toggle.setAttribute('aria-pressed', String(!!s?.on));
    toggle.disabled = !s || !ui.up;
    toggle.title = !s || !ui.up
      ? 'Waiting for the algo’s state…'
      : s.on
        ? `The algo is On (${s.mode}): click to switch it Off`
        : s.startProblem || 'Click to switch the algo On (you confirm what it will trade first)';
  }

  function renderPanel() {
    const s = ui.s;
    if (!s) return;
    const u = s.summary.underlying;
    const lot = s.lotSizes[u];
    status.textContent = statusText(s);
    status.classList.toggle('problem', !s.on && !!s.startProblem);
    for (const b of modes.querySelectorAll('button')) {
      b.setAttribute('aria-pressed', String(b.dataset.mode === s.settings.mode));
      b.disabled = s.locked && b.dataset.mode !== s.settings.mode;
    }
    modes.title = s.locked ? 'Switch the algo Off (and let its trades close) to change the mode' : '';
    indexTitle.textContent = `${u} (for new trades)`;
    for (const f of FIELDS) {
      const { row, input, hint } = fields.get(f.name);
      if (document.activeElement !== input) input.value = valueOf(f);
      let error = ui.errors[f.name] ?? '';
      if (!error && f.name === 'qty') error = qtyProblem(s.settings.qty[u], lot, s.caps[u]);
      row.classList.toggle('invalid', !!error);
      input.setAttribute('aria-invalid', String(!!error));
      hint.textContent = error || hintFor(f, s, u, lot);
    }
    const day = daySummary(s, livePrice);
    todayTitle.replaceChildren(
      `Today · ${s.mode === 'live' ? 'Live' : 'Paper'} · ${day.count} trade${day.count === 1 ? '' : 's'}`,
    );
    if (day.count) {
      const pnl = el('span', `ap-day-pnl ${day.pnl >= 0 ? 'up' : 'down'}`, fmtRupees(day.pnl));
      todayTitle.append(pnl);
    }
    renderTrades(s);
    exitAllBtn.disabled = !ui.up || ui.busy || !openTrades(s).length;
  }

  function hintFor(f, s, u, lot) {
    const st = s.settings;
    switch (f.name) {
      case 'qty':
        return lot ? `${st.qty[u] / lot} lots × ${lot}, at most ${fmtQty(s.caps[u])}` : `at most ${fmtQty(s.caps[u])}`;
      case 'target':
        return `half booked at ${Math.round((st.target[u] / 3) * 100) / 100}`;
      case 'maxOpen':
        return 'at once, one per chart';
      case 'lastEntry':
        return 'no new trades after it';
      case 'exitTime':
        return 'everything closed; Algo Off';
      case 'maxLoss':
        return 'per trade, on the live price';
      case 'dailyLoss':
        return 'today’s algo trades; Algo Off';
      default:
        return 'entries a day';
    }
  }

  function statusText(s) {
    const m = s.summary;
    const what = `${m.underlying} ${fmtExpiry(m.expiry)} · ${m.strikes.map((k) => k.toLocaleString('en-IN')).join(' / ')}`;
    if (s.on) return `On: trading ${what} until ${s.settings.lastEntry}; everything closes at ${s.settings.exitTime}.`;
    if (s.halted) return `Stopped for today: ${s.halted}.`;
    if (s.exitTimeDone) return `Off for today: the Exit Time (${s.settings.exitTime}) has passed.`;
    if (s.startProblem) return s.startProblem;
    return `Ready to trade ${what}. Switch it On with the Algo button.`;
  }

  function renderTrades(s) {
    const trades = s.trades.filter((t) => t.day === s.day || t.active).slice().reverse();
    const key = trades.map((t) => `${t.id}:${t.state}:${t.managed}:${t.holding}:${t.exits.length}`).join();
    if (key !== ui.tradesKey) {
      ui.tradesKey = key;
      tradeList.replaceChildren(...trades.map(tradeItem));
    }
    empty.hidden = trades.length > 0;
    trades.forEach((t, i) => {
      const li = tradeList.children[i];
      const pnl = tradePnl(t, livePrice);
      const out = li.querySelector('.ap-pnl');
      out.textContent = fmtRupees(pnl);
      out.className = `ap-pnl ${pnl >= 0 ? 'up' : 'down'}`;
      const exit = li.querySelector('.ap-exit');
      if (exit) exit.disabled = !ui.up || ui.busy || t.want === 0;
    });
  }

  function tradeItem(t) {
    const li = el('li', 'ap-trade');
    li.dataset.state = t.state;
    li.classList.toggle('active', t.active);
    const top = el('div', 'ap-trade-head');
    const name = el('b', null, `${chartLabel(t.offset)} ${t.strike.toLocaleString('en-IN')}`);
    top.append(el('time', null, fmtClock(t.openedAt)), name);
    if (t.mode === 'paper' && adapter.modeSwitch) top.append(el('span', 'ap-badge', 'Paper'));
    top.append(el('span', 'ap-pnl'));
    const { what, levels } = describeTrade(t);
    li.append(top, el('div', 'ap-trade-what', what));
    if (levels) li.append(el('div', 'ap-trade-levels', levels));
    li.title = `${t.name} (${fmtExpiry(t.expiry)})${t.signal ? ` · double bottom sell at ${fmtClock(t.signal.ts + 60)}` : ''}`;
    if (adapter.trades && t.active) {
      const exit = el('button', 'btn ap-exit', 'Exit');
      exit.type = 'button';
      exit.title = 'Buy this algo trade back at market now';
      exit.addEventListener('click', () => {
        if (claim()) act(() => adapter.exitTrade(t.id), `Exit of ${t.name} failed`);
      });
      li.append(exit);
    }
    return li;
  }

  function makeRow(root) {
    const row = el('div', 'algo-row');
    row.hidden = true;
    const badge = el('span', 'ar-badge', 'Algo');
    const text = el('span', 'ar-text');
    text.append(el('b', 'ar-what'), el('span', 'ar-levels'));
    const pnl = el('span', 'ar-pnl');
    const exit = el('button', 'btn ar-exit', 'Exit');
    exit.type = 'button';
    exit.title = 'Buy this algo trade back at market now';
    exit.hidden = !adapter.trades;
    row.append(badge, text, pnl, exit);
    root.querySelector('.ph-meta').after(row);
    const parts = { row, badge, text, pnl, exit, trade: null };
    exit.addEventListener('click', () => {
      const t = parts.trade;
      if (t && claim()) act(() => adapter.exitTrade(t.id), `Exit of ${t.name} failed`);
    });
    return parts;
  }

  function renderRows() {
    const s = ui.s;
    for (const p of ui.ctx.panels) {
      let parts = ui.rows.get(p.root);
      if (!parts) {
        parts = makeRow(p.root);
        ui.rows.set(p.root, parts);
      }
      const trades = s ? tradesOn(s, p.def).filter((t) => t.day === s.day || t.active) : [];
      p.panel?.setMarks(chartMarks(trades));
      const open = trades.find((t) => t.active);
      const last = trades.at(-1);
      parts.trade = open ?? null;
      let kind = '';
      let what = '';
      let levels = '';
      if (open) {
        ({ what, levels } = describeTrade(open));
        kind = open.managed ? 'open' : 'yours';
      } else if (last) {
        // The last trade's result stays until the next one opens.
        ({ what, levels } = describeTrade(last));
        kind = last.state;
        if (s?.on && !p.def.error) levels = 'watching for the next double bottom sell';
      } else if (s?.on && !p.def.error) {
        kind = 'watching';
        what = 'Watching';
        levels = 'for a double bottom sell below VWAP and VWMA 10';
      }
      parts.row.hidden = !kind;
      parts.row.dataset.kind = kind;
      parts.row.dataset.mode = (open ?? last)?.mode ?? s?.mode ?? 'paper';
      parts.badge.textContent = parts.row.dataset.mode === 'live' ? 'Algo live' : adapter.modeSwitch ? 'Algo paper' : 'Algo';
      parts.text.querySelector('.ar-what').textContent = what;
      parts.text.querySelector('.ar-levels').textContent = levels;
      parts.row.title = levels ? `${what} · ${levels}` : what;
      const shown = open ?? (kind && kind !== 'watching' ? last : null);
      if (shown) {
        const pnl = tradePnl(shown, livePrice);
        parts.pnl.textContent = fmtRupees(pnl);
        parts.pnl.className = `ar-pnl ${pnl >= 0 ? 'up' : 'down'}`;
      } else {
        parts.pnl.textContent = '';
      }
      parts.exit.hidden = !adapter.trades || !open;
      parts.exit.disabled = !ui.up || ui.busy || !open || open.want === 0;
    }
  }

  function renderAlarmBar() {
    const alarms = ui.s?.alarms ?? [];
    alarmBar.hidden = !alarms.length;
    alarmBar.replaceChildren(
      ...alarms.map((a) => {
        const line = el('div', 'aa-line');
        line.append(el('span', 'aa-icon', '!'), el('b', null, a.title), el('span', 'aa-detail', a.detail));
        return line;
      }),
    );
  }

  function setState(state) {
    if (!supersedes(ui.s, state)) return;
    ui.s = state;
    render();
  }

  return {
    /** Put the button in `topbar` before `before`, and the alarm bar right after `banner`. */
    mount({ topbar, before, banner }) {
      topbar.insertBefore(menu, before);
      banner.after(alarmBar);
      document.body.append(dialog);
      render();
    },
    setState,
    /** The charts on screen ({root, def, panel}) and their live prices. */
    setContext(ctx) {
      ui.ctx = ctx;
      renderRows();
      if (!panel.hidden) renderPanel();
    },
    setConnected(up) {
      ui.up = up;
      render();
    },
    event,
    ring,
  };
}
