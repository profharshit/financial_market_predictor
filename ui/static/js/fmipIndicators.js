// FILE PATH: financial-market-project/ui/static/js/fmipIndicators.js
// ACTION: REPLACE the existing file with this one (same folder as fmipLive.js and paperTrading.js).
// No change is needed in index.html for this file; it is already loaded by the last <script> tag.

const LAB_API_URL = window.location.protocol.startsWith('http') ? window.location.origin : 'http://127.0.0.1:8000';
const LAB_REQUEST_TIMEOUT_MS = 30000;
const LAB_SYNC_TIMEOUT_MS = 90000;
const LAB_REFRESH_MS = 60000;
const SYNC_AFTER_HOURS = 2.5;
const SYNC_COOLDOWN_MS = 5 * 60 * 1000;
const STALE_WARNING_HOURS = 3;
const MILLISECONDS_PER_HOUR = 3600000;
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
let isLabLoading = false;
let lastSyncTime = 0;

// ───────────────────────── HTTP + ERROR HANDLING ─────────────────────────

// Reads the API key at call time because the server may inject it after this script loads.
function getLabApiKey() {
  return window.FMIP_API_KEY || localStorage.getItem('fmipApiKey') || '';
}

// Adds the key header only when a key exists, so open servers still work.
function buildLabHeaders() {
  const apiKey = getLabApiKey();
  return apiKey ? { 'X-API-Key': apiKey } : {};
}

// Creates an error that remembers if it was a network, timeout or HTTP problem.
function makeLabError(kind, status, message) {
  const error = new Error(message);
  error.kind = kind;
  error.status = status;
  return error;
}

// Keeps HTTP errors as they are and labels everything else as timeout or network trouble.
function classifyFetchError(error, path) {
  if (error.kind) return error;
  if (error.name === 'AbortError') return makeLabError('timeout', 0, `${path} took too long to answer`);
  return makeLabError('network', 0, `${path} could not be reached`);
}

// Reads the server's own error text so the user sees the real reason instead of a guess.
async function readServerError(response) {
  const body = await response.json().catch(() => ({}));
  return typeof body.detail === 'string' ? body.detail : '';
}

// The single place that talks to the API, so every failure looks the same to the rest of the code.
async function callForecastApi(path, options = {}, timeoutMs = LAB_REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(LAB_API_URL + path, { ...options, headers: buildLabHeaders(), signal: controller.signal });
    if (!response.ok) {
      const serverText = await readServerError(response);
      throw makeLabError('http', response.status, `${path} failed (status ${response.status}) ${serverText}`.trim());
    }
    return await response.json();
  } catch (error) {
    throw classifyFetchError(error, path);
  } finally {
    clearTimeout(timer);
  }
}

// Asks the open health endpoint whether the server itself is running.
async function isServerAlive() {
  try {
    await callForecastApi('/health', {}, 5000);
    return true;
  } catch (error) {
    return false;
  }
}

// Explains an API failure in plain words so the user knows what to fix.
async function describeApiError(error) {
  if (error.status === 401) return 'API key missing or wrong. Open the dashboard through /ui so the key is injected.';
  if (error.status === 404) return 'No prediction yet. Press Refresh to sync bars, or run live_feed.py.';
  if (error.kind === 'http') return `Server is running but returned an error: ${error.message}`;
  if (await isServerAlive()) return `Server is running but slow or busy (${error.message}). Try Refresh in a minute.`;
  return `Forecast API not reachable at ${LAB_API_URL}. Start it with: uvicorn serve_api:app --port 8000`;
}

// Separates real API trouble from drawing bugs, which used to be misreported as "not reachable".
async function reportLabError(error) {
  console.error('Indicator Lab problem:', error);
  const message = error.kind
    ? await describeApiError(error)
    : `Display problem: ${error.message}. Open the browser console (F12) for details.`;
  showLabStatus(message, true);
}

// ───────────────────────── SMALL HELPERS ─────────────────────────

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

// Formats any number with fixed decimals and shows a dash instead of crashing on missing values.
function formatNumber(value, decimals) {
  return Number.isFinite(value) ? value.toFixed(decimals) : '—';
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

// ───────────────────────── LOCAL TIME ─────────────────────────

// Names the viewer's time zone so the screen always says which clock it uses.
function getBrowserTimeZone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'local time';
}

// Converts UTC chart seconds to the viewer's clock so this chart matches TradingView.
function formatLocalTime(utcSeconds) {
  const options = { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false };
  return new Date(utcSeconds * 1000).toLocaleString([], options);
}

// Labels the time axis in local time, showing dates at day boundaries and hours otherwise.
function formatLocalTick(utcSeconds, tickMarkType) {
  const date = new Date(utcSeconds * 1000);
  if (tickMarkType < 3) return date.toLocaleDateString([], { day: '2-digit', month: 'short' });
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

// ───────────────────────── CHART SETUP ─────────────────────────

// Collects the chart look and the local-time formatters in one place.
function buildChartOptions(container) {
  return {
    width: container.clientWidth,
    height: container.clientHeight,
    layout: { background: { color: '#111823' }, textColor: '#8995a8', fontSize: 11 },
    grid: { vertLines: { color: '#1b2533' }, horzLines: { color: '#1b2533' } },
    rightPriceScale: { borderColor: '#242e3b' },
    timeScale: {
      borderColor: '#242e3b', timeVisible: true, secondsVisible: false,
      tickMarkFormatter: formatLocalTick,
    },
    localization: { timeFormatter: formatLocalTime },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  };
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
  labChart = LightweightCharts.createChart(container, buildChartOptions(container));
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
    'lab-stop-distance': `${formatNumber(stop.distance_pips, 1)} pips`,
    'lab-stop-atr': `${formatNumber(stop.atr_pips, 1)} pips`,
    'lab-stop-key': `${formatNumber(stop.key_multiplier, 2)} × base key 3.0`,
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
    formatPrice(forecast.price_p90), formatNumber(forecast.band_width_pips, 1), forecast.model,
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

// ───────────────────────── FRESHNESS ─────────────────────────

// Works out how many hours old the newest bar is, so stale forecasts are never silent.
function getDataAgeHours(prediction) {
  return (Date.now() - new Date(prediction.as_of).getTime()) / MILLISECONDS_PER_HOUR;
}

// Builds the status line and warns loudly when the forecast is built on old data.
function describeFreshness(prediction) {
  const ageHours = getDataAgeHours(prediction);
  const lastBarText = formatLocalTime(new Date(prediction.as_of).getTime() / 1000);
  const details = `Last bar ${lastBarText} (${getBrowserTimeZone()}) · model ${prediction.model_version}`;
  if (ageHours <= STALE_WARNING_HOURS) return { text: `Updated · ${details}`, isWarning: false };
  const warning = `Data is ${Math.round(ageHours)} h old (market closed, or sync needed). Press Refresh to sync.`;
  return { text: `${warning} ${details}`, isWarning: true };
}

// Shows the last update time or the failure reason under the chart.
function showLabStatus(message, isError) {
  const status = document.getElementById('lab-status');
  status.textContent = message;
  status.style.color = isError ? '#f05262' : '';
}

// ───────────────────────── LOADING + SYNC ─────────────────────────

// Fetches bars and the latest prediction together.
async function fetchLabData() {
  const barCount = document.getElementById('lab-bar-count').value;
  const [barsResponse, prediction] = await Promise.all([
    callForecastApi(`/bars?limit=${barCount}`),
    callForecastApi('/latest'),
  ]);
  return { barsResponse, prediction };
}

// Draws the chart, the panels and the freshness line from one fetched data set.
function drawLabData({ barsResponse, prediction }) {
  labRows = buildChartRows(barsResponse);
  labLatestPrediction = prediction;
  drawRowsOnChart();
  fitChartOnce();
  renderAllPanels(prediction);
  const freshness = describeFreshness(prediction);
  showLabStatus(freshness.text, freshness.isWarning);
}

// Loads and draws everything, and guards against two loads running at the same time.
async function loadIndicatorData() {
  if (isLabLoading) return;
  isLabLoading = true;
  try {
    drawLabData(await fetchLabData());
  } catch (error) {
    await reportLabError(error);
  } finally {
    isLabLoading = false;
  }
}

// Asks the server to pull new bars so the forecast is not stuck in the past.
async function syncNewBars() {
  try {
    const result = await callForecastApi('/sync', { method: 'POST' }, LAB_SYNC_TIMEOUT_MS);
    return result.added;
  } catch (error) {
    console.warn('Sync skipped:', error.message);
    return 0;
  }
}

// Locks the refresh button while syncing so repeated clicks cannot pile up requests.
function setRefreshBusy(isBusy) {
  const button = document.getElementById('lab-refresh');
  button.disabled = isBusy;
  button.textContent = isBusy ? '⏳ Syncing…' : '↻ Refresh';
}

// Syncs new bars first and then redraws, so Refresh really brings the page up to date.
async function syncAndReload() {
  setRefreshBusy(true);
  showLabStatus('Syncing latest bars, this can take up to a minute…', false);
  lastSyncTime = Date.now();
  try {
    await syncNewBars();
    await loadIndicatorData();
  } finally {
    setRefreshBusy(false);
  }
}

// Decides if data is old enough to sync, without hammering the data source every minute.
function needsSync() {
  if (!labLatestPrediction) return false;
  const isOld = getDataAgeHours(labLatestPrediction) > SYNC_AFTER_HOURS;
  const isCooledDown = Date.now() - lastSyncTime > SYNC_COOLDOWN_MS;
  return isOld && isCooledDown;
}

// ───────────────────────── PAGE WIRING ─────────────────────────

// Refreshes only while the Indicator Lab is on screen, syncing when the data has gone stale.
function refreshWhileVisible() {
  const page = document.getElementById('page-indicators');
  if (!page.classList.contains('active') || document.hidden) return;
  if (needsSync()) syncAndReload();
  else loadIndicatorData();
}

// Creates the chart the first time the page opens, because a hidden page has no width.
async function openIndicatorsPage() {
  if (!window.LightweightCharts) {
    showLabStatus('Chart library did not load. Check your internet connection.', true);
    return;
  }
  if (!labChart) buildLabChart();
  if (!labRefreshTimer) labRefreshTimer = setInterval(refreshWhileVisible, LAB_REFRESH_MS);
  await loadIndicatorData();
  if (needsSync()) await syncAndReload();
}

// Connects the page's button, dropdowns and checkboxes to their actions.
function wireIndicatorControls() {
  document.getElementById('lab-refresh').addEventListener('click', syncAndReload);
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