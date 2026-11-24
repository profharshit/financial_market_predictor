// FILE: financial-market-project/ui/static/js/fmipDashboardSync.js   (NEW FILE)
// PLACE: ui/static/js/, in the same folder as fmipIndicators.js and paperTrading.js.
// PURPOSE: makes the dashboard read the same /latest, /bars and /forward-test data as the
// Indicator Lab tab, replaces the fake accuracy and random request counter with real numbers.

const DASH_API_URL = window.location.protocol.startsWith('http') ? window.location.origin : 'http://127.0.0.1:8000';
const DASH_API_KEY = window.FMIP_API_KEY || localStorage.getItem('fmipApiKey') || '';
const DASH_REQUEST_TIMEOUT_MS = 15000;
const DASH_SYNC_TIMEOUT_MS = 60000;
const DASH_REFRESH_MS = 30000;
const DASH_COMPARE_HORIZON_HOURS = 4;
const DASH_BARS_FOR_ANN_SCORE = 600;
const DASH_KEY_RANGE = { min: 0.75, max: 1.5 };

let isDashSyncing = false;

// ───────────────────────── HTTP + SMALL HELPERS ─────────────────────────

// One place for HTTP so every call sends the key and times out the same way.
async function requestDashApi(path, options = {}, timeoutMs = DASH_REQUEST_TIMEOUT_MS) {
  const headers = DASH_API_KEY ? { 'X-API-Key': DASH_API_KEY } : {};
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(DASH_API_URL + path, { ...options, headers, signal: controller.signal });
    if (!response.ok) throw new Error(`${path} failed with status ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

// Writes text into an element by id and ignores ids that are not on the page.
function setDashText(elementId, text) {
  const element = document.getElementById(elementId);
  if (element) element.textContent = text;
}

// Turns a 0-1 rate into a readable percent, or a dash when there is no data yet.
function formatDashPercent(rate) {
  return Number.isFinite(rate) ? (rate * 100).toFixed(1) + '%' : '—';
}

// Sets the width of a small progress bar from a 0-1 rate.
function setDashFill(elementId, rate) {
  const fill = document.getElementById(elementId);
  if (fill) fill.style.width = (Number.isFinite(rate) ? rate * 100 : 0) + '%';
}

// Updates one label/bar/value row used in the small dashboard cards.
function setDashProgressRow(row, valueText, percent) {
  row.querySelector('.pr-val').textContent = valueText;
  row.querySelector('.pr-fill').style.width = Math.max(0, Math.min(100, percent)) + '%';
}

// ───────────────────────── FORECAST ACCURACY CARD (XGBOOST + LORENTZIAN) ─────────────────────────

// Replaces the made-up 78.4% tile with two real rows, one per model.
function buildAccuracyCard() {
  const card = document.querySelector('#page-dashboard .g4 .kpi');
  if (!card || document.getElementById('dash-xgb-rate')) return;
  card.title = 'Direction hit rate: 50% is a coin flip. Lorentzian is scored on the chart bars, XGBoost on the live forward test.';
  card.innerHTML = `
    <div class="kpi-stripe" style="background:var(--cyan)"></div>
    <div class="kpi-top"><div class="kpi-label">Forecast Accuracy · ${DASH_COMPARE_HORIZON_HOURS}h direction</div><div class="kpi-ico">📈</div></div>
    <div class="kpi-val" style="font-size:18px"><span style="color:var(--cyan)">XGB</span> <span id="dash-xgb-rate">—</span></div>
    <div class="kpi-sub" id="dash-xgb-sub">Waiting for API</div>
    <div class="kpi-bar"><div class="kpi-fill" id="dash-xgb-fill" style="width:0;background:var(--cyan)"></div></div>
    <div class="kpi-val" style="font-size:18px;margin-top:10px"><span style="color:var(--gold)">ANN</span> <span id="dash-ann-rate">—</span></div>
    <div class="kpi-sub" id="dash-ann-sub">Waiting for API</div>
    <div class="kpi-bar"><div class="kpi-fill" id="dash-ann-fill" style="width:0;background:var(--gold)"></div></div>`;
}

// Scores the Lorentzian bias against what price really did a few bars later, using the same bars the Indicator Lab chart draws.
function scoreLorentzianDirection(barsResponse, holdBars) {
  const bars = barsResponse.bars;
  const signals = barsResponse.lorentzian;
  let hits = 0;
  let calls = 0;
  for (let index = 0; index + holdBars < bars.length; index++) {
    const bias = signals[index] ? signals[index].bias : 'neutral';
    const move = bars[index + holdBars].c - bars[index].c;
    if (bias === 'neutral' || move === 0) continue;
    calls += 1;
    if ((bias === 'up') === (move > 0)) hits += 1;
  }
  return { hitRate: calls > 0 ? hits / calls : null, calls };
}

// Shows the XGBoost forward-test hit rate and the Lorentzian hit rate side by side.
function renderAccuracyCard(forwardTest, lorentzianScore) {
  const xgboost = forwardTest.horizons[String(DASH_COMPARE_HORIZON_HOURS)];
  const xgboostRate = xgboost ? xgboost.directional_accuracy : null;
  const coverage = xgboost ? formatDashPercent(xgboost.band_coverage_p10_p90) : '—';
  setDashText('dash-xgb-rate', formatDashPercent(xgboostRate));
  setDashText('dash-xgb-sub', xgboost ? `${xgboost.n_resolved} resolved · band cover ${coverage}` : 'No resolved forecasts yet');
  setDashFill('dash-xgb-fill', xgboostRate);
  setDashText('dash-ann-rate', formatDashPercent(lorentzianScore.hitRate));
  setDashText('dash-ann-sub', `${lorentzianScore.calls} signals · hold ${DASH_COMPARE_HORIZON_HOURS} bars`);
  setDashFill('dash-ann-fill', lorentzianScore.hitRate);
}

// ───────────────────────── NEXT 1H FORECAST CARD ─────────────────────────

// Fills the three price boxes with the real 1h P10 / P50 / P90 band.
function renderForecastBand(card, oneHourForecast) {
  const boxes = card.querySelectorAll('.fc-val');
  [oneHourForecast.price_p10, oneHourForecast.price_p50, oneHourForecast.price_p90].forEach((price, index) => {
    boxes[index].textContent = price.toFixed(5);
  });
}

// Replaces the demo numbers with live volatility, Lorentzian confidence and stop level.
function renderForecastRows(card, prediction) {
  const rows = card.querySelectorAll('.pr');
  const stop = prediction.trailing_stop;
  const regime = (stop.key_multiplier - DASH_KEY_RANGE.min) / (DASH_KEY_RANGE.max - DASH_KEY_RANGE.min);
  const confidence = prediction.lorentzian.confidence;
  rows[0].querySelector('.pr-lbl').textContent = 'ATR';
  rows[1].querySelector('.pr-lbl').textContent = 'ANN conf.';
  setDashProgressRow(rows[0], `${stop.atr_pips.toFixed(1)} pips`, regime * 100);
  setDashProgressRow(rows[1], Math.round(confidence * 100) + '%', confidence * 100);
  setDashProgressRow(rows[2], stop.level.toFixed(5), 55);
}

// Rebuilds the "Next 1h Forecast" card from /latest, the same call the Indicator Lab uses.
function renderForecastCard(prediction) {
  const card = document.querySelector('#page-dashboard .card.c-purple');
  const oneHour = prediction.forecasts.find(forecast => forecast.horizon_bars === 1);
  if (!card || !oneHour) return;
  card.querySelector('.demo-label').textContent = 'LIVE';
  renderForecastBand(card, oneHour);
  renderForecastRows(card, prediction);
  const asOf = prediction.as_of.slice(0, 16).replace('T', ' ');
  card.querySelector('p').textContent = `As of ${asOf} UTC · ${prediction.model_version} · Signal only, no trading edge implied.`;
}

// ───────────────────────── API REQUESTS CARD ─────────────────────────

// Shows the server's real request count; the old code picked a random number on every refresh.
function renderRequestCounter(stats) {
  setDashText('kv3', stats.requests_today.toLocaleString());
  // The forecast API is protected by a shared API key, not JWT, so the label says so.
  const subtitle = document.getElementById('kv3').parentElement.querySelector('.kpi-sub');
  subtitle.textContent = `Today · ${stats.failed_today} failed · API-key authenticated`;
}

// ───────────────────────── LOADING + REFRESH ─────────────────────────

// Loads all four endpoints in parallel so one failing call cannot blank the other cards.
async function refreshDashboardCards() {
  buildAccuracyCard();
  const [latest, forwardTest, bars, stats] = await Promise.allSettled([
    requestDashApi('/latest'),
    requestDashApi('/forward-test'),
    requestDashApi(`/bars?limit=${DASH_BARS_FOR_ANN_SCORE}`),
    requestDashApi('/stats'),
  ]);
  if (latest.status === 'fulfilled') renderForecastCard(latest.value);
  else setDashText('dash-xgb-sub', 'Forecast API not reachable');
  if (forwardTest.status === 'fulfilled' && bars.status === 'fulfilled') {
    renderAccuracyCard(forwardTest.value, scoreLorentzianDirection(bars.value, DASH_COMPARE_HORIZON_HOURS));
  }
  if (stats.status === 'fulfilled') renderRequestCounter(stats.value);
}

// Asks the server to pull new bars from Dukascopy and returns how many were added.
async function syncNewBars() {
  try {
    const result = await requestDashApi('/sync', { method: 'POST' }, DASH_SYNC_TIMEOUT_MS);
    return result.added;
  } catch (error) {
    console.warn('Sync skipped:', error.message);
    return 0;
  }
}

// Locks the Refresh button while syncing so repeated clicks cannot pile up requests.
function setDashRefreshButtonBusy(isBusy) {
  const button = document.querySelector('#page-dashboard .btn-row .btn-c');
  if (!button) return;
  button.disabled = isBusy;
  button.textContent = isBusy ? '⏳ Syncing…' : '↻ Refresh';
}

// Reloads the Indicator Lab chart too, but only if that tab has already been opened.
function refreshIndicatorLabIfOpen() {
  if (typeof labChart !== 'undefined' && labChart) loadIndicatorData();
}

// Dashboard Refresh button: sync new bars, redraw the cards, then update the Indicator Lab to match.
async function syncAndRefreshDashboard() {
  if (isDashSyncing) return;
  isDashSyncing = true;
  setDashRefreshButtonBusy(true);
  try {
    await syncNewBars();
    await refreshDashboardCards();
    refreshIndicatorLabIfOpen();
  } finally {
    setDashRefreshButtonBusy(false);
    isDashSyncing = false;
  }
}

// ───────────────────────── PAGE WIRING ─────────────────────────

// Makes the Indicator Lab Refresh button also pull new bars, then update the dashboard cards.
function wireIndicatorLabSync() {
  const button = document.getElementById('lab-refresh');
  if (!button) return;
  button.addEventListener('click', async () => {
    const addedBars = await syncNewBars();
    if (addedBars > 0) await loadIndicatorData();
    refreshDashboardCards();
  });
}

// Refreshes the dashboard cards every time the user comes back to the Dashboard page.
function refreshCardsOnDashboardOpen() {
  const previousGo = window.go;
  window.go = function (pageId, navElement) {
    previousGo(pageId, navElement);
    if (pageId === 'dashboard') refreshDashboardCards();
  };
}

// Runs a task on a timer, but skips it while the browser tab is hidden.
function repeatWhileVisible(task, intervalMs) {
  setInterval(() => {
    if (!document.hidden) task();
  }, intervalMs);
}

// Starts everything once when the script loads.
function startDashboardSync() {
  buildAccuracyCard();
  window.refreshDash = syncAndRefreshDashboard;
  refreshCardsOnDashboardOpen();
  wireIndicatorLabSync();
  refreshDashboardCards();
  repeatWhileVisible(refreshDashboardCards, DASH_REFRESH_MS);
}

startDashboardSync();