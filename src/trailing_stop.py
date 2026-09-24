"""
Live, stateful, volatility-ADAPTIVE ATR trailing stop.

Same maths as indicators.atr_trailing_stop_adaptive() (the batch version used
for backtests), refactored to accept ONE bar at a time so the API can keep a
persistent stop across requests. Set adaptive=False to reproduce the original
Pine Script ("Krishna's ATR Bot") exactly.

Key property for live use: the stop only ratchets IN the trend's favour, so a
stop level you have shown to a user never moves against them. It is reset only
when price closes through it (a flip).
"""

from collections import deque
import math

from indicators import ADAPT_LOOKBACK, ADAPT_LO, ADAPT_HI, ADAPT_POWER


class TrailingStopManager:
    def __init__(self, base_key: float = 3.0, atr_period: int = 10, adaptive: bool = True,
                 lookback: int = ADAPT_LOOKBACK, lo: float = ADAPT_LO,
                 hi: float = ADAPT_HI, power: float = ADAPT_POWER):
        self.base_key, self.atr_period, self.adaptive = base_key, atr_period, adaptive
        self.lookback, self.lo, self.hi, self.power = lookback, lo, hi, power
        self.reset()

    def reset(self):
        self.n = 0
        self.prev_close = None
        self.atr = None
        self.stop = None
        self.position = 0
        self.atr_hist = deque(maxlen=self.lookback)

    # -- core ---------------------------------------------------------------
    def update(self, high: float, low: float, close: float) -> dict:
        a = 1.0 / self.atr_period

        # True range + ATR: EXACTLY pandas ewm(alpha=1/p, adjust=False) recursion
        if self.prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.atr = tr if self.atr is None else (1 - a) * self.atr + a * tr
        self.n += 1
        atr_valid = self.n >= self.atr_period          # pandas min_periods
        atr_now = self.atr if atr_valid else float("nan")

        # Adaptive key multiplier
        mult = 1.0
        if atr_valid:
            self.atr_hist.append(atr_now)
            if self.adaptive and len(self.atr_hist) == self.lookback:
                ratio = atr_now / (sum(self.atr_hist) / self.lookback)
                mult = min(self.hi, max(self.lo, ratio ** self.power))
        key = self.base_key * mult
        n_loss = key * atr_now if atr_valid else float("nan")

        buy = sell = 0
        prev_stop, prev_src = self.stop, self.prev_close
        if prev_src is None or math.isnan(n_loss):
            new_stop = close                            # nz(stop[1], src) warm-up
        else:
            if close > prev_stop and prev_src > prev_stop:
                new_stop = max(prev_stop, close - n_loss)
            elif close < prev_stop and prev_src < prev_stop:
                new_stop = min(prev_stop, close + n_loss)
            elif close > prev_stop:
                new_stop = close - n_loss
            else:
                new_stop = close + n_loss

            if prev_src < prev_stop and close > new_stop:
                self.position, buy = 1, 1
            elif prev_src > prev_stop and close < new_stop:
                self.position, sell = -1, 1

        self.stop, self.prev_close = new_stop, close
        trend = "up" if close > new_stop else "down" if close < new_stop else "flat"
        return {
            "stop": new_stop, "position": self.position, "trend": trend,
            "buy": buy, "sell": sell,
            "atr": atr_now, "key_multiplier": mult, "key": key,
            "distance": abs(close - new_stop),
        }

    def warm_up(self, df) -> None:
        """Feed a DataFrame of historical bars (open/high/low/close) through update()."""
        for h, l, c in zip(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()):
            self.update(float(h), float(l), float(c))
