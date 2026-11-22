/**
 * volumeChart.js — a small bar chart of traded volume, aligned pixel-for-
 * pixel with the price chart above it (both call chartLayout.buildScale with
 * the same `includeFuture: true` and the same number of visible bars).
 */

import { buildScale, svgEl, palette } from "./chartLayout.js";

export function drawVolumeChart(box, { bars, rangeN }) {
  if (!bars.length) return;
  const P = palette();
  const vis = bars.slice(-rangeN);

  const W = Math.max(box.clientWidth, 320), H = 90;
  const margin = { t: 6, b: 4 };
  const { m, step, x } = buildScale(vis.length, W, { leftPad: 10, rightPad: 62, includeFuture: true });
  const innerH = H - margin.t - margin.b;

  const maxV = Math.max(1, ...vis.map(b => b.v));
  const y = v => margin.t + innerH - (v / maxV) * innerH;

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Traded volume" });
  const add = n => (svg.appendChild(n), n);
  const bw = Math.max(1.5, step * 0.62);

  vis.forEach((b, i) => {
    const xx = x(i), c = b.c >= b.o ? P.up : P.down, yy = y(b.v);
    add(svgEl("rect", { x: xx - bw / 2, y: yy, width: bw, height: Math.max(1, innerH + margin.t - yy), fill: c, opacity: .55, rx: 1 }));
  });
  add(svgEl("line", { x1: m.l, x2: W - m.r, y1: margin.t + innerH, y2: margin.t + innerH, stroke: P.line, "stroke-width": 1 }));

  box.querySelector("svg")?.remove();
  box.appendChild(svg);
}
