// FILE: financial-market-project/ui/static/js/localTime.js   (NEW FILE)
// PLACE: same folder as fmipLive.js and fmipIndicators.js.
// THEN in financial-market-project/ui/index.html, add this line ABOVE the fmipIndicators.js
// and fmipLive.js script tags (load order matters):
//   <script src="/ui/static/js/localTime.js"></script>
// Why: the API labels bars in UTC, but you are in IST (UTC+5:30). Add the 1-2 h a bar needs
// to close and the "6-7 hours late" you see is really a timezone gap, not stale data.

const STALE_DATA_HOURS = 3;
const TICK_MARK_TYPE = { year: 0, month: 1, dayOfMonth: 2, time: 3, timeWithSeconds: 4 };

// Shows one chart timestamp in the viewer's own timezone so the chart agrees with the wall clock.
function formatLocalDateTime(unixSeconds) {
  return new Date(unixSeconds * 1000).toLocaleString([], {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
  });
}

// Builds the axis label for a tick: a date for day/month ticks, a local clock time otherwise.
function formatLocalTick(unixSeconds, tickType) {
  const date = new Date(unixSeconds * 1000);
  if (tickType === TICK_MARK_TYPE.year) return String(date.getFullYear());
  if (tickType === TICK_MARK_TYPE.month) return date.toLocaleDateString([], { month: 'short' });
  if (tickType === TICK_MARK_TYPE.dayOfMonth) return date.toLocaleDateString([], { day: '2-digit', month: 'short' });
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

// Groups the two chart options that switch labels from UTC to local time.
function buildLocalTimeOptions() {
  return {
    localization: { timeFormatter: formatLocalDateTime },
    timeScale: { tickMarkFormatter: formatLocalTick },
  };
}

// Applies local-time labels to a chart that another file already created.
function applyLocalTimeToChart(chart) {
  if (chart) chart.applyOptions(buildLocalTimeOptions());
}

// Returns a short timezone name so nobody mistakes a local time for UTC.
function getLocalTimezoneName() {
  const parts = new Intl.DateTimeFormat([], { timeZoneName: 'short' }).formatToParts(new Date());
  return parts.find(part => part.type === 'timeZoneName').value;
}

// Converts an API timestamp into text such as "23 Nov, 19:00 GMT+5:30".
function formatIsoInLocalTime(isoTimestamp) {
  const seconds = new Date(isoTimestamp).getTime() / 1000;
  return `${formatLocalDateTime(seconds)} ${getLocalTimezoneName()}`;
}

// Gives a compact HH:MM local label for small chart axes.
function formatIsoAsLocalClock(isoTimestamp) {
  return new Date(isoTimestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

// Measures how old a timestamp is so the UI can warn when data has stopped moving.
function hoursSince(isoTimestamp) {
  return (Date.now() - new Date(isoTimestamp).getTime()) / 3600000;
}