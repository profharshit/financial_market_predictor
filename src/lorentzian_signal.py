"""
Lorentzian ANN buy/sell signal engine - ported from the "Krishna ML Predictor"
Pine Script indicator (via the team's earlier Flask prototype: kernels.py,
core/indicators.py, lorentzian_model.py). This module folds all three of
those into ONE self-contained file that drops straight into this project
next to indicators.py / features.py.

WHY A SEPARATE MODULE, NOT MERGED INTO indicators.py / features.py
  indicators.py + features.py are the trained price models' single source
  of truth (see features.py's own docstring on train/serve skew) - the xgb
  quantile models were fit on that exact feature set. The Lorentzian
  classifier is a self-contained system with its OWN normalized feature set
  (0-1 rescaled RSI/WaveTrend/CCI/ADX) and its OWN "training" (a k-NN lookup
  over recent bars, nothing to fit/pickle). Keeping it separate means adding
  it can never silently change what the price-forecast models see.

WHY THIS ISN'T JUST "RUN analyze() OVER THE ROLLING WINDOW EVERY BAR"
  The obvious approach - recompute the whole thing over inference_engine's
  existing rolling `combined` window every bar, like build_features()
  already does - was tried first and FAILED a single-bar-path ==
  batch-path check (the same property smoke_test_api.py verifies for the
  rest of the engine): the ANN prediction itself is robust to where the
  window starts, but the PERSISTED long/short state ("signal" holds
  LONG/SHORT until the ANN flips it, exactly like the ATR trailing stop's
  `atr_position`) gets silently reset to NEUTRAL at position 0 of every
  recomputed window, which shifts exactly where a "fresh flip" is detected
  near the window boundary. `trailing_stop.py` avoids the equivalent problem
  the same way this module now does: keep the CONTINUOUS, window-safe
  quantities (the ANN vote, filters, kernel trend) recomputed fresh each
  bar, but carry the DISCRETE state (signal, bars-held, pending hold-bar
  exits) as real persisted instance state via LorentzianEngine, never
  reconstructed from scratch.

WHAT IT PRODUCES, PER BAR
  lorentzian_bias         "up" / "down" / "neutral"  - persisted ANN+filter
                          state (mirrors trailing_stop's `trend` naming)
  lorentzian_flip         "buy" / "sell" / None      - set ONLY on the bar
                          where a fresh, fully-filtered entry fires (mirrors
                          trailing_stop's `flip`) - THIS is the buy/sell
                          marker for the chart.
  lorentzian_exit          "long" / "short" / None    - fixed hold_bars exit
  lorentzian_confidence   0-1                         - neighbor-vote strength
  lorentzian_kernel       float                       - Nadaraya-Watson
                          trend estimate (rational-quadratic kernel), same
                          units as price - the "gold line" on the chart.

Nothing here has been backtested yet. train_price_model.py now runs a
holdout, cost-aware backtest of a Lorentzian-gated trailing stop (mirroring
the existing xgb-quantile-gated one) and stores the verdict as
`lorentzian_skill` in the model artifact; the live engine only lets
lorentzian_flip drive signal.action once that verdict is True (see
inference_engine.py). Until then it is surfaced as information only -
exactly how the xgb quantile model's own (currently unproven) bias already
is.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

LONG, SHORT, NEUTRAL = 1, -1, 0
_BIAS_NAME = {LONG: "up", SHORT: "down", NEUTRAL: "neutral"}

OHLCV = ["open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# Kernel regression (Nadaraya-Watson) - jdehorty's KernelFunctions, unchanged
# ---------------------------------------------------------------------------

def _kernel_estimate(src: pd.Series, lookback: int, start_at_bar: int, *, gaussian: bool,
                     relative_weight: float = 8.0) -> pd.Series:
    values = src.to_numpy()
    n = len(values)
    out = np.full(n, np.nan)
    max_lookback = start_at_bar + 1
    i = np.arange(max_lookback)
    if gaussian:
        weights = np.exp(-(i ** 2) / (2 * (lookback ** 2)))
    else:
        weights = (1 + (i ** 2) / ((lookback ** 2) * 2 * relative_weight)) ** (-relative_weight)
    for t in range(n):
        window = min(t + 1, max_lookback)
        vals = values[t - window + 1: t + 1][::-1]
        w = weights[:window]
        wsum = w.sum()
        out[t] = float(np.dot(vals, w) / wsum) if wsum != 0 else values[t]
    return pd.Series(out, index=src.index)


# ---------------------------------------------------------------------------
# Normalized (0-1) feature set for the Lorentzian distance - its OWN
# indicator implementations, deliberately not shared with indicators.py so
# the trained price models never see a changed feature.
# ---------------------------------------------------------------------------

def _ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()


def _sma(s: pd.Series, length: int) -> pd.Series:
    return s.rolling(window=length, min_periods=1).mean()


def _rescale(s: pd.Series, window: int = 200) -> pd.Series:
    """Rolling min-max rescale to [0, 1], matching the Pine `ml.n_*` behaviour."""
    lo = s.rolling(window=window, min_periods=10).min()
    hi = s.rolling(window=window, min_periods=10).max()
    denom = (hi - lo).replace(0, np.nan)
    return ((s - lo) / denom).fillna(0.5).clip(0, 1)


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)


def _raw_rsi(src: pd.Series, length: int) -> pd.Series:
    delta = src.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def _n_rsi(close: pd.Series, length: int, smooth: int) -> pd.Series:
    return (_ema(_raw_rsi(close, length), max(smooth, 1)) / 100.0).clip(0, 1)


def _wave_trend(hlc3: pd.Series, a: int, b: int) -> pd.Series:
    esa = _ema(hlc3, a)
    d = _ema((hlc3 - esa).abs(), a)
    ci = ((hlc3 - esa) / (0.015 * d.replace(0, np.nan))).fillna(0)
    wt1 = _ema(ci, b)
    return wt1 - _sma(wt1, 4)


def _n_wt(hlc3: pd.Series, a: int, b: int) -> pd.Series:
    return _rescale(_wave_trend(hlc3, a, b))


def _cci(src: pd.Series, length: int) -> pd.Series:
    sma_val = _sma(src, length)
    mean_dev = src.rolling(window=length, min_periods=1).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=False)
    return ((src - sma_val) / (0.015 * mean_dev.replace(0, np.nan))).fillna(0)


def _n_cci(src: pd.Series, length: int, smooth: int) -> pd.Series:
    return _rescale(_ema(_cci(src, length), max(smooth, 1)))


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    up, down = high.diff(), -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = _true_range(high, low, close)
    atr_val = tr.ewm(alpha=1 / length, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * pd.Series(plus_dm, index=high.index).ewm(alpha=1 / length, adjust=False).mean() / atr_val
    minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(alpha=1 / length, adjust=False).mean() / atr_val
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100)
    return dx.ewm(alpha=1 / length, adjust=False).mean().fillna(0)


def _n_adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    return (_adx(high, low, close, length) / 100.0).clip(0, 1)


def _feature_series(kind: str, close, high, low, hlc3, a, b) -> pd.Series:
    if kind == "RSI":
        return _n_rsi(close, a, b)
    if kind == "WT":
        return _n_wt(hlc3, a, b)
    if kind == "CCI":
        return _n_cci(close, a, b)
    if kind == "ADX":
        return _n_adx(high, low, close, a)
    raise ValueError(f"unknown Lorentzian feature type: {kind}")


# ---------------------------------------------------------------------------
# Config - one dataclass, same knobs as the original Pine indicator's inputs
# ---------------------------------------------------------------------------

@dataclass
class LorentzianConfig:
    neighbors_count: int = 8
    max_bars_back: int = 260          # ANN neighbor-pool size, in bars
    hold_bars: int = 4                # fixed hold used for the training label AND the exit
    feature_defs: list = field(default_factory=lambda: [
        {"type": "RSI", "a": 14, "b": 1},
        {"type": "WT", "a": 10, "b": 11},
        {"type": "CCI", "a": 20, "b": 1},
        {"type": "ADX", "a": 20, "b": 2},
        {"type": "RSI", "a": 9, "b": 1},
    ])
    # entry filters
    use_volatility_filter: bool = True
    use_regime_filter: bool = True
    regime_threshold: float = -0.1
    use_adx_filter: bool = False
    adx_threshold: float = 20
    use_ema_filter: bool = False
    ema_period: int = 200
    use_sma_filter: bool = False
    sma_period: int = 200
    # kernel trend filter
    use_kernel_filter: bool = True
    use_kernel_smoothing: bool = False
    kernel_lookback: int = 8
    kernel_relative_weight: float = 8.0
    kernel_regression_level: int = 25
    kernel_lag: int = 2

    def as_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in self.__dict__.items()}


# ---------------------------------------------------------------------------
# The ANN core - faithful port of the Pine `for i = 0 to sizeLoop` loop
# ---------------------------------------------------------------------------

def _lorentzian_distances(current: np.ndarray, pool: np.ndarray) -> np.ndarray:
    """current: (F,); pool: (N, F) -> (N,) distances = sum_f log(1 + |delta|)."""
    return np.log1p(np.abs(current[None, :] - pool)).sum(axis=1)


def _ann_predict(distances: np.ndarray, labels: np.ndarray, k: int) -> float:
    last_distance = -1.0
    kept_d: list[float] = []
    kept_p: list[int] = []
    for i, d in enumerate(distances):
        if d >= last_distance and (i % 4 != 0):
            last_distance = d
            kept_d.append(d)
            kept_p.append(labels[i])
            if len(kept_p) > k:
                cut = min(round(k * 3 / 4), len(kept_d) - 1)
                last_distance = kept_d[cut]
                kept_d.pop(0)
                kept_p.pop(0)
    return float(sum(kept_p)) if kept_p else 0.0


def _training_labels(close: pd.Series, hold_bars: int) -> np.ndarray:
    shifted = close.shift(hold_bars)
    labels = np.where(shifted < close, SHORT, np.where(shifted > close, LONG, NEUTRAL))
    labels[:hold_bars] = NEUTRAL
    return labels.astype(int)


# ---------------------------------------------------------------------------
# Continuous, window-safe per-bar quantities: robust to WHERE the supplied
# frame starts (verified empirically), so it is fine to recompute these
# fresh over inference_engine's rolling window every call, the same way
# build_features() recomputes ML features. Everything EXCEPT the discrete
# signal/hold/exit state lives here.
# ---------------------------------------------------------------------------

def _continuous_series(ohlcv: pd.DataFrame, cfg: LorentzianConfig) -> dict:
    df = ohlcv[OHLCV].astype(float)
    n = len(df)
    close, high, low = df["close"], df["high"], df["low"]
    hlc3 = (high + low + close) / 3.0

    feats = np.column_stack([_feature_series(f["type"], close, high, low, hlc3, f["a"], f["b"]).to_numpy()
                             for f in cfg.feature_defs])
    labels = _training_labels(close, cfg.hold_bars)

    predictions = np.zeros(n)
    warmup = max(30, cfg.hold_bars + 5)
    for t in range(warmup, n):
        pool_start = max(0, t - cfg.max_bars_back)
        if t - pool_start < 8:
            continue
        d = _lorentzian_distances(feats[t], feats[pool_start:t])
        predictions[t] = _ann_predict(d, labels[pool_start:t], cfg.neighbors_count)

    filters_ok = np.ones(n, dtype=bool)
    if cfg.use_volatility_filter:
        tr = _true_range(high, low, close)
        filters_ok &= (tr.ewm(alpha=1, adjust=False).mean()
                       > tr.ewm(alpha=1 / 10, adjust=False).mean()).to_numpy()
    if cfg.use_regime_filter:
        lb = 20
        atr_lb = _true_range(high, low, close).ewm(alpha=1 / lb, adjust=False).mean().replace(0, np.nan)
        slope = ((close - close.shift(lb)) / (atr_lb * lb)).fillna(0)
        filters_ok &= (slope > cfg.regime_threshold).to_numpy()
    if cfg.use_adx_filter:
        filters_ok &= (_adx(high, low, close, 14) > cfg.adx_threshold).to_numpy()

    yhat1 = _kernel_estimate(close, cfg.kernel_lookback, cfg.kernel_regression_level,
                             gaussian=False, relative_weight=cfg.kernel_relative_weight).to_numpy()
    if cfg.use_kernel_filter:
        if cfg.use_kernel_smoothing:
            yhat2 = _kernel_estimate(close, cfg.kernel_lookback - cfg.kernel_lag,
                                     cfg.kernel_regression_level, gaussian=True).to_numpy()
            bullish, bearish = yhat2 >= yhat1, yhat2 <= yhat1
        else:
            bullish = np.r_[False, yhat1[1:] > yhat1[:-1]]
            bearish = np.r_[False, yhat1[1:] < yhat1[:-1]]
    else:
        bullish = bearish = np.ones(n, dtype=bool)

    if cfg.use_ema_filter:
        ema = close.ewm(span=cfg.ema_period, adjust=False).mean().to_numpy()
        ema_up, ema_down = close.to_numpy() > ema, close.to_numpy() < ema
    else:
        ema_up = ema_down = np.ones(n, dtype=bool)
    if cfg.use_sma_filter:
        sma = close.rolling(cfg.sma_period, min_periods=1).mean().to_numpy()
        sma_up, sma_down = close.to_numpy() > sma, close.to_numpy() < sma
    else:
        sma_up = sma_down = np.ones(n, dtype=bool)

    warmed = np.arange(n) >= warmup
    return dict(n=n, predictions=predictions, filters_ok=filters_ok & warmed, bullish=bullish, bearish=bearish,
               ema_up=ema_up, ema_down=ema_down, sma_up=sma_up, sma_down=sma_down, kernel=yhat1, warmed=warmed)


def _confidence(prediction: float, cfg: LorentzianConfig) -> float:
    return float(np.clip(abs(prediction) / max(cfg.neighbors_count, 1), 0, 1))


# ---------------------------------------------------------------------------
# Batch (pure, full-history) - for offline backtesting in train_price_model.py.
# Computed once over the TRUE full history, so there is no window-boundary
# state issue here at all; this is the reference implementation the live
# LorentzianEngine below is checked against.
# ---------------------------------------------------------------------------

def analyze_lorentzian(ohlcv: pd.DataFrame, cfg: LorentzianConfig | None = None) -> pd.DataFrame:
    """
    ohlcv: UTC DatetimeIndex, columns open/high/low/close/volume. Intended
    for backtesting over a frame that already IS the full relevant history
    (see LorentzianEngine for the live, bar-by-bar equivalent).

    Returns a frame aligned to ohlcv.index with:
        lorentzian_bias, lorentzian_flip, lorentzian_exit,
        lorentzian_confidence, lorentzian_kernel
    """
    cfg = cfg or LorentzianConfig()
    c = _continuous_series(ohlcv, cfg)
    n = c["n"]

    signal = np.zeros(n, dtype=int)
    bars_held = np.zeros(n, dtype=int)
    start_long = np.zeros(n, dtype=bool)
    start_short = np.zeros(n, dtype=bool)
    end_long = np.zeros(n, dtype=bool)
    end_short = np.zeros(n, dtype=bool)
    pending: deque = deque()

    for t in range(1, n):
        prev = signal[t - 1]
        if c["filters_ok"][t] and c["predictions"][t] > 0:
            signal[t] = LONG
        elif c["filters_ok"][t] and c["predictions"][t] < 0:
            signal[t] = SHORT
        else:
            signal[t] = prev
        changed = signal[t] != prev
        bars_held[t] = 0 if changed else bars_held[t - 1] + 1

        is_buy = signal[t] == LONG and c["ema_up"][t] and c["sma_up"][t]
        is_sell = signal[t] == SHORT and c["ema_down"][t] and c["sma_down"][t]
        start_long[t] = is_buy and changed and bool(c["bullish"][t])
        start_short[t] = is_sell and changed and bool(c["bearish"][t])
        if start_long[t]:
            pending.append((t, LONG))
        if start_short[t]:
            pending.append((t, SHORT))

        target = t - cfg.hold_bars
        if pending and pending[0][0] == target and bars_held[t] >= cfg.hold_bars:
            idx, sig = pending.popleft()
            if sig == LONG and signal[t] == LONG:
                end_long[t] = True
            elif sig == SHORT and signal[t] == SHORT:
                end_short[t] = True
        while pending and pending[0][0] <= target:
            pending.popleft()

    out = pd.DataFrame(index=ohlcv.index)
    out["lorentzian_bias"] = [_BIAS_NAME[s] for s in signal]
    # IMPORTANT for callers: pandas (3.x, with future.infer_string on by default)
    # silently rewrites the `default=None` entries below as float NaN once they
    # land in a DataFrame string column - not Python None - no matter what
    # dtype is requested on the way in. Reading a "no flip" cell back out of
    # this frame (itertuples, to_dict, ...) must therefore check pd.isna(...)
    # and convert to None explicitly before it reaches any plain (non-Pydantic)
    # JSON response - see inference_engine.py's warm_up() for the pattern.
    # LorentzianEngine.step()'s live dict output is NOT affected (it never
    # puts flip/exit through a pandas column), only this batch/backtest path.
    out["lorentzian_flip"] = np.select([start_long, start_short], ["buy", "sell"], default=None)
    out["lorentzian_exit"] = np.select([end_long, end_short], ["long", "short"], default=None)
    out["lorentzian_confidence"] = np.clip(np.abs(c["predictions"]) / max(cfg.neighbors_count, 1), 0, 1)
    out["lorentzian_kernel"] = c["kernel"]
    out.loc[~c["warmed"], "lorentzian_bias"] = "neutral"
    out.loc[~c["warmed"], "lorentzian_flip"] = None
    out.loc[~c["warmed"], "lorentzian_exit"] = None
    out.loc[~c["warmed"], "lorentzian_confidence"] = 0.0
    return out


# ---------------------------------------------------------------------------
# Live, stateful engine - genuinely persists the discrete signal/hold/exit
# state across calls (like trailing_stop.TrailingStopManager), while still
# recomputing the continuous ANN/filter/kernel inputs from a rolling window
# each call. warm_up() reuses the fast vectorized batch path above to seed
# that persisted state instead of replaying bar-by-bar.
# ---------------------------------------------------------------------------

class LorentzianEngine:
    def __init__(self, cfg: LorentzianConfig | None = None):
        self.cfg = cfg or LorentzianConfig()
        self.reset()

    def reset(self):
        self.signal = NEUTRAL
        self.bars_held = 0
        self.entries: deque = deque()   # (absolute bar index, LONG/SHORT) awaiting their hold-bar exit
        self._n = 0                     # absolute bar index of the NEXT bar to process

    def warm_up(self, ohlcv: pd.DataFrame) -> None:
        """Seed persisted state from the full history, via one fast batch pass."""
        self.reset()
        cfg = self.cfg
        c = _continuous_series(ohlcv, cfg)
        n = c["n"]
        if n == 0:
            return
        signal = NEUTRAL
        bars_held = 0
        pending: deque = deque()
        for t in range(1, n):
            prev = signal
            if c["filters_ok"][t] and c["predictions"][t] > 0:
                signal = LONG
            elif c["filters_ok"][t] and c["predictions"][t] < 0:
                signal = SHORT
            changed = signal != prev
            bars_held = 0 if changed else bars_held + 1
            is_buy = signal == LONG and c["ema_up"][t] and c["sma_up"][t]
            is_sell = signal == SHORT and c["ema_down"][t] and c["sma_down"][t]
            if is_buy and changed and c["bullish"][t]:
                pending.append((t, LONG))
            if is_sell and changed and c["bearish"][t]:
                pending.append((t, SHORT))
            target = t - cfg.hold_bars
            while pending and pending[0][0] <= target:
                pending.popleft()
        self.signal, self.bars_held, self._n = signal, bars_held, n
        # re-base to indices relative to "bars before the next live bar" (negative == already in the past)
        self.entries = deque((idx - n, sig) for idx, sig in pending)

    def step(self, prediction: float, filters_ok: bool, bullish: bool, bearish: bool,
             ema_up: bool, ema_down: bool, sma_up: bool, sma_down: bool, kernel: float) -> dict:
        cfg = self.cfg
        prev = self.signal
        if filters_ok and prediction > 0:
            new_signal = LONG
        elif filters_ok and prediction < 0:
            new_signal = SHORT
        else:
            new_signal = prev
        changed = new_signal != prev
        self.bars_held = 0 if changed else self.bars_held + 1

        is_buy = new_signal == LONG and ema_up and sma_up
        is_sell = new_signal == SHORT and ema_down and sma_down
        start_long = is_buy and changed and bool(bullish)
        start_short = is_sell and changed and bool(bearish)
        if start_long:
            self.entries.append((self._n, LONG))
        if start_short:
            self.entries.append((self._n, SHORT))

        exit_long = exit_short = False
        target = self._n - cfg.hold_bars
        if self.entries and self.entries[0][0] == target and self.bars_held >= cfg.hold_bars:
            idx, sig = self.entries.popleft()
            if sig == LONG and new_signal == LONG:
                exit_long = True
            elif sig == SHORT and new_signal == SHORT:
                exit_short = True
        while self.entries and self.entries[0][0] <= target:
            self.entries.popleft()

        self.signal = new_signal
        self._n += 1
        return {
            "bias": _BIAS_NAME[new_signal],
            "flip": "buy" if start_long else "sell" if start_short else None,
            "exit": "long" if exit_long else "short" if exit_short else None,
            "confidence": _confidence(prediction, cfg),
            "kernel": float(kernel),
        }

    def update(self, window_ohlcv: pd.DataFrame) -> dict:
        """
        window_ohlcv: a rolling window ENDING at the new bar (the same
        `combined` frame inference_engine already builds for the price
        features). Only the last row's continuous inputs are used; the
        discrete state is this engine's own persisted state, not the
        window's.
        """
        c = _continuous_series(window_ohlcv, self.cfg)
        i = c["n"] - 1
        return self.step(c["predictions"][i], bool(c["filters_ok"][i]), bool(c["bullish"][i]),
                         bool(c["bearish"][i]), bool(c["ema_up"][i]), bool(c["ema_down"][i]),
                         bool(c["sma_up"][i]), bool(c["sma_down"][i]), c["kernel"][i])

    def update_batch(self, window_ohlcv: pd.DataFrame, n_new: int) -> list[dict]:
        """
        Same idea as update(), but for several new bars landing in one
        on_bars() call (e.g. /predict/batch): compute the continuous series
        ONCE over the whole window (cheap: one pass) instead of once per new
        bar, then step the persisted discrete state through the new bars in
        order. Gives identical results to calling update() n_new times in a
        loop, just far less redundant computation for large batches.
        """
        c = _continuous_series(window_ohlcv, self.cfg)
        n = c["n"]
        out = []
        for i in range(n - n_new, n):
            out.append(self.step(c["predictions"][i], bool(c["filters_ok"][i]), bool(c["bullish"][i]),
                                 bool(c["bearish"][i]), bool(c["ema_up"][i]), bool(c["ema_down"][i]),
                                 bool(c["sma_up"][i]), bool(c["sma_down"][i]), c["kernel"][i]))
        return out