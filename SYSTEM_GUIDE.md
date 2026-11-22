# System Guide - Live Price Forecast + Adaptive Trailing Stop

One doc that ties the whole live system together: how it's built, how to run every piece, and
where to take it next. For the API field-by-field contract, see `INTEGRATION.md`. For how the
model was chosen and validated, see `PRICE_MODEL_REPORT.md`. For the original research phase
(direction classifier, 4,156-row dataset), see `DOCUMENTATION.md` - superseded by this system for
anything about live prediction, still correct on methodology (chronological splitting, walk-forward
validation, the leakage/non-stationarity argument).

## 1. What this is

A EUR/USD hourly forecaster: three price-band forecasts (1h / 4h / 24h, each a 10th/50th/90th
percentile) plus a volatility-adaptive trailing stop-loss, served over an HTTP API, with a small
dashboard on top. Built for a 5th-semester mini project (team of 7: 3 DS/ML, 4 cybersecurity) as
the DS/ML leg behind an Auth + API gateway with audit logging the cyber half will attach.

**The honest headline finding:** none of it has a demonstrated trading edge. Bands are well
calibrated (they cover the true price about 80% of the time, as intended); direction is not
predictable beyond noise; the trailing stop does not turn a profit net of costs. This is expected
for next-bar EUR/USD and is reported as a finding - see `PRICE_MODEL_REPORT.md` §"Results" for the
numbers and §"Paste-ready paragraph" for report text. Every API response carries
`signal.edge_verified: false` and the dashboard shows a disclaimer banner driven by that same flag.
Don't remove the disclaimer without re-running the backtest and confirming the flag actually flips.

## 2. Architecture

```
                    ┌──────────────────┐
   Dukascopy   ───▶ │  live_feed.py    │  polls every 5 min, backfills on first run
  (internet)        └────────┬─────────┘
                              │ POST /predict, /predict/batch
                              ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ serve_api.py  (FastAPI)                                                    │
│   Bar validation (OHLC consistency, dedupe, >3% jump guard)                │
│   │                                                                        │
│   ▼                                                                        │
│ inference_engine.py :: PredictionEngine                                    │
│   ┌────────────┐ ┌────────────────┐ ┌──────────────┐ ┌────────────────┐    │
│   │features.py │ │ price_models.py│ │trailing_stop │ │lorentzian_     │    │
│   │(indicators.│ │ (xgb_quantile /│ │.py           │ │signal.py       │    │
│   │py inside)  │ │ ridge/vol_only)│ │(adaptive ATR)│ │(ANN buy/sell)  │    │
│   └─────┬──────┘ └───────┬────────┘ └──────┬───────┘ └───────┬────────┘    │
│         └────────────────┴── PredictionResult ──────────────┘              │
│   │                                                                        │
│   ▼                                                                        │
│ store.py :: Store (SQLite: data/predictions.db)                            │
│   logs every prediction, resolves forecasts once the target                │
│   bar arrives -> the forward-test scorecard                                │
└────────────────────────────────────────────────────────────────────────────┘
              │
              ├── GET /bars, /latest, /history, /forward-test, /model/info
              ▼
   ui/index.html  (served at GET /ui - candlestick chart, forecast
                    table, live scorecard, no build step)
```

**Why it's split this way.** `features.py` is imported by both training (`train_price_model.py`)
and live inference (`inference_engine.py`), so the model never sees a differently-computed feature
in production than it trained on - the single most important correctness property of the whole
system (train/serve skew is a common, hard-to-notice way live ML systems quietly break).
`price_models.py` lives in its own module (not inside the training script) so `joblib` can
unpickle a saved model from the API process - a class pickled from a script run as `__main__`
cannot be loaded anywhere else. `trailing_stop.py`'s `TrailingStopManager` is verified numerically
identical to the batch version in `indicators.py` (max difference 0.0 over the full 16,577-bar
history) - see §6 for what to re-check if you ever touch either.

`lorentzian_signal.py` is intentionally its **own** module, not folded into `features.py` /
`indicators.py`, even though it also reads OHLCV and produces a buy/sell read: it has its own
normalized (0-1 rescaled RSI/WaveTrend/CCI/ADX) feature set and its own "training" (a k-NN lookup
over recent bars - nothing to fit or pickle), so adding it can never silently change what the
price-forecast models see. Its `LorentzianEngine` follows the exact same live-engine shape as
`TrailingStopManager` for the same reason: continuous, window-safe quantities (the ANN vote,
filters, kernel trend) are recomputed fresh from the rolling window every bar, but the *discrete*
state (bias, bars-held, pending hold-bar exits) is real persisted instance state, carried across
calls, never reconstructed from a window slice - see the module's own docstring for the exact bug
that design avoids. `inference_engine.py` only lets it confirm/veto `signal.action` once
`train_price_model.py`'s holdout backtest has shown it a net-of-cost edge (`lorentzian_skill` in
the model artifact); until then, like the xgb model's own bias before its skill was established,
it is surfaced as information only.

## 3. The model

| Horizon | Model | Why |
|---|---|---|
| 1h | XGBoost quantile regression | beat a volatility-only benchmark in 6/6 walk-forward folds and on holdout |
| 4h | XGBoost quantile regression | same |
| 24h | Volatility-only benchmark | no candidate beat the benchmark consistently |

Target: `z = log(close[t+h]/close[t]) / (ATR_pct[t] * sqrt(h))` - a volatility-scaled forward
return, so one model works in calm and violent markets and the price band naturally widens or
narrows with current volatility. 25 stationary features (returns, oscillators, candle shape, the
user's ATR trailing-stop state, time-of-day) - never raw price levels, for the same
non-stationarity reason `DOCUMENTATION.md` §4.3 explains for the classifier.

Current artifact: `models/eurusd_price_model.joblib`, version
`20260924-0426-xgb_quantile+xgb_quantile+vol_only`, trained on 16,577 hourly bars
(2024-01-08 -> 2026-09-04). Re-running `train_price_model.py` on fresh data produces a new
version string automatically (timestamp + which model won each horizon), so old and new artifacts
are never confused.

**Secondary signal - Lorentzian ANN classifier.** Alongside the table above, `train_price_model.py`
now also backtests a stop-loss gated by `lorentzian_signal.py`'s persisted bias (same net-of-cost,
holdout, >2 s.e. Sharpe bar used for `edge_verified`), and saves the config it used plus that
verdict into the artifact as `lorentzian_config` / `lorentzian_skill`. It is a fully independent
read on direction (its own k-NN lookup over its own 0-1 rescaled RSI/WaveTrend/CCI/ADX features,
not the xgb model's), so it can confirm/veto `signal.action` on its own once its own skill is
established, whether or not the xgb model's `directional_skill` is. `PredictionEngine.__init__`
falls back to an untrained default config and `lorentzian_skill=False` for any artifact saved
before this existed, so redeploying the new `inference_engine.py` never requires retraining first.

## 4. Running it end to end

```bash
cd src
pip install -r ../requirements.txt --break-system-packages   # drop the flag if not using system Python

python3 train_price_model.py     # ~20s: walk-forward bake-off, holdout check, backtest, saves the model
python3 smoke_test_api.py        # must print ALL TESTS PASSED before you trust the API

uvicorn serve_api:app --port 8000       # the API + dashboard
python3 live_feed.py                     # in a second terminal: needs internet, pulls real bars
```

Open **http://localhost:8000/ui** for the dashboard, or **http://localhost:8000/docs** to try
each endpoint directly. `GET /health` is the quickest liveness check.

The API warms itself up from `data/raw/eurusd_ohlcv.csv` at startup and immediately predicts on
the last historical bar, so `/latest` and the dashboard have something to show before you've sent
it anything live. `live_feed.py` (or the dashboard's **Sync latest data** button, which calls
`POST /sync`) then backfills everything since the training data ended - those bars are genuinely
unseen by the model, so `/forward-test` on them is a real out-of-sample readout, not a training
metric in disguise.

## 5. Directory layout

```
financial-market-project/
├── data/
│   ├── raw/eurusd_ohlcv.csv          hourly OHLCV (headered or not, both load fine)
│   └── predictions.db                 SQLite log, created automatically on first API start
├── src/
│   ├── indicators.py                  registry-pattern TA library + the ATR trailing stop
│   ├── features.py                    single source of truth for model features/targets
│   ├── price_models.py                the 3 candidate model classes
│   ├── trailing_stop.py               live, stateful, adaptive stop (TrailingStopManager)
│   ├── lorentzian_signal.py           NEW: Lorentzian ANN buy/sell classifier (LorentzianEngine),
│   │                                  self-contained - its own features, no fitted model to pickle
│   ├── train_price_model.py           walk-forward bake-off -> models/*.joblib (now also backtests
│   │                                  + saves the Lorentzian config/skill verdict into the artifact)
│   ├── inference_engine.py            PredictionEngine: features + model + stop + Lorentzian, combined
│   ├── store.py                       SQLite prediction log + forward-test scorer
│   ├── serve_api.py                   FastAPI app - the integration contract
│   ├── live_feed.py                   Dukascopy poller (CLI) - shares fetch logic with /sync
│   ├── smoke_test_api.py              end-to-end test, run this after any change
│   ├── fetch_eurusd.py                original bulk data puller (unchanged)
│   ├── plot_chart.py                  static PNG charts (unchanged; independent of the API)
│   └── train_model.py                 the original direction classifier (research/reporting only)
├── models/eurusd_price_model.joblib   created by train_price_model.py
├── ui/index.html                      the dashboard, served at GET /ui
├── outputs/                            price_model_metrics.json, training_metrics.json, charts
├── schema.sql                          SQLite DDL (Postgres port notes inline)
├── DOCUMENTATION.md                    original research methodology (classifier era)
├── PRICE_MODEL_REPORT.md               live model's validation report (supersedes DOCUMENTATION §5/§8)
├── INTEGRATION.md                      API field-by-field contract, for frontend/DB teammates
├── SYSTEM_GUIDE.md                     this file
└── PROJECT_SUMMARY.md                  short resumption notes for a future session
```

## 6. If you change something - what to re-verify

| You changed | Re-run / re-check |
|---|---|
| `indicators.py` or `features.py` | `smoke_test_api.py` (checks single-bar == batch-bar consistency, which catches feature drift) |
| `trailing_stop.py` | The parity check inline in `smoke_test_api.py`'s engine-level test, and manually re-confirm zero max-diff against `indicators.atr_trailing_stop_adaptive()` on the full history if you touch the recursion itself |
| `lorentzian_signal.py` | `smoke_test_api.py`'s Lorentzian single-bar == batch-path check (bias/confidence/kernel), same reasoning as `trailing_stop.py`'s parity check - and re-run `analyze_lorentzian()` vs `LorentzianEngine` on the full history if you touch the persisted-state recursion in `LorentzianEngine.step()` itself |
| Model params / candidates in `price_models.py` | `train_price_model.py` end to end - read the printed verdict, don't just check it ran |
| Feature defs / entry filters in `LorentzianConfig` | `train_price_model.py` end to end - the Lorentzian-gated backtest and `lorentzian_skill` verdict both need re-running, same as the price model's own verdict |
| `serve_api.py` request/response shape | `smoke_test_api.py` AND `INTEGRATION.md` (keep the example JSON there in sync) |
| Anything touching `on_bars()` | The "single-bar path == batch path" check - this is what catches state bugs that only show up under real traffic patterns |

`smoke_test_api.py` is the regression net for this whole project. It's fast (a few seconds); run
it after every change before touching `uvicorn` by hand.

## 7. Known limitations 

- **No verified trading edge.** Bands are calibrated; direction and the trailing-stop strategy are
  not. See §1 and `PRICE_MODEL_REPORT.md`.
- **Single instrument, single timeframe.** EUR/USD, 1h bars. Adding a symbol means one
  `PredictionEngine` + one trained artifact per symbol (the code doesn't hard-code EUR/USD beyond
  the default `symbol=` argument and the Dukascopy instrument in `live_feed.py`).
- **In-memory engine state.** The rolling window and stop level live in the running process, not
  the database. Run one `uvicorn` worker. After a restart, the warm-up CSV plus a `/sync` (or
  `live_feed.py --once`) catches it back up.
- **No real user auth.** `FMP_API_KEY` is one shared secret, not per-user accounts - see §8.
- **Holdout is ~4.5 months, one market regime.** Sharpe estimates on it have wide error bars
  (s.e. ≈ 1.6). Don't over-read small differences between strategies in the backtest table.
- **`target_ts_est` is an estimate,** not exact - it steps over the weekend close but not around
  daylight-saving shifts (+/-1h twice a year).
- **Lorentzian signal isn't in the DB or dashboard yet.** It's live in `/predict`, `/predict/batch`,
  `/latest` and `/bars` (see `INTEGRATION.md`), but `store.py`/`schema.sql` don't log it and
  `ui/index.html` doesn't draw it - deliberately held back from this pass so the model side can be
  tested against the API contract before the dashboard changes. See §8 item 10.

## 8. Future upgrade paths

Roughly in order of how much they'd move the needle, cheapest first:

1. **Retraining cadence.** Nothing retrains itself. Decide a schedule (weekly? after N new bars?)
   and either cron `train_price_model.py` or build a small trigger. Watch `/forward-test` -
   if band coverage on live bars drifts far from 80%, that's the signal to retrain sooner.
2. **A real target, not just next-bar sign/return.** The highest-leverage change per
   `DOCUMENTATION.md` §6.1: triple-barrier labels, a longer horizon, or volatility-scaled labels
   with a neutral class. This is a `features.py` + `train_price_model.py` change; the API and
   dashboard don't need to know the difference.
3. **More history.** 16,577 bars is ~2.7 years; Dukascopy has 20. Pulling more (edit `START_DATE`
   in `fetch_eurusd.py` or the range in `live_feed.py`'s backfill) is close to free and may matter
   more than any modeling change.
4. **User accounts / real auth**, replacing the single `FMP_API_KEY`. This is the cyber team's
   natural next attachment point: a `users` table (id, username, password hash - argon2id or
   bcrypt, role, disabled flag), a login endpoint issuing a short-lived JWT, and `require_key` in
   `serve_api.py` swapped for a JWT check. An `audit_log` table (timestamp, user, endpoint, source
   IP, status) belongs alongside it. I can build this as a reference module if useful - it wasn't
   in scope for this session.
5. **Multiple symbols.** Parametrize `PredictionEngine`/`fetch_bars` by instrument, one model
   artifact and one engine instance per symbol, one `/bars`, `/predict` etc. per symbol path
   (e.g. `/eurusd/predict`) or a `symbol` field on every request.
6. **Postgres instead of SQLite**, once more than one process needs to read `predictions.db`
   concurrently. `schema.sql` has inline Postgres-port notes; re-implement `store.Store`'s five
   methods (`log`, `pending`, `resolve`, `history`, `forward_stats`) against the new DB - nothing
   else in the API touches storage directly.
7. **A neural sequence model (LSTM/TCN)**, only after #2 and #3 - see `DOCUMENTATION.md` §6.2 for
   the full ordering and why reaching for this first would waste the walk-forward XGBoost baseline
   that currently makes the results interpretable.
8. **Swap the dashboard's hand-drawn chart for TradingView's Lightweight Charts library** if you
   want built-in pan/zoom on long histories - `/bars` and `/latest` already return data in a shape
   that works for either approach (see `INTEGRATION.md` §2b).
9. **Price-feed anomaly detection (the cyber team's half).** The >3% one-bar move guard in
   `inference_engine.py::_validate` is the natural hook: a real anomaly detector can veto or flag a
   bar before it reaches `/predict`, using the same `BarRejected` mechanism.
10. **Wire the Lorentzian signal into the DB and dashboard.** The engine and API contract are done
    (this pass); left for a follow-up, once the model integration itself has been tested: log
    `lorentzian.bias/flip/confidence` per bar in `signals` (a `schema.sql` + `store.Store.log()`
    change), and draw `flip` markers + the `kernel_estimate` "gold line" on the candlestick chart
    in `ui/index.html`, gated behind `skill_established` the same way `edge_verified` gates the
    existing disclaimer banner.

## 9. Usage quick-reference

| I want to... | Do this |
|---|---|
| See the current forecast | Open `/ui`, or `GET /latest` |
| Get raw chart data | `GET /bars?limit=N` |
| Feed it new/historical bars | `POST /predict` (one) or `/predict/batch` (many) |
| Pull real data on demand | `POST /sync`, or run `live_feed.py` |
| Check if the model is any good right now | `GET /forward-test` |
| Understand one field in a response | `INTEGRATION.md` §3 |
| Understand why this model was picked | `PRICE_MODEL_REPORT.md` |
| Retrain from scratch | `python3 train_price_model.py` |
| Verify nothing broke | `python3 smoke_test_api.py` |
