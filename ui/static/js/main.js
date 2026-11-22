/**
 * main.js — the only module that knows about intervals, DOM events and the
 * fetch sequence. Everything else (charts, signal maths, formatting,
 * rendering) is a pure-ish helper this file calls in order:
 *
 *   fetch  ->  compute (S/R, composite signal, prediction ledger)  ->  render
 *
 * That order runs on: first load, every manual/auto "Sync latest data", and
 * a plain 60s tick (which recomputes against whatever was last fetched, so
 * the "data is Nh old" pill and the composite signal stay honest between
 * network calls too).
 */

import { api, ApiKeyRequiredError, setStoredKey } from "./api.js";
import { queryDom } from "./dom.js";
import * as ui from "./ui.js";
import { drawPriceChart } from "./priceChart.js";
import { drawVolumeChart } from "./volumeChart.js";
import { computeSupportResistance } from "./supportResistance.js";
import { computeCompositeSignal } from "./compositeSignal.js";
import * as tracker from "./predictionTracker.js";
import { esc, when } from "./format.js";

const dom = queryDom();
let S = { latest: null, bars: [], stops: [], lorentzian: [], fwd: null, health: null, info: null };
let rangeN = 120;

// --------------------------------------------------------------------- fetch
async function load() {
  try {
    const [health, latest, b, fwd, info] = await Promise.all([
      api("/health"), api("/latest"), api("/bars?limit=300"), api("/forward-test"), api("/model/info").catch(() => null),
    ]);
    S = { health, latest, bars: b.bars, stops: b.stop, lorentzian: b.lorentzian || [], fwd, info };
    computeAndRender();
  } catch (e) {
    if (e instanceof ApiKeyRequiredError) { askKey(); return; }
    ui.setStatus(dom, "err", "Can’t reach the API");
    ui.notice(dom, `Couldn’t load data: ${esc(e.message)}. Is <code>uvicorn serve_api:app</code> running?`, "bad");
  }
}

async function sync(silent = false) {
  dom.syncBtn.disabled = true; dom.syncBtn.textContent = "Syncing…";
  try {
    const r = await api("/sync", { method: "POST" });
    if (!silent || r.added) {
      ui.notice(dom, r.added
        ? `Added <b>${r.added}</b> new bar${r.added > 1 ? "s" : ""} · latest ${when(r.last_bar_ts)} UTC`
        : `Already up to date · latest bar ${when(r.last_bar_ts)} UTC`);
      setTimeout(() => ui.clearNotice(dom), 6000);
    }
    await load();
  } catch (e) {
    if (e instanceof ApiKeyRequiredError) { askKey(); return; }
    ui.notice(dom, `Sync failed: ${esc(e.message)}`, "bad");
  } finally {
    dom.syncBtn.disabled = false; dom.syncBtn.textContent = "Sync latest data";
  }
}

function askKey() {
  ui.notice(dom, `<span>This API needs a key.</span><input id="keyin" type="password" placeholder="X-API-Key" autocomplete="off"><button id="keysave">Save</button>`, "warn");
  document.getElementById("keysave").onclick = () => {
    setStoredKey(document.getElementById("keyin").value.trim());
    ui.clearNotice(dom);
    load();
  };
}

// ------------------------------------------------------- compute + render pass
function computeAndRender() {
  if (!S.latest || !S.bars.length) return;

  const sr = computeSupportResistance(S.bars);
  const composite = computeCompositeSignal({
    lastClose: S.latest.last_close,
    lorentzian: S.latest.lorentzian,
    stop: S.latest.trailing_stop,
    sr,
  });

  tracker.recordIfNew({
    action: composite.action, confidence: composite.confidence, factors: composite.factors,
    asOf: S.latest.as_of, lastClose: S.latest.last_close,
  });
  tracker.resolvePending(S.bars);

  ui.renderTopStatus(dom, S.latest);
  ui.renderKPIs(dom, S.latest);
  ui.renderSignalPanel(dom, composite);
  ui.renderSRPanel(dom, sr, S.latest.last_close);
  ui.renderPredictionPanel(dom, tracker.getStats(), tracker.getHistory());
  ui.renderForecastTable(dom, S.latest);
  ui.renderScorecard(dom, S.fwd);
  ui.renderFooter(dom, S.health, S.info);

  drawPriceChart(dom.chart, dom.tip, { bars: S.bars, stops: S.stops, lorentzian: S.lorentzian, latest: S.latest, sr, rangeN });
  drawVolumeChart(dom.volume, { bars: S.bars, rangeN });
}

// -------------------------------------------------------------------- events
dom.syncBtn.onclick = () => sync(false);

dom.rangeGroup.onclick = e => {
  const b = e.target.closest("button"); if (!b) return;
  rangeN = +b.dataset.n;
  [...dom.rangeGroup.children].forEach(c => c.classList.toggle("on", c === b));
  computeAndRender();
};

dom.resetLedgerBtn.onclick = () => {
  if (!confirm("Clear the recorded prediction history and stats for this browser?")) return;
  tracker.reset();
  computeAndRender();
};

let autoTimer = null;
function setAuto(on) {
  try { localStorage.setItem("fmp_auto", on ? "1" : "0"); } catch {}
  clearInterval(autoTimer);
  if (on) autoTimer = setInterval(() => sync(true), 5 * 60 * 1000);
}
dom.autoToggle.checked = (() => { try { return localStorage.getItem("fmp_auto") === "1"; } catch { return false; } })();
dom.autoToggle.onchange = e => setAuto(e.target.checked);
setAuto(dom.autoToggle.checked);

new ResizeObserver(() => {
  clearTimeout(window.__fmp_resize);
  window.__fmp_resize = setTimeout(computeAndRender, 80);
}).observe(dom.chart);

// keeps the "data is X old" pill (and the composite signal) honest between syncs
setInterval(() => { if (S.latest) computeAndRender(); }, 60 * 1000);

load();
