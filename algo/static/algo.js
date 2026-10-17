// The algo on the live page. The algo runs in the server (algo/runner.py); this page
// shows its state, pushed on /algo/ws, through ui.js, and sends back what you switch
// or set. pnf/static/app.js only calls mountAlgo and updateAlgo.

import { createAlgoUI } from './ui.js';

/**
 * POST as JSON. A refusal carries the server's message, and for the settings
 * `errors` ({field: message}).
 */
async function post(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    const errors = detail?.errors ?? null;
    const message = errors
      ? Object.values(errors).join('; ')
      : Array.isArray(detail) ? detail.map((d) => d.msg).join('; ') : detail || `${res.status} ${res.statusText}`;
    const err = new Error(message);
    err.errors = errors;
    throw err;
  }
  return data;
}

const adapter = {
  confirm: true,
  trades: true,
  modeSwitch: true,
  switchOn: (expect) => post('/api/algo/on', { expect }),
  switchOff: () => post('/api/algo/off', {}),
  saveSettings: (changes) => post('/api/algo/settings', { changes }),
  exitAll: () => post('/api/algo/exit-all', {}),
  exitTrade: (trade) => post('/api/algo/exit', { trade }),
  // An error a page showed goes into the alert log, like the Kite trading errors.
  logError: (title, detail) =>
    post('/api/alerts', {
      type: 'error',
      key: `error:${Date.now()}:${Math.random().toString(36).slice(2, 10)}`,
      title: title.slice(0, 200),
      detail: String(detail).slice(0, 600),
    }).catch(() => {}),
};

let ui = null;
let retryDelay = 500;

function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/algo/ws`);
  let up = false;
  ws.onopen = () => {
    up = true;
    retryDelay = 500;
    ui.setConnected(true);
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'algo') ui.setState(msg);
    else if (msg.type === 'algo-event') ui.event(msg);
    else if (msg.type === 'algo-ring') ui.ring(msg.alarms);
  };
  ws.onclose = () => {
    if (up) ui.setConnected(false);
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 5000);
  };
}

/**
 * Put the Algo button into `topbar` before `before` and its alarm bar after `banner`.
 * `onLock(locked)` is told when the index, expiry, ATM and days must stay as they are.
 */
export function mountAlgo({ topbar, before, banner, onLock }) {
  ui = createAlgoUI({ adapter, onLock });
  ui.mount({ topbar, before, banner });
  connect();
}

/** The charts on screen ({root, def, panel}) and their live prices (the engine's ticks). */
export function updateAlgo(ctx) {
  ui?.setContext(ctx);
}
