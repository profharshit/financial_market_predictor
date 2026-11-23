// FILE: financial-market-project/ui/static/js/fmipIndicators.js
// (new file, same folder as fmipLive.js; served automatically at /ui/static/js/fmipIndicators.js)

const LAB_API_URL = window.location.protocol.startsWith('http') ? window.location.origin : 'http://127.0.0.1:8000';
const LAB_API_KEY = window.FMIP_API_KEY || localStorage.getItem('fmipApiKey') || '';
const LAB_REQUEST_TIMEOUT_MS = 15000;
const LAB_REFRESH_MS = 60000;
const LAB_COLORS = {
  stopUp: '#16c784', stopDown: '#f05262', kernel: '#FFD700',
  stopMarker: '#38bdf8', annMarker: '#FFD700',
  bandLow: '#f05262', bandMiddle: '#38bdf8', bandHigh: '#16c784',
};
const LAYER_CHECKBOX_IDS = [
  'lab-show-stop', 'lab-show-kernel', 'lab-show-stop-flips', 'lab-show-ann-flips', 'lab-show-band',
];

let labChart = null;
let labCandleSeries = null;
let labStopSeries = null;
let labKernelSeries = null;
let labForecastLines = [];
let labRows = [];
let labLatestPrediction = null;
let labRefreshTimer = null;
let labFitNextDraw = true;

// ───────────────────────── HTTP + SMALL HELPERS ─────────────────────────

// One place for HTTP so every call sends the key and times out the same way.
async function callForecastApi(path) {
  const headers = LAB_API_KEY ? { 'X-API-Key': LAB_API_KEY } : {};
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), LAB_REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(LAB_API_URL + path, { headers, signal: controller.signal });
    if (response.status === 401) throw new Error('API key missing or wrong');
    if (!response.ok) throw new Error(`${path} failed with status ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

// Writes text into an element by id and ignores ids that are not on the page.
function setLabText(elementId, text) {
  const element = document.getElementById(elementId);
  if (element) element.textContent = text;
}

// Fills many elements at once from an { elementId: text } object.
function fillLabFields(textById) {
  Object.entries(textById).forEach(([elementId, text]) => setLabText(elementId, text));
}

// Shows prices with five decimals, the usual EUR/USD precision.
function formatPrice(value) {
  return Number.isFinite(value) ? value.toFixed(5) : '—';
}

// Capitalises a word like "up" so it reads nicely in a card.
function capitalise(word) {
  return word ? word.charAt(0).toUpperCase() + word.slice(1) : '—';
}

// Prints a buy/sell flip, or "None" when no flip happened on that bar.
function describeFlip(flip) {
  return flip ? flip.toUpperCase() : 'None';
}

// Finds the forecast for one horizon (1, 4 or 24 hours) inside a prediction.
function findHorizonForecast(prediction, horizonHours) {
  return prediction.forecasts.find(forecast => forecast.horizon_bars === horizonHours);
}

// Reads the horizon chosen in the dropdown above the chart.
function getSelectedHorizon() {
  return Number(document.getElementById('lab-horizon').value);
}

// Reads one layer checkbox, treating a missing checkbox as "on".
function isLayerOn(checkboxId) {
  const checkbox = document.getElementById(checkboxId);
  return checkbox ? checkbox.checked : true;
}

// ───────────────────────── CHART SETUP ─────────────────────────

// Builds the empty chart with the same dark theme as the rest of the dashboard.
function createChartShell(container) {
  return LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight,
    layout: { background: { color: '#111823' }, textColor: '#8995a8', fontSize: 11 },
    grid: { vertLines: { color: '#1b2533' }, horzLines: { color: '#1b2533' } },
    rightPriceScale: { borderColor: '#242e3b' },
    timeScale: { borderColor: '#242e3b', timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
}

// Adds the candles plus the two overlay lines (adaptive stop and Lorentzian kernel).
function addChartSeries() {
  labCandleSeries = labChart.addCandlestickSeries({
    upColor: '#16c784', downColor: '#f05262', borderVisible: false,
    wickUpColor: '#16c784', wickDownColor: '#f05262',
    priceFormat: { type: 'price', precision: 5, minMove: 0.00001 },
  });
  const lineStyle = { lineWidth: 2, lastValueVisible: false, priceLineVisible: false };
  labStopSeries = labChart.addLineSeries({ ...lineStyle, color: LAB_COLORS.stopUp });
  labKernelSeries = labChart.addLineSeries({ ...lineStyle, lineWidth: 1, color: LAB_COLORS.kernel });
}

// Keeps the chart the same size as its card when the window changes.
function keepChartSized(container) {
  if (!window.ResizeObserver) return;
  new ResizeObserver(() => {
    if (container.clientWidth === 0) return;
    labChart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
  }).observe(container);
}

// Creates chart, series and resizing once, the first time the page opens.
function buildLabChart() {
  const container = document.getElementById('lab-chart');
  container.replaceChildren();
  labChart = createChartShell(container);
  addChartSeries();
  keepChartSized(container);
}

// ───────────────────────── DATA → CHART ─────────────────────────

// Merges the API's parallel arrays (bars, stop, lorentzian) into one row per hourly bar.
function buildChartRows(barsResponse) {
  return barsResponse.bars.map((bar, index) => ({
    time: Math.floor(new Date(bar.t).getTime() / 1000),
    open: bar.o, high: bar.h, low: bar.l, close: bar.c,
    stopLevel: barsResponse.stop[index]?.level,
    stopTrend: barsResponse.stop[index]?.trend,
    stopFlip: barsResponse.stop[index]?.flip,
    kernel: barsResponse.lorentzian[index]?.kernel,
    lorentzianFlip: barsResponse.lorentzian[index]?.flip,
  }));
}

// Colours each stop point green in an up-trend and red in a down-trend.
function toStopPoint(row) {
  const color = row.stopTrend === 'up' ? LAB_COLORS.stopUp : LAB_COLORS.stopDown;
  return { time: row.time, value: row.stopLevel, color };
}

// Builds one chart arrow for a buy or sell flip.
function makeFlipMarker(time, direction, label, color) {
  const isBuy = direction === 'buy';
  return {
    time, color,
    position: isBuy ? 'belowBar' : 'aboveBar',
    shape: isBuy ? 'arrowUp' : 'arrowDown',
    text: `${label} ${direction.toUpperCase()}`,
  };
}

// Collects flip arrows for the layers that are switched on, oldest first as the chart requires.
function buildFlipMarkers() {
  const markers = [];
  labRows.forEach(row => {
    if (row.stopFlip && isLayerOn('lab-show-stop-flips')) {
      markers.push(makeFlipMarker(row.time, row.stopFlip, 'STOP', LAB_COLORS.stopMarker));
    }
    if (row.lorentzianFlip && isLayerOn('lab-show-ann-flips')) {
      markers.push(makeFlipMarker(row.time, row.lorentzianFlip, 'ANN', LAB_COLORS.annMarker));
    }
  });
  return markers;
}

// Removes the old P10/P50/P90 lines before new ones are drawn.
function clearForecastLines() {
  labForecastLines.forEach(line => labCandleSeries.removePriceLine(line));
  labForecastLines = [];
}

// Adds one dashed horizontal line for a forecast price.
function addForecastLine(price, title, color) {
  const line = labCandleSeries.createPriceLine({
    price, title, color, lineWidth: 1, axisLabelVisible: true,
    lineStyle: LightweightCharts.LineStyle.Dashed,
  });
  labForecastLines.push(line);
}

// Draws the P10/P50/P90 band of the chosen horizon when that layer is on.
function drawForecastLines() {
  if (!labChart) return;
  clearForecastLines();
  if (!labLatestPrediction || !isLayerOn('lab-show-band')) return;
  const forecast = findHorizonForecast(labLatestPrediction, getSelectedHorizon());
  if (!forecast) return;
  addForecastLine(forecast.price_p10, 'P10', LAB_COLORS.bandLow);
  addForecastLine(forecast.price_p50, 'P50', LAB_COLORS.bandMiddle);
  addForecastLine(forecast.price_p90, 'P90', LAB_COLORS.bandHigh);
}

// Shows or hides each layer to match the checkboxes without refetching data.
function applyLayerVisibility() {
  if (!labChart) return;
  labStopSeries.applyOptions({ visible: isLayerOn('lab-show-stop') });
  labKernelSeries.applyOptions({ visible: isLayerOn('lab-show-kernel') });
  labCandleSeries.setMarkers(buildFlipMarkers());
  drawForecastLines();
}

// Sends the rows to the candles and both overlay lines, then applies the layer switches.
function drawRowsOnChart() {
  const candles = labRows.map(({ time, open, high, low, close }) => ({ time, open, high, low, close }));
  const stopPoints = labRows.filter(row => Number.isFinite(row.stopLevel)).map(toStopPoint);
  const kernelPoints = labRows
    .filter(row => Number.isFinite(row.kernel))
    .map(row => ({ time: row.time, value: row.kernel }));
  labCandleSeries.setData(candles);
  labStopSeries.setData(stopPoints);
  labKernelSeries.setData(kernelPoints);
  applyLayerVisibility();
}

// Fits all bars on screen only after a bar-count change, so refreshes keep the user's zoom.
function fitChartOnce() {
  if (!labFitNextDraw) return;
  labChart.timeScale().fitContent();
  labFitNextDraw = false;
}

// ───────────────────────── PANELS (TILES + CARDS) ─────────────────────────

// Colours the signal tile: green for BUY, red for SELL, gold for HOLD.
function colourSignalTile(action) {
  const colours = { BUY: '#16c784', SELL: '#f05262', HOLD: '#FFD700' };
  document.getElementById('lab-kpi-signal').style.color = colours[action] || '';
}

// Fills the four headline tiles above the chart.
function renderHeadlineTiles(prediction) {
  const { signal, trailing_stop: stop, lorentzian } = prediction;
  fillLabFields({
    'lab-kpi-signal': signal.action,
    'lab-kpi-signal-sub': signal.basis.replace(/[_+]/g, ' '),
    'lab-kpi-stop': formatPrice(stop.level),
    'lab-kpi-stop-sub': `${capitalise(stop.trend)}-trend stop`,
    'lab-kpi-ann': capitalise(lorentzian.bias),
    'lab-kpi-ann-sub': `${Math.round(lorentzian.confidence * 100)}% confidence`,
    'lab-kpi-edge': signal.edge_verified ? 'Verified' : 'Not verified',
    'lab-kpi-edge-sub': 'Net of costs, holdout backtest',
  });
  colourSignalTile(signal.action);
}

// Fills the adaptive trailing stop card.
function renderStopCard(stop) {
  fillLabFields({
    'lab-stop-trend': capitalise(stop.trend),
    'lab-stop-level': formatPrice(stop.level),
    'lab-stop-distance': `${stop.distance_pips.toFixed(1)} pips`,
    'lab-stop-atr': Number.isFinite(stop.atr_pips) ? `${stop.atr_pips.toFixed(1)} pips` : '—',
    'lab-stop-key': `${stop.key_multiplier.toFixed(2)} × base key 3.0`,
    'lab-stop-flip': describeFlip(stop.flip),
  });
}

// Fills the Lorentzian ANN card.
function renderLorentzianCard(lorentzian) {
  fillLabFields({
    'lab-ann-bias': capitalise(lorentzian.bias),
    'lab-ann-confidence': `${Math.round(lorentzian.confidence * 100)}%`,
    'lab-ann-kernel': formatPrice(lorentzian.kernel_estimate),
    'lab-ann-flip': describeFlip(lorentzian.flip),
    'lab-ann-exit': lorentzian.exit ? capitalise(lorentzian.exit) : 'None',
    'lab-ann-skill': lorentzian.skill_established ? 'Yes, may confirm signals' : 'No, informational only',
  });
}

// Builds one table cell from plain text so API values can never be read as HTML.
function makeTableCell(text) {
  const cell = document.createElement('td');
  cell.textContent = text;
  return cell;
}

// Builds one table row for a forecast horizon.
function makeForecastRow(forecast) {
  const row = document.createElement('tr');
  const values = [
    `${forecast.horizon_bars}h`, formatPrice(forecast.price_p10), formatPrice(forecast.price_p50),
    formatPrice(forecast.price_p90), forecast.band_width_pips.toFixed(1), forecast.model,
  ];
  values.forEach(text => row.appendChild(makeTableCell(text)));
  return row;
}

// Replaces the forecast table body with the latest three horizons.
function renderForecastTable(prediction) {
  const tableBody = document.getElementById('lab-forecast-rows');
  tableBody.replaceChildren(...prediction.forecasts.map(makeForecastRow));
}

// Updates every tile and card from one prediction.
function renderAllPanels(prediction) {
  renderHeadlineTiles(prediction);
  renderStopCard(prediction.trailing_stop);
  renderLorentzianCard(prediction.lorentzian);
  renderForecastTable(prediction);
}

// ───────────────────────── LOADING + PAGE WIRING ─────────────────────────

// Shows the last update time or the failure reason under the chart.
function showLabStatus(message, isError) {
  const status = document.getElementById('lab-status');
  status.textContent = message;
  status.style.color = isError ? '#f05262' : '';
}

// Turns a raw error into a short message that tells the user what to fix.
function describeLabError(error) {
  if (error.message.includes('key')) return 'API key missing or wrong. Open the dashboard through /ui so the key is injected.';
  if (error.message.includes('status 404')) return 'No prediction yet. Run live_feed.py or press Sync on the dashboard.';
  return `Forecast API not reachable at ${LAB_API_URL}. Start it with: uvicorn serve_api:app --port 8000`;
}

// Fetches bars and the latest prediction together, then redraws the chart and panels.
async function loadIndicatorData() {
  try {
    const barCount = document.getElementById('lab-bar-count').value;
    const [barsResponse, prediction] = await Promise.all([
      callForecastApi(`/bars?limit=${barCount}`),
      callForecastApi('/latest'),
    ]);
    labRows = buildChartRows(barsResponse);
    labLatestPrediction = prediction;
    drawRowsOnChart();
    fitChartOnce();
    renderAllPanels(prediction);
    showLabStatus(`Updated · last bar ${prediction.as_of.slice(0, 16).replace('T', ' ')} UTC · model ${prediction.model_version}`, false);
  } catch (error) {
    showLabStatus(describeLabError(error), true);
  }
}

// Refreshes only while the Indicator Lab page is the one on screen.
function refreshWhileVisible() {
  const page = document.getElementById('page-indicators');
  if (page.classList.contains('active') && !document.hidden) loadIndicatorData();
}

// Creates the chart the first time the page opens, because a hidden page has no width.
function openIndicatorsPage() {
  if (!window.LightweightCharts) {
    showLabStatus('Chart library did not load. Check your internet connection.', true);
    return;
  }
  if (!labChart) buildLabChart();
  loadIndicatorData();
  if (!labRefreshTimer) labRefreshTimer = setInterval(refreshWhileVisible, LAB_REFRESH_MS);
}

// Connects the page's button, dropdowns and checkboxes to their actions.
function wireIndicatorControls() {
  document.getElementById('lab-refresh').addEventListener('click', loadIndicatorData);
  document.getElementById('lab-horizon').addEventListener('change', drawForecastLines);
  document.getElementById('lab-bar-count').addEventListener('change', () => {
    labFitNextDraw = true;
    loadIndicatorData();
  });
  LAYER_CHECKBOX_IDS.forEach(checkboxId => {
    document.getElementById(checkboxId).addEventListener('change', applyLayerVisibility);
  });
}

// Wraps the dashboard's page switcher so opening Indicator Lab loads its data.
function hookIntoNavigation() {
  const previousGo = window.go;
  window.go = function (pageId, navElement) {
    previousGo(pageId, navElement);
    if (pageId === 'indicators') openIndicatorsPage();
  };
}

// Starts the Indicator Lab only if its page markup was added to index.html.
function startIndicatorLab() {
  if (!document.getElementById('page-indicators')) return;
  wireIndicatorControls();
  hookIntoNavigation();
}

startIndicatorLab();