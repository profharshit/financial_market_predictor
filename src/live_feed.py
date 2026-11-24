# FILE: financial-market-project/src/live_feed.py   (REPLACE THE EXISTING FILE)
# PLACE IN: src/   (same folder as serve_api.py and inference_engine.py)
# THEN: restart uvicorn, press Refresh on the dashboard, and check /health shows a newer last_bar_ts.
# FIX: Dukascopy only returns today's bars when the search window ends at the next midnight.
#
# Live data feeder: pulls newly CLOSED hourly EUR/USD bars from Dukascopy and pushes them to the API.
#
#     python3 live_feed.py                       # loop forever, poll every 5 min
#     python3 live_feed.py --once                # single catch-up pass, then exit
#     python3 live_feed.py --api http://host:8000 --key SECRET

import argparse
import time
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]


# Downloads hourly bid bars from Dukascopy, so the rest of the app never touches the library directly.
def fetch_bars(start: datetime, end: datetime) -> pd.DataFrame:
    import dukascopy_python
    from dukascopy_python.instruments import INSTRUMENT_FX_MAJORS_EUR_USD
    df = dukascopy_python.fetch(
        instrument=INSTRUMENT_FX_MAJORS_EUR_USD, interval=dukascopy_python.INTERVAL_HOUR_1,
        offer_side=dukascopy_python.OFFER_SIDE_BID, start=start.replace(tzinfo=None), end=end.replace(tzinfo=None))
    df.index = pd.to_datetime(df.index, utc=True)
    return df[BAR_COLUMNS].dropna()


# Converts bars into the JSON shape the API expects.
def to_payload(df: pd.DataFrame) -> list[dict]:
    return [{"timestamp": ts.isoformat(), "open": float(r.open), "high": float(r.high),
             "low": float(r.low), "close": float(r.close), "volume": float(r.volume)}
            for ts, r in df.iterrows()]


# Dukascopy skips today's newest bars unless the window ends at the next midnight, so we always ask up to there.
def end_of_search_window(now: datetime) -> datetime:
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)


# Keeps only bars that are newer than the last one we hold and have fully finished.
def keep_closed_bars(bars: pd.DataFrame, last_ts: pd.Timestamp, now: datetime) -> pd.DataFrame:
    is_new = bars.index > last_ts
    is_closed = bars.index + pd.Timedelta(hours=1) <= pd.Timestamp(now)
    return bars[is_new & is_closed]


# Returns closed hourly bars after last_ts, used by both run_once and the API's /sync endpoint.
def new_closed_bars(last_ts: pd.Timestamp, now: datetime, fetch_fn=None) -> pd.DataFrame:
    fetch_fn = fetch_fn or fetch_bars
    start = (last_ts + pd.Timedelta(hours=1)).to_pydatetime()
    if start >= now:
        return pd.DataFrame(columns=BAR_COLUMNS)
    bars = fetch_fn(start, end_of_search_window(now))
    return keep_closed_bars(bars, last_ts, now)


# Sends one bar to /predict, or many bars to /predict/batch in chunks of 5000.
def post_bars(client, headers: dict, payload: list[dict]):
    if len(payload) == 1:
        return client.post("/predict", json=payload[0], headers=headers)
    response = None
    for start in range(0, len(payload), 5000):
        body = {"bars": payload[start:start + 5000], "mode": "replay"}
        response = client.post("/predict/batch", json=body, headers=headers)
    return response


# Runs one poll: finds new closed bars and posts them, returning how many were accepted.
def run_once(client, headers: dict, fetch_fn=fetch_bars, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    health = client.get("/health").json()
    bars = new_closed_bars(pd.Timestamp(health["last_bar_ts"]), now, fetch_fn)
    if bars.empty:
        return 0
    payload = to_payload(bars)
    response = post_bars(client, headers, payload)
    if response.status_code != 200:
        print(f"API rejected bars: {response.status_code} {response.text[:300]}")
        return 0
    print(f"{datetime.now(timezone.utc):%H:%M:%S}  posted {len(payload)} bar(s), last = {bars.index[-1]}")
    return len(payload)


# Reads the command line options for the feeder.
def read_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--key", default=None, help="X-API-Key, if the API has FMP_API_KEY set")
    parser.add_argument("--interval", type=int, default=300, help="poll seconds")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


# Polls forever (or once), staying alive through temporary network failures.
def main():
    args = read_arguments()
    headers = {"X-API-Key": args.key} if args.key else {}
    with httpx.Client(base_url=args.api, timeout=120) as client:
        while True:
            try:
                run_once(client, headers)
            except Exception as error:
                print("poll failed:", repr(error))
            if args.once:
                break
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
