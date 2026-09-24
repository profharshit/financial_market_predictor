"""
EUR/USD historical data fetcher — for training the price-prediction model
in the DS/ML branch of the pipeline (feeds into the Feature Store).

Source: Dukascopy (free, no API key, tick-level bank data since ~2003)
"""

from datetime import datetime
import pandas as pd
import dukascopy_python
from dukascopy_python.instruments import INSTRUMENT_FX_MAJORS_EUR_USD

# ---------------------------------------------------------------------------
# 1. CONFIG — adjust date range / granularity to your needs
# ---------------------------------------------------------------------------
START_DATE = datetime(2024, 1, 1)
END_DATE = datetime(2026, 9, 7)

# Options: TIME_UNIT_TICK, TIME_UNIT_MIN, TIME_UNIT_HOUR, TIME_UNIT_DAY
TIME_UNIT = dukascopy_python.INTERVAL_HOUR_1   # 1-hour bars — good balance
                                                 # of history depth vs. noise
                                                 # for a first model
OFFER_SIDE = dukascopy_python.OFFER_SIDE_BID    # bid or ask; bid is standard
OUTPUT_CSV = "eurusd_ohlcv.csv"


def fetch_eurusd(start: datetime, end: datetime) -> pd.DataFrame:
    """Pull EUR/USD OHLCV candles from Dukascopy and return a clean DataFrame."""
    df = dukascopy_python.fetch(
        instrument=INSTRUMENT_FX_MAJORS_EUR_USD,
        interval=TIME_UNIT,
        offer_side=OFFER_SIDE,
        start=start,
        end=end,
    )

    # dukascopy_python returns a DataFrame indexed by timestamp already;
    # normalize column names for downstream feature engineering
    df = df.rename(columns={
        "timestamp": "timestamp",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    })
    df.index.name = "timestamp"
    df = df.reset_index()
    df = df.dropna()
    return df


def add_basic_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Minimal feature set to hand off to the Feature Store — extend this
    with whatever your DS/ML subteam decides on (RSI, MACD, rolling vol,
    sentiment score merge-in, etc.)
    """
    df = df.copy()
    df["return_1"] = df["close"].pct_change()
    df["log_return_1"] = (df["close"] / df["close"].shift(1)).apply(
        lambda x: pd.NA if pd.isna(x) else __import__("math").log(x)
    )
    df["sma_10"] = df["close"].rolling(10).mean()
    df["sma_50"] = df["close"].rolling(50).mean()
    df["volatility_10"] = df["close"].rolling(10).std()

    # crude spike flag — a real anomaly detector should replace this
    # (this is just a placeholder hook for your cyber team's
    # price/volume anomaly detector to plug into)
    df["price_change_pct"] = df["close"].pct_change().abs()
    df["volume_spike"] = (
        df["volume"] > df["volume"].rolling(20).mean() * 3
    )

    return df.dropna()


if __name__ == "__main__":
    if START_DATE >= END_DATE:
        raise ValueError(
            f"START_DATE ({START_DATE.date()}) must be earlier than "
            f"END_DATE ({END_DATE.date()}) — check for a swapped date range."
        )

    print(f"Fetching EUR/USD {TIME_UNIT} data: {START_DATE.date()} to {END_DATE.date()} ...")
    raw = fetch_eurusd(START_DATE, END_DATE)
    print(f"Fetched {len(raw)} rows.")

    featured = add_basic_features(raw)
    featured.to_csv(OUTPUT_CSV, index=False)
    print(f"Saved {len(featured)} rows with features to {OUTPUT_CSV}")
    print(featured.head())