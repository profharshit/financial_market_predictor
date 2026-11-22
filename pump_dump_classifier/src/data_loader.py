"""
Loads the two datasets this module needs:

1. Sentiment data   -> text + sentiment label (positive/negative/neutral)
2. Pump-and-dump data -> text + timestamp + group_id + is_pump_announcement

If the real files aren't present at the configured paths, a small synthetic
sample is generated instead, so the rest of the pipeline (features, models,
evaluation) can be run and tested immediately.

Real datasets (see README.md for links):
  - Bitcoin Tweets Sentiment (HuggingFace / Kaggle) -> sentiment_csv
  - SystemsLab-Sapienza Telegram Pump-and-Dump dataset -> pump_dump_csv
"""

import os
import numpy as np
import pandas as pd

from config import DATA_PATHS, RANDOM_STATE


def _synthetic_sentiment_df(n=2000, seed=RANDOM_STATE):
    rng = np.random.default_rng(seed)

    bullish_templates = [
        "This coin is going to the moon, {t} is unstoppable!",
        "Just bought more {t}, feeling bullish long term.",
        "{t} fundamentals look strong, accumulating here.",
        "Massive volume on {t} today, breakout incoming.",
        "I'm confident {t} will 10x this year.",
    ]
    bearish_templates = [
        "Selling all my {t}, this project looks dead.",
        "{t} is dumping hard, get out while you can.",
        "Really disappointed with {t} team, no updates in months.",
        "{t} chart looks terrible, avoid for now.",
        "Lost faith in {t}, cutting my losses.",
    ]
    neutral_templates = [
        "{t} traded sideways today, nothing notable.",
        "Anyone have thoughts on {t}'s roadmap?",
        "{t} volume was in line with the weekly average.",
        "Reading the {t} whitepaper this weekend.",
        "{t} listed on a new exchange today.",
    ]
    tickers = ["BTC", "ETH", "DOGE", "SHIB", "XRP", "SOL", "ADA", "PEPE"]

    rows = []
    for _ in range(n):
        label = rng.choice(["positive", "negative", "neutral"])
        t = rng.choice(tickers)
        template = rng.choice(
            bullish_templates if label == "positive"
            else bearish_templates if label == "negative"
            else neutral_templates
        )
        rows.append({"text": template.format(t=t), "sentiment": label})

    return pd.DataFrame(rows)


def _synthetic_pump_dump_df(n_groups=6, msgs_per_group=40, seed=RANDOM_STATE):
    rng = np.random.default_rng(seed)

    hype_templates = [
        "🚀🚀 {t} PUMP STARTING NOW, BUY IMMEDIATELY, DON'T MISS OUT!!!",
        "INSIDER INFO: {t} about to explode, guaranteed 100x, buy now!!!",
        "LAST CHANCE to get {t} before it moons, act fast!!!",
        "Everyone buy {t} RIGHT NOW, this is not a drill, to the moon!!!",
    ]
    normal_templates = [
        "Discussing {t} price action from last week.",
        "What's everyone's take on {t} long term?",
        "{t} had a quiet trading day.",
        "Sharing an article about {t} adoption trends.",
    ]
    tickers = ["ALPHA", "ZETA", "NOVA", "QUARK", "LUNA2", "FLUX"]

    rows = []
    base_time = pd.Timestamp("2024-01-01")
    for g in range(n_groups):
        t = tickers[g % len(tickers)]
        pump_at = rng.integers(low=msgs_per_group // 3, high=msgs_per_group - 5)
        for i in range(msgs_per_group):
            is_pump_window = pump_at <= i < pump_at + 5
            if is_pump_window:
                # near-duplicate hype messages posted in a tight burst -> coordination signal
                text = rng.choice(hype_templates).format(t=t)
                ts = base_time + pd.Timedelta(minutes=i * 0.5)  # fast burst
                label = 1
            else:
                text = rng.choice(normal_templates).format(t=t)
                ts = base_time + pd.Timedelta(minutes=i * 20)  # normal pacing
                label = 0
            rows.append({
                "text": text,
                "timestamp": ts,
                "group_id": f"group_{g}",
                "is_pump_announcement": label,
            })

    df = pd.DataFrame(rows)
    return df.sort_values(["group_id", "timestamp"]).reset_index(drop=True)


def load_sentiment_data(path=None, use_synthetic_if_missing=True):
    path = path or DATA_PATHS["sentiment_csv"]
    if os.path.exists(path):
        df = pd.read_csv(path)
        assert {"text", "sentiment"}.issubset(df.columns), \
            "sentiment_csv must have columns: text, sentiment"
        return df
    if use_synthetic_if_missing:
        print(f"[data_loader] '{path}' not found - using synthetic sentiment sample.")
        return _synthetic_sentiment_df()
    raise FileNotFoundError(path)


def load_pump_dump_data(path=None, use_synthetic_if_missing=True):
    path = path or DATA_PATHS["pump_dump_csv"]
    if os.path.exists(path):
        df = pd.read_csv(path, parse_dates=["timestamp"])
        required = {"text", "timestamp", "group_id", "is_pump_announcement"}
        assert required.issubset(df.columns), \
            f"pump_dump_csv must have columns: {required}"
        return df
    if use_synthetic_if_missing:
        print(f"[data_loader] '{path}' not found - using synthetic pump-dump sample.")
        return _synthetic_pump_dump_df()
    raise FileNotFoundError(path)


if __name__ == "__main__":
    s = load_sentiment_data()
    p = load_pump_dump_data()
    print("Sentiment sample:\n", s.head(), "\n")
    print("Pump-dump sample:\n", p.head())
