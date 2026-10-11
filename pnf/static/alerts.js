// Visual and audio alerts for P&F signals.
//
// Sounds are synthesized with Web Audio (no files). Browsers keep audio
// suspended until the user interacts with the page, so the context is
// resumed on the first pointer or key press; until then alerts are
// visual only and the toast says so.

const SOUND_KEY = 'pnf.sound';
const TOAST_LIFETIME_MS = 60_000;
const MAX_TOASTS = 5;

let audio = null;

function ensureAudio() {
  audio ??= new AudioContext();
  if (audio.state === 'suspended') audio.resume().catch(() => {});
  return audio;
}

for (const event of ['pointerdown', 'keydown']) {
  window.addEventListener(event, ensureAudio, { capture: true, passive: true });
}

export function soundEnabled() {
  try {
    return localStorage.getItem(SOUND_KEY) !== 'off';
  } catch {
    return true;
  }
}

/** Ask once for system notifications; call from a user gesture (e.g. going live). */
export function requestNotifications() {
  if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();
}

export function setSoundEnabled(on) {
  try {
    localStorage.setItem(SOUND_KEY, on ? 'on' : 'off');
  } catch {
    // storage unavailable (private mode): the choice lasts for this page only
  }
  if (on) {
    ensureAudio();
    requestNotifications();
  }
}

const audioReady = () => audio?.state === 'running';

/** Buy: a rising three-note chime. Sell: the same falling. Played twice. */
export function playSignal(type) {
  const ctx = ensureAudio();
  const notes = type === 'buy' ? [523.25, 659.25, 783.99] : [783.99, 659.25, 523.25];
  const start = ctx.currentTime + 0.02;
  for (let rep = 0; rep < 2; rep++) {
    notes.forEach((freq, i) => {
      const t = start + rep * 0.75 + i * 0.13;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'triangle';
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.35, t + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.32);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t);
      osc.stop(t + 0.34);
    });
  }
}

/**
 * Show one alert: toast, optional sound, and a system notification when the
 * tab is in the background.
 * @param {{type: 'buy'|'sell', title: string, detail: string}} alert
 */
export function raiseAlert({ type, title, detail }) {
  const withSound = soundEnabled();
  if (withSound) playSignal(type);

  const box = document.getElementById('toasts');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.setAttribute('role', 'alert');
  const muted = withSound && !audioReady();
  toast.innerHTML = `
    <div class="toast-icon" aria-hidden="true">${type === 'buy' ? '▲' : '▼'}</div>
    <div class="toast-body">
      <b></b>
      <span class="toast-detail"></span>
      ${muted ? '<span class="toast-hint">Click anywhere on the page to enable alert sounds.</span>' : ''}
    </div>
    <button type="button" class="toast-close" aria-label="Dismiss">×</button>`;
  toast.querySelector('b').textContent = title;
  toast.querySelector('.toast-detail').textContent = detail;
  const dismiss = () => toast.remove();
  toast.querySelector('.toast-close').addEventListener('click', dismiss);
  setTimeout(dismiss, TOAST_LIFETIME_MS);
  box.prepend(toast);
  while (box.children.length > MAX_TOASTS) box.lastElementChild.remove();

  if (document.hidden && 'Notification' in window && Notification.permission === 'granted') {
    new Notification(title, { body: detail, tag: `${type}:${detail}` });
  }
}
