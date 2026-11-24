// FILE: financial-market-project/ui/static/js/paperTrading.js   (NEW FILE)
// Server-backed paper trading for the FMIP terminal: per-user accounts, saved trades, forecast-vs-actual log.

const PAPER_API_BASE = window.location.protocol.startsWith('http') ? window.location.origin : 'http://127.0.0.1:8000';
const PAPER_TOKEN_KEY = 'fmipPaperToken';
const PAPER_USER_KEY = 'fmipPaperUser';
const PAPER_REFRESH_MS = 60000;
const PAPER_HORIZONS = [1, 4, 24];
const PAPER_CSV_HEADER = [
  'Placed at', 'Symbol', 'Side', 'Type', 'Quantity', 'Fill price', 'Stop loss', 'Take profit', 'Note',
  'Forecast bar', 'Forecast close', 'Model version', 'Signal',
  'Horizon (h)', 'P10', 'P50', 'P90', 'Actual price', 'Inside band', 'Error (pips)', 'Result at horizon',
];

let paperAccount = null;
let paperHorizon = 4;
const paperOriginalSetMarket = setMarket;
const paperOriginalToggleAuth = toggleAuth;

// ───────────────────────── SMALL HELPERS ─────────────────────────

// Sets the text of one element by id, ignoring ids that are not on the page.
function setPaperText(id, text) {
  const element = document.getElementById(id);
  if (element) element.textContent = text;
}

// Shows a message under the order ticket.
function setPaperFeedback(message) {
  setPaperText('trade-feedback', message);
}

// Forex prices need five decimals, larger prices only two.
function formatPaperPrice(value) {
  if (value === null || value === undefined) return '—';
  return Number(value) < 10 ? Number(value).toFixed(5) : Number(value).toFixed(2);
}

// Formats a profit or loss with an explicit sign.
function formatSignedMoney(value) {
  return (value >= 0 ? '+' : '−') + money(Math.abs(value));
}

// Formats a 0-1 rate as a whole percent.
function formatPercent(value) {
  return value === null ? '—' : Math.round(value * 100) + '%';
}

// Picks the green or red text class for a profit or loss.
function pnlClass(value) {
  return value >= 0 ? 'up' : 'down';
}

// Reads a number input and returns null when it is empty or not positive.
function readPositiveNumber(id) {
  const value = Number(document.getElementById(id).value);
  return value > 0 ? value : null;
}

// ───────────────────────── HTTP ─────────────────────────

// Builds the headers each request needs: the team key and the user's session token.
function buildPaperHeaders(includeSession) {
  const headers = { 'Content-Type': 'application/json' };
  const teamKey = window.FMIP_API_KEY || localStorage.getItem('fmipApiKey');
  const sessionToken = localStorage.getItem(PAPER_TOKEN_KEY);
  if (teamKey) headers['X-API-Key'] = teamKey;
  if (includeSession && sessionToken) headers['X-Paper-Token'] = sessionToken;
  return headers;
}

// Reads the server's error text so the user sees exactly why a request was refused.
async function readPaperError(response) {
  const body = await response.json().catch(() => ({}));
  if (typeof body.detail === 'string') return body.detail;
  if (Array.isArray(body.detail) && body.detail.length) {
    return `${body.detail[0].loc.slice(-1)[0]}: ${body.detail[0].msg}`;
  }
  return 'Request rejected. Check the values you entered.';
}

// The single place that talks to the API and turns failures into readable errors.
async function paperRequest(path, options = {}, includeSession = true) {
  const response = await fetch(PAPER_API_BASE + path, { ...options, headers: buildPaperHeaders(includeSession) });
  if (!response.ok) {
    const error = new Error(await readPaperError(response));
    error.status = response.status;
    throw error;
  }
  return response.json();
}

// ───────────────────────── SESSION + SIGN-IN MODAL ─────────────────────────

// Tells whether this browser holds a saved session token.
function isSignedIn() {
  return Boolean(localStorage.getItem(PAPER_TOKEN_KEY));
}

// Remembers the session token and username so a refresh keeps the user signed in.
function saveSession(session) {
  localStorage.setItem(PAPER_TOKEN_KEY, session.token);
  localStorage.setItem(PAPER_USER_KEY, session.username);
}

// Forgets the session on this browser only; the saved trades stay on the server.
function clearSession() {
  localStorage.removeItem(PAPER_TOKEN_KEY);
  localStorage.removeItem(PAPER_USER_KEY);
  paperAccount = null;
}

// Writes a message in the sign-in modal.
function showAuthMessage(message, color) {
  const feedback = document.getElementById('auth-feedback');
  feedback.textContent = message;
  feedback.style.color = color;
}

// Replaces the old demo defaults in the sign-in modal with real account wording.
function prepareAuthModal() {
  const usernameInput = document.getElementById('auth-email');
  usernameInput.type = 'text';
  usernameInput.value = '';
  usernameInput.placeholder = 'username, e.g. happy';
  document.getElementById('auth-pass').value = '';
  document.querySelector('#auth-modal .field label').textContent = 'USERNAME';
  document.querySelector('#auth-name-wrap label').textContent = 'DISPLAY NAME (OPTIONAL)';
  document.querySelector('#auth-modal p').textContent =
    'Each teammate creates one account. Paper trades are saved on the server, so they survive refreshes and other devices.';
}

// Keeps the main button text in step with the Sign in / Register switch.
function toggleAuthAndRelabel() {
  paperOriginalToggleAuth();
  document.querySelector('#auth-modal .btn-solid').textContent = isRegister ? 'Create account' : 'Sign In';
}

// Builds the JSON body for register and login.
function buildAuthBody(username, password) {
  const displayName = document.getElementById('auth-name').value.trim();
  return JSON.stringify({ username, password, display_name: displayName || null });
}

// Registers or signs in against the server, then opens the dashboard.
async function submitAccountForm() {
  const username = document.getElementById('auth-email').value.trim();
  const password = document.getElementById('auth-pass').value;
  if (!username || !password) {
    showAuthMessage('Please fill username and password.', 'var(--red)');
    return;
  }
  const path = isRegister ? '/paper/auth/register' : '/paper/auth/login';
  try {
    const session = await paperRequest(path, { method: 'POST', body: buildAuthBody(username, password) }, false);
    saveSession(session);
    showAuthMessage('Signed in. Loading your account…', 'var(--green)');
    await refreshPaperAccount();
    launchDash(session.username);
  } catch (error) {
    showAuthMessage(error.message, 'var(--red)');
  }
}

// Re-opens the sign-in modal, used by the terminal's sign-in prompts.
function openSignInModal() {
  document.getElementById('auth-pass').value = '';
  showAuthMessage('', 'var(--red)');
  document.getElementById('auth-modal').classList.add('open');
}

// Ends the session on the server and this browser, then asks for the next sign-in.
async function signOutPaperAccount() {
  await paperRequest('/paper/auth/logout', { method: 'POST' }).catch(() => null);
  clearSession();
  drawPaperAccount();
  openSignInModal();
}

// After a page refresh, restores the saved session so the user never has to sign in again.
async function restorePaperSession() {
  if (!isSignedIn()) return;
  try {
    paperAccount = await paperRequest('/paper/account');
    launchDash(paperAccount.username);
    drawPaperAccount();
  } catch (error) {
    handlePaperError(error);
  }
}

// ───────────────────────── ACCOUNT DATA ─────────────────────────

// Loads the signed-in user's account from the server and redraws the terminal.
async function refreshPaperAccount() {
  if (!isSignedIn()) return;
  try {
    paperAccount = await paperRequest('/paper/account');
    drawPaperAccount();
  } catch (error) {
    handlePaperError(error);
  }
}

// Signs out locally when the server rejects the session, and always explains what happened.
function handlePaperError(error) {
  if (error.status === 401) clearSession();
  setPaperFeedback(error.message);
  drawPaperAccount();
}

// Gives the old terminal code (recalcOrder) the cash it expects from the server data.
function readPaperCache() {
  return { cash: paperAccount ? paperAccount.cash : 0, orders: paperAccount ? paperAccount.trades : [] };
}

// Re-reads the server data every minute so forecast outcomes appear once their target bar arrives.
function startPaperRefreshTimer() {
  setInterval(() => {
    if (!document.hidden) refreshPaperAccount();
  }, PAPER_REFRESH_MS);
}

// ───────────────────────── DRAWING ─────────────────────────

// Redraws every paper-trading widget from the cached account.
function drawPaperAccount() {
  drawAccountBar();
  drawPortfolioNumbers();
  drawHoldingsTable();
  drawOrdersTable();
  drawForecastLog();
  recalcOrder();
}

// Shows who is signed in, or a sign-in prompt.
function drawAccountBar() {
  const bar = document.getElementById('paper-account-bar');
  if (!bar) return;
  if (!isSignedIn()) {
    bar.innerHTML = '<div>Not signed in. Sign in to save paper trades to your own account.</div>' +
      '<button class="btn btn-solid" onclick="openSignInModal()">Sign in / Register</button>';
    return;
  }
  const name = paperAccount ? paperAccount.display_name : localStorage.getItem(PAPER_USER_KEY);
  bar.innerHTML = `<div>Paper account: <strong style="color:#fff">${escapeText(name)}</strong> ` +
    '<span class="source-pill">SAVED ON SERVER</span></div>' +
    '<button class="btn btn-p" onclick="signOutPaperAccount()">Sign out</button>';
}

// Fills the cash, position count and realized profit tiles.
function drawPortfolioNumbers() {
  const cash = paperAccount ? money(paperAccount.cash) : '—';
  const positionCount = paperAccount ? paperAccount.positions.length : 0;
  const realized = paperAccount ? paperAccount.realized_pnl : 0;
  ['pro-cash', 'portfolio-cash', 'order-cash'].forEach(id => setPaperText(id, cash));
  ['pro-positions', 'portfolio-count'].forEach(id => setPaperText(id, positionCount));
  setPaperText('pro-realized', formatSignedMoney(realized));
  document.getElementById('pro-realized').style.color = realized >= 0 ? '#16c784' : '#f05262';
}

// Builds one open-position row, with unrealised profit when the model has a live price.
function buildHoldingRow(position) {
  const valuation = position.market_price === null
    ? '<span class="source-pill">NO LIVE QUOTE</span>'
    : `<span class="${pnlClass(position.unrealised_pnl)}">${formatSignedMoney(position.unrealised_pnl)}</span> @ ${formatPaperPrice(position.market_price)}`;
  return `<tr><td>${escapeText(position.symbol)}</td><td>${position.quantity}</td>
    <td>${formatPaperPrice(position.average_entry)}</td><td>${money(position.invested_cost)}</td><td>${valuation}</td></tr>`;
}

// Fills the open positions table.
function drawHoldingsTable() {
  const positions = paperAccount ? paperAccount.positions : [];
  document.getElementById('pro-holdings').innerHTML = positions.length
    ? positions.map(buildHoldingRow).join('')
    : '<tr><td colspan="5" class="pro-empty">No open paper positions.</td></tr>';
}

// Builds one row of the plain order history table.
function buildOrderRow(trade) {
  const sideClass = trade.side === 'buy' ? 'up' : 'down';
  return `<tr><td>${escapeText(new Date(trade.placed_at).toLocaleString())}</td><td>${escapeText(trade.symbol)}</td>
    <td class="${sideClass}">${trade.side.toUpperCase()}</td><td>${escapeText(trade.order_type)}</td>
    <td>${trade.quantity}</td><td>${formatPaperPrice(trade.fill_price)}</td><td><span class="source-pill">SAVED</span></td></tr>`;
}

// Fills the order history table with the 100 newest trades.
function drawOrdersTable() {
  const trades = paperAccount ? paperAccount.trades.slice(0, 100) : [];
  document.getElementById('paper-orders').innerHTML = trades.length
    ? trades.map(buildOrderRow).join('')
    : '<tr><td colspan="7" style="color:#94a3b8">No paper orders yet.</td></tr>';
}

// Says whether the model leaned up, down or flat, and whether that matched the trade.
function describeModelView(trade, forecast) {
  const move = forecast.price_p50 - trade.forecast_close;
  let direction = 'FLAT';
  if (move > 0) direction = 'UP';
  if (move < 0) direction = 'DOWN';
  return `${direction} · ${forecast.agrees_with_trade ? 'agrees' : 'disagrees'}`;
}

// Builds the four outcome cells, or one pending cell while the target bar has not arrived.
function buildOutcomeCells(forecast) {
  if (!forecast.resolved) {
    const target = new Date(forecast.target_ts_est).toLocaleString();
    return `<td colspan="4"><span class="source-pill">PENDING · target ≈ ${escapeText(target)}</span></td>`;
  }
  const bandMark = forecast.inside_band ? '<span class="up">✓ inside</span>' : '<span class="down">✗ outside</span>';
  return `<td>${formatPaperPrice(forecast.realised_price)}</td><td>${bandMark}</td>
    <td>${forecast.error_pips.toFixed(1)}</td>
    <td class="${pnlClass(forecast.result_at_horizon)}">${formatSignedMoney(forecast.result_at_horizon)}</td>`;
}

// Builds one forecast-vs-actual row for the horizon chosen in the dropdown.
function buildForecastRow(trade) {
  const forecast = trade.forecasts.find(item => item.horizon_bars === paperHorizon);
  if (!forecast) return '';
  const sideClass = trade.side === 'buy' ? 'up' : 'down';
  return `<tr><td>${escapeText(new Date(trade.placed_at).toLocaleString())}</td><td>${escapeText(trade.symbol)}</td>
    <td class="${sideClass}">${trade.side.toUpperCase()} ${trade.quantity}</td><td>${formatPaperPrice(trade.fill_price)}</td>
    <td>${describeModelView(trade, forecast)}</td>
    <td>${formatPaperPrice(forecast.price_p10)}</td><td>${formatPaperPrice(forecast.price_p50)}</td>
    <td>${formatPaperPrice(forecast.price_p90)}</td>${buildOutcomeCells(forecast)}</tr>`;
}

// Writes the one-line accuracy summary for the selected horizon.
function describeScorecard() {
  const stats = paperAccount ? paperAccount.scorecard[String(paperHorizon)] : null;
  if (!stats || stats.resolved === 0) return `${paperHorizon}h: no resolved forecasts yet.`;
  return `${paperHorizon}h · ${stats.resolved} resolved · inside band ${formatPercent(stats.inside_band_rate)} (target 80%)` +
    ` · direction right ${formatPercent(stats.direction_hit_rate)} · mean error ${stats.mean_abs_error_pips.toFixed(1)} pips`;
}

// Fills the forecast-vs-actual table and its scorecard line.
function drawForecastLog() {
  const body = document.getElementById('paper-forecast-rows');
  if (!body) return;
  const covered = (paperAccount ? paperAccount.trades : []).filter(trade => trade.forecasts.length > 0);
  body.innerHTML = covered.length
    ? covered.map(buildForecastRow).join('')
    : '<tr><td colspan="12" class="pro-empty">No trades with a saved forecast yet. Trade FX:EURUSD to start comparing.</td></tr>';
  setPaperText('paper-scorecard', describeScorecard());
}

// ───────────────────────── ORDERS ─────────────────────────

// Reads the optional note box under the order ticket.
function readOrderNote() {
  const note = document.getElementById('order-note');
  return note && note.value.trim() ? note.value.trim() : null;
}

// Collects the order ticket fields into the object the server expects.
function readOrderTicket() {
  return {
    symbol: currentMarket,
    side: paperSide,
    order_type: document.getElementById('order-type').value,
    quantity: readPositiveNumber('order-qty'),
    price: readPositiveNumber('order-price'),
    stop_loss: readPositiveNumber('order-sl'),
    take_profit: readPositiveNumber('order-tp'),
    note: readOrderNote(),
  };
}

// Tells the user whether the model forecast was stored with the trade.
function describeSavedOrder(forecastSaved) {
  return forecastSaved
    ? 'Saved. The model forecast at this moment was stored and will be compared with the real price.'
    : 'Saved. This symbol has no model forecast, so it will not appear in the forecast comparison.';
}

// Sends the order to the server and redraws from the account it returns.
async function sendOrder(ticket) {
  const button = document.getElementById('trade-submit');
  button.disabled = true;
  try {
    const reply = await paperRequest('/paper/orders', { method: 'POST', body: JSON.stringify(ticket) });
    paperAccount = reply.account;
    drawPaperAccount();
    setPaperFeedback(describeSavedOrder(reply.forecast_saved));
  } catch (error) {
    handlePaperError(error);
  } finally {
    button.disabled = false;
  }
}

// Replaces the browser-only order with a saved, per-user order.
async function placeServerPaperOrder() {
  if (!isSignedIn()) {
    setPaperFeedback('Sign in first so your trades are saved to your account.');
    openSignInModal();
    return;
  }
  const ticket = readOrderTicket();
  if (!ticket.quantity) {
    setPaperFeedback('Enter a quantity above zero.');
    return;
  }
  await sendOrder(ticket);
}

// Deletes only this user's saved trades after an explicit confirmation.
async function resetServerPaperAccount() {
  if (!isSignedIn()) return;
  if (!confirm('Delete ALL your saved paper trades and restore your starting cash? This cannot be undone.')) return;
  try {
    paperAccount = await paperRequest('/paper/reset', { method: 'POST' });
    drawPaperAccount();
    setPaperFeedback('Your paper account was reset.');
  } catch (error) {
    handlePaperError(error);
  }
}

// Pre-fills the order price with the model's latest EUR/USD close so entry matches the forecast base.
async function fillOrderPriceFromModel() {
  if (!currentMarket.endsWith('EURUSD')) return;
  try {
    const latest = await paperRequest('/latest');
    document.getElementById('order-price').value = latest.last_close;
    recalcOrder();
  } catch (error) {
    console.warn('Model price not available:', error.message);
  }
}

// Wraps the original symbol loader so EUR/USD gets the model's price automatically.
function setMarketAndFillPrice(symbol) {
  paperOriginalSetMarket(symbol);
  fillOrderPriceFromModel();
}

// ───────────────────────── CSV EXPORT ─────────────────────────

// Wraps one CSV cell in quotes and escapes quotes inside it.
function quoteCsvCell(value) {
  return '"' + String(value ?? '').replace(/"/g, '""') + '"';
}

// Stops spreadsheet programs from running a note that starts with a formula character.
function safeCsvText(text) {
  return text && /^[=+\-@]/.test(text) ? "'" + text : text;
}

// Builds the CSV columns that describe the trade itself.
function buildTradeColumns(trade) {
  return [trade.placed_at, trade.symbol, trade.side, trade.order_type, trade.quantity, trade.fill_price,
    trade.stop_loss, trade.take_profit, safeCsvText(trade.note), trade.forecast_bar_ts, trade.forecast_close,
    trade.model_version, trade.signal_action];
}

// Builds one CSV row per horizon, or a single row with blanks when no forecast was stored.
function buildCsvRowsForTrade(trade) {
  const tradeColumns = buildTradeColumns(trade);
  if (trade.forecasts.length === 0) return [[...tradeColumns, ...new Array(8).fill('')]];
  return trade.forecasts.map(forecast => [...tradeColumns, forecast.horizon_bars, forecast.price_p10,
    forecast.price_p50, forecast.price_p90, forecast.realised_price, forecast.inside_band,
    forecast.error_pips, forecast.result_at_horizon]);
}

// Downloads text as a file without needing a server round trip.
function downloadTextFile(text, fileName) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Exports the saved trades with forecast and actual columns for the team report.
function exportPaperTradeLog() {
  if (!paperAccount || paperAccount.trades.length === 0) {
    setPaperFeedback('No saved trades to export yet.');
    return;
  }
  const rows = [PAPER_CSV_HEADER, ...paperAccount.trades.flatMap(buildCsvRowsForTrade)];
  const csv = rows.map(row => row.map(quoteCsvCell).join(',')).join('\r\n');
  downloadTextFile(csv, `FMIP_Paper_Trades_${paperAccount.username}.csv`);
}

// ───────────────────────── PAGE SETUP ─────────────────────────

// Creates the signed-in / signed-out bar at the top of the terminal.
function addAccountBar() {
  const bar = document.createElement('div');
  bar.className = 'card';
  bar.id = 'paper-account-bar';
  bar.style.cssText = 'display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 16px';
  document.querySelector('#page-terminal .pro-stat-grid').before(bar);
}

// Adds a small "why this trade" box to the order ticket.
function addOrderNoteInput() {
  if (document.getElementById('order-note')) return;
  const wrapper = document.createElement('div');
  wrapper.style.marginTop = '12px';
  wrapper.innerHTML = '<label class="trade-label">Why this trade? (optional note)</label>' +
    '<input class="trade-input" id="order-note" maxlength="200" placeholder="e.g. followed the model BUY bias">';
  document.getElementById('trade-submit').before(wrapper);
}

// Builds the HTML of the forecast-vs-actual card.
function buildForecastLogHtml() {
  const options = PAPER_HORIZONS.map(hours =>
    `<option value="${hours}"${hours === paperHorizon ? ' selected' : ''}>${hours}h ahead</option>`).join('');
  const columns = ['TIME', 'SYMBOL', 'SIDE / QTY', 'FILL', 'MODEL VIEW', 'P10', 'P50', 'P90', 'ACTUAL', 'IN BAND', 'ERROR (PIPS)', 'RESULT'];
  const headers = columns.map(name => `<th>${name}</th>`).join('');
  return `<div class="ch"><div class="ct">Forecast vs actual <span class="source-pill">EUR/USD MODEL</span></div>
    <div style="display:flex;gap:8px"><select id="paper-horizon" aria-label="Forecast horizon">${options}</select>
    <button class="btn btn-c" onclick="exportPaperCsv()">↓ Export CSV</button></div></div>
    <div class="status-line" id="paper-scorecard" style="margin-bottom:10px"></div>
    <div class="terminal-table-wrap"><table class="terminal-table"><thead><tr>${headers}</tr></thead>
    <tbody id="paper-forecast-rows"></tbody></table></div>
    <p class="status-line" style="margin-top:10px">The model forecast is saved at the moment you trade. “Actual” fills in
    automatically once the target bar arrives (use Refresh on the dashboard to pull new bars). Trades placed in the same
    hour share one forecast.</p>`;
}

// Adds the forecast-vs-actual card below the order history.
function addForecastLogCard() {
  const card = document.createElement('div');
  card.className = 'card c-cyan';
  card.id = 'paper-forecast-log';
  card.innerHTML = buildForecastLogHtml();
  document.getElementById('paper-orders').closest('.card').after(card);
  document.getElementById('paper-horizon').addEventListener('change', event => {
    paperHorizon = Number(event.target.value);
    drawForecastLog();
  });
}

// Updates the old "local only" wording so it matches the new saved-on-server behaviour.
function relabelPaperTerminal() {
  document.querySelectorAll('#page-terminal .source-pill, #page-terminal .demo-label').forEach(label => {
    const isOldLabel = label.textContent === 'LOCAL ONLY' || label.textContent === 'LOCAL SIMULATION';
    if (isOldLabel) label.textContent = 'SAVED TO YOUR ACCOUNT';
  });
  document.getElementById('pro-holdings').closest('.card').querySelector('p.status-line').textContent =
    'Unrealised P&L is shown for EUR/USD only, priced from the forecast API latest bar. Other symbols show cost basis only.';
  document.querySelector('#page-terminal > p.status-line').textContent =
    'Chart data comes from TradingView. Paper trades, model forecasts and outcomes are saved in your account on the FMIP server. No real orders are ever sent.';
  setPaperFeedback('Sign in, then place a simulated order. The order and the model forecast at that moment are saved to your account.');
}

// Builds all the extra terminal widgets once, when the script loads.
function mountPaperTerminalUi() {
  addAccountBar();
  addOrderNoteInput();
  addForecastLogCard();
  relabelPaperTerminal();
  drawPaperAccount();
}

// Draws from the cache straight away, then re-reads the server so the page is always current.
function renderPaperTerminal() {
  drawPaperAccount();
  refreshPaperAccount();
}

// ───────────────────────── WIRING ─────────────────────────

window.getPaper = readPaperCache;
window.placePaperOrder = placeServerPaperOrder;
window.renderPaper = renderPaperTerminal;
window.resetPaperPortfolio = resetServerPaperAccount;
window.exportPaperCsv = exportPaperTradeLog;
window.doAuth = submitAccountForm;
window.toggleAuth = toggleAuthAndRelabel;
window.setMarket = setMarketAndFillPrice;

prepareAuthModal();
mountPaperTerminalUi();
restorePaperSession();
startPaperRefreshTimer();