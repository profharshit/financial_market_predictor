"""
Text cleaning shared by the sentiment classifier and the pump-dump classifier.

Two cleaned variants are produced per text:
  - clean_for_classical : lowercased, punctuation/url/mentions stripped, for TF-IDF/classical ML
  - clean_for_transformer: lightly cleaned (keeps casing/punctuation), for BERT-style models
"""

import re

URL_RE = re.compile(r"http\S+|www\.\S+")
MENTION_RE = re.compile(r"@\w+")
HASHTAG_RE = re.compile(r"#(\w+)")
NON_ALPHANUM_RE = re.compile(r"[^a-z0-9\s]")
MULTI_SPACE_RE = re.compile(r"\s+")


def clean_for_classical(text: str) -> str:
    if not isinstance(text, str):
        return ""
    t = text.lower()
    t = URL_RE.sub(" ", t)
    t = MENTION_RE.sub(" ", t)
    t = HASHTAG_RE.sub(r"\1", t)   # keep hashtag word, drop the '#'
    t = NON_ALPHANUM_RE.sub(" ", t)
    t = MULTI_SPACE_RE.sub(" ", t).strip()
    return t


def clean_for_transformer(text: str) -> str:
    if not isinstance(text, str):
        return ""
    t = URL_RE.sub(" ", text)
    t = MENTION_RE.sub(" ", t)
    t = MULTI_SPACE_RE.sub(" ", t).strip()
    return t


def add_clean_columns(df, text_col="text"):
    df = df.copy()
    df["clean_for_classical"] = df[text_col].apply(clean_for_classical)
    df["clean_for_transformer"] = df[text_col].apply(clean_for_transformer)
    return df
