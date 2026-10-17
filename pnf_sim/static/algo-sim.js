// The algo in the simulator: the live page's rules over the replay. The server works
// out every algo trade of a run at once (algo/sim.py, POST /api/algo); this module
// shows them as the replay reaches them - scrubbing back included - through the live
// page's Algo menu, chart lines and marks (/algo/static/ui.js). Paper only: nothing
// here places orders. The settings are the simulator's own, kept by this browser.

import { createAlgoUI } from '/algo/static/ui.js';

const SETTINGS_KEY = 'pnf.sim.algo';

function readSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY));
    return saved && typeof saved === 'object' ? saved : null;
  } catch {
    return null;
  }
}

function storeSettings(settings) {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    // storage unavailable: the settings last for this page only
  }
}

async function postAlgo(body) {
  const res = await fetch('/api/algo', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const errors = data.detail?.errors ?? null;
    const err = new Error(errors ? Object.values(errors).join('; ') : data.detail || `${res.status} ${res.statusText}`);
    err.errors = errors;
    throw err;
  }
  return data;
}

/** The last of `frames` (sorted by `at`) at or before `t`. */
function latest(frames, t, at = (f) => f.at) {
  let found = null;
  for (const f of frames) {
    if (at(f) > t) break;
    found = f;
  }
  return found;
}

/**
 * @param {{now: () => number}} replay the replay's current moment (epoch seconds)
 */
export function createSimAlgo({ now }) {
  const sim = {
    options: null, // /api/options' algo part: settings, caps, defaults
    settings: readSettings(),
    run: null,
    on: false,
    startAt: null,
    result: null, // the run's algo trades with these settings
    computing: false,
    error: '',
    shownUpTo: 0, // events up to this moment have had their toast
    ctx: { panels: [], live: null },
  };

  const adapter = {
    confirm: false,
    trades: false,
    modeSwitch: false,
    async switchOn() {
      if (!sim.run) throw new Error('Load a run first.');
      sim.on = true;
      sim.startAt = now();
      await compute();
      return state();
    },
    async switchOff() {
      sim.on = false;
      sim.result = null;
      return state();
    },
    async saveSettings(changes) {
      const merged = { ...settings(), ...changes };
      for (const key of ['qty', 'target']) if (changes[key]) merged[key] = { ...settings()[key], ...changes[key] };
      const checked = await postAlgo({ run: null, settings: merged });
      sim.settings = checked.settings;
      storeSettings(sim.settings);
      if (sim.on) await compute();
      return state();
    },
  };
  const ui = createAlgoUI({ adapter });

  const settings = () => sim.settings ?? sim.options?.settings;

  async function compute() {
    sim.computing = true;
    sim.error = '';
    try {
      sim.result = await postAlgo({ run: sim.run.key, settings: settings(), startAt: sim.startAt });
      sim.shownUpTo = now(); // trades already behind the replay come without toasts
    } catch (err) {
      sim.result = null;
      sim.on = false;
      sim.error = err.message;
      throw err;
    } finally {
      sim.computing = false;
    }
  }

  /** The algo's state as of the replay's current moment, shaped as the live server's. */
  function state() {
    const t = now();
    const st = settings();
    const run = sim.run;
    const u = run?.underlying.key ?? 'NIFTY';
    const status = sim.result ? latest(sim.result.status, t) : null;
    const trades = [];
    for (const tr of sim.result?.trades ?? []) {
      const frame = latest(tr.frames, t);
      if (!frame) continue;
      const stop = latest(tr.stops, t, (s) => s[0]);
      trades.push({ ...frame.view, stop: stop?.[1] != null ? { level: stop[1], at: stop[0] } : null, pnl: frame.view.booked });
    }
    return {
      type: 'algo',
      on: sim.on,
      mode: 'paper',
      settings: st,
      halted: status?.halted ?? '',
      exitTimeDone: status?.exitTimeDone ?? false,
      startProblem: !run ? 'Load a run first, then switch the algo On to replay it with the run.' : sim.error,
      locked: false,
      summary: {
        underlying: u,
        expiry: run?.expiry ?? null,
        strikes: run?.charts.map((c) => c.strike) ?? [],
        mode: 'paper',
        qty: st?.qty?.[u],
        lotSize: run?.lotSize ?? null,
        target: st?.target?.[u],
      },
      lotSizes: run ? { [u]: run.lotSize } : {},
      caps: sim.options?.caps ?? {},
      defaults: sim.options?.defaults ?? {},
      day: status?.day ?? null,
      entries: status?.entries ?? 0,
      trades,
      alarms: [],
    };
  }

  function render() {
    if (!settings()) return;
    ui.setState(state());
  }

  return {
    mount({ topbar, before, banner }) {
      ui.mount({ topbar, before, banner });
    },
    /** The options' algo part arrived: defaults for the menu. */
    setOptions(algo) {
      sim.options = algo;
      ui.setConnected(true);
      render();
    },
    /** A new run: with the algo On, it replays from the run's start. */
    async setRun(run) {
      sim.run = run;
      sim.result = null;
      sim.startAt = null;
      if (sim.on) {
        try {
          await compute();
        } catch {
          // shown in the menu's status line
        }
      }
      render();
    },
    /**
     * The replay moved on (`playing`) or jumped. Toasts come for the algo's events the
     * replay played through, never for a jump.
     */
    update(ctx, playing) {
      sim.ctx = ctx;
      ui.setContext(ctx);
      const t = now();
      if (sim.result && playing) {
        for (const ev of sim.result.events) {
          if (ev.at > sim.shownUpTo && ev.at <= t) ui.event(ev);
        }
      }
      sim.shownUpTo = Math.max(sim.shownUpTo, t);
      if (!playing) sim.shownUpTo = t;
      render();
    },
    /** The total of the run so far (for the banner when it ends). */
    total() {
      return sim.result?.total ?? null;
    },
    get on() {
      return sim.on;
    },
  };
}
