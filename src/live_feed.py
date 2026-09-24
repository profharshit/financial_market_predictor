"""
Live data feeder: pulls newly CLOSED hourly EUR/USD bars from Dukascopy and
pushes them to the API. Run it next to the API:

    python3 live_feed.py                       # loop forever, poll every 5 min
    python3 live_feed.py --once                # single catch-up pass, then exit
    python3 live_feed.py --api http://host:8000 --key SECRET

First run does a BACKFILL: it asks the API for its last bar, downloads everything
since then and posts it as one /predict/batch (mode=replay). Because those bars
happened AFTER the model's training data ended, the /forward-test scorecard on
them is a genuine out-of-sample test. After that, each new bar goes to /predict.

Needs network access to Dukascopy (run on your own machine, not the sandbox).
"""

import argparse
import time
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd


def fetch_bars(start: datetime, end: datetime) -> pd.DataFrame:
    """Dukascopy hourly bid bars in [start, end), UTC-indexed. Same call as fetch_eurusd.py."""
    import dukascopy_python
    from dukascopy_python.instruments import INSTRUMENT_FX_MAJORS_EUR_USD
    df = dukascopy_python.fetch(
        instrument=INSTRUMENT_FX_MAJORS_EUR_USD, interval=dukascopy_python.INTERVAL_HOUR_1,
        offer_side=dukascopy_python.OFFER_SIDE_BID, start=start.replace(tzinfo=None), end=end.replace(tzinfo=None))
    df.index = pd.to_datetime(df.index, utc=True)
    return df[["open", "high", "low", "close", "volume"]].dropna()


def to_payload(df: pd.DataFrame) -> list[dict]:
    return [{"timestamp": ts.isoformat(), "open": float(r.open), "high": float(r.high),
             "low": float(r.low), "close": float(r.close), "volume": float(r.volume)}
            for ts, r in df.iterrows()]


def run_once(client, headers: dict, fetch_fn=fetch_bars, now: datetime | None = None) -> int:
    """One poll. `client` is anything with .get/.post (httpx.Client or a FastAPI TestClient)."""
    now = now or datetime.now(timezone.utc)
    h = client.get("/health").json()
    last = pd.Timestamp(h["last_bar_ts"])
    start = (last + pd.Timedelta(hours=1)).to_pydatetime()
    if start >= now:
        return 0
    bars = fetch_fn(start, now + timedelta(hours=1))
    bars = bars[(bars.index > last) & (bars.index + pd.Timedelta(hours=1) <= pd.Timestamp(now))]  # closed bars only
    if bars.empty:
        return 0
    payload = to_payload(bars)
    if len(payload) == 1:
        r = client.post("/predict", json=payload[0], headers=headers)
    else:
        for i in range(0, len(payload), 5000):
            r = client.post("/predict/batch", json={"bars": payload[i:i + 5000], "mode": "replay"}, headers=headers)
    if r.status_code != 200:
        print(f"API rejected bars: {r.status_code} {r.text[:300]}")
        return 0
    print(f"{datetime.now(timezone.utc):%H:%M:%S}  posted {len(payload)} bar(s), last = {bars.index[-1]}")
    return len(payload)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--key", default=None, help="X-API-Key, if the API has FMP_API_KEY set")
    ap.add_argument("--interval", type=int, default=300, help="poll seconds")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    hdr = {"X-API-Key": a.key} if a.key else {}
    with httpx.Client(base_url=a.api, timeout=120) as c:
        while True:
            try:
                run_once(c, hdr)
            except Exception as e:                      # keep the feeder alive through transient failures
                print("poll failed:", repr(e))
            if a.once:
                break
            time.sleep(a.interval)
