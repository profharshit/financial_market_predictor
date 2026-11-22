/**
 * ui.js — pure DOM rendering. Every function here takes the `dom` map from
 * dom.js plus already-computed data and writes it to the page; none of them
 * fetch anything or run the signal/S-R maths themselves.
 */

import { f1, f5, when, whenShort, esc, NAMES, marketClosed } from "./format.js";

// ------------------------------------------------------------------ status/notice
export function setStatus(dom, kind, text) {
  dom.status.className = "pill " + kind;
  dom.status.lastElementChild.textContent = text;
}

export function notice(dom, html, kind = "") {
  dom.notice.className = "notice show " + kind;
  dom.notice.innerHTML = html;
}
export function clearNotice(dom) { dom.notice.className = "notice"; }

// ------------------------------------------------------------------------ KPIs
export function renderKPIs(dom, latest) {
  const L = latest;
  dom.kClose.textContent = f5(L.last_close);
  dom.kCloseN.textContent = `${when(L.as_of)} UTC`;

  const ts = L.trailing_stop, up = ts.trend === "up";
  dom.kStop.innerHTML = `<span class="${up ? "up" : "down"}">${f5(ts.level)}</span>`;
  dom.kStopN.textContent = `${up ? "▲ uptrend" : "▼ downtrend"} · ${f1(ts.distance_pips)} pips away · width ×${ts.key_multiplier.toFixed(2)}`;

  const sg = L.signal;
  dom.kSig.textContent = sg.action;
  dom.kSig.className = "chip " + sg.action;
  dom.kSigN.textContent = sg.action === "HOLD"
    ? (ts.flip ? "Stop flipped, held back" : "No stop flip on this bar")
    : `Stop flipped ${sg.action === "BUY" ? "up" : "down"} on this bar`;

  const f1h = L.forecasts.find(f => f.horizon_bars === 1);
  dom.kNext.textContent = `${f5(f1h.price_p10)} – ${f5(f1h.price_p90)}`;
  dom.kNextN.textContent = `${f1(f1h.band_width_pips)} pips wide · median ${f5(f1h.price_p50)}`;
}

// -------------------------------------------------------------- composite signal
export function renderSignalPanel(dom, composite) {
  dom.signalAction.textContent = composite.action;
  dom.signalAction.className = "chip big " + composite.action;
  dom.signalConfidence.textContent = `${composite.confidence}% confidence`;

  dom.signalFactors.innerHTML = composite.factors.map(f => {
    const pct = Math.round(Math.abs(f.vote) * 100);
    const dir = f.vote > 0.02 ? "up" : f.vote < -0.02 ? "down" : "flat";
    return `<li class="factor">
      <div class="factor-head"><span>${esc(f.label)}</span><span class="m">${esc(f.detail)}</span></div>
      <div class="factor-bar"><span class="fill ${dir}" style="width:${Math.max(4, pct)}%"></span></div>
    </li>`;
  }).join("");

  dom.signalNote.textContent = "Composite dashboard signal — a transparent, unverified heuristic; not the same as the API's own edge_verified flag.";
}

// ------------------------------------------------------------- support/resistance
export function renderSRPanel(dom, sr, lastClose) {
  const row = (l, kind) => `<li class="sr-row ${kind}">
      <span class="dot"></span>
      <span class="price">${f5(l.price)}</span>
      <span class="m">${kind === "support" ? "support" : "resistance"} · ${l.strength}× touched</span>
      <span class="m">${(Math.abs(lastClose - l.price) * 1e4).toFixed(1)} pips away</span>
    </li>`;
  const rows = [...sr.resistance.map(l => row(l, "resistance")), ...sr.support.map(l => row(l, "support"))];
  dom.srList.innerHTML = rows.length ? rows.join("") : `<li class="m">Not enough swing pivots in range yet.</li>`;
}

// -------------------------------------------------------------- prediction ledger
export function renderPredictionPanel(dom, stats, history) {
  dom.predTotal.textContent = stats.total;
  dom.predCorrect.textContent = stats.correct;
  dom.predIncorrect.textContent = stats.incorrect;
  dom.predPush.textContent = stats.push;
  dom.predPending.textContent = stats.pending;
  dom.predAccuracy.textContent = stats.accuracy == null ? "–" : `${(stats.accuracy * 100).toFixed(0)}%`;
  const pnl = stats.simulatedPips;
  dom.predPnl.textContent = `${pnl >= 0 ? "+" : ""}${pnl.toFixed(1)} pips`;
  dom.predPnl.className = pnl >= 0 ? "up" : "down";

  dom.predHistoryBody.innerHTML = history.length ? history.map(p => {
    const badge = p.pending ? "pending" : p.outcome; // pending | win | loss | push
    const pips = p.pipsResult == null ? "–" : `${p.pipsResult >= 0 ? "+" : ""}${p.pipsResult.toFixed(1)}`;
    return `<tr>
      <td>${whenShort(p.asOf)}</td>
      <td><span class="chip sm ${p.action}">${p.action}</span></td>
      <td>${f5(p.entryPrice)}</td>
      <td><span class="badge ${badge}">${badge}</span></td>
      <td class="${p.pipsResult >= 0 ? "up" : "down"}">${pips}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" style="text-align:left;color:var(--muted)">No directional calls recorded yet.</td></tr>`;
}

// ---------------------------------------------------------------- forecast table
export function renderForecastTable(dom, latest) {
  dom.fcBody.innerHTML = latest.forecasts.map(f => {
    const bps = f.expected_return_bps;
    return `<tr><td class="h">+${f.horizon_bars}h</td><td>${whenShort(f.target_ts_est)}</td>
      <td>${f5(f.price_p10)}</td><td><b>${f5(f.price_p50)}</b><span class="m ${bps >= 0 ? "up" : "down"}">${bps >= 0 ? "+" : ""}${f1(bps)} bps</span></td>
      <td>${f5(f.price_p90)}</td><td>${f1(f.band_width_pips)} pips</td><td>${esc(NAMES[f.model] || f.model)}</td></tr>`;
  }).join("");
  dom.fcFoot.textContent = "Median moves are tiny by design: no model here can reliably call direction. The useful part is how wide the band is.";
}

// -------------------------------------------------------------------- scorecard
export function renderScorecard(dom, fwd) {
  const hs = fwd.horizons || {};
  const keys = Object.keys(hs).sort((a, b) => a - b);
  if (!keys.length) {
    dom.scBody.innerHTML = `<tr><td colspan="4" style="text-align:left;color:var(--muted)">Nothing scored yet.</td></tr>`;
    dom.scFoot.textContent = "Click “Sync latest data” to backfill bars that came after the training data, or leave Auto-sync on to score live bars as they arrive.";
    return;
  }
  dom.scBody.innerHTML = keys.map(k => {
    const h = hs[k], cov = h.band_coverage_p10_p90 * 100, ok = cov >= 70 && cov <= 90;
    return `<tr><td class="h">+${k}h</td><td>${h.n_resolved}</td>
      <td><b class="${ok ? "up" : "down"}">${cov.toFixed(0)}%</b><span class="m">target 80%</span></td>
      <td>${f1(h.mae_pips_model)} vs ${f1(h.mae_pips_naive_no_change)}<span class="m">pips</span></td></tr>`;
  }).join("");
  dom.scFoot.textContent = `“In band” should sit near 80%. Error is the average miss of the median forecast against simply assuming the price stays put. ${fwd.pending_unresolved} forecast(s) waiting for their target bar.`;
}

// --------------------------------------------------------------------- misc
export function renderFooter(dom, health, info) {
  dom.foot.textContent = `Model ${health.model_version}`
    + (info ? ` · trained on ${info.training_data[0].slice(0, 10)} → ${info.training_data[1].slice(0, 10)}` : "")
    + " · all times UTC";
}

export function renderTopStatus(dom, latest) {
  const last = new Date(latest.as_of), ageH = (Date.now() - last.getTime()) / 3.6e6;
  const stale = ageH > 3 && !marketClosed(new Date());
  if (stale) {
    const d = Math.floor(ageH / 24), h = Math.round(ageH % 24);
    setStatus(dom, "stale", `Data is ${d ? d + "d " : ""}${h}h old`);
    if (!dom.notice.classList.contains("show")) {
      notice(dom, `Latest bar is <b>${when(latest.as_of)} UTC</b>. Click <b>Sync latest data</b> to pull newer bars.`, "warn");
    }
  } else {
    setStatus(dom, "ok", marketClosed(new Date()) ? "Market closed · up to date" : "Up to date");
  }
  dom.disclaimer.classList.toggle("show", !latest.signal.edge_verified);
}
