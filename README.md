# Financial Market Intelligence Platform (FMIP)

A research prototype that combines data science and cybersecurity in one dashboard:

| Module | What it does | Technique |
|---|---|---|
| EUR/USD forecaster | 1h / 4h / 24h price bands (P10, P50, P90) and an adaptive trailing stop | XGBoost quantile regression, ATR stop |
| Lorentzian signal | Second, independent buy/sell read on direction | Lorentzian-distance k-NN, kernel trend |
| NSE anomaly detector | Flags unusual price/volume behaviour in NSE stocks | Isolation Forest |
| Sentiment and pump-and-dump classifier | Scores social/chat messages for sentiment and coordinated pump behaviour | TF-IDF + Logistic Regression, Random Forest with burst features |
| Secure API gateway (CY-3) | Registration, login, JWT, encrypted portfolio | FastAPI, Argon2, Fernet (separate service) |
| FMIP dashboard | Single front end for all of the above | HTML, Chart.js, Lightweight Charts |

> **Research prototype, not trading advice.** Every forecast response carries `edge_verified: false`
> because backtests with transaction costs did not show a tradable edge. See `DOCUMENTATION.md`.

## Quick start (Windows)

```powershell
cd D:\financial-market-project
.\mpenv\Scripts\Activate.ps1
pip install -r requirements.txt
python run_fmip.py
```

`run_fmip.py` does everything in one command:

1. Creates `.env` with a random `FMP_API_KEY` on the first run (nothing to type).
2. Trains the classifier models on the first run only.
3. Starts the classifier service (port 8001) and the forecast service (port 8000).
4. Opens the dashboard at http://127.0.0.1:8000/ui.

Press `Ctrl+C` in the terminal to stop both services.

## Project structure

```
financial-market-project/
├── run_fmip.py                     one-command launcher
├── .env                            auto-created API key (git-ignored)
├── requirements.txt
├── data/
│   ├── raw/eurusd_ohlcv.csv        hourly EUR/USD bars
│   ├── nse/nse_cy1_data.csv        NSE data for the anomaly detector
│   └── predictions.db              SQLite prediction log (auto-created)
├── models/eurusd_price_model.joblib
├── outputs/                        metrics and charts
├── src/                            forecast service (port 8000)
│   ├── serve_api.py                FastAPI app
│   ├── fmip_routes.py              anomaly and feature-importance endpoints
│   ├── anomaly_detector.py         Isolation Forest scan
│   ├── inference_engine.py         features + model + stop + Lorentzian
│   ├── features.py, indicators.py, price_models.py
│   ├── trailing_stop.py, lorentzian_signal.py
│   ├── store.py, live_feed.py, train_price_model.py
│   └── smoke_test_api.py           regression test
├── pump_dump_classifier/           classifier service (port 8001)
│   ├── outputs/                    trained .joblib models
│   └── src/
│       ├── serve_classifier_api.py classifier API
│       ├── main.py                 trains and evaluates both models
│       └── config.py, data_loader.py, preprocessing.py, features.py,
│           sentiment_model.py, pump_dump_model.py, evaluate.py
└── ui/
    ├── index.html                  FMIP dashboard
    └── static/js/fmipLive.js       connects the dashboard to the APIs
```

The classifier is a separate service because both projects contain a `features.py` and a `config.py`;
two modules with the same name cannot be loaded in one Python process.

## Architecture

```
                 Browser: FMIP dashboard (ui/index.html + fmipLive.js)
                      │                              │
          X-API-Key   │                              │   X-API-Key
                      ▼                              ▼
     Forecast service :8000                Classifier service :8001
     ├─ /latest /bars /forward-test        ├─ /sentiment/predict
     ├─ /model/info /model/importance      ├─ /pump/predict
     ├─ /anomaly/scan  (Isolation Forest)  └─ /dashboard/summary
     ├─ /predict /predict/batch /sync
     └─ SQLite prediction log
```

## API endpoints

**Forecast service (port 8000)**

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness, model version, last bar (no key needed) |
| POST | `/predict`, `/predict/batch` | Send closed hourly bars, receive forecast, stop and signal |
| GET | `/latest` | Most recent prediction |
| GET | `/bars?limit=N` | Recent bars with stop and Lorentzian values, for the chart |
| GET | `/forward-test` | Live scorecard on unseen bars (band coverage, MAE, direction) |
| GET | `/model/info` | Model kinds and validation summary |
| GET | `/model/importance?top=5` | Top XGBoost features for the 4h model |
| GET | `/anomaly/scan?refresh=false` | Isolation Forest report on the NSE csv |
| POST | `/sync` | Pull new bars from Dukascopy (needs internet) |

**Classifier service (port 8001)**

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Models loaded, pump threshold, data source |
| POST | `/sentiment/predict` | Positive / neutral / negative for a list of texts |
| POST | `/pump/predict` | Pump probability per message (send several from one group for burst features) |
| GET | `/dashboard/summary` | Sentiment shares, trend, pump signals by group, top hype keywords |

Interactive docs: http://127.0.0.1:8000/docs and http://127.0.0.1:8001/docs.

## Configuration (environment variables, all optional)

| Variable | Default | Meaning |
|---|---|---|
| `FMP_API_KEY` | generated by `run_fmip.py` | Shared key for both services (`X-API-Key` header) |
| `FMP_NSE_CSV` | `data/nse/nse_cy1_data.csv` | Input for the anomaly detector |
| `FMP_PUMP_THRESHOLD` | `0.5` | Probability above which a message counts as a pump |
| `FMP_MAX_ROWS` | `2000` | Most recent messages scored for the dashboard summary |
| `FMP_CORS_ORIGINS` | `*` | Allowed browser origins (restrict before deployment) |
| `FMP_MAX_BAR_MOVE` | `0.03` | Largest accepted one-bar price move |

## Running the parts separately

```powershell
# classifier (once, then service)
cd pump_dump_classifier\src
python main.py
uvicorn serve_classifier_api:app --port 8001

# forecast service
cd src
python train_price_model.py        # only when retraining
python smoke_test_api.py           # must print ALL TESTS PASSED
uvicorn serve_api:app --port 8000
```

## Key results (honest summary)

- **Price bands are calibrated.** 10-90 coverage on the untouched holdout was 0.805 (1h) and 0.815 (4h)
  against a target of 0.80. XGBoost beat a volatility-only benchmark by about 3% pinball loss at 1h and 4h.
  No model beat the benchmark at 24h.
- **No directional or trading edge.** Median forecasts were statistically tied with "no change", and the
  stop-loss strategies lost money net of costs on the holdout.
- **Classifier metrics of 1.00 come from synthetic data** and only prove the pipeline runs. Retrain on the
  real Telegram pump-and-dump dataset before quoting any score.
- **Anomaly flags are not fraud findings.** The Isolation Forest is unsupervised and has no accuracy figure.

## Known limitations

- Single instrument and timeframe (EUR/USD, 1h). Engine state is in memory, so run one worker.
- The shared API key is not per-user authentication. The JWT/Argon2 security API (CY-3) runs as its own
  service and is not yet connected to the forecast and classifier services.
- The dashboard sign-in screen, NIFTY/SENSEX/VIX/GBP tickers, the Audit Logs, API Gateway and Auth pages,
  and the Settings page still show demo data. BTC and ETH prices come from the Binance websocket.
- The Lorentzian signal is shown on the chart but is not stored in the database yet.

## Team modules

| Module | Area |
|---|---|
| CY-1 | NSE price and volume anomaly detection |
| CY-3 | Secure API and authentication gateway |
| DS/ML | EUR/USD forecaster, Lorentzian signal, sentiment and pump-and-dump classifier |
| Front end | FMIP dashboard and integration |

## Further reading

`DOCUMENTATION.md` (design, methods, results, report text), `INTEGRATION.md` (API field contract),
`PRICE_MODEL_REPORT.md` (model selection and validation), `SYSTEM_GUIDE.md` (operations guide).
