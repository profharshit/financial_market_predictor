/**
 * chartLayout.js — the ONE place that decides pixel geometry for the price
 * and volume charts, so the two SVGs (drawn by separate modules) always line
 * up on the same x-axis. If you change margins or the forecast run-out,
 * change it here and both charts stay in sync.
 */

export const FUTURE_BARS = 30; // reserved space on the right for the forecast fan

/**
 * @param barsShown  number of historical bars currently visible
 * @param width      full SVG width in px
 * @param opts.includeFuture  reserve FUTURE_BARS of empty space on the right
 *                            (the price chart needs this for the forecast fan;
 *                            the volume chart passes the same flag so widths match)
 */
export function buildScale(barsShown, width, { leftPad = 10, rightPad = 62, includeFuture = true } = {}) {
  const m = { l: leftPad, r: rightPad };
  const fut = includeFuture ? FUTURE_BARS : 0;
  const total = barsShown + fut;
  const iw = width - m.l - m.r;
  const step = total > 0 ? iw / total : 0;
  const x = i => m.l + (i + 0.5) * step;
  return { m, step, total, iw, x, lastIndex: barsShown - 1 };
}

export const svgEl = (name, attrs, text) => {
  const e = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (text != null) e.textContent = text;
  return e;
};

export function palette() {
  const cs = getComputedStyle(document.documentElement);
  const g = n => cs.getPropertyValue(n).trim();
  return {
    up: g("--up"), down: g("--down"), accent: g("--accent"), ink: g("--ink"),
    muted: g("--muted"), line: g("--line"), card: g("--card"),
    lor: g("--lor"), support: g("--support"), resistance: g("--resistance"),
  };
}
