"""
FastAPI service - THE INTEGRATION CONTRACT for the frontend and DB teammates.

Run (from src/):   uvicorn serve_api:app --port 8000
Interactive docs:  http://localhost:8000/docs      (auto-generated from the models below)

Endpoints
  GET  /health            liveness + model version + last bar seen      (no auth)
  GET  /model/info        model kinds, horizons, validation summary
  POST /predict           ONE new closed bar in -> forecast + stop + signal out
  POST /predict/batch     many bars in (catch-up after downtime / backfill)
  GET  /latest            most recent prediction
  GET  /history           logged predictions (with realised prices once known)
  GET  /forward-test      live scorecard: band coverage, MAE vs naive, directional accuracy

Config (environment variables, all optional)
  FMP_MODEL_PATH   ../models/eurusd_price_model.joblib
  FMP_WARMUP_CSV   ../data/raw/eurusd_ohlcv.csv   (history the engine warms up from)
  FMP_DB_PATH      ../data/predictions.db
  FMP_API_KEY      if set, every endpoint except /health requires header  X-API-Key
  FMP_CORS_ORIGINS comma-separated, default http://localhost:3000,http://localhost:5173
  FMP_MAX_BAR_MOVE largest accepted one-bar move as a fraction, default 0.03
"""

import json
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal, Optional

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, model_validator

from features import load_ohlcv
from inference_engine import BarRejected, PredictionEngine
from store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.environ.get("FMP_MODEL_PATH", os.path.join(HERE, "../models/eurusd_price_model.joblib"))
WARMUP_CSV = os.environ.get("FMP_WARMUP_CSV", os.path.join(HERE, "../data/raw/eurusd_ohlcv.csv"))
DB_PATH = os.environ.get("FMP_DB_PATH", os.path.join(HERE, "../data/predictions.db"))
METRICS_PATH = os.path.join(HERE, "../outputs/price_model_metrics.json")
API_KEY = os.environ.get("FMP_API_KEY")
CORS = os.environ.get("FMP_CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")
MAX_MOVE = float(os.environ.get("FMP_MAX_BAR_MOVE", "0.03"))

state: dict = {}
lock = threading.Lock()          # one writer at a time: the stop & window are sequential state


# ------------------------------------------------------------------ schemas --
class Bar(BaseModel):
    """One CLOSED hourly bar. Timestamp = bar OPEN time (Dukascopy convention), UTC."""
    timestamp: datetime = Field(..., examples=["2026-09-04T21:00:00Z"])
    open: float = Field(..., gt=0)
    high: float = Field(..., gt=0)
    low: float = Field(..., gt=0)
    close: float = Field(..., gt=0)
    volume: float = Field(0.0, ge=0)

    @model_validator(mode="after")
    def _ohlc_consistent(self):
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)
                and self.low <= self.high):
            raise ValueError("inconsistent OHLC: need low <= open,close <= high")
        return self


class BatchRequest(BaseModel):
    bars: list[Bar] = Field(..., min_length=1, max_length=5000)
    mode: Literal["live", "replay"] = Field(
        "replay", description="'replay' = backfilling bars that already happened; 'live' = real-time")


class Forecast(BaseModel):
    horizon_bars: int
    target_ts_est: str = Field(..., description="Estimated timestamp of the target bar (+/-1h around DST/weekends)")
    price_p10: float
    price_p50: float = Field(..., description="Median forecast price")
    price_p90: float
    expected_return_bps: float
    band_width_pips: float
    model: str = Field(..., description="Which model produced it: xgb_quantile | ridge | vol_only")


class TrailingStop(BaseModel):
    level: float = Field(..., description="Current stop-loss price. Ratchets in the trend's favour only.")
    trend: Literal["up", "down", "flat"] = Field(..., description="up: stop is below price (long side); down: above")
    distance_pips: float
    distance_atr: Optional[float]
    atr_pips: Optional[float]
    key_multiplier: float = Field(..., description="Volatility-adaptive multiplier applied to the base key (3.0)")
    adaptive: bool
    flip: Optional[Literal["buy", "sell"]] = Field(None, description="Set only on the bar where price crossed the stop")


class Signal(BaseModel):
    action: Literal["BUY", "SELL", "HOLD"] = Field(..., description="BUY/SELL only on a stop flip bar")
    basis: Literal["stop_only", "stop+model"]
    model_bias: Literal["up", "down", "neutral"] = Field(..., description="4h model lean - informational")
    confirmed_by_model: Optional[bool]
    edge_verified: bool = Field(..., description="False = no net-of-cost trading edge was demonstrated. "
                                                 "Show a disclaimer in the UI.")


class PredictionResponse(BaseModel):
    symbol: str
    timeframe: str
    as_of: str
    last_close: float
    model_version: str
    forecasts: list[Forecast]
    trailing_stop: TrailingStop
    signal: Signal
    warnings: list[str]


class BatchResponse(BaseModel):
    count: int
    results: list[PredictionResponse]


# --------------------------------------------------------------------- app ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    eng = PredictionEngine(MODEL_PATH, max_bar_move=MAX_MOVE)
    eng.warm_up(load_ohlcv(WARMUP_CSV))
    state["engine"], state["store"] = eng, Store(DB_PATH)
    yield
    state["store"].conn.close()


app = FastAPI(title="EUR/USD Price Forecast + Adaptive Trailing Stop API", version="1.0.0",
              lifespan=lifespan,
              description="Probabilistic price bands (10/50/90th percentile) for 1h/4h/24h ahead, "
                          "a volatility-adaptive ratcheting stop-loss, and a live forward-test scorecard.")
app.add_middleware(CORSMiddleware, allow_origins=CORS, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type", "X-API-Key"])


def require_key(x_api_key: Optional[str] = Header(None)):
    if API_KEY and not (x_api_key and secrets.compare_digest(x_api_key, API_KEY)):
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")


def _to_frame(bars: list[Bar]) -> pd.DataFrame:
    rows = []
    for b in bars:
        ts = pd.Timestamp(b.timestamp)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        rows.append({"timestamp": ts, "open": b.open, "high": b.high, "low": b.low,
                     "close": b.close, "volume": b.volume})
    return pd.DataFrame(rows).set_index("timestamp").sort_index()


def _process(bars: list[Bar], mode: str) -> list[dict]:
    eng: PredictionEngine = state["engine"]
    store: Store = state["store"]
    with lock:
        try:
            results = eng.on_bars(_to_frame(bars))
        except BarRejected as e:
            raise HTTPException(status_code=e.status, detail=str(e))
        store.log(results, mode)
        for p in store.pending():                       # resolve any forecast whose target bar has now arrived
            hit = eng.close_at(pd.Timestamp(p["bar_ts"]), p["horizon_bars"])
            if hit:
                store.resolve(p["id"], hit[1], hit[0].isoformat())
    return results


# ---------------------------------------------------------------- endpoints --
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.get("/health")
def health():
    eng: PredictionEngine = state["engine"]
    return {"status": "ok" if eng.ready else "warming_up", "model_version": eng.version,
            "bars_in_window": len(eng.hist),
            "last_bar_ts": eng.last_ts.isoformat() if eng.last_ts is not None else None,
            "edge_verified": bool(eng.art["edge_verified"])}


@app.get("/model/info", dependencies=[Depends(require_key)])
def model_info():
    eng: PredictionEngine = state["engine"]
    info = {"version": eng.version, "trained_at": eng.art["trained_at"], "kinds": eng.art["kinds"],
            "horizons": eng.art["horizons"], "quantiles": eng.art["quantiles"],
            "directional_skill": eng.art["directional_skill"], "edge_verified": eng.art["edge_verified"],
            "n_features": len(eng.art["feature_cols"]), "training_data": eng.art["data_range"]}
    if os.path.exists(METRICS_PATH):
        m = json.load(open(METRICS_PATH))
        info["validation"] = {
            h: {"winner": v["winner"],
                "holdout": {k: {"pinball": x["pinball_mean"], "coverage_10_90": x["coverage_10_90"]}
                            for k, x in v["holdout"].items()}}
            for h, v in m["horizons"].items()}
        info["backtest_holdout"] = m["backtest"]["holdout"]
    return info


@app.post("/predict", response_model=PredictionResponse, dependencies=[Depends(require_key)])
def predict(bar: Bar):
    """Send each hourly bar once it has CLOSED. Bars must arrive in time order."""
    return _process([bar], mode="live")[-1]


@app.post("/predict/batch", response_model=BatchResponse, dependencies=[Depends(require_key)])
def predict_batch(req: BatchRequest):
    res = _process(req.bars, mode=req.mode)
    return {"count": len(res), "results": res}


@app.get("/latest", response_model=PredictionResponse, dependencies=[Depends(require_key)])
def latest():
    r = state["engine"].last_result
    if r is None:
        raise HTTPException(404, "no prediction yet - POST a bar to /predict first")
    return r


@app.get("/history", dependencies=[Depends(require_key)])
def history(limit: int = Query(100, ge=1, le=2000), mode: Optional[Literal["live", "replay"]] = None):
    return state["store"].history(limit, mode)


@app.get("/forward-test", dependencies=[Depends(require_key)])
def forward_test(mode: Optional[Literal["live", "replay"]] = None):
    """Scorecard on bars the model never saw. Band coverage should sit near 0.80."""
    return state["store"].forward_stats(mode)
