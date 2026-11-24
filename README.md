<!-- FILE: financial-market-project/README.md  (project root, replaces the existing README.md) -->

# Financial Market Intelligence Platform (FMIP)

A research prototype that combines data science and cybersecurity behind one dashboard.

> **Research prototype, not trading advice.** Every forecast response carries `edge_verified: false`
> because backtests with transaction costs showed no tradable edge.

## Modules

| Module | What it does | Technique |
|---|---|---|
| EUR/USD forecaster | 1h / 4h / 24h price bands (P10, P50, P90) plus an adaptive trailing stop | XGBoost quantile regression, ATR stop |
| Lorentzian signal | Second, independent buy/sell read on direction | Lorentzian-distance k-NN, kernel trend |
| NSE anomaly detector (CY-1) | Flags unusual price/volume behaviour in NSE stocks | Isolation Forest |
| Sentiment and pump-and-dump classifier | Scores messages for sentiment and coordinated pump behaviour | TF-IDF + Logistic Regression, Random Forest |
| Secure API gateway (CY-3) | Registration, login, JWT, encrypted portfolio | FastAPI, Argon2, Fernet |
| Dashboard | Single front end: charts, Indicator Lab, paper-trading terminal | HTML, Chart.js, Lightweight Charts, TradingView widget |

## Directory layout

```text
financial-market-project/
├── README.md                          <- this file (root)
├── DOCUMENTATION.md                   <- design, results, upgrades (root)
├── INTEGRATION.md                     <- API field contract
├── PRICE_MODEL_REPORT.md              <- model selection report
├── SYSTEM_GUIDE.md                    <- operations guide
├── run_fmip.py                        <- one-command launcher
├── .env                               <- FMP_API_KEY (git-ignored)
├── requirements.txt
├── data/
│   ├── raw/eurusd_ohlcv.csv           <- hourly EUR/USD bars
│   ├── nse/nse_cy1_data.csv           <- NSE data for the anomaly detector
│   └── predictions.db                 <- SQLite log (auto-created)
├── models/eurusd_price_model.joblib   <- created by train_price_model.py
├── outputs/                           <- metrics JSON and charts
├── schema.sql                         <- database contract
├── src/                               <- forecast service, port 8000
│   ├── serve_api.py                   <- FastAPI app
│   ├── fmip_routes.py                 <- anomaly + feature-importance endpoints
│   ├── anomaly_detector.py            <- Isolation Forest scan
│   ├── inference_engine.py            <- features + model + stop + Lorentzian
│   ├── features.py, indicators.py, price_models.py
│   ├── trailing_stop.py, lorentzian_signal.py
│   ├── store.py, live_feed.py, fetch_eurusd.py
│   ├── train_price_model.py, train_model.py, plot_chart.py
│   └── smoke_test_api.py              <- regression test
├── pump_dump_classifier/              <- classifier service, port 8001
│   ├── outputs/                       <- trained .joblib models
│   └── src/
│       ├── serve_classifier_api.py, main.py
│       └── config.py, data_loader.py, preprocessing.py, features.py,
│           sentiment_model.py, pump_dump_model.py, evaluate.py
├── security-api/                      <- CY-3 service (separate, own README)
│   └── app/ (main.py, api/, core/, db/)
└── ui/
    ├── index.html                     <- dashboard shell
    └── static/js/
        ├── fmipLive.js                <- live wiring for forecast, anomaly, sentiment
        ├── fmipIndicators.js          <- Indicator Lab page
        └── paperTrading.js            <- server-backed paper trading terminal
```

The classifier is a separate service because both projects contain a `features.py` and a `config.py`;
two modules with the same name cannot be loaded in one Python process.

## Quick start (Windows)

```powershell
cd D:\financial-market-project
.\mpenv\Scripts\Activate.ps1
pip install -r requirements.txt
python run_fmip.py
```

`run_fmip.py` creates `.env` with a random `FMP_API_KEY` on first run, trains the classifier if its
models are missing, starts both services, and opens http://127.0.0.1:8000/ui.

## Running the parts separately

```powershell
# forecast service
cd src
python train_price_model.py        # only when retraining
python smoke_test_api.py           # must print ALL TESTS PASSED
uvicorn serve_api:app --port 8000  # keep a single worker

# classifier service
cd pump_dump_classifier\src
python main.py
uvicorn serve_classifier_api:app --port 8001

# security API (CY-3)
cd security-api
python -m uvicorn app.main:app --reload --port 8002
```

## Dashboard pages

| Page | Data source |
|---|---|
| Dashboard | TradingView chart, forecast card, anomaly and sentiment KPIs |
| Trading Terminal | TradingView chart, paper orders saved per user on the server |
| EUR/USD Forecast | `/latest`, `/bars`, `/forward-test`, `/model/importance` |
| Indicator Lab | `/bars` and `/latest`: stop, Lorentzian kernel, flips, forecast band |
| Anomaly Detection | `/anomaly/scan` |
| Sentiment and Pump | classifier service `/dashboard/summary` |
| API Gateway, Auth, Audit Logs, Settings | demo data only |

## API endpoints

**Forecast service (8000)**

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness and last bar (no key) |
| POST | `/predict`, `/predict/batch` | Send closed hourly bars, receive forecast, stop and signal |
| GET | `/latest` | Most recent prediction |
| GET | `/bars?limit=N` | Recent bars with stop and Lorentzian values |
| GET | `/history`, `/forward-test` | Logged predictions and live scorecard |
| GET | `/model/info`, `/model/importance` | Model summary and top features |
| GET | `/anomaly/scan` | Isolation Forest report on the NSE csv |
| POST | `/sync` | Pull new bars from Dukascopy (needs internet) |

**Paper trading (used by `paperTrading.js`, must be served by the forecast service)**
`POST /paper/auth/register`, `POST /paper/auth/login`, `POST /paper/auth/logout`,
`GET /paper/account`, `POST /paper/orders`, `POST /paper/reset`.

**Classifier service (8001):** `/health`, `POST /sentiment/predict`, `POST /pump/predict`, `GET /dashboard/summary`.

**Security API (CY-3):** `/api/v1/auth/register`, `/login`, `/me`, plus notes and portfolio routes.

Interactive docs: `/docs` on each service.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FMP_API_KEY` | generated by launcher | Shared key sent as `X-API-Key` |
| `FMP_NSE_CSV` | `data/nse/nse_cy1_data.csv` | Anomaly detector input |
| `FMP_PUMP_THRESHOLD` | `0.5` | Probability that counts as a pump |
| `FMP_MAX_ROWS` | `2000` | Messages scored for the summary |
| `FMP_CORS_ORIGINS` | `*` | Restrict before deployment |
| `FMP_MAX_BAR_MOVE` | `0.03` | Largest accepted one-bar move |

## Key results

- Price bands are calibrated: holdout 10-90 coverage 0.805 (1h) and 0.815 (4h) against a 0.80 target.
- XGBoost beat a volatility-only benchmark by about 3% pinball loss at 1h and 4h; nothing beat it at 24h.
- No directional or trading edge; stop strategies lost money net of costs on the holdout.
- Classifier scores of 1.00 come from synthetic data and only prove the pipeline runs.
- Anomaly flags are unsupervised rankings, not fraud findings.

## Known limitations

- Single instrument and timeframe (EUR/USD, 1h); engine state is in memory, so run one worker.
- The shared API key is not per-user authentication; the JWT gateway is not connected yet.
- Ticker values for GBP/USD, NIFTY, SENSEX, VIX, and the API Gateway, Auth, Audit Logs and Settings pages are demo data.
- The Lorentzian signal is drawn on charts but not stored in the database.
- Confirm that `index.html` loads `fmipLive.js`; the current page only references `paperTrading.js` and `fmipIndicators.js`.

## Team modules

| Module | Area |
|---|---|
| CY-1 | NSE price and volume anomaly detection |
| CY-3 | Secure API and authentication gateway |
| DS/ML | Forecaster, Lorentzian signal, classifier |
| Front end | Dashboard and integration |

## Further reading

`DOCUMENTATION.md`, `INTEGRATION.md`, `PRICE_MODEL_REPORT.md`, `SYSTEM_GUIDE.md`.