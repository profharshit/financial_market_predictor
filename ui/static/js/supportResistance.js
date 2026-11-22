/**
 * supportResistance.js — a small, transparent Support & Resistance detector.
 *
 * Method (classic "swing pivot" / fractal levels, the same idea most retail
 * charting tools use): a bar's high is a resistance pivot if it is the
 * highest high within `leftBars`/`rightBars` neighbours either side; a bar's
 * low is a support pivot the mirrored way. Pivots that land within
 * `tolerancePct` of each other are merged into one level, and the number of
 * merged touches becomes that level's "strength".
 *
 * This is intentionally simple and inspectable rather than a black box —
 * good for a presentation where you may be asked "how was that line drawn?".
 */

/** @param bars [{t,o,h,l,c,v}] ascending by time */
export function findPivots(bars, leftBars = 4, rightBars = 4) {
  const highs = [], lows = [];
  for (let i = leftBars; i < bars.length - rightBars; i++) {
    const win = bars.slice(i - leftBars, i + rightBars + 1);
    const h = bars[i].h, l = bars[i].l;
    if (h === Math.max(...win.map(b => b.h))) highs.push({ i, price: h, t: bars[i].t });
    if (l === Math.min(...win.map(b => b.l))) lows.push({ i, price: l, t: bars[i].t });
  }
  return { highs, lows };
}

function clusterLevels(points, tolerancePct) {
  const sorted = [...points].sort((a, b) => a.price - b.price);
  const clusters = [];
  for (const p of sorted) {
    const last = clusters[clusters.length - 1];
    if (last && Math.abs(p.price - last.avg) / last.avg <= tolerancePct) {
      last.points.push(p);
      last.avg = last.points.reduce((s, x) => s + x.price, 0) / last.points.length;
    } else {
      clusters.push({ avg: p.price, points: [p] });
    }
  }
  return clusters.map(c => ({
    price: c.avg,
    strength: c.points.length,
    lastTouchIndex: c.points.reduce((m, x) => Math.max(m, x.i), 0),
  }));
}

/**
 * @returns { support: Level[], resistance: Level[] } — nearest to the last
 *          close first, each level capped at `maxLevels` per side.
 *          Level = { price, strength, lastTouchIndex }
 */
export function computeSupportResistance(bars, opts = {}) {
  const {
    leftBars = 4, rightBars = 4,
    tolerancePct = 0.0008,  // ~8 pips at 1.10 — merge pivots this close together
    maxLevels = 4,
    lookback = 250,
  } = opts;

  const recent = bars.slice(-lookback);
  if (recent.length < leftBars + rightBars + 1) return { support: [], resistance: [] };

  const lastClose = recent[recent.length - 1].c;
  const { highs, lows } = findPivots(recent, leftBars, rightBars);
  const byDistance = (a, b) => Math.abs(lastClose - a.price) - Math.abs(lastClose - b.price);

  const support = clusterLevels(lows, tolerancePct)
    .filter(l => l.price < lastClose)
    .sort(byDistance)
    .slice(0, maxLevels)
    .sort((a, b) => b.price - a.price); // display order: nearest (highest) first

  const resistance = clusterLevels(highs, tolerancePct)
    .filter(l => l.price > lastClose)
    .sort(byDistance)
    .slice(0, maxLevels)
    .sort((a, b) => a.price - b.price); // display order: nearest (lowest) first

  return { support, resistance };
}
