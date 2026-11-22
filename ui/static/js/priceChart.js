/**
 * priceChart.js — the main candlestick panel. Draws, in this order (back to
 * front): future-zone shading, gridlines + x labels, candles, S/R levels,
 * the adaptive trailing stop, the Lorentzian kernel line + its buy/sell
 * markers, the forecast fan, the last-price tag, then a hover crosshair.
 *
 * Everything here is pure rendering: it takes already-computed data in and
 * draws it. The composite signal and S/R math live in their own modules.
 */

import { buildScale, svgEl, palette } from "./chartLayout.js";
import { f4, f5, whenShort } from "./format.js";

export function drawPriceChart(box, tip, { bars, stops, lorentzian, latest, sr, rangeN }) {
  if (!bars.length || !latest) return;
  const P = palette();
  const vis = bars.slice(-rangeN), st = stops.slice(-rangeN), lor = (lorentzian || []).slice(-rangeN);

  const W = Math.max(box.clientWidth, 320), H = W < 560 ? 330 : 460;
  const scale = buildScale(vis.length, W, { leftPad: 10, rightPad: 62, includeFuture: true });
  const { m, step, total, x, lastIndex: lastI } = scale;

  const margin = { t: 14, b: 30 };
  const iw = W - m.l - m.r, innerH = H - margin.t - margin.b;
  const fcs = latest.forecasts;

  let lo = Infinity, hi = -Infinity;
  vis.forEach(b => { lo = Math.min(lo, b.l); hi = Math.max(hi, b.h); });
  st.forEach(s => { lo = Math.min(lo, s.level); hi = Math.max(hi, s.level); });
  fcs.forEach(f => { lo = Math.min(lo, f.price_p10); hi = Math.max(hi, f.price_p90); });
  (sr?.support || []).forEach(l => { lo = Math.min(lo, l.price); });
  (sr?.resistance || []).forEach(l => { hi = Math.max(hi, l.price); });
  const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
  const y = p => margin.t + (hi - p) / (hi - lo) * innerH;

  const svg = svgEl("svg", {
    viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "EUR/USD price chart with trailing stop, Lorentzian signal, support/resistance and forecast bands",
  });
  const add = n => (svg.appendChild(n), n);

  // ---- future zone + gridlines --------------------------------------------
  add(svgEl("rect", { x: x(lastI) + step / 2, y: margin.t, width: W - m.r - (x(lastI) + step / 2), height: innerH, fill: P.accent, opacity: .05 }));
  const ticks = 6;
  for (let i = 0; i <= ticks; i++) {
    const p = lo + (hi - lo) * i / ticks, yy = y(p);
    add(svgEl("line", { x1: m.l, x2: W - m.r, y1: yy, y2: yy, stroke: P.line, "stroke-width": 1 }));
    add(svgEl("text", { x: W - m.r + 8, y: yy + 4, fill: P.muted, "font-size": 11 }, f4(p)));
  }
  const every = Math.max(1, Math.ceil(96 / step));
  for (let i = 0; i < vis.length; i += every) {
    if (x(i) > x(lastI) - 40 && i !== lastI && i > 0 && x(lastI) - x(i) < 50) continue;
    const d = new Date(vis[i].t);
    const lab = d.toLocaleString("en-GB", { timeZone: "UTC", day: "2-digit", month: "short" }) + " " + String(d.getUTCHours()).padStart(2, "0") + ":00";
    add(svgEl("text", { x: x(i), y: H - 9, fill: P.muted, "font-size": 11, "text-anchor": "middle" }, lab));
  }

  // ---- candles --------------------------------------------------------------
  const bw = Math.max(1.5, step * 0.62);
  vis.forEach((b, i) => {
    const c = b.c >= b.o ? P.up : P.down, xx = x(i);
    add(svgEl("line", { x1: xx, x2: xx, y1: y(b.h), y2: y(b.l), stroke: c, "stroke-width": 1, opacity: .9 }));
    add(svgEl("rect", { x: xx - bw / 2, y: Math.min(y(b.o), y(b.c)), width: bw, height: Math.max(1, Math.abs(y(b.o) - y(b.c))), fill: c, rx: 1 }));
  });

  // ---- support / resistance levels ------------------------------------------
  const drawLevel = (lvl, color, label) => {
    const yy = y(lvl.price);
    add(svgEl("line", { x1: m.l, x2: W - m.r, y1: yy, y2: yy, stroke: color, "stroke-width": 1, "stroke-dasharray": "5 4", opacity: .55 }));
    add(svgEl("text", { x: m.l + 4, y: yy - 4, fill: color, "font-size": 10.5, "font-weight": 600 }, `${label} ${f5(lvl.price)} · ${lvl.strength}×`));
  };
  (sr?.support || []).forEach(l => drawLevel(l, P.support, "S"));
  (sr?.resistance || []).forEach(l => drawLevel(l, P.resistance, "R"));

  // ---- adaptive stop, coloured per trend run --------------------------------
  let run = [], runTrend = null;
  const flushStop = () => { if (run.length > 1) add(svgEl("polyline", { points: run.join(" "), fill: "none", stroke: runTrend === "up" ? P.up : P.down, "stroke-width": 2, "stroke-linejoin": "round", opacity: .95 })); };
  st.forEach((s, i) => {
    if (s.trend !== runTrend) { flushStop(); run = []; runTrend = s.trend; }
    run.push(`${x(i).toFixed(1)},${y(s.level).toFixed(1)}`);
  });
  flushStop();
  st.forEach((s, i) => {
    if (!s.flip) return;
    const xx = x(i), buy = s.flip === "buy", b = vis[i];
    const yy = buy ? y(b.l) + 10 : y(b.h) - 10, d = buy ? -1 : 1;
    add(svgEl("path", { d: `M${xx},${yy + 6 * d} l-5,${-9 * d} l10,0 z`, fill: buy ? P.up : P.down }));
  });

  // ---- Lorentzian kernel line ("gold line") + entry markers -----------------
  if (lor.length === vis.length) {
    const pts = lor.map((l, i) => `${x(i).toFixed(1)},${y(l.kernel).toFixed(1)}`).join(" ");
    add(svgEl("polyline", { points: pts, fill: "none", stroke: P.lor, "stroke-width": 1.8, opacity: .9 }));
    lor.forEach((l, i) => {
      if (!l.flip) return;
      const xx = x(i), buy = l.flip === "buy", b = vis[i];
      const yy = buy ? y(b.l) + 22 : y(b.h) - 22;
      add(svgEl("path", {
        d: `M${xx},${yy - 6} L${xx + 6},${yy} L${xx},${yy + 6} L${xx - 6},${yy} Z`,
        fill: buy ? P.up : P.down, stroke: P.lor, "stroke-width": 1,
      }));
    });
  }

  // ---- forecast fan -----------------------------------------------------------
  const x0 = x(lastI), y0 = y(latest.last_close);
  const pts = key => [[x0, y0], ...fcs.map(f => [x(lastI + f.horizon_bars), y(f[key])])];
  const hiPts = pts("price_p90"), loPts = pts("price_p10"), mdPts = pts("price_p50");
  const path = a => a.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  add(svgEl("polygon", { points: path(hiPts) + " " + path([...loPts].reverse()), fill: P.accent, opacity: .14 }));
  add(svgEl("polyline", { points: path(hiPts), fill: "none", stroke: P.accent, "stroke-width": 1, opacity: .55 }));
  add(svgEl("polyline", { points: path(loPts), fill: "none", stroke: P.accent, "stroke-width": 1, opacity: .55 }));
  add(svgEl("polyline", { points: path(mdPts), fill: "none", stroke: P.accent, "stroke-width": 1.6, "stroke-dasharray": "4 3" }));
  fcs.forEach(f => {
    const xx = x(lastI + f.horizon_bars), yt = y(f.price_p90), yb = y(f.price_p10);
    add(svgEl("rect", { x: xx - 4, y: yt, width: 8, height: Math.max(2, yb - yt), rx: 3, fill: P.accent, opacity: .3, stroke: P.accent, "stroke-width": 1 }));
    add(svgEl("circle", { cx: xx, cy: y(f.price_p50), r: 3.2, fill: P.accent }));
    add(svgEl("text", { x: xx, y: yt - 6, fill: P.accent, "font-size": 11, "font-weight": 600, "text-anchor": "middle" }, "+" + f.horizon_bars + "h"));
  });

  // ---- last price line + tag ---------------------------------------------------
  add(svgEl("line", { x1: m.l, x2: W - m.r, y1: y0, y2: y0, stroke: P.ink, "stroke-width": 1, "stroke-dasharray": "2 4", opacity: .45 }));
  add(svgEl("rect", { x: W - m.r + 2, y: y0 - 10, width: m.r - 4, height: 20, rx: 5, fill: P.ink }));
  add(svgEl("text", { x: W - m.r + 2 + (m.r - 4) / 2, y: y0 + 4, fill: P.card, "font-size": 11, "font-weight": 600, "text-anchor": "middle" }, f4(latest.last_close)));

  // ---- hover crosshair + tooltip -------------------------------------------------
  const cross = add(svgEl("line", { y1: margin.t, y2: margin.t + innerH, stroke: P.muted, "stroke-width": 1, opacity: 0, "stroke-dasharray": "3 3" }));
  const hit = add(svgEl("rect", { x: m.l, y: margin.t, width: iw, height: innerH, fill: "transparent" }));

  const move = ev => {
    const r = svg.getBoundingClientRect(), k = W / r.width;
    const px = (ev.clientX - r.left) * k, i = Math.max(0, Math.min(total - 1, Math.floor((px - m.l) / step)));
    let html;
    if (i <= lastI) {
      const b = vis[i], s = st[i], l = lor[i];
      html = `<div class="t">${whenShort(b.t)} UTC</div>O ${f5(b.o)}  H ${f5(b.h)}<br>L ${f5(b.l)}  C <b>${f5(b.c)}</b>`
           + `<br>Stop ${f5(s.level)} (${s.trend})`
           + (l ? `<br>Lorentzian ${l.bias} · ${(l.confidence * 100).toFixed(0)}%` : "");
    } else {
      const h = i - lastI, f = fcs.reduce((a, cnd) => Math.abs(cnd.horizon_bars - h) < Math.abs(a.horizon_bars - h) ? cnd : a);
      html = `<div class="t">Forecast +${f.horizon_bars}h · ${whenShort(f.target_ts_est)}</div>Low ${f5(f.price_p10)}<br>Median <b>${f5(f.price_p50)}</b><br>High ${f5(f.price_p90)}`;
    }
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", .6);
    tip.innerHTML = html; tip.style.display = "block";
    const bx = box.getBoundingClientRect(), tw = tip.offsetWidth;
    let left = ev.clientX - bx.left + 14; if (left + tw > bx.width) left = ev.clientX - bx.left - tw - 14;
    tip.style.left = Math.max(0, left) + "px"; tip.style.top = Math.max(0, ev.clientY - bx.top - 20) + "px";
  };
  const leave = () => { tip.style.display = "none"; cross.setAttribute("opacity", 0); };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerleave", leave);

  box.querySelector("svg")?.remove();
  box.querySelector(".empty")?.remove();
  box.insertBefore(svg, tip);
}
