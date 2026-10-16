// One chart at a time can span the whole row of charts: the button at the right
// of its header, or Esc, toggles it. The top bar stays as it is and the chart
// keeps its height. The other charts keep their grid cells behind it at their
// usual size (style.css places every panel in a cell of its own), so they keep
// drawing and alerting; toasts and the hover tooltip sit above it.

const ICON_EXPAND = 'M14 4h6v6M10 20H4v-6M20 4l-6.5 6.5M4 20l6.5-6.5';
const ICON_RESTORE = 'M20 10h-6V4M4 14h6v6M14 10l6.5-6.5M10 14l-6.5 6.5';

let current = null; // panel root of the expanded chart
let wanted = null; // key of the chart to expand, kept while the charts are rebuilt

/**
 * Give a chart panel its expand button. `key` names the chart's place (e.g. its
 * ATM offset): a rebuilt panel with the same key comes back expanded.
 */
export function mountExpand(root, key) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'ph-expand';
  button.innerHTML =
    '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path/></svg>';
  root.querySelector('.panel-head').append(button);
  button.addEventListener('click', () => {
    if (current === root) restore();
    else expand(root, key);
  });
  sync(root, false);
  if (key === wanted) expand(root, key);
}

function expand(root, key) {
  restore();
  root.classList.add('expanded');
  current = root;
  wanted = key;
  sync(root, true);
}

function restore() {
  wanted = null;
  if (!current) return;
  current.classList.remove('expanded');
  sync(current, false);
  current = null;
}

function sync(root, expanded) {
  const button = root.querySelector('.ph-expand');
  button.querySelector('path').setAttribute('d', expanded ? ICON_RESTORE : ICON_EXPAND);
  button.title = expanded ? 'Back to all three charts (Esc)' : 'Widen this chart over the other two';
  button.setAttribute('aria-label', expanded ? 'Restore chart size' : 'Expand chart');
  button.setAttribute('aria-pressed', String(expanded));
}

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !current?.isConnected) return;
  // Esc closes an open menu or dialog first.
  if (document.querySelector('[role="dialog"]:not([hidden]), .view-panel:not([hidden])')) return;
  restore();
});
