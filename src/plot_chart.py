"""
Plots EUR/USD with the full indicator set overlaid:
- Panel 0 (price): candles/line + Bollinger Bands + ATR trailing stop + buy/sell signals
- Panel 1: volume
- Panel 2: RSI
- Panel 3: MACD (line, signal, histogram)

For large datasets a single candlestick chart becomes unreadable (too many
candles to resolve visually), so this script auto-produces TWO charts once the
dataset crosses RECENT_WINDOW_BARS:
  - eurusd_chart_overview.png : full date range, line style (macro view)
  - eurusd_chart_recent.png   : last RECENT_WINDOW_BARS bars, full candlesticks
For small datasets it falls back to a single candlestick chart.
"""

import pandas as pd
import numpy as np
import mplfinance as mpf
from indicators import apply_indicators

INPUT_CSV = "../data/raw/eurusd_ohlcv.csv"
OUTPUT_DIR = "../outputs"

# Below this row count one candlestick chart is still legible;
# above it we split into overview + recent-detail.
RECENT_WINDOW_BARS = 300


def load_ohlcv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").set_index("timestamp")
    # mplfinance requires these exact capitalised column names
    return df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the shared indicator set and merge the new columns back in."""
    lower_df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    featured = apply_indicators(lower_df)
    new_cols = [c for c in featured.columns if c not in lower_df.columns]
    return pd.concat([df, featured[new_cols]], axis=1)


def build_addplots(df: pd.DataFrame, marker_size: int = 80) -> list:
    buy_marker = np.where(df["atr_buy_signal"] == 1, df["Low"] * 0.999, np.nan)
    sell_marker = np.where(df["atr_sell_signal"] == 1, df["High"] * 1.001, np.nan)

    addplots = [
        mpf.make_addplot(df["bb_upper"], panel=0, color="gray", width=0.8, linestyle="--"),
        mpf.make_addplot(df["bb_lower"], panel=0, color="gray", width=0.8, linestyle="--"),
        mpf.make_addplot(df["bb_mid"], panel=0, color="orange", width=0.8),
        mpf.make_addplot(df["atr_trailing_stop"], panel=0, color="blue", width=1.0),
        mpf.make_addplot(df["rsi"], panel=2, color="purple", ylabel="RSI"),
        mpf.make_addplot(df["macd_line"], panel=3, color="blue", ylabel="MACD"),
        mpf.make_addplot(df["macd_signal"], panel=3, color="orange"),
        mpf.make_addplot(df["macd_hist"], panel=3, type="bar", color="gray", alpha=0.5),
    ]

    # mplfinance cannot render an addplot series that is entirely NaN, so only
    # include the marker series if at least one signal fired in this window.
    if np.isfinite(buy_marker).any():
        addplots.append(mpf.make_addplot(buy_marker, panel=0, type="scatter",
                                         marker="^", markersize=marker_size, color="green"))
    if np.isfinite(sell_marker).any():
        addplots.append(mpf.make_addplot(sell_marker, panel=0, type="scatter",
                                         marker="v", markersize=marker_size, color="red"))
    return addplots


def render_chart(df, chart_type, title, output_path, marker_size=80):
    mpf.plot(
        df,
        type=chart_type,
        style="yahoo",
        addplot=build_addplots(df, marker_size=marker_size),
        volume=True,
        panel_ratios=(3, 1, 1, 1),
        figsize=(14, 11),
        title=title,
        warn_too_much_data=len(df) + 1,   # we handle density ourselves
        savefig=dict(fname=output_path, dpi=150),
    )
    print(f"Chart saved to {output_path}")


if __name__ == "__main__":
    df = compute_indicators(load_ohlcv(INPUT_CSV))

    if len(df) > RECENT_WINDOW_BARS:
        render_chart(
            df, "line",
            f"EUR/USD Overview - {df.index[0].date()} to {df.index[-1].date()} ({len(df)} bars)",
            f"{OUTPUT_DIR}/eurusd_chart_overview.png",
            marker_size=18,
        )
        recent = df.tail(RECENT_WINDOW_BARS)
        render_chart(
            recent, "candle",
            f"EUR/USD Detail - last {RECENT_WINDOW_BARS} bars "
            f"({recent.index[0].date()} to {recent.index[-1].date()})",
            f"{OUTPUT_DIR}/eurusd_chart_recent.png",
            marker_size=80,
        )
    else:
        render_chart(
            df, "candle",
            "EUR/USD - Price with Indicators (Bollinger, ATR Trailing Stop, RSI, MACD)",
            f"{OUTPUT_DIR}/eurusd_chart.png",
        )
