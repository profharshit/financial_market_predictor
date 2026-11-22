"""
Feature engineering for the pump-and-dump classifier.

Two feature families are combined, because wording alone is a weak signal
(scammers vary their phrasing) while *coordination* patterns are much harder
to fake:

  A) Text/lexical features   - hype-keyword density, exclamation/emoji rate, caps rate
  B) Behavioral features     - posting velocity (messages/min in a group),
                                near-duplicate burst detection within a time window

The output is a numeric feature matrix that can be concatenated with TF-IDF
or transformer embeddings before the final classifier.
"""

import re
import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from config import HYPE_KEYWORDS, BURST_WINDOW_MINUTES, DUPLICATE_SIMILARITY_THRESHOLD

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F000-\U0001F0FF]"
)


def hype_keyword_count(text: str) -> int:
    t = text.lower()
    return sum(1 for kw in HYPE_KEYWORDS if kw in t)


def exclamation_rate(text: str) -> float:
    return text.count("!") / max(len(text), 1)


def caps_rate(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def emoji_count(text: str) -> int:
    return len(EMOJI_RE.findall(text))


def add_lexical_features(df, text_col="text"):
    df = df.copy()
    df["hype_keyword_count"] = df[text_col].apply(hype_keyword_count)
    df["exclamation_rate"] = df[text_col].apply(exclamation_rate)
    df["caps_rate"] = df[text_col].apply(caps_rate)
    df["emoji_count"] = df[text_col].apply(emoji_count)
    df["text_length"] = df[text_col].str.len()
    return df


def add_behavioral_features(df, text_col="text", time_col="timestamp",
                             group_col="group_id",
                             window_minutes=BURST_WINDOW_MINUTES,
                             dup_threshold=DUPLICATE_SIMILARITY_THRESHOLD):
    """
    Adds, per message:
      - posting_velocity      : # messages from the same group within +/- window_minutes
      - near_duplicate_burst  : # near-duplicate messages (cosine sim > threshold)
                                 from the same group within the same window
    These approximate "coordinated campaign" behavior that a single message's
    wording can't capture on its own.
    """
    df = df.copy()
    df[time_col] = pd.to_datetime(df[time_col])
    df = df.sort_values([group_col, time_col]).reset_index(drop=True)

    df["posting_velocity"] = 0
    df["near_duplicate_burst"] = 0

    for group_id, g in df.groupby(group_col):
        idx = g.index.to_numpy()
        times = g[time_col].to_numpy()
        texts = g[text_col].tolist()

        if len(texts) > 1:
            tfidf = TfidfVectorizer().fit_transform(texts)
            sim_matrix = cosine_similarity(tfidf)
        else:
            sim_matrix = np.zeros((len(texts), len(texts)))

        window = np.timedelta64(window_minutes, "m")

        for i in range(len(idx)):
            in_window = np.abs(times - times[i]) <= window
            in_window[i] = False  # exclude self
            df.loc[idx[i], "posting_velocity"] = int(in_window.sum())

            dup_in_window = in_window & (sim_matrix[i] > dup_threshold)
            df.loc[idx[i], "near_duplicate_burst"] = int(dup_in_window.sum())

    return df


BEHAVIORAL_FEATURE_COLS = [
    "hype_keyword_count", "exclamation_rate", "caps_rate",
    "emoji_count", "text_length", "posting_velocity", "near_duplicate_burst",
]


def build_feature_matrix(df, feature_cols=BEHAVIORAL_FEATURE_COLS):
    return df[feature_cols].fillna(0).to_numpy(dtype=float)
