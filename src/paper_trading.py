# FILE: financial-market-project/src/paper_trading.py   (NEW FILE, same folder as serve_api.py)
# Per-user paper trading: accounts, saved trades, model forecast snapshots and real-outcome tracking.
# Data is stored in financial-market-project/data/paper_trades.db (created automatically on first start).

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import pandas as pd
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

HERE = os.path.dirname(os.path.abspath(__file__))
PAPER_DB_PATH = os.environ.get("FMP_PAPER_DB_PATH", os.path.join(HERE, "../data/paper_trades.db"))
TEAM_API_KEY = os.environ.get("FMP_API_KEY")
STARTING_CASH = 100_000.0
SESSION_DAYS = 30
MAX_PRICE_GAP = 0.02
MODEL_SYMBOL_SUFFIX = "EURUSD"
FORECAST_HORIZONS = (1, 4, 24)
PIP = 0.0001
TRADE_LOG_LIMIT = 300
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9._-]+:[A-Z0-9._/-]+$")
USERNAME_PATTERN = re.compile(r"^[a-z0-9._@-]{3,64}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    starting_cash REAL NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES paper_users(id),
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES paper_users(id),
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    quantity        REAL NOT NULL,
    fill_price      REAL NOT NULL,
    stop_loss       REAL,
    take_profit     REAL,
    note            TEXT,
    placed_at       TEXT NOT NULL,
    forecast_bar_ts TEXT,
    forecast_close  REAL,
    model_version   TEXT,
    signal_action   TEXT,
    model_bias      TEXT,
    stop_level      REAL,
    lorentzian_bias TEXT,
    edge_verified   INTEGER
);
CREATE TABLE IF NOT EXISTS paper_trade_forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES paper_trades(id),
    horizon_bars    INTEGER NOT NULL,
    target_ts_est   TEXT,
    price_p10       REAL NOT NULL,
    price_p50       REAL NOT NULL,
    price_p90       REAL NOT NULL,
    realised_price  REAL,
    realised_bar_ts TEXT,
    UNIQUE (trade_id, horizon_bars)
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_user ON paper_trades (user_id, id);
"""

INSERT_USER_SQL = ("INSERT INTO paper_users (username, display_name, password_hash, starting_cash, created_at) "
                   "VALUES (?, ?, ?, ?, ?)")
INSERT_TRADE_SQL = ("INSERT INTO paper_trades (user_id, symbol, side, order_type, quantity, fill_price, stop_loss, "
                    "take_profit, note, placed_at, forecast_bar_ts, forecast_close, model_version, signal_action, "
                    "model_bias, stop_level, lorentzian_bias, edge_verified) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
INSERT_FORECAST_SQL = ("INSERT INTO paper_trade_forecasts (trade_id, horizon_bars, target_ts_est, price_p10, "
                       "price_p50, price_p90) VALUES (?, ?, ?, ?, ?, ?)")
PENDING_FORECASTS_SQL = ("SELECT f.id, f.horizon_bars, t.forecast_bar_ts FROM paper_trade_forecasts f "
                         "JOIN paper_trades t ON t.id = f.trade_id "
                         "WHERE t.user_id = ? AND f.realised_price IS NULL")
USER_FORECASTS_SQL = ("SELECT f.* FROM paper_trade_forecasts f JOIN paper_trades t ON t.id = f.trade_id "
                      "WHERE t.user_id = ? ORDER BY f.horizon_bars")

paper = {"connection": None, "engine_state": {}}
database_lock = threading.Lock()
order_lock = threading.Lock()


# ------------------------------------------------------------------ database --
# Opens the paper database and keeps a reference to the forecast engine's shared state.
def connect(engine_state: dict, database_path: str = PAPER_DB_PATH) -> None:
    os.makedirs(os.path.dirname(database_path), exist_ok=True)
    connection = sqlite3.connect(database_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    connection.commit()
    paper["connection"] = connection
    paper["engine_state"] = engine_state


# Runs a SELECT from any request thread and returns plain dictionaries.
def fetch_rows(sql, params=()):
    with database_lock:
        return [dict(row) for row in paper["connection"].execute(sql, params)]


# Runs one INSERT, UPDATE or DELETE and returns the new row id.
def execute_write(sql, params=()):
    with database_lock:
        cursor = paper["connection"].execute(sql, params)
        paper["connection"].commit()
        return cursor.lastrowid


# Returns the current UTC time as ISO text, the format used throughout this database.
def utc_now_text():
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------- passwords/sessions --
# Turns a password into a scrypt digest so the plain text is never stored.
def derive_password_digest(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)


# Hashes a new password with a random salt for storage.
def hash_password(password):
    salt = secrets.token_bytes(16)
    return salt.hex() + "$" + derive_password_digest(password, salt).hex()


# Checks a login password against the stored salted hash in constant time.
def password_matches(password, stored_hash):
    salt_hex, digest_hex = stored_hash.split("$")
    digest = derive_password_digest(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(digest.hex(), digest_hex)


DUMMY_PASSWORD_HASH = hash_password("not-a-real-password")


# Stores only a hash of each session token so a leaked database cannot be used to sign in.
def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Creates a login session and returns the plain token that the browser keeps.
def create_session(user_id):
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    execute_write("INSERT INTO paper_sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                  (hash_token(token), user_id, expires_at))
    return token


# Applies the same team API key rule as the rest of the forecast service.
def require_team_key(x_api_key: Optional[str] = Header(None)):
    if TEAM_API_KEY and not (x_api_key and secrets.compare_digest(x_api_key, TEAM_API_KEY)):
        raise HTTPException(401, "missing or invalid X-API-Key")


# Finds the signed-in teammate from the X-Paper-Token header or rejects the request.
def current_user(x_paper_token: Optional[str] = Header(None)) -> dict:
    if not x_paper_token:
        raise HTTPException(401, "sign in to use paper trading")
    rows = fetch_rows("SELECT u.*, s.expires_at FROM paper_sessions s JOIN paper_users u ON u.id = s.user_id "
                      "WHERE s.token_hash = ?", (hash_token(x_paper_token),))
    if not rows or rows[0]["expires_at"] < utc_now_text():
        raise HTTPException(401, "session expired, sign in again")
    return rows[0]


# Looks up one teammate by the lower-case username they registered with.
def find_user(username):
    rows = fetch_rows("SELECT * FROM paper_users WHERE username = ?", (username,))
    return rows[0] if rows else None


# Lower-cases and trims the username so "Happy" and "happy" are the same account.
def normalise_username(raw_username):
    return raw_username.strip().lower()


# Rejects usernames with unusual characters before they reach the database.
def validate_username(username):
    if not USERNAME_PATTERN.match(username):
        raise HTTPException(422, "username must be 3-64 characters: letters, numbers and . _ @ -")


# Builds the reply the browser stores after a successful register or login.
def build_session_reply(user_id, username, display_name):
    return {"token": create_session(user_id), "username": username, "display_name": display_name}


# ------------------------------------------------------------------- schemas --
class RegisterForm(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)
    display_name: Optional[str] = Field(None, max_length=40)


class LoginForm(BaseModel):
    username: str = Field(..., max_length=64)
    password: str = Field(..., max_length=128)


class OrderForm(BaseModel):
    symbol: str = Field(..., min_length=3, max_length=40)
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"] = "market"
    quantity: float = Field(..., gt=0, lt=1e12)
    price: Optional[float] = Field(None, gt=0, lt=1e12)
    stop_loss: Optional[float] = Field(None, gt=0, lt=1e12)
    take_profit: Optional[float] = Field(None, gt=0, lt=1e12)
    note: Optional[str] = Field(None, max_length=200)


router = APIRouter(prefix="/paper", tags=["Paper trading"], dependencies=[Depends(require_team_key)])


# ------------------------------------------------------------ model snapshots --
# Tells whether the forecast model covers this symbol (EUR/USD only).
def is_model_symbol(symbol):
    return symbol.endswith(MODEL_SYMBOL_SUFFIX)


# Returns the engine's most recent prediction, or None while the service is warming up.
def latest_prediction():
    engine = paper["engine_state"].get("engine")
    return engine.last_result if engine else None


# Keeps only the prediction fields we want to store next to each trade.
def flatten_prediction(prediction):
    signal, stop, lorentzian = prediction["signal"], prediction["trailing_stop"], prediction["lorentzian"]
    return {
        "bar_ts": prediction["as_of"], "close": prediction["last_close"],
        "model_version": prediction["model_version"], "signal_action": signal["action"],
        "model_bias": signal["model_bias"], "edge_verified": signal["edge_verified"],
        "stop_level": stop["level"], "lorentzian_bias": lorentzian["bias"],
        "forecasts": prediction["forecasts"],
    }


# Captures what the model said at the moment of the trade so it can be judged later.
def take_forecast_snapshot(symbol):
    prediction = latest_prediction()
    if prediction is None or not is_model_symbol(symbol):
        return None
    return flatten_prediction(prediction)


# Gets the newest model close for symbols the model covers, otherwise None.
def latest_model_price(symbol):
    prediction = latest_prediction()
    if prediction is None or not is_model_symbol(symbol):
        return None
    return prediction["last_close"]


# ----------------------------------------------------------- order validation --
# Cleans the symbol into the EXCHANGE:SYMBOL form used by the terminal.
def normalise_symbol(raw_symbol):
    symbol = raw_symbol.strip().upper().replace(" ", "")
    if not SYMBOL_PATTERN.match(symbol):
        raise HTTPException(422, "use EXCHANGE:SYMBOL, for example FX:EURUSD")
    return symbol


# Uses the typed price, or the model's latest close for EUR/USD, and blocks typos or stale data.
def choose_fill_price(order, snapshot):
    model_price = snapshot["close"] if snapshot else None
    price = order.price or model_price
    if price is None:
        raise HTTPException(422, "enter a price for this symbol")
    if model_price and abs(price / model_price - 1) > MAX_PRICE_GAP:
        raise HTTPException(422, f"price is more than {MAX_PRICE_GAP * 100:.0f}% away from the model's latest "
                                 f"close {model_price:.5f}. Sync the latest bars and try again.")
    return price


# Makes sure a buy has a stop below and a target above the entry price.
def check_exit_levels(order, price):
    if order.side != "buy":
        return
    if order.stop_loss and order.stop_loss >= price:
        raise HTTPException(422, "stop loss must be below the buy price")
    if order.take_profit and order.take_profit <= price:
        raise HTTPException(422, "take profit must be above the buy price")


# Stops orders the virtual account cannot afford, and sells of units it does not hold.
def check_order_is_affordable(order, symbol, price, user):
    trades = fetch_rows("SELECT symbol, side, quantity, fill_price FROM paper_trades "
                        "WHERE user_id = ? ORDER BY id ASC", (user["id"],))
    holdings, _ = replay_fills(trades)
    cash = calculate_cash(user["starting_cash"], trades)
    if order.side == "buy" and order.quantity * price > cash + 1e-9:
        raise HTTPException(422, "not enough virtual cash for this order")
    held = holdings.get(symbol, {"quantity": 0.0})["quantity"]
    if order.side == "sell" and order.quantity > held + 1e-8:
        raise HTTPException(422, f"you hold {held:g} units of {symbol}; short selling is disabled")


# ----------------------------------------------------------------- saving trades --
# Lists the values for one paper_trades row, with blanks when no model forecast exists.
def build_trade_values(user_id, symbol, price, order, snapshot):
    model = snapshot or {}
    return (user_id, symbol, order.side, order.order_type, order.quantity, price,
            order.stop_loss, order.take_profit, order.note, utc_now_text(),
            model.get("bar_ts"), model.get("close"), model.get("model_version"),
            model.get("signal_action"), model.get("model_bias"), model.get("stop_level"),
            model.get("lorentzian_bias"), model.get("edge_verified"))


# Stores the three horizon forecasts that belong to one trade.
def save_forecast_rows(trade_id, snapshot):
    for forecast in (snapshot["forecasts"] if snapshot else []):
        paper["connection"].execute(INSERT_FORECAST_SQL, (
            trade_id, forecast["horizon_bars"], forecast["target_ts_est"],
            forecast["price_p10"], forecast["price_p50"], forecast["price_p90"]))


# Saves the trade and its forecast snapshot together so they can never get out of step.
def save_trade(user_id, symbol, price, order, snapshot):
    with database_lock:
        values = build_trade_values(user_id, symbol, price, order, snapshot)
        trade_id = paper["connection"].execute(INSERT_TRADE_SQL, values).lastrowid
        save_forecast_rows(trade_id, snapshot)
        paper["connection"].commit()
    return trade_id


# ------------------------------------------------------------ outcome tracking --
# Fills in the real price for every forecast whose target bar has now arrived in the engine.
def resolve_pending_outcomes(user_id):
    engine = paper["engine_state"].get("engine")
    if engine is None:
        return
    for row in fetch_rows(PENDING_FORECASTS_SQL, (user_id,)):
        actual = engine.close_at(pd.Timestamp(row["forecast_bar_ts"]), row["horizon_bars"])
        if actual:
            execute_write("UPDATE paper_trade_forecasts SET realised_price = ?, realised_bar_ts = ? WHERE id = ?",
                          (actual[1], actual[0].isoformat(), row["id"]))


# True when two price moves point the same way, None when either one is flat.
def direction_matches(expected_move, actual_move):
    if expected_move == 0 or actual_move == 0:
        return None
    return (expected_move > 0) == (actual_move > 0)


# Tells whether the model's median forecast pointed the same way as the user's trade.
def model_agrees_with_trade(trade, forecast):
    expected_move = forecast["price_p50"] - trade["forecast_close"]
    return expected_move > 0 if trade["side"] == "buy" else expected_move < 0


# Measures how the real price compared with the saved forecast.
def measure_outcome(trade, forecast, realised_price, side_sign):
    base_price = trade["forecast_close"]
    return {
        "inside_band": forecast["price_p10"] <= realised_price <= forecast["price_p90"],
        "error_pips": (realised_price - forecast["price_p50"]) / PIP,
        "direction_correct": direction_matches(forecast["price_p50"] - base_price, realised_price - base_price),
        "result_at_horizon": side_sign * (realised_price - trade["fill_price"]) * trade["quantity"],
    }


# Adds expected and actual results to one stored forecast so the dashboard can show hit or miss.
def compare_forecast(trade, forecast):
    comparison = dict(forecast)
    side_sign = 1 if trade["side"] == "buy" else -1
    comparison["agrees_with_trade"] = model_agrees_with_trade(trade, forecast)
    comparison["expected_result"] = side_sign * (forecast["price_p50"] - trade["fill_price"]) * trade["quantity"]
    comparison["resolved"] = forecast["realised_price"] is not None
    if comparison["resolved"]:
        comparison.update(measure_outcome(trade, forecast, forecast["realised_price"], side_sign))
    return comparison


# Hours between the model's bar closing and the trade, so stale forecasts are easy to spot.
def forecast_age_hours(trade):
    if not trade["forecast_bar_ts"]:
        return None
    bar_open = pd.Timestamp(trade["forecast_bar_ts"])
    seconds = (pd.Timestamp(trade["placed_at"]) - bar_open).total_seconds()
    return round(max(seconds / 3600 - 1, 0), 1)


# Groups stored forecast rows under the trade they belong to.
def group_forecasts_by_trade(forecast_rows):
    grouped = {}
    for row in forecast_rows:
        grouped.setdefault(row["trade_id"], []).append(row)
    return grouped


# Loads the user's trades oldest-first, each with its forecasts compared to real outcomes.
def load_trades_with_forecasts(user_id):
    trades = fetch_rows("SELECT * FROM paper_trades WHERE user_id = ? ORDER BY id ASC", (user_id,))
    by_trade = group_forecasts_by_trade(fetch_rows(USER_FORECASTS_SQL, (user_id,)))
    for trade in trades:
        stored = by_trade.get(trade["id"], [])
        trade["forecasts"] = [compare_forecast(trade, forecast) for forecast in stored]
        trade["forecast_age_hours"] = forecast_age_hours(trade)
    return trades


# Picks the resolved forecasts for one horizon across all of the user's trades.
def collect_resolved_forecasts(trades, horizon):
    return [forecast for trade in trades for forecast in trade["forecasts"]
            if forecast["horizon_bars"] == horizon and forecast["resolved"]]


# Turns a list of resolved forecasts into band hit rate, direction hit rate and average error.
def summarise_resolved(resolved):
    if not resolved:
        return {"resolved": 0, "inside_band_rate": None, "direction_hit_rate": None, "mean_abs_error_pips": None}
    called = [forecast for forecast in resolved if forecast["direction_correct"] is not None]
    return {
        "resolved": len(resolved),
        "inside_band_rate": sum(forecast["inside_band"] for forecast in resolved) / len(resolved),
        "direction_hit_rate": (sum(forecast["direction_correct"] for forecast in called) / len(called)) if called else None,
        "mean_abs_error_pips": sum(abs(forecast["error_pips"]) for forecast in resolved) / len(resolved),
    }


# Summarises, per horizon, how often the saved forecasts matched what the market really did.
def build_scorecard(trades):
    return {str(horizon): summarise_resolved(collect_resolved_forecasts(trades, horizon))
            for horizon in FORECAST_HORIZONS}


# ------------------------------------------------------------- account maths --
# Adds a buy fill to the running holding for its symbol.
def add_to_holding(holding, trade):
    holding["quantity"] += trade["quantity"]
    holding["cost"] += trade["quantity"] * trade["fill_price"]


# Removes sold units at average cost and returns the realized profit of that sale.
def reduce_holding(holding, trade):
    if holding["quantity"] <= 0:
        return 0.0
    sold = min(trade["quantity"], holding["quantity"])
    average_cost = holding["cost"] / holding["quantity"]
    holding["quantity"] -= sold
    holding["cost"] -= average_cost * sold
    if holding["quantity"] < 1e-8:
        holding["quantity"], holding["cost"] = 0.0, 0.0
    return (trade["fill_price"] - average_cost) * sold


# Replays the fills oldest-first to get open units, average cost and realized profit.
def replay_fills(trades):
    holdings, realized_pnl = {}, 0.0
    for trade in trades:
        holding = holdings.setdefault(trade["symbol"], {"quantity": 0.0, "cost": 0.0})
        if trade["side"] == "buy":
            add_to_holding(holding, trade)
        else:
            realized_pnl += reduce_holding(holding, trade)
    return holdings, realized_pnl


# Works out the virtual cash left after every fill, so cash can never drift out of sync.
def calculate_cash(starting_cash, trades):
    cash = starting_cash
    for trade in trades:
        notional = trade["quantity"] * trade["fill_price"]
        cash += notional if trade["side"] == "sell" else -notional
    return cash


# Describes one open position, with unrealised profit when the model has a live price.
def describe_one_position(symbol, holding):
    average_entry = holding["cost"] / holding["quantity"]
    market_price = latest_model_price(symbol)
    unrealised = None if market_price is None else (market_price - average_entry) * holding["quantity"]
    return {"symbol": symbol, "quantity": holding["quantity"], "average_entry": average_entry,
            "invested_cost": holding["cost"], "market_price": market_price, "unrealised_pnl": unrealised}


# Lists every position that still has units open.
def describe_positions(holdings):
    return [describe_one_position(symbol, holding) for symbol, holding in holdings.items()
            if holding["quantity"] > 1e-8]


# Builds everything the terminal needs in one object: cash, positions, P&L, scorecard and trade log.
def build_account(user):
    resolve_pending_outcomes(user["id"])
    trades = load_trades_with_forecasts(user["id"])
    holdings, realized_pnl = replay_fills(trades)
    return {
        "username": user["username"], "display_name": user["display_name"],
        "starting_cash": user["starting_cash"],
        "cash": calculate_cash(user["starting_cash"], trades),
        "realized_pnl": realized_pnl, "positions": describe_positions(holdings),
        "scorecard": build_scorecard(trades),
        "trades": list(reversed(trades))[:TRADE_LOG_LIMIT],
    }


# ----------------------------------------------------------------- endpoints --
# Creates a teammate's account and signs them in.
@router.post("/auth/register")
def register(form: RegisterForm):
    username = normalise_username(form.username)
    validate_username(username)
    display_name = (form.display_name or "").strip()[:40] or username
    try:
        user_id = execute_write(INSERT_USER_SQL, (username, display_name, hash_password(form.password),
                                                  STARTING_CASH, utc_now_text()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "that username is already taken")
    return build_session_reply(user_id, username, display_name)


# Signs a teammate in with the same error message for a wrong username or wrong password.
@router.post("/auth/login")
def login(form: LoginForm):
    user = find_user(normalise_username(form.username))
    stored_hash = user["password_hash"] if user else DUMMY_PASSWORD_HASH
    if not password_matches(form.password, stored_hash) or user is None:
        raise HTTPException(401, "wrong username or password")
    return build_session_reply(user["id"], user["username"], user["display_name"])


# Ends the current session so the token stops working.
@router.post("/auth/logout")
def logout(x_paper_token: str = Header(...), user: dict = Depends(current_user)):
    execute_write("DELETE FROM paper_sessions WHERE token_hash = ?", (hash_token(x_paper_token),))
    return {"signed_out": True}


# Returns the signed-in user's full paper account and forecast-vs-actual log.
@router.get("/account")
def get_account(user: dict = Depends(current_user)):
    return build_account(user)


# Places one simulated order, saves it with the model forecast, and returns the updated account.
@router.post("/orders")
def place_order(order: OrderForm, user: dict = Depends(current_user)):
    symbol = normalise_symbol(order.symbol)
    snapshot = take_forecast_snapshot(symbol)
    with order_lock:
        price = choose_fill_price(order, snapshot)
        check_exit_levels(order, price)
        check_order_is_affordable(order, symbol, price, user)
        trade_id = save_trade(user["id"], symbol, price, order, snapshot)
    return {"trade_id": trade_id, "forecast_saved": snapshot is not None, "account": build_account(user)}


# Deletes only the signed-in user's trades and restores their starting cash.
@router.post("/reset")
def reset_account(user: dict = Depends(current_user)):
    execute_write("DELETE FROM paper_trade_forecasts WHERE trade_id IN "
                  "(SELECT id FROM paper_trades WHERE user_id = ?)", (user["id"],))
    execute_write("DELETE FROM paper_trades WHERE user_id = ?", (user["id"],))
    return build_account(user)