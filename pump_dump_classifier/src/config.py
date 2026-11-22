"""
Central configuration for the Fake-Sentiment / Pump-and-Dump Classifier module.

Edit DATA_PATHS to point at your downloaded datasets. See README.md for
download links and expected column formats.
"""

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Point these at real files once downloaded. If a path doesn't exist,
# the pipeline falls back to a small synthetic sample so you can test
# the code end-to-end before you have real data.
DATA_PATHS = {
    # Expected columns: text, sentiment  (sentiment in {positive, negative, neutral})
    "sentiment_csv": os.path.join(DATA_DIR, "bitcoin_tweets_sentiment.csv"),

    # Expected columns: text, timestamp, group_id, is_pump_announcement (0/1)
    "pump_dump_csv": os.path.join(DATA_DIR, "telegram_pump_dump.csv"),
}

RANDOM_STATE = 42
TEST_SIZE = 0.2
VAL_SIZE = 0.1  # taken out of the remaining train set

# Behavioral / coordination feature windows
BURST_WINDOW_MINUTES = 15   # window to measure posting velocity
DUPLICATE_SIMILARITY_THRESHOLD = 0.85  # cosine similarity to flag near-duplicate posts

# Pump-hype lexicon used for rule-based / feature signal (not the final model on its own)
HYPE_KEYWORDS = [
    "moon", "mooning", "to the moon", "guaranteed", "100x", "1000x",
    "buy now", "don't miss", "dont miss", "last chance", "next big thing",
    "pump", "explode", "explosive", "rocket", "🚀", "get in now", "insider",
    "easy money", "life changing", "double your money", "act fast",
]
