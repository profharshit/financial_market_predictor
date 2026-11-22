"""
PredictionEngine - the live brain. Four cooperating parts:

  1. FEATURES   features.build_features() on a rolling window of recent bars
                (the SAME function training used -> no train/serve skew)
  2. FORECAST   per-horizon quantile model -> price band (p10 / p50 / p90)
  3. STOP       TrailingStopManager: persistent, adaptive, ratchet-only stop
  4. LORENTZIAN LorentzianEngine: persistent ANN buy/sell classifier (ported
                "Krishna ML Predictor") - a second, independent read on
                direction, used to further confirm BUY/SELL once (and only
                once) train_price_model.py's backtest has shown it actually
                adds edge net of costs (see `lorentzian_skill` below).

`on_bars()` is the single entry point (one bar = a batch of one). All bars in a
call are validated BEFORE any state is touched, so a bad bar can never leave
the engine half-updated.
"""

from __future__ import annotations

from collections import deque

import joblib
import numpy as np
import pandas as pd

from features import (FEATURE_COLS, PIP, add_trading_hours, build_features, z_to_price)
from lorentzian_signal import LorentzianConfig, LorentzianEngine, analyze_lorentzian
from price_models import QUANTILES  # noqa: F401  (import so joblib can resolve model classes)
from trailing_stop import TrailingStopManager

OHLCV = ["open", "high", "low", "close", "volume"]
MIN_BARS = 200          # below this the indicators/features are not warmed up


class BarRejected(ValueError):
    """Raised for a bar that must not enter the engine. .status is the HTTP code to use."""
    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


class PredictionEngine:
    def __init__(self, model_path: str, window: int = 600, symbol: str = "EURUSD",
                 timeframe: str = "1h", max_bar_move: float = 0.03):
        self.art = joblib.load(model_path)
        self.window, self.symbol, self.timeframe = window, symbol, timeframe
        self.max_bar_move = max_bar_move
        self.hist = pd.DataFrame(columns=OHLCV, dtype=float)
        self.hist.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
        self.stop = TrailingStopManager(adaptive=True)
        self.stop_hist: deque = deque(maxlen=window)   # (level, trend, flip) aligned with self.hist
        self.lorentzian_hist: deque = deque(maxlen=window)   # (bias, flip, confidence, kernel) aligned with self.hist
        self.last_result: dict | None = None
        self.sig_h = self.art["signal_horizon"]

        # Lorentzian ANN signal - config + "has it shown a net edge" verdict
        # both come from the trained artifact when present (train_price_model.py
        # writes them), so an OLDER model artifact (trained before this was
        # added) still loads fine: the engine just falls back to defaults and
        # treats the signal as informational-only (lorentzian_skill=False),
        # exactly like the xgb quantile model's own bias already is until
        # `directional_skill` is established.
        lor_cfg = self.art.get("lorentzian_config")
        self.lorentzian = LorentzianEngine(LorentzianConfig(**lor_cfg) if lor_cfg else LorentzianConfig())
        self.lorentzian_skill = bool(self.art.get("lorentzian_skill", False))

    # ------------------------------------------------------------ properties
    @property
    def version(self) -> str:
        return self.art["version"]

    @property
    def ready(self) -> bool:
        return len(self.hist) >= MIN_BARS

    @property
    def last_ts(self):
        return self.hist.index[-1] if len(self.hist) else None

    # --------------------------------------------------------------- warm-up
    def warm_up(self, ohlcv: pd.DataFrame, predict_last: bool = True) -> None:
        """
        Feed history (UTC-indexed OHLCV). The stop and Lorentzian engine both see
        ALL of it; features keep the last `window` bars. With predict_last, the
        final bar is pushed through on_bars() so /latest and the dashboard have
        a prediction immediately after startup.
        """
        ohlcv = ohlcv[OHLCV].astype(float).sort_index()
        body = ohlcv.iloc[:-1] if (predict_last and len(ohlcv) > MIN_BARS) else ohlcv
        self.stop.reset()
        self.stop_hist.clear()
        for h, l, c in zip(body["high"].to_numpy(), body["low"].to_numpy(), body["close"].to_numpy()):
            st = self.stop.update(float(h), float(l), float(c))
            self.stop_hist.append((st["stop"], st["trend"], "buy" if st["buy"] else "sell" if st["sell"] else None))
        self.lorentzian.warm_up(body)   # same `body` the stop warms on - on_bars() below covers the rest
        # Chart-history-only approximation for older bars: one batch pass over
        # enough trailing runway (window + buffer) for the continuous ANN/filter
        # values to be well converged, THEN keep only the last `window` rows.
        # This is display data, not engine state - it is never read back into
        # self.lorentzian (that only ever advances via the exact incremental
        # warm_up()/update_batch() path above and below).
        self.lorentzian_hist.clear()
        lor_runway = body.tail(self.window + 400)
        lor_disp = analyze_lorentzian(lor_runway, self.lorentzian.cfg).tail(min(self.window, len(body)))
        for row in lor_disp.itertuples():
            # pandas 3.x rewrites "no flip" as float NaN inside this DataFrame
            # column, not Python None - see analyze_lorentzian()'s docstring
            # comment. Must sanitize here or /bars fails to JSON-serialize.
            flip = None if pd.isna(row.lorentzian_flip) else row.lorentzian_flip
            self.lorentzian_hist.append((row.lorentzian_bias, flip, float(row.lorentzian_confidence),
                                         float(row.lorentzian_kernel)))
        self.hist = body.tail(self.window).copy()
        if body is not ohlcv:
            self.on_bars(ohlcv.iloc[[-1]])

    def recent(self, limit: int = 300) -> dict:
        """Last `limit` bars + the stop/Lorentzian signal that was live at each one (for charting)."""
        n = min(limit, len(self.hist), len(self.stop_hist))
        h, st = self.hist.tail(n), list(self.stop_hist)[-n:]
        lor = list(self.lorentzian_hist)[-n:]
        return {
            "bars": [{"t": ts.isoformat(), "o": r.open, "h": r.high, "l": r.low, "c": r.close, "v": r.volume}
                     for ts, r in h.iterrows()],
            "stop": [{"level": a, "trend": b, "flip": c} for a, b, c in st],
            "lorentzian": [{"bias": a, "flip": b, "confidence": c, "kernel": d} for a, b, c, d in lor],
        }

    # ------------------------------------------------------------ validation
    def _validate(self, new: pd.DataFrame) -> list[str]:
        """Return per-bar warnings; raise BarRejected for anything unsafe."""
        prev_ts, prev_close = self.last_ts, (self.hist["close"].iloc[-1] if len(self.hist) else None)
        warnings: list[str] = []
        for ts, r in new.iterrows():
            if prev_ts is not None and ts <= prev_ts:
                raise BarRejected(f"bar {ts.isoformat()} is not newer than last bar "
                                  f"{prev_ts.isoformat()} (duplicate or out of order)", status=409)
            if prev_close is not None and abs(r["close"] / prev_close - 1) > self.max_bar_move:
                raise BarRejected(f"bar {ts.isoformat()} moves {abs(r['close']/prev_close-1)*100:.2f}% "
                                  f"in one bar (limit {self.max_bar_move*100:.1f}%) - rejected as a "
                                  f"possible bad/tampered tick", status=422)
            prev_ts, prev_close = ts, r["close"]
        return warnings

    # ------------------------------------------------------------- inference
    def on_bars(self, bars: pd.DataFrame) -> list[dict]:
        """bars: UTC-indexed, ascending OHLCV. Returns one result dict per bar."""
        bars = bars[OHLCV].astype(float).sort_index()
        self._validate(bars)
        if len(self.hist) + len(bars) < MIN_BARS:
            raise BarRejected(f"engine needs >= {MIN_BARS} bars of history (have {len(self.hist)}); "
                              f"warm it up first", status=503)

        prev_last = self.last_ts
        combined = pd.concat([self.hist, bars])
        feats = build_features(combined).iloc[-len(bars):]
        X = feats[FEATURE_COLS]
        if X.isna().any().any():
            raise BarRejected("features not warmed up - send more history", status=503)

        # forecasts, vectorised over the whole batch
        qz = {h: self.art["models"][h].predict(X) for h in self.art["horizons"]}
        close, sigma = feats["close"].to_numpy(), feats["sigma"].to_numpy()

        # Lorentzian signal for the whole batch in one pass (see lorentzian_signal.py
        # for why this is safe: continuous ANN/filter/kernel values are recomputed
        # from the rolling window same as the price features are, but the discrete
        # signal/hold/exit state is this engine's own persisted state, never reset).
        lor_batch = self.lorentzian.update_batch(combined[OHLCV], len(bars))

        results = []
        gap_ref = prev_last
        for i, (ts, bar) in enumerate(bars.iterrows()):
            st = self.stop.update(bar["high"], bar["low"], bar["close"])
            self.stop_hist.append((st["stop"], st["trend"], "buy" if st["buy"] else "sell" if st["sell"] else None))
            lor = lor_batch[i]
            self.lorentzian_hist.append((lor["bias"], lor["flip"], lor["confidence"], lor["kernel"]))
            warns = []
            if gap_ref is not None:
                gap_h = (ts - gap_ref).total_seconds() / 3600
                if gap_h > 3 and not (46 <= gap_h <= 52):
                    warns.append(f"gap_in_data:{gap_h:.0f}h")
            gap_ref = ts
            if abs(X["zret_1"].iloc[i]) > 6:
                warns.append("large_move:>6_sigma")

            forecasts = []
            for h in self.art["horizons"]:
                p10, p50, p90 = z_to_price(qz[h][i], close[i], sigma[i], h)
                forecasts.append({
                    "horizon_bars": h,
                    "target_ts_est": add_trading_hours(ts, h).isoformat(),
                    "price_p10": float(p10), "price_p50": float(p50), "price_p90": float(p90),
                    "expected_return_bps": float((p50 / close[i] - 1) * 1e4),
                    "band_width_pips": float((p90 - p10) / PIP),
                    "model": self.art["kinds"][h],
                })

            # ---- signal: stop flip, optionally confirmed by whichever of the xgb
            # quantile model / Lorentzian ANN have actually demonstrated a net-of-
            # cost edge in train_price_model.py's backtest (both start False/None
            # on a fresh model until proven, same bar the ATR-stop-only signal
            # itself had to clear before anything was trusted here).
            p50_z = float(qz[self.sig_h][i][1])
            thr = self.art["conf_threshold"][self.sig_h]
            bias = "up" if p50_z >= thr else "down" if p50_z <= -thr else "neutral"
            flip = "buy" if st["buy"] else "sell" if st["sell"] else None
            action = {"buy": "BUY", "sell": "SELL"}.get(flip, "HOLD")

            model_skill = bool(self.art["directional_skill"][self.sig_h])
            confirmed_model = None
            if model_skill and action != "HOLD":
                confirmed_model = (action == "BUY" and bias == "up") or (action == "SELL" and bias == "down")
                if not confirmed_model:
                    action = "HOLD"

            confirmed_lorentzian = None
            if self.lorentzian_skill and action != "HOLD":
                confirmed_lorentzian = (action == "BUY" and lor["bias"] == "up") or \
                                       (action == "SELL" and lor["bias"] == "down")
                if not confirmed_lorentzian:
                    action = "HOLD"

            basis_parts = ["stop"]
            if model_skill:
                basis_parts.append("model")
            if self.lorentzian_skill:
                basis_parts.append("lorentzian")
            basis = "stop_only" if len(basis_parts) == 1 else "+".join(basis_parts)

            atr_v = st["atr"]
            results.append({
                "symbol": self.symbol, "timeframe": self.timeframe,
                "as_of": ts.isoformat(), "last_close": float(close[i]),
                "model_version": self.version,
                "forecasts": forecasts,
                "trailing_stop": {
                    "level": float(st["stop"]), "trend": st["trend"],
                    "distance_pips": float(st["distance"] / PIP),
                    "distance_atr": float(st["distance"] / atr_v) if atr_v == atr_v and atr_v > 0 else None,
                    "atr_pips": float(atr_v / PIP) if atr_v == atr_v else None,
                    "key_multiplier": float(st["key_multiplier"]), "adaptive": True,
                    "flip": flip,
                },
                "lorentzian": {
                    "bias": lor["bias"], "confidence": lor["confidence"],
                    "flip": lor["flip"], "exit": lor["exit"],
                    "kernel_estimate": lor["kernel"],
                    "skill_established": self.lorentzian_skill,
                },
                "signal": {
                    "action": action, "basis": basis, "model_bias": bias,
                    "confirmed_by_model": confirmed_model,
                    "confirmed_by_lorentzian": confirmed_lorentzian,
                    "edge_verified": bool(self.art["edge_verified"]),
                },
                "warnings": warns,
            })

        self.hist = combined.tail(self.window)
        self.last_result = results[-1]
        return results

    # -------------------------------------------------- forward-test support
    def close_at(self, ts: pd.Timestamp, offset_bars: int):
        """(timestamp, close) of the bar `offset_bars` after bar `ts`, if we hold both."""
        try:
            pos = self.hist.index.get_loc(ts)
        except KeyError:
            return None
        if pos + offset_bars >= len(self.hist):
            return None
        return self.hist.index[pos + offset_bars], float(self.hist["close"].iloc[pos + offset_bars])