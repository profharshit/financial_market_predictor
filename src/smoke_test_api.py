"""
End-to-end smoke test - run from src/:   python3 smoke_test_api.py
Simulates "upcoming data": warms the engine on history ENDING 400 bars before the
CSV's last bar, then feeds those 400 bars in (mix of single + batch), and checks
the contract, the stop's ratchet property, and forward-test resolution.
"""
import os, tempfile, sys
import numpy as np, pandas as pd

tmp = tempfile.mkdtemp()
from features import load_ohlcv            # handles headerless / headered / CRLF files
full = load_ohlcv("../data/raw/eurusd_ohlcv.csv").reset_index()
head, tail = full.iloc[:-400], full.iloc[-400:]
head.to_csv(f"{tmp}/warm.csv", index=False)   # always written WITH a proper header
os.environ.update(FMP_WARMUP_CSV=f"{tmp}/warm.csv", FMP_DB_PATH=f"{tmp}/p.db", FMP_API_KEY="secret123")

from fastapi.testclient import TestClient
import serve_api

H = {"X-API-Key": "secret123"}
def bar(r): return {k: (r[k].isoformat() if k == "timestamp" else float(r[k])) for k in ["timestamp","open","high","low","close","volume"]}
ok = lambda c, m: (print(("PASS " if c else "FAIL ") + m), c)[1]
allok = True

with TestClient(serve_api.app) as c:
    h = c.get("/health").json();                              allok &= ok(h["status"] == "ok", f"health ok, last bar {h['last_bar_ts']}")
    allok &= ok(c.post("/predict", json=bar(tail.iloc[0])).status_code == 401, "no API key -> 401")
    allok &= ok(c.post("/predict", json=bar(tail.iloc[0]), headers={"X-API-Key": "bad"}).status_code == 401, "wrong API key -> 401")

    r = c.post("/predict", json=bar(tail.iloc[0]), headers=H); j = r.json()
    allok &= ok(r.status_code == 200, "first live bar -> 200")
    allok &= ok([f["horizon_bars"] for f in j["forecasts"]] == [1, 4, 24], "3 horizons returned")
    allok &= ok(all(f["price_p10"] < f["price_p50"] < f["price_p90"] for f in j["forecasts"]), "p10 < p50 < p90 for every horizon")
    print("      sample:", {k: j[k] for k in ("as_of", "last_close")}, "| stop", round(j["trailing_stop"]["level"], 5),
          j["trailing_stop"]["trend"], "| 4h band", round(j["forecasts"][1]["price_p10"], 5), "-", round(j["forecasts"][1]["price_p90"], 5))

    allok &= ok(c.post("/predict", json=bar(tail.iloc[0]), headers=H).status_code == 409, "duplicate bar -> 409")
    bad = bar(tail.iloc[1]); bad["high"] = bad["low"] - 0.001
    allok &= ok(c.post("/predict", json=bad, headers=H).status_code == 422, "inconsistent OHLC -> 422")
    spike = bar(tail.iloc[1]); spike.update(open=1.5, high=1.5, low=1.5, close=1.5)
    rs = c.post("/predict", json=spike, headers=H)
    allok &= ok(rs.status_code == 422 and "possible bad" in rs.json()["detail"], "40% price spike -> 422 (tamper/bad-tick guard)")
    allok &= ok(pd.Timestamp(c.get("/health").json()["last_bar_ts"]) == pd.Timestamp(tail.iloc[0]["timestamp"]), "rejected bars did not change engine state")

    # feed 99 more singly, rest as one batch
    outs = [j]
    for i in range(1, 100):
        outs.append(c.post("/predict", json=bar(tail.iloc[i]), headers=H).json())
    rb = c.post("/predict/batch", headers=H, json={"bars": [bar(r) for _, r in tail.iloc[100:].iterrows()], "mode": "live"})
    allok &= ok(rb.status_code == 200 and rb.json()["count"] == 300, "batch of 300 -> 200, 300 results")
    outs += rb.json()["results"]

    # ratchet: within an unbroken trend the stop never moves against the position
    viol = 0
    for a, b in zip(outs[:-1], outs[1:]):
        if b["trailing_stop"]["flip"] is None and a["trailing_stop"]["trend"] == b["trailing_stop"]["trend"]:
            s0, s1 = a["trailing_stop"]["level"], b["trailing_stop"]["level"]
            if (b["trailing_stop"]["trend"] == "up" and s1 < s0 - 1e-12) or (b["trailing_stop"]["trend"] == "down" and s1 > s0 + 1e-12):
                viol += 1
    allok &= ok(viol == 0, f"stop never loosens inside a trend (violations: {viol}); flips seen: {sum(o['trailing_stop']['flip'] is not None for o in outs)}")

    # batch results must equal what single-bar posting would have produced (stateless-consistency check on tail)
    ft = c.get("/forward-test", headers=H).json()
    print("      forward-test:", {h: {k: (round(v, 3) if isinstance(v, float) else v) for k, v in d.items()} for h, d in ft["horizons"].items()})
    allok &= ok(ft["horizons"]["1"]["n_resolved"] == 399 and ft["horizons"]["4"]["n_resolved"] == 396 and ft["horizons"]["24"]["n_resolved"] == 376,
                "forward-test resolved 399 / 396 / 376 forecasts (1h / 4h / 24h)")
    allok &= ok(0.65 < ft["horizons"]["1"]["band_coverage_p10_p90"] < 0.92, "1h band coverage plausible (nominal 0.80)")
    hist = c.get("/history?limit=5", headers=H).json();       allok &= ok(len(hist) == 5, "history endpoint")
    allok &= ok(c.get("/latest", headers=H).json()["as_of"] == outs[-1]["as_of"], "latest == last prediction")
    info = c.get("/model/info", headers=H).json();            allok &= ok(set(info["kinds"]) == {"1", "4", "24"} and "backtest_holdout" in info, "model/info returns kinds + validation summary")
    print("      edge_verified flag:", outs[-1]["signal"]["edge_verified"], "| signal basis:", outs[-1]["signal"]["basis"])


# engine-level: one-bar-at-a-time must equal one big batch (same forecasts, same stop)
from inference_engine import PredictionEngine
from features import load_ohlcv
w = load_ohlcv(f"{tmp}/warm.csv"); t = tail.set_index("timestamp")
e1, e2 = PredictionEngine(serve_api.MODEL_PATH), PredictionEngine(serve_api.MODEL_PATH)
e1.warm_up(w); e2.warm_up(w)
single = [e1.on_bars(t.iloc[[i]])[0] for i in range(120)]
batch = e2.on_bars(t.iloc[:120])
d = max(abs(a["forecasts"][k]["price_p50"] - b["forecasts"][k]["price_p50"]) for a, b in zip(single, batch) for k in range(3))
ds = max(abs(a["trailing_stop"]["level"] - b["trailing_stop"]["level"]) for a, b in zip(single, batch))
allok &= ok(d < 1e-7 and ds < 1e-12, f"single-bar path == batch path (max p50 diff {d:.1e}, max stop diff {ds:.1e})")

print("\nALL TESTS PASSED" if allok else "\nSOME TESTS FAILED"); sys.exit(0 if allok else 1)
