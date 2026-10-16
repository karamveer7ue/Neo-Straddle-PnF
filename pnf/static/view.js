// The View menu, shared by the live page and the simulator: the overlay
// checkboxes (VWAP, VWMA 10), box size and, on the live page, the day count sit
// behind one top-bar button to keep the bar short.

const OVERLAYS_KEY = 'pnf.overlays'; // per origin: the live page and the simulator each keep their own
const DEFAULT_OVERLAYS = { vwap: true, vwma: false };

/** The browser's VWAP / VWMA 10 choice, or the defaults (VWAP on, VWMA off). */
export function loadOverlays() {
  try {
    const saved = JSON.parse(localStorage.getItem(OVERLAYS_KEY));
    // A choice saved before VWMA 10 replaced SMA 10 carries over from the SMA one.
    const vwma = saved?.vwma ?? saved?.sma;
    if (saved && typeof saved.vwap === 'boolean' && typeof vwma === 'boolean') return { vwap: saved.vwap, vwma };
  } catch {
    // unreadable or blocked storage: defaults
  }
  return { ...DEFAULT_OVERLAYS };
}

export function saveOverlays(overlays) {
  try {
    localStorage.setItem(OVERLAYS_KEY, JSON.stringify(overlays));
  } catch {
    // storage unavailable: the choice lasts for this page only
  }
}

/**
 * Open and close `menu` from `button`: a click on the button toggles it;
 * Escape or a press anywhere else closes it. Changing a setting inside keeps
 * it open, so several can be changed in one go.
 */
export function mountMenu(button, menu) {
  const setOpen = (open) => {
    menu.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (!open) return;
    // Hang from the button's left edge, or from its right edge where that would overflow the window.
    menu.classList.remove('align-end');
    menu.classList.toggle('align-end', menu.getBoundingClientRect().right > document.documentElement.clientWidth - 8);
  };
  button.addEventListener('click', () => setOpen(menu.hidden));
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || menu.hidden) return;
    setOpen(false);
    button.focus();
  });
  // pointerdown, not click: other top-bar buttons stop their clicks from propagating.
  document.addEventListener('pointerdown', (e) => {
    if (!menu.hidden && !menu.contains(e.target) && !button.contains(e.target)) setOpen(false);
  });
}
