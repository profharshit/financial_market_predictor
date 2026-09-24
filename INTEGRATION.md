# Integration Guide - Forecast + Adaptive Stop API

For the **frontend** and **database** teammates. The ML side is done; this is
everything you need to plug in without reading the model code.

## 1. Run it (5 minutes)

```bash
cd src
pip install -r ../requirements.txt
python3 train_price_model.py          # ~20 s, writes ../models/eurusd_price_model.joblib
python3 smoke_test_api.py             # must print ALL TESTS PASSED
uvicorn serve_api:app --port 8000     # keep --workers 1 (engine state lives in memory)
```

Interactive docs (try every endpoint in the browser): **http://localhost:8000/docs**

To feed it real bars (run on a machine with internet access to Dukascopy):

```bash
python3 live_feed.py                  # backfills since the last bar it has, then polls every 5 min
```

## 2. Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness, model version, last bar the engine has seen. **No auth.** |
| POST | `/predict` | ONE newly closed hourly bar in -> forecast + stop + signal out |
| POST | `/predict/batch` | Up to 5,000 bars (catch-up / backfill). `mode`: `live` or `replay` |
| GET | `/latest` | Most recent prediction |
| GET | `/history?limit=100&mode=live` | Logged predictions, with `realised_price` filled in once known |
| GET | `/forward-test?mode=live` | Live scorecard on unseen bars (see section 5) |
| GET | `/model/info` | Which model runs each horizon + validation summary |

If `FMP_API_KEY` is set, every endpoint except `/health` needs header `X-API-Key: <key>`.

**Request** (`POST /predict`) - timestamp = bar **open** time, UTC; send only *closed* bars, in time order:

```json
{"timestamp": "2026-09-04T16:00:00Z", "open": 1.16175, "high": 1.16245,
 "low": 1.16175, "close": 1.16206, "volume": 5190.91}
```

**Response** (real output):

```json
{
  "symbol": "EURUSD", "timeframe": "1h",
  "as_of": "2026-09-04T16:00:00+00:00", "last_close": 1.16206,
  "model_version": "20260924-0426-xgb_quantile+xgb_quantile+vol_only",
  "forecasts": [
    {"horizon_bars": 1,  "target_ts_est": "2026-09-04T17:00:00+00:00",
     "price_p10": 1.16139, "price_p50": 1.16201, "price_p90": 1.1627,
     "expected_return_bps": -0.42, "band_width_pips": 13.1, "model": "xgb_quantile"},
    {"horizon_bars": 4,  "...": "..."},
    {"horizon_bars": 24, "...": "...", "model": "vol_only"}
  ],
  "trailing_stop": {
    "level": 1.16366, "trend": "down", "distance_pips": 16.0, "distance_atr": 1.40,
    "atr_pips": 11.5, "key_multiplier": 1.17, "adaptive": true, "flip": null
  },
  "signal": {"action": "HOLD", "basis": "stop_only", "model_bias": "neutral",
             "confirmed_by_model": null, "edge_verified": false},
  "warnings": []
}
```

## 3. What the fields mean (and how to show them)

- **`forecasts[].price_p10 / p50 / p90`** - an 80% prediction interval, not a point guess.
  Draw it as a **fan/band** at `target_ts_est`. `p50` is the median forecast; expect it to sit
  very close to `last_close` (see the honesty note below). `band_width_pips` is the useful
  number: it widens in volatile hours and narrows in quiet ones.
- **`trailing_stop.level`** - the current stop-loss price. It **only ever moves in the trend's
  favour** (verified in the smoke test), so it is safe to draw as a step line. `trend: "up"` means
  the stop sits *below* price (protecting a long); `"down"` means *above* (protecting a short).
  `flip` is non-null on the single bar where price crossed the stop.
- **`key_multiplier`** - how the stop adapted: >1 = market hotter than its recent norm (stop is
  wider), <1 = quieter (stop is tighter). Range 0.75-1.5 around the base key 3.0.
- **`signal.action`** - `BUY`/`SELL` only on a flip bar, otherwise `HOLD` (same semantics as the
  original Pine "ATR Bot" alerts).
- **`signal.edge_verified`** - **please surface this in the UI.** It is `false`: backtests with
  transaction costs did not show a tradable edge (see `PRICE_MODEL_REPORT.md`). A small
  "research prototype - not trading advice" badge tied to this flag is the honest way to show it.
- **`warnings`** - e.g. `gap_in_data:6h`, `large_move:>6_sigma`. Show as a yellow chip.

```js
// frontend example
const r = await fetch("http://localhost:8000/latest", { headers: { "X-API-Key": KEY } });
const p = await r.json();
const band4h = p.forecasts.find(f => f.horizon_bars === 4);   // {price_p10, price_p50, price_p90}
const stop = p.trailing_stop.level;
```

## 4. Database

`schema.sql` is the contract; `store.py` creates it automatically in SQLite (`data/predictions.db`).

- `signals`   - one row per predicted bar (stop level, trend, flip, action, model version, `mode`).
- `forecasts` - one row per bar x horizon (p10/p50/p90 + **`realised_price`**, filled in later).
- Optional `bars` table (commented at the bottom) if you want the API to warm up from the DB.

To move to Postgres/MySQL: re-implement the five methods of `store.Store`
(`log`, `pending`, `resolve`, `history`, `forward_stats`) against your DB - nothing else in the
API touches storage. Timestamps are ISO-8601 UTC strings.

## 5. Testing on live / upcoming data

Every forecast is stored; when the target bar arrives the API fills in `realised_price`.
`GET /forward-test` then reports, per horizon:

| Metric | Healthy value | Meaning |
|---|---|---|
| `band_coverage_p10_p90` | ~0.80 | share of realised prices inside the band - calibration |
| `mae_pips_model` vs `mae_pips_naive_no_change` | model <= naive | does the median beat "price stays put"? |
| `directional_accuracy` | ~0.50 expected | coin-flip territory is the honest expectation |

The training data ends 2026-09-04. Bars after that date are **genuinely unseen**: run
`live_feed.py --once` to backfill them (they arrive as `mode=replay`) and read `/forward-test`.
Then leave the feeder running to keep accumulating live results.

## 6. Security notes (for the cyber team)

- API-key check (constant-time compare) + CORS allow-list are built in (`FMP_API_KEY`, `FMP_CORS_ORIGINS`).
  Rate limiting, TLS and audit logging belong in the gateway in front of this service.
- **Bar sanity guard:** a bar moving >3% from the previous close (`FMP_MAX_BAR_MOVE`) is rejected
  (HTTP 422) before it can touch model or stop state; inconsistent OHLC is rejected by the schema.
  This is the natural hook for the price-feed anomaly detector: it can veto or flag before `/predict`.
- Duplicate / out-of-order timestamps -> 409. A rejected request never changes engine state.
- All-or-nothing batches: every bar is validated before any is applied.

## 7. Error codes

| Code | Meaning |
|---|---|
| 401 | missing/invalid `X-API-Key` |
| 409 | bar not newer than the last one (duplicate or out of order) |
| 422 | invalid bar (bad OHLC, or a >3% one-bar jump) |
| 503 | not enough history to compute features yet |

## 8. Known limitations

- Engine state (rolling window + stop) is **in memory**, warmed from `FMP_WARMUP_CSV` at startup:
  run one worker only; after a restart run `live_feed.py --once` to catch up.
- Horizons are in *trading* bars (24 = 24 trading hours, skipping the weekend);
  `target_ts_est` is an estimate (+/-1 h around DST).
- Single instrument (EUR/USD, 1h). Adding a symbol means one engine + model per symbol.
