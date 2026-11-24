// FILE: financial-market-project/ui/static/js/fmipForecastPage.js   (NEW FILE)
// PLACE IN: ui/static/js/   (same folder as fmipIndicators.js)
// ADD TO ui/index.html: <script src="/ui/static/js/fmipForecastPage.js"></script>
//   as the LAST script tag, after fmipDashboardSync.js, just before </body>.
// Replaces the demo numbers on the "EUR/USD Forecast" tab with live API data.

const FORECAST_PAGE_API_URL = window.location.protocol.startsWith('http') ? window.location.origin : 'http://127.0.0.1:8000';
const FORECAST_PAGE_API_KEY = window.FMIP_API_KEY || localStorage.getItem('fmipApiKey') || '';
const FORECAST_PAGE_TIMEOUT_MS = 15000;
const FORECAST_PAGE_REFRESH_MS = 60000;
const FORECAST_PAGE_HORIZONS = [1, 4, 24];
const FORECAST_PAGE_BARS = 48;

let forecastPagePrediction = null;

// One place for HTTP so every call sends the key and times out the same way.
async function fetchForecastPageJson(path) {
  const headers = FORECAST_PAGE_API_KEY ? { 'X-API-Key': FORECAST_PAGE_API_KEY } : {};
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FORECAST_PAGE_TIMEOUT_MS);
  try {
    const response = await fetch(FORECAST_PAGE_API_URL + path, { headers, signal: controller.signal });
    if (response.status === 401) throw new Error('API key missing or wrong');
    if (!response.ok) throw new Error(`${path} failed with status ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

// Writes text into the first element matching a selector, ignoring missing ones.
function setForecastPageText(selector, text) {
  const element = document.querySelector(selector);
  if (element) element.textContent = text;
}

// Shows prices with five decimals, the usual EUR/USD precision.
function formatForecastPrice(value) {
  return Number.isFinite(value) ? value.toFixed(5) : '—';
}

// Reads which horizon (1, 4 or 24 hours) is chosen in the dropdown.
function getForecastPageHorizon() {
  const dropdown = document.querySelector('#page-forecast .ph select');
  return FORECAST_PAGE_HORIZONS[dropdown.selectedIndex] || 1;
}

// Finds the forecast for one horizon inside a prediction.
function findForecastByHorizon(prediction, horizonHours) {
  return prediction.forecasts.find(forecast => forecast.horizon_bars === horizonHours);
}

// Removes the "DEMO" wording because this page now reads real model output.
function removeDemoLabels() {
  document.querySelectorAll('#page-forecast .demo-label').forEach(label => label.remove());
  setForecastPageText('#page-forecast .ps', 'Quantile model · live output from the forecast API');
}

// Tells the user why the API call failed, so a wrong key looks different from offline.
function showForecastPageError(error) {
  const isKeyProblem = error.message.includes('key');
  const message = isKeyProblem
    ? 'API key missing or wrong. Open the dashboard through /ui.'
    : 'Forecast API not reachable. Start it with: uvicorn serve_api:app --port 8000';
  setForecastPageText('#page-forecast .ps', message);
}

// Fills the four KPI tiles for the selected horizon.
function renderForecastKpis(prediction) {
  const forecast = findForecastByHorizon(prediction, getForecastPageHorizon());
  const values = document.querySelectorAll('#page-forecast .g4 .kpi-val');
  values[0].textContent = formatForecastPrice(forecast.price_p10);
  values[1].textContent = formatForecastPrice(forecast.price_p50);
  values[2].textContent = formatForecastPrice(forecast.price_p90);
  values[3].textContent = formatForecastPrice(prediction.trailing_stop.level);
  const subtitles = document.querySelectorAll('#page-forecast .g4 .kpi-sub');
  subtitles[1].textContent = `${forecast.model} · as of ${prediction.as_of.slice(0, 16).replace('T', ' ')} UTC`;
}

// Builds a two-point line that fans out from the last close to a forecast value.
function buildFanLine(closes, forecastPrice) {
  const line = new Array(closes.length + 1).fill(null);
  line[closes.length - 1] = closes[closes.length - 1];
  line[closes.length] = forecastPrice;
  return line;
}

// Makes one dataset for the forecast chart.
function makeFanDataset(label, data, color, fillTarget) {
  return {
    label, data, borderColor: color, borderWidth: 2, pointRadius: 2,
    tension: 0.2, fill: fillTarget, backgroundColor: 'rgba(0,212,255,.09)',
  };
}

// Draws recent closes plus the P10 / P50 / P90 fan for the chosen horizon.
async function drawForecastFanChart(prediction) {
  const hours = getForecastPageHorizon();
  const forecast = findForecastByHorizon(prediction, hours);
  const barsResponse = await fetchForecastPageJson(`/bars?limit=${FORECAST_PAGE_BARS}`);
  const closes = barsResponse.bars.map(bar => bar.c);
  const labels = barsResponse.bars.map(bar => bar.t.slice(11, 16)).concat(`+${hours}h`);
  destroyChart('forecast');
  charts['forecast'] = new Chart(document.getElementById('forecastChart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        makeFanDataset('P90', buildFanLine(closes, forecast.price_p90), '#00FF9D', '+1'),
        makeFanDataset('P50', buildFanLine(closes, forecast.price_p50), '#00D4FF', false),
        makeFanDataset('P10', buildFanLine(closes, forecast.price_p10), '#FF4466', '-1'),
        { label: 'Close', data: closes.concat(null), borderColor: '#B8C8E8', borderWidth: 1.5, pointRadius: 0, fill: false },
      ],
    },
    options: {
      responsive: true,
      plugins: { legend: { position: 'top' }, tooltip: { mode: 'index', intersect: false } },
      scales: { x: { grid: { color: '#1A2C5A' } }, y: { grid: { color: '#1A2C5A' }, ticks: { callback: value => value.toFixed(4) } } },
    },
  });
}

// Picks green when coverage is near the 80% target and gold otherwise.
function pickCoverageColor(coverage) {
  return coverage >= 0.7 && coverage <= 0.9 ? 'var(--green)' : 'var(--gold)';
}

// Replaces the fake "Horizon Accuracy" card with measured band coverage per horizon.
function renderCoverageCard(report) {
  const card = document.querySelectorAll('#page-forecast .g2 .card')[1];
  card.querySelector('.ct').textContent = 'Live Band Coverage (target 80%)';
  card.querySelectorAll('.pr').forEach((row, index) => {
    const entry = report.horizons[String(FORECAST_PAGE_HORIZONS[index])];
    const coverage = entry ? entry.band_coverage_p10_p90 : 0;
    row.querySelector('.pr-lbl').textContent = `${FORECAST_PAGE_HORIZONS[index]}h P10–P90 band`;
    row.querySelector('.pr-fill').style.width = coverage * 100 + '%';
    row.querySelector('.pr-fill').style.background = pickCoverageColor(coverage);
    row.querySelector('.pr-val').textContent = entry ? Math.round(coverage * 100) + '%' : 'n/a';
  });
  card.querySelector('p').textContent = '⚠ Share of realised prices inside the band, on bars after training. Calibration only, no trading edge implied.';
}

// Builds one label and value line for the model card.
function makeModelRow(label, value) {
  const row = document.createElement('div');
  row.className = 'lab-kv';
  const name = document.createElement('span');
  const strong = document.createElement('strong');
  name.textContent = label;
  strong.textContent = value;
  row.append(name, strong);
  return row;
}

// Replaces the made-up feature importance with the real model chosen for each horizon.
function renderModelCard(info) {
  const card = document.querySelectorAll('#page-forecast .g2 .card')[0];
  const rows = FORECAST_PAGE_HORIZONS.map(hours => makeModelRow(`${hours}h model`, info.kinds[String(hours)]));
  rows.push(makeModelRow('Version', info.version));
  rows.push(makeModelRow('Training data', `${info.training_data[0].slice(0, 10)} → ${info.training_data[1].slice(0, 10)}`));
  card.querySelector('.ct').textContent = 'Model in use';
  card.replaceChildren(card.querySelector('.ch'), ...rows);
}

// Makes the Run button call the real GET /latest instead of the fake demo.
async function runLiveForecastPredict() {
  const badge = document.getElementById('predict-status');
  const responseBox = document.getElementById('api-resp');
  badge.textContent = 'Calling API…';
  badge.className = 'badge b-gold';
  try {
    const prediction = await fetchForecastPageJson('/latest');
    responseBox.textContent = JSON.stringify(prediction, null, 2);
    responseBox.style.cssText = 'display:block;white-space:pre;overflow-x:auto';
    badge.textContent = '200 OK';
    badge.className = 'badge b-green';
  } catch (error) {
    badge.textContent = 'Offline';
    badge.className = 'badge b-red';
  }
}

// Renames the old demo POST /predict labels so they describe the real call.
function relabelForecastEndpoint() {
  setForecastPageText('#page-forecast .card.c-purple .ct', '⚡ GET /latest · Live Response');
  setForecastPageText('#page-forecast .ph .btn-solid', '▶ Fetch /latest');
  const terminal = document.querySelector('#page-forecast .terminal');
  terminal.textContent = `$ curl ${FORECAST_PAGE_API_URL}/latest -H "X-API-Key: <key>"`;
}

// Loads every live piece of the Forecast page and draws it.
async function buildForecastPageLive() {
  try {
    forecastPagePrediction = await fetchForecastPageJson('/latest');
    removeDemoLabels();
    renderForecastKpis(forecastPagePrediction);
    const [report, info] = await Promise.all([fetchForecastPageJson('/forward-test'), fetchForecastPageJson('/model/info')]);
    renderCoverageCard(report);
    renderModelCard(info);
    await drawForecastFanChart(forecastPagePrediction);
  } catch (error) {
    showForecastPageError(error);
  }
}

// Refreshes only while the Forecast page is on screen, so syncs show up without a click.
function refreshForecastPageWhileVisible() {
  const page = document.getElementById('page-forecast');
  if (page.classList.contains('active') && !document.hidden) buildForecastPageLive();
}

// Swaps the demo functions for live ones and starts the refresh timer.
function startForecastPage() {
  window.buildForecastChart = buildForecastPageLive;
  window.runPredict = runLiveForecastPredict;
  relabelForecastEndpoint();
  setInterval(refreshForecastPageWhileVisible, FORECAST_PAGE_REFRESH_MS);
}

startForecastPage();