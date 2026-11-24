# FILE: financial-market-project/src/debug_dukascopy_range.py   (NEW FILE)
# PLACE IN: src/   (same folder as live_feed.py)
# RUN: from src/ ->  python debug_dukascopy_range.py
# Finds which date window makes Dukascopy return today's newest hourly bars.

from datetime import datetime, timedelta, timezone

import live_feed


# Builds the date windows to try, from the current setup to wider ones.
def build_test_windows(now):
    return {
        "current sync window (last bar + 1h to now + 1h)": (now - timedelta(hours=5), now + timedelta(hours=1)),
        "end exactly now": (now - timedelta(days=2), now),
        "end tomorrow midnight": (now - timedelta(days=2), (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)),
        "start at today's midnight": (now.replace(hour=0, minute=0, second=0, microsecond=0), now + timedelta(hours=1)),
    }


# Downloads one window and prints how far the newest bar reaches.
def try_window(name, start, end):
    print(f"\n--- {name}")
    print(f"asking for {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC")
    try:
        bars = live_feed.fetch_bars(start, end)
    except Exception as error:
        print(f"FAILED: {error!r}")
        return
    if bars.empty:
        print("returned 0 bars")
        return
    print(f"returned {len(bars)} bars, newest = {bars.index[-1]}")


# Runs every window so you can see which one reaches today's bars.
def main():
    now = datetime.now(timezone.utc)
    print(f"Now (UTC): {now:%Y-%m-%d %H:%M}")
    for name, (start, end) in build_test_windows(now).items():
        try_window(name, start, end)


if __name__ == "__main__":
    main()