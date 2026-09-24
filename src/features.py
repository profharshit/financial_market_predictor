"""
Single source of truth for model features + targets.

Used by BOTH train_price_model.py (batch) and inference_engine.py (live), so
the model can never see a differently-computed feature in production than it
was trained on (train/serve skew).

Every feature is scale-free / stationary: returns and ranges are expressed in
ATR units, volatilities as ratios, price LEVELS are never used (a tree would
memorise "when" a bar occurred instead of how the market behaved).
"""

import numpy as np
import pandas as pd

from indicators import apply_indicators

HORIZONS = (1, 4, 24)          # forecast horizons, in TRADING bars (hours)
SIGNAL_HORIZON = 4             # horizon used to gate buy/sell signals
PIP = 0.0001

FEATURE_COLS = [
    # multi-horizon returns, in units of expected move (ATR * sqrt(n))
    "zret_1", "zret_2", "zret_3", "zret_6", "zret_12", "zret_24",
    # volatility level & regime
    "atr_pct", "vol_24", "vol_ratio_6_24", "vol_ratio_24_72",
    # oscillators / bands, normalised
    "rsi", "macd_hist_atr", "bb_percent_b", "bb_width", "momentum_zscore",
    # candle shape
    "session_range_ratio", "range_atr", "body_atr",
    # user's ATR trailing stop (Pine conversion), stationary forms only
    "atr_position", "stop_dist_atr", "bars_since_flip",
    # calendar / activity
    "hour_sin", "hour_cos", "dow", "vol_rel_24",
]


def load_ohlcv(path: str) -> pd.DataFrame:
    """
    Load raw OHLCV, tolerant of the two file shapes seen in this project:
    with a header row, or headerless with extra columns after volume.
    Returns a UTC-indexed frame with open/high/low/close/volume only.
    """
    cols = ["timestamp", "open", "high", "low", "close", "volume"]
    with open(path) as f:
        first = f.readline().strip().lower()
    has_header = first.startswith("timestamp")
    df = pd.read_csv(path, header=0 if has_header else None,
                     usecols=range(6), names=None if has_header else cols)
    df = df[cols] if has_header else df
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.drop_duplicates("timestamp").sort_values("timestamp").set_index("timestamp")
    return df.astype(float)


def build_features(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """
    ohlcv: UTC DatetimeIndex, columns open/high/low/close/volume.
    Returns a frame with FEATURE_COLS plus helper columns `close`, `sigma`
    (ATR/close, the per-bar volatility unit used to scale targets).
    Rows before all indicators have warmed up contain NaN.
    """
    d = apply_indicators(ohlcv[["open", "high", "low", "close", "volume"]])
    close = d["close"]
    logret = np.log(close / close.shift(1))
    sigma = (d["atr"] / close).rename("sigma")          # per-bar vol unit

    f = pd.DataFrame(index=d.index)
    for n in (1, 2, 3, 6, 12, 24):
        f[f"zret_{n}"] = np.log(close / close.shift(n)) / (sigma * np.sqrt(n))

    f["atr_pct"] = sigma
    f["vol_24"] = logret.rolling(24).std()
    vol_6, vol_72 = logret.rolling(6).std(), logret.rolling(72).std()
    f["vol_ratio_6_24"] = vol_6 / f["vol_24"]
    f["vol_ratio_24_72"] = f["vol_24"] / vol_72

    f["rsi"] = d["rsi"]
    f["macd_hist_atr"] = d["macd_hist"] / d["atr"]
    f["bb_percent_b"] = d["bb_percent_b"]
    f["bb_width"] = (d["bb_upper"] - d["bb_lower"]) / d["bb_mid"]
    f["momentum_zscore"] = d["momentum_zscore"]

    f["session_range_ratio"] = d["session_range_ratio"]
    f["range_atr"] = (d["high"] - d["low"]) / d["atr"]
    f["body_atr"] = (d["close"] - d["open"]) / d["atr"]

    f["atr_position"] = d["atr_position"].astype(float)
    f["stop_dist_atr"] = (close - d["atr_trailing_stop"]) / d["atr"]
    flip = (d["atr_buy_signal"] + d["atr_sell_signal"]) > 0
    f["bars_since_flip"] = flip.cumsum().groupby(flip.cumsum()).cumcount().clip(upper=200).astype(float)

    idx = d.index
    f["hour_sin"] = np.sin(2 * np.pi * idx.hour / 24)
    f["hour_cos"] = np.cos(2 * np.pi * idx.hour / 24)
    f["dow"] = idx.dayofweek.astype(float)
    f["vol_rel_24"] = d["volume"] / d["volume"].rolling(24).mean()

    f["close"], f["sigma"] = close, sigma
    return f.replace([np.inf, -np.inf], np.nan)


def make_targets(feat: pd.DataFrame, horizons=HORIZONS) -> pd.DataFrame:
    """
    Volatility-scaled forward return, one column per horizon:
        z_h = log(close[t+h] / close[t]) / (sigma[t] * sqrt(h))
    Scaling by the CURRENT volatility makes the target roughly homoskedastic,
    so one model serves calm and violent regimes; the price band is recovered
    by multiplying back (see z_to_price).
    """
    out = pd.DataFrame(index=feat.index)
    for h in horizons:
        fwd = np.log(feat["close"].shift(-h) / feat["close"])
        out[f"z_{h}"] = fwd / (feat["sigma"] * np.sqrt(h))
    return out


def z_to_price(z, close, sigma, h):
    """Invert the target scaling: z-quantile -> price."""
    return close * np.exp(np.asarray(z) * sigma * np.sqrt(h))


def add_trading_hours(ts: pd.Timestamp, h: int) -> pd.Timestamp:
    """Estimate the timestamp h TRADING hours ahead (skips the weekend close). +/-1h (DST)."""
    t = ts
    for _ in range(h):
        t = t + pd.Timedelta(hours=1)
        while (t.dayofweek == 5) or (t.dayofweek == 4 and t.hour >= 22) or \
              (t.dayofweek == 6 and t.hour < 22):
            t = t + pd.Timedelta(hours=1)
    return t
