// FILE: financial-market-project/ui/static/js/fmipLive.js
// NEW FILE. Place it in ui/static/js/ (the folder serve_api.py already mounts at /ui/static).
// Connects the FMIP dashboard to the forecast API (:8000), the anomaly detector and the classifier API (:8001).

const FMIP_CONFIG = {
  forecastApiUrl: window.location.protocol.startsWith('http') ? window.location.origin : 'http://127.0.0.1:8000',
  classifierApiUrl: 'http://127.0.0.1:8001',
  apiKey: window.FMIP_API_KEY || localStorage.getItem('fmipApiKey') || '',
};

const DASHBOARD_REFRESH_MS = 30000;
const HOURS_PER_TIMEFRAME = { '1H': 1, '4H': 4, '1D': 24 };
const HORIZON_HOURS = [1, 4, 24];
const KEY_MULTIPLIER_RANGE = { min: 0.75, max: 1.5 };
const HIGH_CONFIDENCE_PROBABILITY = 0.75;
const VELOCITY_PERCENT_PER_MESSAGE = 10;
const DUPLICATE_PERCENT_PER_MESSAGE = 20;
const HEATMAP_COLORS = {
  normal: 'rgba(0,255,157,.25)', watch: 'rgba(255,215,0,.5)',
  anomaly: 'rgba(255,107,53,.65)', high: 'rgba(255,68,102,.9)',
};
const FEATURE_LABELS = {
  PRICE_CHANGE_PCT: 'Price Δ %', VOLUME_CHANGE_PCT: 'Volume Δ %', VOLUME_RATIO: 'Volume Ratio',
  VOLATILITY_20: 'Volatility', VWAP_DEVIATION_PCT: 'VWAP Dev',
  TRADE_COUNT_CHANGE_PCT: 'Trade Count Δ', DELIVERY_CHANGE_PCT: 'Delivery Δ',
};
const KEYWORD_COLORS = ['var(--red)', '#AA77FF', 'var(--orange)', 'var(--gold)'];

let latestPrediction = null;
let sentimentSummary = null;
let stopLineSeries = null;
let kernelLineSeries = null;
let lastBuiltChartKey = '';

const originalBuildPriceChart = buildPriceChart;
const originalUpdateTradeSignalUI = updateTradeSignalUI;
const originalJitterTicker = jitterTicker;
const originalGo = go;
const originalInitAll = initAll;

// ───────────────────────── HTTP + DOM HELPERS ─────────────────────────

// One place that talks HTTP so every call sends the API key and handles errors the same way.
async function requestJson(baseUrl, path, options = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (FMIP_CONFIG.apiKey) headers['X-API-Key'] = FMIP_CONFIG.apiKey;
  const response = await fetch(baseUrl + path, { ...options, headers });
  if (!response.ok) throw new Error(`${path} failed with status ${response.status}`);
  return response.json();
}

// Shortcut for calls to the forecast API.
function getForecastApi(path, options) {
  return requestJson(FMIP_CONFIG.forecastApiUrl, path, options);
}

// Shortcut for calls to the classifier API.
function getClassifierApi(path, options) {
  return requestJson(FMIP_CONFIG.classifierApiUrl, path, options);
}

// Replaces the text of the first element that matches a selector.
function setText(selector, text) {
  const element = document.querySelector(selector);
  if (element) element.textContent = text;
}

// Stops API text (like a Telegram message) from being interpreted as HTML.
function escapeHtml(text) {
  const holder = document.createElement('div');
  holder.textContent = String(text);
  return holder.innerHTML;
}

// Formats a percent with an explicit sign so moves read as +1.20% or −0.80%.
function formatSignedPercent(value) {
  return `${value >= 0 ? '+' : '−'}${Math.abs(value).toFixed(2)}%`;
}

// Finds the forecast for one horizon (1, 4 or 24 hours) inside a prediction.
function findForecast(prediction, horizonHours) {
  return prediction.forecasts.find(forecast => forecast.horizon_bars === horizonHours);
}

// Shows the user that the forecast API cannot be reached instead of failing silently.
function reportForecastApiOffline(error) {
  console.warn('Forecast API problem:', error.message);
  setText('#priceFeedStatus', 'API OFFLINE');
}

// Marks the classifier-driven widgets as unavailable when port 8001 is not running.
function markClassifierOffline(error) {
  console.warn('Classifier API problem:', error.message);
  setText('#page-sentiment .ph .badge', 'Classifier offline');
}

// ───────────────────────── PRICE CHART (EUR/USD) ─────────────────────────

// Merges the API's parallel arrays (bars, stop, lorentzian) into one row per hourly bar.
function toRows(barsResponse) {
  return barsResponse.bars.map((bar, index) => ({
    time: Math.floor(new Date(bar.t).getTime() / 1000),
    open: bar.o, high: bar.h, low: bar.l, close: bar.c, volume: bar.v,
    stop: barsResponse.stop[index].level,
    stopFlip: barsResponse.stop[index].flip,
    kernel: barsResponse.lorentzian[index].kernel,
    lorentzianFlip: barsResponse.lorentzian[index].flip,
  }));
}

// Folds one more hourly row into a 4H or 1D bucket.
function mergeRowIntoBucket(bucket, row) {
  return {
    ...bucket,
    high: Math.max(bucket.high, row.high),
    low: Math.min(bucket.low, row.low),
    close: row.close,
    volume: bucket.volume + row.volume,
    stop: row.stop,
    kernel: row.kernel,
    stopFlip: row.stopFlip || bucket.stopFlip,
    lorentzianFlip: row.lorentzianFlip || bucket.lorentzianFlip,
  };
}

// Builds 4H or 1D candles from hourly rows because the API only serves 1H bars.
function aggregateRows(rows, hours) {
  if (hours === 1) return rows;
  const bucketSeconds = hours * 3600;
  const buckets = new Map();
  rows.forEach(row => {
    const bucketTime = Math.floor(row.time / bucketSeconds) * bucketSeconds;
    const existing = buckets.get(bucketTime);
    buckets.set(bucketTime, existing ? mergeRowIntoBucket(existing, row) : { ...row, time: bucketTime });
  });
  return Array.from(buckets.values());
}

// Reads which timeframe button (1H / 4H / 1D) is selected on the dashboard.
function getSelectedTimeframe() {
  const activeButton = document.querySelector('#page-dashboard .tog:not(.asset-tog) .tog-btn.active');
  return activeButton ? activeButton.textContent.trim() : '1H';
}

// Colours a volume bar green when price rose and red when it fell.
function toVolumeBar(row) {
  const color = row.close >= row.open ? 'rgba(0,255,157,.32)' : 'rgba(255,68,102,.32)';
  return { time: row.time, value: row.volume, color };
}

// Builds one buy/sell arrow for the chart.
function makeMarker(time, direction, label, color) {
  const isBuy = direction === 'buy';
  return {
    time, color,
    position: isBuy ? 'belowBar' : 'aboveBar',
    shape: isBuy ? 'arrowUp' : 'arrowDown',
    text: `${label} ${direction.toUpperCase()}`,
  };
}

// Turns stop flips and Lorentzian flips into chart markers, oldest first as the chart requires.
function buildFlipMarkers(rows) {
  const markers = [];
  rows.forEach(row => {
    if (row.stopFlip) markers.push(makeMarker(row.time, row.stopFlip, 'STOP', '#00D4FF'));
    if (row.lorentzianFlip) markers.push(makeMarker(row.time, row.lorentzianFlip, 'ANN', '#FFD700'));
  });
  return markers;
}

// Sends one batch of rows to every series on the price chart.
function pushRowsToChart(rows) {
  const candles = rows.map(({ time, open, high, low, close }) => ({ time, open, high, low, close }));
  tvCandleSeries.setData(candles);
  tvVolumeSeries.setData(rows.map(toVolumeBar));
  stopLineSeries.setData(rows.filter(row => Number.isFinite(row.stop)).map(row => ({ time: row.time, value: row.stop })));
  kernelLineSeries.setData(rows.filter(row => Number.isFinite(row.kernel)).map(row => ({ time: row.time, value: row.kernel })));
  tvCandleSeries.setMarkers(buildFlipMarkers(rows));
  tvData = candles;
  tvLastCandle = { ...candles[candles.length - 1] };
}

// Adds the two overlay lines (adaptive stop and Lorentzian kernel) to a freshly created chart.
function addOverlayLines() {
  const lineStyle = { lineWidth: 1, lastValueVisible: false, priceLineVisible: false };
  stopLineSeries = tvPriceChart.addLineSeries({ ...lineStyle, color: '#00D4FF' });
  kernelLineSeries = tvPriceChart.addLineSeries({ ...lineStyle, color: '#FFD700' });
}

// Stops the demo random-walk timer because real bars now come from the API.
function stopDemoFeed() {
  if (tvTimer) {
    clearInterval(tvTimer);
    tvTimer = null;
  }
}

// Shows the real EUR/USD close and last-hour change in the top ticker.
function updateEurusdTicker(hourlyRows) {
  const last = hourlyRows[hourlyRows.length - 1];
  const previous = hourlyRows[hourlyRows.length - 2];
  const changePct = ((last.close - previous.close) / previous.close) * 100;
  setText('#t0', last.close.toFixed(4));
  setText('#c0', formatSignedPercent(changePct));
  document.getElementById('c0').className = 't-chg ' + (changePct >= 0 ? 'up' : 'down');
}

// Fetches real bars and draws them; only rebuilds the chart when asset or timeframe changed.
async function drawEurusdFromApi() {
  const barsResponse = await getForecastApi('/bars?limit=600');
  const hourlyRows = toRows(barsResponse);
  const timeframe = getSelectedTimeframe();
  const chartKey = `EURUSD|${timeframe}`;
  if (chartKey !== lastBuiltChartKey || !stopLineSeries) {
    originalBuildPriceChart();
    stopDemoFeed();
    addOverlayLines();
    lastBuiltChartKey = chartKey;
  }
  pushRowsToChart(aggregateRows(hourlyRows, HOURS_PER_TIMEFRAME[timeframe]));
  updateEurusdTicker(hourlyRows);
  updatePatternUI(tvData);
  setText('#priceFeedStatus', 'API · ' + barsResponse.bars[barsResponse.bars.length - 1].t.slice(0, 16).replace('T', ' ') + ' UTC');
}

// Replaces the demo chart builder: real data for EUR/USD, the original builder for BTC and ETH.
async function buildPriceChartLive() {
  if (activeAsset !== 'EURUSD') {
    stopLineSeries = kernelLineSeries = null;
    lastBuiltChartKey = '';
    originalBuildPriceChart();
    return;
  }
  try {
    await drawEurusdFromApi();
  } catch (error) {
    originalBuildPriceChart();
    reportForecastApiOffline(error);
  }
}

// ───────────────────────── SIGNAL BAR + FORECAST PANEL ─────────────────────────

// Picks the 4h band edge that works as a target for a BUY or SELL signal.
function pickTargetPrice(action, fourHourForecast) {
  if (action === 'BUY') return fourHourForecast.price_p90;
  if (action === 'SELL') return fourHourForecast.price_p10;
  return null;
}

// Fills the signal bar under the chart from the API's real stop and signal output.
function renderTradeSignal(prediction) {
  const { signal, trailing_stop: stop, lorentzian } = prediction;
  const target = pickTargetPrice(signal.action, findForecast(prediction, 4));
  const colors = { BUY: 'var(--green)', SELL: 'var(--red)', HOLD: 'var(--gold)' };
  const label = document.getElementById('tradeSignal');
  label.textContent = signal.action;
  label.style.color = colors[signal.action];
  document.getElementById('signalDot').className = 'signal-dot ' + { BUY: 'buy', SELL: 'sell', HOLD: 'wait' }[signal.action];
  setText('#signalConfidence', Math.round(lorentzian.confidence * 100) + '%');
  setText('#signalEntry', prediction.last_close.toFixed(5));
  setText('#signalSL', stop.level.toFixed(5));
  setText('#signalTP', target ? target.toFixed(5) : '—');
  const verdict = signal.edge_verified ? 'edge verified' : 'no verified edge · research prototype';
  setText('#signalNote', `${signal.basis.replace(/[_+]/g, ' ')} · ${verdict}`);
}

// Fills the "Next 1h Forecast" card with the real P10 / P50 / P90 band and stop.
function renderForecastPanel(prediction) {
  const oneHour = findForecast(prediction, 1);
  const bandValues = document.querySelectorAll('#page-dashboard .fc-val');
  [oneHour.price_p10, oneHour.price_p50, oneHour.price_p90].forEach((price, index) => {
    bandValues[index].textContent = price.toFixed(5);
  });
  const rows = document.querySelectorAll('#page-dashboard .card.c-purple .pr');
  const stop = prediction.trailing_stop;
  const regime = (stop.key_multiplier - KEY_MULTIPLIER_RANGE.min) / (KEY_MULTIPLIER_RANGE.max - KEY_MULTIPLIER_RANGE.min);
  setProgressRow(rows[0], `${stop.atr_pips.toFixed(1)} pips`, regime * 100);
  setProgressRow(rows[1], Math.round(prediction.lorentzian.confidence * 100) + '%', prediction.lorentzian.confidence * 100);
  setProgressRow(rows[2], stop.level.toFixed(5), 55);
}

// Updates one label/bar/value row used in the small dashboard cards.
function setProgressRow(row, valueText, percent) {
  row.querySelector('.pr-val').textContent = valueText;
  row.querySelector('.pr-fill').style.width = Math.max(0, Math.min(100, percent)) + '%';
}

// Replaces the demo signal logic: EUR/USD uses the API signal, other assets keep the demo logic.
function updateTradeSignalForActiveAsset(candles) {
  if (activeAsset !== 'EURUSD') {
    originalUpdateTradeSignalUI(candles);
    return;
  }
  if (latestPrediction) renderTradeSignal(latestPrediction);
}

// Loads the newest prediction and refreshes every panel that shows it.
async function refreshLatestPrediction() {
  latestPrediction = await getForecastApi('/latest');
  renderForecastPanel(latestPrediction);
  renderTradeSignal(latestPrediction);
}

// Colours a coverage number: green near the 80% target, gold otherwise.
function coverageColor(coverage) {
  return coverage >= 0.7 && coverage <= 0.9 ? 'var(--green)' : 'var(--gold)';
}

// Shows live P10–P90 band coverage in the first dashboard KPI (replaces the made-up accuracy).
function renderCoverageKpi(report) {
  const oneHour = report.horizons['1'];
  const kpi = document.querySelector('#page-dashboard .g4 .kpi');
  const percent = oneHour ? oneHour.band_coverage_p10_p90 * 100 : 0;
  kpi.querySelector('.kpi-label').textContent = 'Band Coverage (1h)';
  kpi.querySelector('.kpi-val').textContent = oneHour ? percent.toFixed(1) + '%' : '—';
  kpi.querySelector('.kpi-sub').textContent = oneHour ? 'P10–P90 · target 80% · forward test' : 'No resolved forecasts yet';
  kpi.querySelector('.kpi-fill').style.width = percent + '%';
}

// ───────────────────────── FORECAST PAGE ─────────────────────────

// Reads the horizon (1, 4 or 24 hours) chosen in the forecast page dropdown.
function getSelectedHorizonHours() {
  const dropdown = document.querySelector('#page-forecast .ph select');
  return HORIZON_HOURS[dropdown.selectedIndex] || 1;
}

// Fills the four forecast KPIs for the selected horizon.
function renderForecastPage(prediction) {
  const forecast = findForecast(prediction, getSelectedHorizonHours());
  const values = document.querySelectorAll('#page-forecast .g4 .kpi-val');
  values[0].textContent = forecast.price_p10.toFixed(5);
  values[1].textContent = forecast.price_p50.toFixed(5);
  values[2].textContent = forecast.price_p90.toFixed(5);
  values[3].textContent = prediction.trailing_stop.level.toFixed(5);
  setText('#page-forecast .ps', `${forecast.model} · model ${prediction.model_version}`);
}

// Builds a two-point line that fans out from the last close to a forecast value.
function buildFanSeries(closes, forecastValue) {
  const series = new Array(closes.length + 1).fill(null);
  series[closes.length - 1] = closes[closes.length - 1];
  series[closes.length] = forecastValue;
  return series;
}

// Makes one line in the forecast chart.
function makeFanDataset(label, data, color, fillTarget) {
  return {
    label, data, borderColor: color, borderWidth: 2, pointRadius: 2, tension: 0.2,
    fill: fillTarget, backgroundColor: 'rgba(0,212,255,.09)',
  };
}

// Draws recent closes plus the P10 / P50 / P90 fan for the chosen horizon.
async function drawForecastBandChart() {
  const barsResponse = await getForecastApi('/bars?limit=48');
  const hours = getSelectedHorizonHours();
  const forecast = findForecast(latestPrediction, hours);
  const closes = barsResponse.bars.map(bar => bar.c);
  const labels = barsResponse.bars.map(bar => bar.t.slice(11, 16)).concat(`+${hours}h`);
  destroyChart('forecast');
  charts['forecast'] = new Chart(document.getElementById('forecastChart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        makeFanDataset('P90', buildFanSeries(closes, forecast.price_p90), '#00FF9D', '+1'),
        makeFanDataset('P50', buildFanSeries(closes, forecast.price_p50), '#00D4FF', false),
        makeFanDataset('P10', buildFanSeries(closes, forecast.price_p10), '#FF4466', '-1'),
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

// Shows the real top features of the 4h model instead of the demo numbers.
async function renderFeatureImportance() {
  const features = await getForecastApi('/model/importance?top=5');
  const rows = document.querySelectorAll('#page-forecast .g2 .card')[0].querySelectorAll('.pr');
  features.forEach((feature, index) => {
    rows[index].querySelector('.pr-lbl').textContent = feature.name;
    rows[index].querySelector('.pr-fill').style.width = feature.weight * 100 + '%';
    rows[index].querySelector('.pr-val').textContent = feature.weight.toFixed(2);
  });
}

// Replaces the fake "horizon accuracy" with the measured band coverage per horizon.
function renderCoverageCard(report) {
  const card = document.querySelectorAll('#page-forecast .g2 .card')[1];
  card.querySelector('.ct').textContent = 'Live Band Coverage (target 80%)';
  card.querySelectorAll('.pr').forEach((row, index) => {
    const entry = report.horizons[String(HORIZON_HOURS[index])];
    const coverage = entry ? entry.band_coverage_p10_p90 : 0;
    row.querySelector('.pr-lbl').textContent = `${HORIZON_HOURS[index]}h P10–P90 band`;
    row.querySelector('.pr-fill').style.width = coverage * 100 + '%';
    row.querySelector('.pr-fill').style.background = coverageColor(coverage);
    row.querySelector('.pr-val').textContent = entry ? Math.round(coverage * 100) + '%' : '—';
  });
  card.querySelector('p').textContent = '⚠ Share of realised prices that landed inside the band. Calibration only — no trading edge implied.';
}

// Builds the whole Forecast page from live data.
async function buildForecastPageLive() {
  try {
    if (!latestPrediction) await refreshLatestPrediction();
    renderForecastPage(latestPrediction);
    const report = await getForecastApi('/forward-test');
    renderCoverageCard(report);
    await Promise.all([drawForecastBandChart(), renderFeatureImportance()]);
  } catch (error) {
    reportForecastApiOffline(error);
  }
}

// Replaces the fake /predict demo with a real call to GET /latest.
async function runLivePrediction() {
  const badge = document.getElementById('predict-status');
  const responseBox = document.getElementById('api-resp');
  badge.textContent = 'Calling API…';
  badge.className = 'badge b-gold';
  try {
    const prediction = await getForecastApi('/latest');
    responseBox.textContent = JSON.stringify(prediction, null, 2);
    responseBox.style.cssText = 'display:block;white-space:pre;overflow-x:auto';
    badge.textContent = '200 OK';
    badge.className = 'badge b-green';
  } catch (error) {
    badge.textContent = 'Offline';
    badge.className = 'badge b-red';
  }
}

// Renames the demo "POST /predict" labels so they describe the real call.
function relabelForecastEndpoint() {
  setText('#page-forecast .card.c-purple .ct', '⚡ GET /latest · Live Response');
  setText('#page-forecast .ph .btn-solid', '▶ Fetch /latest');
  document.querySelector('#page-forecast .terminal').innerHTML =
    `<div><span class="t-prompt">$</span> <span class="t-cmd">curl ${escapeHtml(FMIP_CONFIG.forecastApiUrl)}/latest</span></div>`;
}

// ───────────────────────── ANOMALY DETECTOR ─────────────────────────

// Picks the coloured chip that matches an anomaly status.
function statusChipHtml(status) {
  if (status === 'Anomaly') return '<span class="sig sig-orange">ANOMALY</span>';
  if (status === 'Watch') return '<span class="sig sig-gold">WATCH</span>';
  return '<span class="sig sig-green">NORMAL</span>';
}

// Builds one row of the dashboard alert table.
function buildAlertRowHtml(row) {
  const changeClass = row.price_change_pct >= 0 ? 'up' : 'down';
  return `<tr>
    <td style="color:var(--white);font-weight:600">${escapeHtml(row.symbol)}</td>
    <td style="color:var(--mid)">${escapeHtml(row.trigger)}</td>
    <td style="font-family:var(--mono);color:var(--red)">${row.score.toFixed(2)}</td>
    <td class="${changeClass}" style="font-family:var(--mono)">${formatSignedPercent(row.price_change_pct)}</td>
    <td>${statusChipHtml(row.status)}</td></tr>`;
}

// Builds one row of the detailed "Flagged Stocks" table on the Anomaly page.
function buildDetailRowHtml(row) {
  const changeClass = row.price_change_pct >= 0 ? 'up' : 'down';
  const symbol = escapeHtml(row.symbol);
  return `<tr>
    <td style="color:var(--white);font-weight:700">${symbol}</td>
    <td style="font-family:var(--mono);color:var(--red)">${row.score.toFixed(2)}</td>
    <td style="color:var(--mid)">${escapeHtml(row.trigger)}</td>
    <td class="${changeClass}" style="font-family:var(--mono)">${formatSignedPercent(row.price_change_pct)}</td>
    <td style="font-family:var(--mono);color:var(--orange)">${row.volume_ratio.toFixed(1)}×</td>
    <td>${statusChipHtml(row.status)}</td>
    <td><button class="btn btn-c" style="padding:3px 8px;font-size:10px" onclick="investigateStock('${symbol}')">Investigate</button></td></tr>`;
}

// Paints the heatmap with the last 60 scored rows, coloured by severity.
function renderHeatmapCells(cells) {
  const grid = document.getElementById('heatmap');
  grid.innerHTML = '';
  cells.forEach(cell => {
    const square = document.createElement('div');
    square.className = 'hm-cell';
    square.style.background = HEATMAP_COLORS[cell.level];
    square.title = `${cell.symbol} · ${cell.date} · score ${cell.score.toFixed(2)}`;
    grid.appendChild(square);
  });
}

// Shows the four strongest features behind the anomalies.
function renderFeatureWeights(weights) {
  const card = document.querySelectorAll('#page-anomaly .row .card')[1];
  card.querySelectorAll('.pr').forEach((row, index) => {
    const feature = weights[index];
    if (!feature) return;
    row.querySelector('.pr-lbl').textContent = FEATURE_LABELS[feature.name] || feature.name;
    row.querySelector('.pr-fill').style.width = feature.weight * 100 + '%';
    row.querySelector('.pr-val').textContent = feature.weight.toFixed(2);
  });
}

// Fills the four KPI tiles and every anomaly counter in the UI.
function renderAnomalyCounters(scan) {
  const values = document.querySelectorAll('#page-anomaly .g4 .kpi-val');
  values[0].textContent = scan.symbol_count;
  values[1].textContent = scan.anomaly_count;
  values[2].textContent = scan.anomaly_rate_pct.toFixed(1) + '%';
  values[3].textContent = scan.contamination.toFixed(2);
  const labels = document.querySelectorAll('#page-anomaly .g4 .kpi-label');
  labels[2].textContent = 'Anomaly Rate';
  document.querySelectorAll('#page-anomaly .g4 .kpi-sub')[2].textContent = `Of ${scan.total_rows} rows scanned`;
  setText('#page-anomaly .ph .badge', `${scan.anomaly_count} Anomalies Active`);
  setText('.nav-badge', scan.anomaly_count);
  setText('#kv1', scan.anomaly_count);
  setText('#page-dashboard .badge.b-orange', `${scan.anomaly_count} Active`);
}

// Updates both anomaly tables and the heatmap from one scan result.
function renderAnomalyTables(scan) {
  const flaggedRows = scan.ranked_rows.filter(row => row.status !== 'Normal');
  const emptyRow = '<tr><td colspan="7" style="color:var(--mid)">No anomalies flagged</td></tr>';
  document.querySelector('#page-dashboard table tbody').innerHTML = scan.ranked_rows.slice(0, 5).map(buildAlertRowHtml).join('');
  document.querySelector('#page-anomaly table tbody').innerHTML = flaggedRows.map(buildDetailRowHtml).join('') || emptyRow;
  renderHeatmapCells(scan.heatmap);
  renderFeatureWeights(scan.feature_weights);
}

// Loads the anomaly scan from the forecast API and renders it everywhere.
async function loadAnomalyData() {
  const scan = await getForecastApi('/anomaly/scan');
  renderAnomalyCounters(scan);
  renderAnomalyTables(scan);
}

// ───────────────────────── SENTIMENT + PUMP-AND-DUMP ─────────────────────────

// Builds one coloured progress row for the pump signal card.
function buildProgressRowHtml(label, percent, color) {
  return `<div class="pr"><div class="pr-lbl" style="min-width:100px;font-size:11px">${label}</div>
    <div class="pr-track"><div class="pr-fill" style="width:${percent}%;background:${color}"></div></div>
    <div class="pr-val" style="font-size:11px;color:${color}">${percent}%</div></div>`;
}

// Draws the red pump card for the most suspicious group, or an all-clear message.
function renderPumpSignalCard(signal) {
  const card = document.querySelector('#page-sentiment .card.c-red');
  if (!signal) {
    card.innerHTML = '<div class="ch"><div class="ct">🚨 Pump Signal</div><span class="badge b-green">CLEAR</span></div><p style="font-size:12px;color:var(--mid)">No group crossed the pump threshold.</p>';
    return;
  }
  const probability = Math.round(signal.max_probability * 100);
  const velocity = Math.min(100, signal.peak_velocity * VELOCITY_PERCENT_PER_MESSAGE);
  const duplicates = Math.min(100, signal.peak_duplicates * DUPLICATE_PERCENT_PER_MESSAGE);
  const isHigh = signal.max_probability >= HIGH_CONFIDENCE_PROBABILITY;
  card.innerHTML = `<div class="ch"><div class="ct">🚨 ${isHigh ? 'HIGH ' : ''}Pump Signal</div><span class="badge b-red">${isHigh ? 'ACTIVE' : 'WATCH'}</span></div>
    <div style="background:rgba(255,68,102,.08);border:1px solid rgba(255,68,102,.3);border-radius:8px;padding:12px;margin-bottom:12px">
      <div style="font-size:14px;font-weight:700;color:var(--red)">${escapeHtml(signal.group_id)}</div>
      <div style="font-size:11px;color:var(--mid);margin-top:2px">${signal.pump_messages} of ${signal.total_messages} messages flagged</div>
      <div style="font-size:11px;color:var(--light);margin-top:6px;font-style:italic">“${escapeHtml(signal.sample_text)}”</div></div>
    ${buildProgressRowHtml('Pump probability', probability, 'var(--red)')}
    ${buildProgressRowHtml('Post velocity', velocity, 'var(--orange)')}
    ${buildProgressRowHtml('Near-duplicates', duplicates, 'var(--orange)')}
    <p style="font-size:10px;color:var(--mid);font-style:italic;margin-top:8px">Suspicious pattern — not confirmed fraud.</p>`;
}

// Fills the three sentiment KPI tiles.
function renderSentimentKpis(shares) {
  const values = document.querySelectorAll('#page-sentiment .g3 .kpi-val');
  const fills = document.querySelectorAll('#page-sentiment .g3 .kpi-fill');
  ['positive', 'neutral', 'negative'].forEach((label, index) => {
    values[index].textContent = Math.round(shares[label]) + '%';
    fills[index].style.width = shares[label] + '%';
  });
}

// Builds one dataset for the stacked sentiment chart.
function makeSentimentDataset(label, data, rgb, borderColor) {
  return { label, data, backgroundColor: `rgba(${rgb},.45)`, borderColor, borderWidth: 1, borderRadius: 4 };
}

// Draws the daily sentiment mix as stacked bars.
function drawSentimentTrendChart(trend) {
  destroyChart('sentiment');
  charts['sentiment'] = new Chart(document.getElementById('sentimentChart'), {
    type: 'bar',
    data: {
      labels: trend.labels,
      datasets: [
        makeSentimentDataset('Positive', trend.series.positive, '0,255,157', '#00FF9D'),
        makeSentimentDataset('Neutral', trend.series.neutral, '110,136,176', '#6E88B0'),
        makeSentimentDataset('Negative', trend.series.negative, '255,68,102', '#FF4466'),
      ],
    },
    options: {
      responsive: true, plugins: { legend: { position: 'top' } },
      scales: { x: { stacked: true, grid: { color: '#1A2C5A' } }, y: { stacked: true, max: 100, grid: { color: '#1A2C5A' } } },
    },
  });
}

// Fills the hype keyword cloud, sizing each word by how often it appears.
function renderKeywordCloud(keywords) {
  const cloud = document.getElementById('tag-cloud');
  cloud.innerHTML = '';
  const maxCount = Math.max(1, ...keywords.map(keyword => keyword.count));
  keywords.forEach((keyword, index) => {
    const tag = document.createElement('div');
    const color = KEYWORD_COLORS[index % KEYWORD_COLORS.length];
    tag.className = 'tag';
    tag.textContent = `${keyword.keyword.toUpperCase()} · ${keyword.count}`;
    tag.style.cssText = `font-size:${11 + Math.round((7 * keyword.count) / maxCount)}px;color:${color};border-color:${color}`;
    cloud.appendChild(tag);
  });
}

// Downloads the classifier summary and updates the pump KPI on the dashboard.
async function fetchSentimentSummary() {
  sentimentSummary = await getClassifierApi('/dashboard/summary');
  const highSignals = sentimentSummary.pump_signals.filter(signal => signal.max_probability >= HIGH_CONFIDENCE_PROBABILITY);
  setText('#kv2', highSignals.length);
  return sentimentSummary;
}

// Draws the full Sentiment & Pump page from the classifier summary.
function renderSentimentPage(summary) {
  const highCount = summary.pump_signals.filter(signal => signal.max_probability >= HIGH_CONFIDENCE_PROBABILITY).length;
  setText('#page-sentiment .ph .badge', `🚨 ${highCount} High-Confidence Signal${highCount === 1 ? '' : 's'}`);
  setText('#page-sentiment .ps', `TF-IDF + LR · Random Forest · data: ${summary.data_source}`);
  renderSentimentKpis(summary.sentiment);
  drawSentimentTrendChart(summary.trend);
  renderPumpSignalCard(summary.pump_signals[0]);
  renderKeywordCloud(summary.top_keywords);
}

// Replaces the demo sentiment chart builder with the live one.
async function buildSentimentPageLive() {
  addMessageAnalyzerCard();
  try {
    renderSentimentPage(await fetchSentimentSummary());
  } catch (error) {
    markClassifierOffline(error);
  }
}

// Adds a small box where the user can paste any message and see the classifier's verdict.
function addMessageAnalyzerCard() {
  if (document.getElementById('analyzer-card')) return;
  const card = document.createElement('div');
  card.className = 'card c-cyan';
  card.id = 'analyzer-card';
  card.innerHTML = `<div class="ch"><div class="ct">🧪 Analyze a Message</div></div>
    <textarea id="analyzer-text" rows="3" placeholder="Paste a Telegram / social post…" style="width:100%;background:var(--bg3);border:1px solid var(--border);border-radius:7px;color:var(--white);padding:8px;font-family:var(--font);font-size:12px"></textarea>
    <div style="display:flex;gap:10px;align-items:center;margin-top:8px">
      <button class="btn btn-solid" onclick="analyzeMessage()">Analyze</button>
      <span id="analyzer-result" style="font-size:12px;color:var(--light)"></span></div>`;
  document.getElementById('page-sentiment').appendChild(card);
}

// Sends the pasted message to the classifier and prints sentiment plus pump probability.
async function analyzeMessage() {
  const text = document.getElementById('analyzer-text').value.trim();
  const output = document.getElementById('analyzer-result');
  if (!text) return;
  try {
    const body = JSON.stringify({ messages: [{ text, timestamp: new Date().toISOString(), group_id: 'manual' }] });
    const reply = await getClassifierApi('/pump/predict', { method: 'POST', body });
    const result = reply.results[0];
    const verdict = result.is_pump ? 'PUMP SIGNAL' : 'normal';
    output.textContent = `Sentiment: ${result.sentiment} · Pump probability ${Math.round(result.pump_probability * 100)}% → ${verdict} (single message, no burst context)`;
  } catch (error) {
    output.textContent = 'Classifier service unreachable on ' + FMIP_CONFIG.classifierApiUrl;
  }
}

// ───────────────────────── PAGE WIRING ─────────────────────────

// Keeps the real EUR/USD ticker value while the demo jitter moves the other tickers.
function keepEurusdTickerReal() {
  const priceElement = document.getElementById('t0');
  const changeElement = document.getElementById('c0');
  const saved = { price: priceElement.textContent, change: changeElement.textContent, style: changeElement.className };
  originalJitterTicker();
  priceElement.textContent = saved.price;
  changeElement.textContent = saved.change;
  changeElement.className = saved.style;
}

// Loads every live panel on the dashboard; each service fails independently.
function loadDashboardData() {
  refreshLatestPrediction().catch(reportForecastApiOffline);
  getForecastApi('/forward-test').then(renderCoverageKpi).catch(reportForecastApiOffline);
  loadAnomalyData().catch(error => console.warn('Anomaly scan problem:', error.message));
  fetchSentimentSummary().catch(markClassifierOffline);
}

// Refresh button: pull new bars from Dukascopy when possible, then reload everything.
async function syncAndReload() {
  try {
    await getForecastApi('/sync', { method: 'POST' });
  } catch (error) {
    console.warn('Sync skipped:', error.message);
  }
  lastBuiltChartKey = '';
  await buildPriceChartLive();
  loadDashboardData();
}

// Opens a page and loads the anomaly data when that page is selected.
function goAndLoad(pageId, navElement) {
  originalGo(pageId, navElement);
  if (pageId === 'anomaly') loadAnomalyData().catch(error => console.warn('Anomaly scan problem:', error.message));
}

// Starts the demo initialisation, then adds live data loading and its refresh timer.
function initAllLive() {
  originalInitAll();
  relabelForecastEndpoint();
  loadDashboardData();
  setInterval(loadDashboardData, DASHBOARD_REFRESH_MS);
}

window.buildPriceChart = buildPriceChartLive;
window.updateTradeSignalUI = updateTradeSignalForActiveAsset;
window.buildForecastChart = buildForecastPageLive;
window.buildSentimentChart = buildSentimentPageLive;
window.runPredict = runLivePrediction;
window.refreshDash = syncAndReload;
window.jitterTicker = keepEurusdTickerReal;
window.go = goAndLoad;
window.initAll = initAllLive;