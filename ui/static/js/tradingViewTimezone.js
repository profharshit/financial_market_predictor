// FILE PATH: financial-market-project/ui/static/js/tradingViewTimezone.js
// ACTION: CREATE this NEW file in the same folder as fmipIndicators.js.
// THEN add this line in financial-market-project/ui/index.html, directly after the
// <script src="/ui/static/js/fmipIndicators.js"></script> line (just before </body>):
//   <script src="/ui/static/js/tradingViewTimezone.js"></script>

const TRADINGVIEW_CONTAINER_IDS = ['dashboard-tv-chart', 'tradingview-chart'];
const TRADINGVIEW_WIDGET_PREFIX = 'https://s.tradingview.com/widgetembed/';

// Reads the viewer's own time zone so chart times match the clock on their screen.
function getViewerTimeZone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Etc/UTC';
}

// Picks out TradingView widget frames so no other iframe on the page is touched.
function isTradingViewFrame(node) {
  return node.tagName === 'IFRAME' && node.src.startsWith(TRADINGVIEW_WIDGET_PREFIX);
}

// TradingView defaults to exchange time, which looks many hours behind for Indian viewers.
function addTimeZoneToFrame(frame) {
  const address = new URL(frame.src);
  const viewerZone = getViewerTimeZone();
  if (address.searchParams.get('timezone') === viewerZone) return;
  address.searchParams.set('timezone', viewerZone);
  frame.src = address.toString();
}

// Fixes every TradingView frame the dashboard or terminal adds after this script starts.
function fixAddedFrames(mutations) {
  mutations.forEach(mutation => {
    mutation.addedNodes.forEach(node => {
      if (isTradingViewFrame(node)) addTimeZoneToFrame(node);
    });
  });
}

// Fixes frames that were already on the page before this script loaded.
function fixExistingFrames() {
  TRADINGVIEW_CONTAINER_IDS.forEach(containerId => {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.querySelectorAll('iframe').forEach(frame => {
      if (isTradingViewFrame(frame)) addTimeZoneToFrame(frame);
    });
  });
}

// Shows which time zone the charts use, so any mismatch is visible instead of confusing.
function addTimeZoneNote() {
  const status = document.getElementById('dashboard-chart-status');
  if (!status || document.getElementById('chart-timezone-note')) return;
  const note = document.createElement('div');
  note.id = 'chart-timezone-note';
  note.className = 'status-line';
  note.style.marginTop = '4px';
  note.textContent = `Chart times shown in ${getViewerTimeZone()} (your device clock).`;
  status.after(note);
}

// Watches both chart holders so every newly loaded chart gets the right time zone.
function watchChartContainers() {
  const observer = new MutationObserver(fixAddedFrames);
  TRADINGVIEW_CONTAINER_IDS.forEach(containerId => {
    const container = document.getElementById(containerId);
    if (container) observer.observe(container, { childList: true });
  });
}

// Starts the time zone fix once, when the script loads.
function startTimeZoneFix() {
  watchChartContainers();
  fixExistingFrames();
  addTimeZoneNote();
}

startTimeZoneFix();