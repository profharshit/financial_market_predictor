# FILE: financial-market-project/src/debug_sync.py   (NEW FILE)
# PLACE IN: src/   (same folder as live_feed.py and serve_api.py)
# RUN: with uvicorn running, from src/ ->  python debug_sync.py
# Shows why /sync adds no new bars by testing each step on its own.

import os
import traceback
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd

import live_feed

API_URL = "http://127.0.0.1:8000"
API_KEY = os.environ.get("FMP_API_KEY", "")


# Builds the request headers, adding the key only when one is set.
def build_headers():
    return {"X-API-Key": API_KEY} if API_KEY else {}


# Asks the running API which bar it currently holds as its newest.
def read_api_last_bar():
    response = httpx.get(f"{API_URL}/health", timeout=15)
    return pd.Timestamp(response.json()["last_bar_ts"])


# Tests Dukascopy directly, so a download failure shows its real error message.
def test_dukascopy_download(last_bar_time, now):
    start = (last_bar_time + pd.Timedelta(hours=1)).to_pydatetime()
    print(f"Downloading bars from {start} to {now + timedelta(hours=1)} ...")
    try:
        bars = live_feed.fetch_bars(start, now + timedelta(hours=1))
        print(f"Dukascopy returned {len(bars)} bar(s):")
        print(bars.tail(6))
        return bars
    except Exception:
        print("DUKASCOPY DOWNLOAD FAILED:")
        traceback.print_exc()
        return None


# Shows which downloaded bars the closed-bar filter keeps or throws away.
def test_closed_bar_filter(last_bar_time, now):
    closed_bars = live_feed.new_closed_bars(last_bar_time, now)
    print(f"Closed bars after filtering: {len(closed_bars)}")
    print(closed_bars.tail(6))


# Calls the real /sync endpoint and prints the raw answer, including errors.
def test_sync_endpoint():
    response = httpx.post(f"{API_URL}/sync", headers=build_headers(), timeout=120)
    print(f"POST /sync -> status {response.status_code}")
    print(response.text[:500])


# Runs every check in order so the first failing step is easy to spot.
def main():
    now = datetime.now(timezone.utc)
    last_bar_time = read_api_last_bar()
    print(f"Now (UTC): {now:%Y-%m-%d %H:%M}   API last bar (UTC): {last_bar_time}\n")
    downloaded = test_dukascopy_download(last_bar_time, now)
    if downloaded is not None:
        test_closed_bar_filter(last_bar_time, now)
    print()
    test_sync_endpoint()


if __name__ == "__main__":
    main()