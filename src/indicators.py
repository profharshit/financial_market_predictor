"""
Custom technical indicator library for the EUR/USD predictor.

Design: each indicator is a plain function that takes the OHLCV DataFrame
and returns either a Series (for single-column indicators) or a DataFrame
(for multi-column ones like MACD or Bollinger Bands). Register it in
INDICATOR_REGISTRY and it's automatically picked up by apply_indicators().

This means any team member can add a new indicator without touching the
pipeline - just write a function here and register it.

CHANGES in this version
  * apply_indicators() no longer re-reads eurusd_ohlcv.csv from disk (the old
    version ignored its `df` argument and also ran itself at import time, so
    live inference would have silently scored stale CSV data).
  * atr_trailing_stop() accepts a per-bar key_value array, which is what makes
    the ADAPTIVE trailing stop possible (see atr_trailing_stop_adaptive).
"""

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# STANDARD INDICATORS
# ---------------------------------------------------------------------------

def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Relative Strength Index - momentum oscillator, 0-100."""
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    return result.rename("rsi")


def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line, signal line, and histogram."""
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()

    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    return pd.DataFrame({
        "macd_line": macd_line,
        "macd_signal": signal_line,
        "macd_hist": histogram,
    })


def bollinger_bands(df: pd.DataFrame, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands + %B (position within the bands, 0-1)."""
    sma = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()

    upper = sma + num_std * std
    lower = sma - num_std * std
    percent_b = (df["close"] - lower) / (upper - lower)

    return pd.DataFrame({
        "bb_upper": upper,
        "bb_lower": lower,
        "bb_mid": sma,
        "bb_percent_b": percent_b,
    })


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range - volatility measure."""
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()

    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean().rename("atr")


# ---------------------------------------------------------------------------
# CUSTOM / TEAM-SPECIFIC INDICATORS
# ---------------------------------------------------------------------------

def momentum_zscore(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """z-score of the latest 1-bar return vs the recent mean/std of returns."""
    returns = df["close"].pct_change()
    rolling_mean = returns.rolling(period).mean()
    rolling_std = returns.rolling(period).std()
    zscore = (returns - rolling_mean) / rolling_std.replace(0, np.nan)
    return zscore.rename("momentum_zscore")


def atr_trailing_stop(df: pd.DataFrame, key_value=3.0, atr_period: int = 10) -> pd.DataFrame:
    """
    ATR Trailing Stop (Chandelier-exit style) - converted from the Pine Script
    indicator "Krishna's ATR Bot".

    STATEFUL: each bar's stop depends on the previous bar's stop, exactly like
    Pine's `var float xATRTrailingStop = na`. Needs an explicit bar-by-bar loop.

    key_value may be a float (original Pine behaviour, default 3.0) or an
    array/Series with one multiplier per bar (adaptive stop).

    Returns 4 columns:
      - atr_trailing_stop : the trailing stop level
      - atr_position       : 1 (long), -1 (short), 0 (flat/undetermined)
      - atr_buy_signal     : 1 where price crosses above the stop
      - atr_sell_signal    : 1 where price crosses below the stop
    """
    src = df["close"].to_numpy()
    atr_vals = atr(df, period=atr_period).to_numpy()
    key = np.asarray(key_value, dtype=float)
    n_loss = key * atr_vals
    n = len(src)

    stop = np.full(n, np.nan)
    pos = np.zeros(n, dtype=int)
    buy = np.zeros(n, dtype=int)
    sell = np.zeros(n, dtype=int)

    for i in range(n):
        if i == 0 or np.isnan(n_loss[i]):
            # Mirrors `nz(xATRTrailingStop[1], src)` before ATR warms up
            stop[i] = src[i]
            continue

        prev_stop = stop[i - 1] if not np.isnan(stop[i - 1]) else src[i - 1]
        prev_src = src[i - 1]

        if src[i] > prev_stop and prev_src > prev_stop:
            stop[i] = max(prev_stop, src[i] - n_loss[i])
        elif src[i] < prev_stop and prev_src < prev_stop:
            stop[i] = min(prev_stop, src[i] + n_loss[i])
        elif src[i] > prev_stop:
            stop[i] = src[i] - n_loss[i]
        else:
            stop[i] = src[i] + n_loss[i]

        if prev_src < prev_stop and src[i] > stop[i]:
            pos[i] = 1
            buy[i] = 1
        elif prev_src > prev_stop and src[i] < stop[i]:
            pos[i] = -1
            sell[i] = 1
        else:
            pos[i] = pos[i - 1]

    return pd.DataFrame({
        "atr_trailing_stop": stop,
        "atr_position": pos,
        "atr_buy_signal": buy,
        "atr_sell_signal": sell,
    }, index=df.index)


# --- adaptive key multiplier -------------------------------------------------
# The stop distance is key * ATR. ATR already scales with volatility; the
# adaptive layer additionally scales `key` by how hot/cold the market is
# relative to its own recent norm:
#     multiplier = clip( (ATR / mean(ATR, last 100 bars)) ** 0.5 , 0.75, 1.5 )
# Hot market  -> wider stop (fewer noise stop-outs)
# Quiet market -> tighter stop (lock in gains sooner)
# Because the stop only ratchets in the trend's favour, a wider key never
# LOOSENS an existing stop - it only slows how fast a new stop tightens.
ADAPT_LOOKBACK = 100
ADAPT_LO, ADAPT_HI, ADAPT_POWER = 0.75, 1.5, 0.5


def adaptive_key_multiplier(atr_series: pd.Series, lookback: int = ADAPT_LOOKBACK,
                            lo: float = ADAPT_LO, hi: float = ADAPT_HI,
                            power: float = ADAPT_POWER) -> pd.Series:
    ref = atr_series.rolling(lookback, min_periods=lookback).mean()
    mult = (atr_series / ref) ** power
    return mult.clip(lo, hi).fillna(1.0)


def atr_trailing_stop_adaptive(df: pd.DataFrame, base_key: float = 3.0,
                               atr_period: int = 10, **adapt_kwargs) -> pd.DataFrame:
    """Same as atr_trailing_stop but with the volatility-adaptive key."""
    mult = adaptive_key_multiplier(atr(df, period=atr_period), **adapt_kwargs)
    out = atr_trailing_stop(df, key_value=base_key * mult.to_numpy(), atr_period=atr_period)
    out["key_multiplier"] = mult.to_numpy()
    return out


def session_range_ratio(df: pd.DataFrame) -> pd.Series:
    """Where the close sits inside the candle's range (1 = at high, 0 = at low)."""
    candle_range = (df["high"] - df["low"]).replace(0, np.nan)
    ratio = (df["close"] - df["low"]) / candle_range
    return ratio.rename("session_range_ratio")


# ---------------------------------------------------------------------------
# REGISTRY - add new indicators here to include them automatically
# ---------------------------------------------------------------------------

INDICATOR_REGISTRY = {
    "rsi": rsi,
    "macd": macd,
    "bollinger_bands": bollinger_bands,
    "atr": atr,
    "momentum_zscore": momentum_zscore,
    "session_range_ratio": session_range_ratio,
    "atr_trailing_stop": atr_trailing_stop,
}


def apply_indicators(df: pd.DataFrame, indicators: list[str] | None = None) -> pd.DataFrame:
    """
    Apply registered indicators to an OHLCV DataFrame and return a new
    DataFrame with the indicator columns appended.

    df must have ['open','high','low','close','volume'] columns.
    indicators: registry keys to apply (default: all).
    """
    df = df.copy()
    keys = indicators or list(INDICATOR_REGISTRY.keys())

    for key in keys:
        if key not in INDICATOR_REGISTRY:
            raise KeyError(f"Unknown indicator '{key}'. Available: {list(INDICATOR_REGISTRY)}")

        result = INDICATOR_REGISTRY[key](df)

        if isinstance(result, pd.Series):
            df[result.name] = result
        elif isinstance(result, pd.DataFrame):
            df = pd.concat([df, result], axis=1)
        else:
            raise TypeError(f"Indicator '{key}' returned unsupported type {type(result)}")

    return df
