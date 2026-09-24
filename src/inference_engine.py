"""
PredictionEngine - the live brain. Three cooperating parts:

  1. FEATURES   features.build_features() on a rolling window of recent bars
                (the SAME function training used -> no train/serve skew)
  2. FORECAST   per-horizon quantile model -> price band (p10 / p50 / p90)
  3. STOP       TrailingStopManager: persistent, adaptive, ratchet-only stop

`on_bars()` is the single entry point (one bar = a batch of one). All bars in a
call are validated BEFORE any state is touched, so a bad bar can never leave
the engine half-updated.
"""

from __future__ import annotations

import joblib
import numpy as np
import pandas as pd

from features import (FEATURE_COLS, PIP, add_trading_hours, build_features, z_to_price)
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
        self.last_result: dict | None = None
        self.sig_h = self.art["signal_horizon"]

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
    def warm_up(self, ohlcv: pd.DataFrame) -> None:
        """Feed history (UTC-indexed OHLCV). Stop state sees ALL of it; features keep the last `window`."""
        ohlcv = ohlcv[OHLCV].astype(float).sort_index()
        self.stop.reset()
        self.stop.warm_up(ohlcv)
        self.hist = ohlcv.tail(self.window).copy()

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

        results = []
        gap_ref = prev_last
        for i, (ts, bar) in enumerate(bars.iterrows()):
            st = self.stop.update(bar["high"], bar["low"], bar["close"])
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

            # ---- signal
            p50_z = float(qz[self.sig_h][i][1])
            thr = self.art["conf_threshold"][self.sig_h]
            bias = "up" if p50_z >= thr else "down" if p50_z <= -thr else "neutral"
            flip = "buy" if st["buy"] else "sell" if st["sell"] else None
            action = {"buy": "BUY", "sell": "SELL"}.get(flip, "HOLD")
            skill = bool(self.art["directional_skill"][self.sig_h])
            confirmed = None
            basis = "stop_only"
            if skill:
                basis = "stop+model"
                if action != "HOLD":
                    confirmed = (action == "BUY" and bias == "up") or (action == "SELL" and bias == "down")
                    if not confirmed:
                        action = "HOLD"

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
                "signal": {
                    "action": action, "basis": basis, "model_bias": bias,
                    "confirmed_by_model": confirmed,
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
