# FMIP Project Documentation

Financial Market Intelligence Platform: design, methods, results and limitations.
This document supersedes the classifier-era `DOCUMENTATION.md` for anything about the integrated system.

---

## 1. Project overview

FMIP is a mini project by a team of seven (three data science/ML, four cybersecurity). It joins four
analytical modules and a secure API behind one dashboard:

1. EUR/USD probabilistic price forecaster with an adaptive trailing stop
2. Lorentzian ANN buy/sell signal
3. NSE price/volume anomaly detector (Isolation Forest)
4. Sentiment and pump-and-dump classifier
5. Secure API gateway with JWT authentication (CY-3, separate service)

The goal is to show a complete pipeline: data in, models, validated outputs, secured API, dashboard.
The project reports its negative results (no trading edge) as findings rather than hiding them.

## 2. System architecture

```
Browser (FMIP dashboard)
   ├── Forecast service :8000   forecaster, Lorentzian, anomaly detector, SQLite log
   └── Classifier service :8001 sentiment model, pump-and-dump model
```

**Why two services.** The forecaster and the classifier each have their own `features.py` and `config.py`.
Python caches imports by module name, so both cannot live in one process. Separate services also match
how the team works: each module owns its own code and can be restarted independently.

**Launcher.** `run_fmip.py` generates a random `FMP_API_KEY` once (stored in `.env`), trains the classifier
if its models are missing, starts both services with that key, waits for `/health`, and opens the browser.

**Key hand-off.** The forecast service injects the key into the dashboard page it serves
(`window.FMIP_API_KEY`), so the user never types it. Both services check it with a constant-time compare.

## 3. Module descriptions

### 3.1 EUR/USD forecaster
- Data: 16,577 hourly bars, 2024-01-08 to 2026-09-04 (Dukascopy, bid side).
- Target: volatility-scaled forward return `z = log(P[t+h] / P[t]) / (ATR_pct * sqrt(h))` for h = 1, 4, 24.
- Output: 10th, 50th, 90th percentile converted back to prices.
- Features: 25 stationary features (multi-horizon returns in ATR units, volatility regime, RSI, MACD,
  Bollinger %B, candle shape, trailing-stop state, hour of day). Raw price levels are never used.
- Models compared: `vol_only` (no-skill benchmark), `ridge`, `xgb_quantile`.
- Selection rule fixed before looking at results: a model replaces the benchmark only if its mean pinball loss
  is lower and it wins at least 4 of 6 walk-forward folds.
- Trailing stop: ATR stop ported from the Pine Script "Krishna's ATR Bot", with a volatility-adaptive key
  multiplier (clipped to 0.75-1.5). It only ratchets in the trend's favour.

### 3.2 Lorentzian signal
A k-nearest-neighbour vote over five normalised indicators (RSI, WaveTrend, CCI, ADX) using Lorentzian
distance, with volatility, regime and kernel-trend filters. It keeps persistent bias state like the trailing
stop. It may confirm or veto BUY/SELL only after its own holdout backtest shows a net-of-cost edge
(`lorentzian_skill`). Until then it is informational and drawn on the chart as gold markers and a kernel line.

### 3.3 NSE anomaly detector (`src/anomaly_detector.py`)
- Input: `nse_cy1_data.csv` (492 rows, NSE daily data with delivery and trade counts).
- Features (per stock): price change %, volume change %, volume ratio vs 20-day mean, 20-day volatility,
  VWAP deviation %, trade-count change %, delivery change.
- Cleaning: numeric parsing, duplicate removal, 1%/99% clipping so one bad value cannot dominate.
- Model: Isolation Forest, 250 trees, contamination = min(0.10, max(0.03, 5 / rows)), fixed seed.
- Labels: **Anomaly** (model flag), **Watch** (score in the lowest 15% but not flagged), **Normal**.
- Trigger: the feature with the largest absolute z-score for that row.
- Heatmap level: an anomaly with volume ratio of at least 3 is shown as High.
- Feature weights: mean absolute z-score of each feature over the anomalies, scaled so the top is 1.0.
- Served by `GET /anomaly/scan` and cached, because the csv does not change between requests.

### 3.4 Sentiment and pump-and-dump classifier
- **Sentiment**: TF-IDF (20,000 features, unigrams and bigrams) + Logistic Regression, three classes.
- **Pump-and-dump**: TF-IDF (5,000 features) joined with seven numeric features, then a Random Forest
  (400 trees, balanced class weights). Numeric features:
  hype keyword count, exclamation rate, capital-letter rate, emoji count, text length,
  **posting velocity** (messages from the same group within 15 minutes) and
  **near-duplicate burst** (cosine similarity above 0.85 within the window).
- Behaviour features matter because wording is easy to vary but coordinated timing is harder to hide.
- Served by `/sentiment/predict`, `/pump/predict` and `/dashboard/summary`. The summary scores the most
  recent 2,000 messages, groups them, and returns sentiment shares, a 7-day trend, flagged groups and
  top hype keywords. A single pasted message has no burst context, so its probability relies on wording only.

### 3.5 Secure API gateway (CY-3)
FastAPI + SQLite service with Argon2 password hashing, JWT access tokens with expiry, user-scoped notes and
portfolio, Fernet-encrypted portfolio data, BOLA/IDOR ownership checks, login rate limiting (5 per minute),
and security headers. It runs separately; see its own README.

### 3.6 Dashboard
`ui/index.html` is the front end. `ui/static/js/fmipLive.js` overrides its demo functions so panels read from
the APIs:

| Dashboard area | Data source |
|---|---|
| EUR/USD candles, volume, adaptive stop, Lorentzian line and markers | `/bars` |
| Signal bar (BUY/SELL/HOLD, entry, stop, target) | `/latest` |
| Next 1h forecast card, Forecast page, band chart | `/latest`, `/bars` |
| Band coverage KPI and coverage card | `/forward-test` |
| Feature importance | `/model/importance` |
| Anomaly KPIs, alert table, heatmap, feature weights | `/anomaly/scan` |
| Sentiment KPIs, trend chart, pump card, keyword cloud, message analyzer | classifier service |
| BTC and ETH charts | Binance websocket |

Each service fails independently: if one is offline, only its panels show an offline notice.

## 4. Validation methodology

- Chronological splits only; no shuffling anywhere.
- Six-fold expanding-window walk-forward validation with an embargo of h bars between train and test.
- Untouched holdout: last 2,487 bars (2026-04-14 to 2026-09-04).
- Directional skill tested with a one-sided binomial test, with counts divided by the horizon because
  overlapping targets are not independent.
- Backtest with 1 pip cost per position change; an edge counts as verified only if net return is positive and
  Sharpe exceeds two standard errors.
- `smoke_test_api.py` checks the API contract, the stop's ratchet property, forward-test resolution, and that
  one-bar-at-a-time processing equals batch processing for forecasts, stop and Lorentzian state.

## 5. Results

### 5.1 Forecaster (walk-forward mean pinball loss, lower is better)

| Horizon | vol_only | ridge | xgb_quantile | Winner |
|---|---|---|---|---|
| 1h | 0.1701 | 0.1706 | **0.1648** | xgb_quantile (6/6 folds) |
| 4h | 0.1781 | 0.1793 | **0.1731** | xgb_quantile (6/6 folds) |
| 24h | **0.1884** | 0.1922 | 0.1903 | vol_only |

Holdout 10-90 coverage: 0.805 (1h), 0.815 (4h), 0.851 (24h), against a target of 0.80.
Median forecast MAE versus "no change" was 0.997x to 1.000x, so there is no point-forecast skill.
The top features are `hour_cos` and `hour_sin`, then range, Bollinger width and ATR, which shows the gain
comes from calibrating band width to the intraday volatility pattern, not from predicting direction.

### 5.2 Trading backtest (holdout, net of 1 pip cost)

| Strategy | Net return | Sharpe |
|---|---|---|
| Buy and hold | -1.42% | -0.71 |
| Fixed stop (original Pine) | -7.17% | -3.68 |
| Adaptive stop | -6.71% | -3.43 |
| Adaptive stop + model gate | -1.58% | -2.74 |

Conclusion: `edge_verified = false`. Sharpe standard error is about 1.6, so small differences are noise.

### 5.3 Classifier
Both saved reports show accuracy, precision, recall and F1 of 1.00 (sentiment: 400 test rows; pump: 48 rows
with 6 pump messages). **These were produced on the built-in synthetic sample**, where templated sentences
make the task trivial. They show the pipeline works end to end, not real-world accuracy. Final numbers must
come from the real Telegram pump-and-dump dataset, reported with recall on the pump class, since pumps are rare.

### 5.4 Anomaly detector
Unsupervised, so no accuracy metric exists. Results are rankings for analyst review. A flag means "unusual
compared with this dataset", not fraud. A false-positive estimate needs labelled events (for example
known SEBI-flagged episodes) to measure.

## 6. Security considerations

- Shared API key checked with `secrets.compare_digest`; stored in `.env`, which must stay out of git.
- Bar sanity guard: one-bar moves above 3% are rejected (HTTP 422) before touching model or stop state;
  inconsistent OHLC is rejected by schema; duplicate or out-of-order bars return 409.
- Dashboard text from APIs (such as Telegram messages) is HTML-escaped before display.
- CORS defaults to `*` for local development and must be restricted before any deployment.
- Broker API keys must never be placed in a shared project; the platform uses data endpoints only.
- **Integration boundary:** the forecast and classifier services use one shared key, not the JWT gateway.
  Service-to-service authentication through the CY-3 gateway is the next milestone.

## 7. Limitations

1. No verified trading edge; direction is not predictable beyond noise.
2. The holdout covers about 4.5 months and one market regime.
3. Classifier scores are from synthetic data until the real dataset is used.
4. The anomaly detector has no labels and was run on a small csv (492 rows).
5. Engine state is in memory; run one worker and run `/sync` after a restart.
6. Dashboard sign-in, several tickers, Audit Logs, API Gateway, Auth and Settings pages remain demo data.
7. The Lorentzian signal is not yet stored in the database.
8. EUR/USD only, hourly bars only; 4H and 1D candles are built in the browser from hourly bars.

## 8. Future work

- Replace the shared key with the JWT gateway and add per-user audit logging.
- Train the pump-and-dump model on the real Sapienza Telegram dataset and report pump-class recall.
- Label anomaly events to measure precision, and extend the detector to many NSE symbols.
- Better target definitions (triple-barrier or volatility-scaled labels) and more history before any
  neural sequence model.
- Persist Lorentzian output and the prediction ledger in the database.
- Scheduled retraining watched by the `/forward-test` coverage figure.

## 9. Paragraph for the report

> The Financial Market Intelligence Platform integrates four analytical modules and a secured API behind a
> single dashboard. A quantile gradient-boosting model produces calibrated 10/50/90th percentile price bands for
> EUR/USD at 1, 4 and 24 trading-hour horizons, validated by six-fold walk-forward testing and a 2,487-bar
> untouched holdout (coverage 0.805 and 0.815 at 1h and 4h). It improved pinball loss by about 3% over a
> volatility-only benchmark, while directional accuracy and trailing-stop strategies showed no net-of-cost
> edge, which is reported as a finding consistent with market efficiency. An Isolation Forest scans NSE price
> and volume features to rank unusual stock behaviour for analyst review, and a TF-IDF plus behavioural-feature
> Random Forest scores messages for coordinated pump-and-dump patterns using posting velocity and
> near-duplicate bursts. The services run separately, share an API key generated by a one-command launcher, and
> feed one dashboard that degrades gracefully when a service is offline. Classifier results to date come from
> synthetic data and anomaly flags are unlabelled, so both are presented as pipeline demonstrations pending
> real-data evaluation.

## 10. Run and test checklist

1. `python run_fmip.py` and confirm the dashboard opens.
2. In `src/`, run `python smoke_test_api.py` and confirm `ALL TESTS PASSED`.
3. Check http://127.0.0.1:8000/health and http://127.0.0.1:8001/health both report `ok`.
4. Open the Anomaly and Sentiment pages and confirm the tables and charts fill from the APIs.
