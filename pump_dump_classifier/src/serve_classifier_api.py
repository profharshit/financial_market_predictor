# FILE: financial-market-project/pump_dump_classifier/src/serve_classifier_api.py
# NEW FILE. Place it inside the classifier's own src/ folder (next to config.py, features.py, main.py).
# It runs as a SEPARATE service because the classifier and the forecaster both have a features.py / config.py,
# and two modules with the same name cannot be loaded in one Python process.
#
# One-time setup:  cd pump_dump_classifier/src && python main.py        (creates the .joblib models in ../outputs)
# Run the service: cd pump_dump_classifier/src && uvicorn serve_classifier_api:app --port 8001

import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import joblib
import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import DATA_PATHS, HYPE_KEYWORDS, OUTPUT_DIR
from data_loader import load_pump_dump_data
from preprocessing import add_clean_columns
from pump_dump_model import build_combined_matrix, prepare_features

API_KEY = os.environ.get("FMP_API_KEY")
ALLOWED_ORIGINS = os.environ.get("FMP_CORS_ORIGINS", "*").split(",")
PUMP_THRESHOLD = float(os.environ.get("FMP_PUMP_THRESHOLD", "0.5"))
MAX_ROWS_TO_SCORE = int(os.environ.get("FMP_MAX_ROWS", "2000"))
SENTIMENT_LABELS = ("positive", "neutral", "negative")
TREND_DAYS = 7
TOP_KEYWORD_COUNT = 10

loaded_models = {}
summary_cache = {}


class PumpMessage(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    timestamp: datetime
    group_id: str = "manual"


class PumpRequest(BaseModel):
    messages: list[PumpMessage] = Field(..., min_length=1, max_length=2000)


class SentimentRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=500)


# Loads the three trained models once at startup so every request is fast.
def load_models():
    loaded_models["sentiment"] = joblib.load(os.path.join(OUTPUT_DIR, "sentiment_model.joblib"))
    loaded_models["pump_tfidf"] = joblib.load(os.path.join(OUTPUT_DIR, "pump_dump_tfidf.joblib"))
    loaded_models["pump"] = joblib.load(os.path.join(OUTPUT_DIR, "pump_dump_model.joblib"))


# Starts the service by loading the models; fails loudly if main.py was never run.
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_models()
    yield


app = FastAPI(title="FMIP Sentiment + Pump-and-Dump Classifier API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET", "POST"], allow_headers=["*"])


# Uses the same shared API key as the forecast API so one key protects both services.
def require_key(x_api_key: Optional[str] = Header(None)):
    if API_KEY and not (x_api_key and secrets.compare_digest(x_api_key, API_KEY)):
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")


# Predicts positive / neutral / negative for already-cleaned texts.
def predict_sentiment(clean_texts):
    pipeline = loaded_models["sentiment"]
    probabilities = pipeline.predict_proba(clean_texts)
    labels = pipeline.classes_[probabilities.argmax(axis=1)]
    return labels, probabilities.max(axis=1)


# Turns the request messages into a dataframe with timezone-free UTC timestamps.
def messages_to_frame(messages):
    rows = [{"text": m.text, "timestamp": to_naive_utc(m.timestamp), "group_id": m.group_id} for m in messages]
    return pd.DataFrame(rows)


# The behaviour features compare timestamps with numpy, which needs naive datetimes.
def to_naive_utc(moment):
    stamp = pd.Timestamp(moment)
    return stamp.tz_convert("UTC").tz_localize(None) if stamp.tzinfo else stamp


# Scores every message with the pump model (text + burst behaviour) and adds its sentiment.
def score_pump_messages(messages):
    prepared = prepare_features(add_clean_columns(messages))
    matrix = build_combined_matrix(prepared, loaded_models["pump_tfidf"], fit=False)
    prepared["pump_probability"] = loaded_models["pump"].predict_proba(matrix)[:, 1]
    prepared["is_pump"] = prepared["pump_probability"] >= PUMP_THRESHOLD
    labels, confidence = predict_sentiment(prepared["clean_for_classical"])
    prepared["sentiment_label"] = labels
    prepared["sentiment_confidence"] = confidence
    return prepared


# Shapes one scored message for the JSON response.
def format_pump_result(row):
    return {
        "text": row["text"],
        "group_id": row["group_id"],
        "timestamp": row["timestamp"].isoformat(),
        "pump_probability": float(row["pump_probability"]),
        "is_pump": bool(row["is_pump"]),
        "sentiment": row["sentiment_label"],
        "hype_keyword_count": int(row["hype_keyword_count"]),
        "posting_velocity": int(row["posting_velocity"]),
        "near_duplicate_burst": int(row["near_duplicate_burst"]),
    }


# Tells the dashboard whether the numbers come from real data or the built-in synthetic sample.
def describe_data_source():
    return "real dataset" if os.path.exists(DATA_PATHS["pump_dump_csv"]) else "synthetic sample"


# Share of positive / neutral / negative messages, in percent.
def sentiment_shares(scored):
    shares = scored["sentiment_label"].value_counts(normalize=True) * 100
    return {label: round(float(shares.get(label, 0.0)), 1) for label in SENTIMENT_LABELS}


# Daily sentiment mix for the most recent days, used by the stacked trend chart.
def daily_trend(scored):
    counts = pd.crosstab(scored["timestamp"].dt.date, scored["sentiment_label"], normalize="index") * 100
    recent = counts.reindex(columns=list(SENTIMENT_LABELS), fill_value=0).tail(TREND_DAYS)
    return {
        "labels": [day.strftime("%b %d") for day in recent.index],
        "series": {label: recent[label].round(1).tolist() for label in SENTIMENT_LABELS},
    }


# Shapes one flagged group for the JSON response.
def format_group_signal(group_id, row):
    return {
        "group_id": str(group_id),
        "total_messages": int(row["total_messages"]),
        "pump_messages": int(row["pump_messages"]),
        "max_probability": float(row["max_probability"]),
        "peak_velocity": int(row["peak_velocity"]),
        "peak_duplicates": int(row["peak_duplicates"]),
        "last_seen": row["last_seen"].isoformat(),
        "sample_text": row["sample_text"],
    }


# Lists the groups that contain pump messages, most suspicious first.
def pump_signals_by_group(scored):
    per_group = scored.groupby("group_id").agg(
        total_messages=("text", "size"), pump_messages=("is_pump", "sum"),
        max_probability=("pump_probability", "max"), peak_velocity=("posting_velocity", "max"),
        peak_duplicates=("near_duplicate_burst", "max"), last_seen=("timestamp", "max"))
    top_text = scored.sort_values("pump_probability", ascending=False).drop_duplicates("group_id")
    flagged = per_group[per_group["pump_messages"] > 0].join(top_text.set_index("group_id")["text"].rename("sample_text"))
    flagged = flagged.sort_values("max_probability", ascending=False)
    return [format_group_signal(group_id, row) for group_id, row in flagged.iterrows()]


# Counts how many messages contain each known hype phrase.
def count_hype_keywords(scored):
    lowered = scored["text"].fillna("").str.lower()
    counts = {word: int(lowered.str.contains(word, regex=False).sum()) for word in HYPE_KEYWORDS}
    found = [(word, count) for word, count in counts.items() if count > 0]
    found.sort(key=lambda item: item[1], reverse=True)
    return [{"keyword": word, "count": count} for word, count in found[:TOP_KEYWORD_COUNT]]


# Scores the message dataset once and builds the whole Sentiment & Pump page payload.
def build_dashboard_summary():
    messages = load_pump_dump_data().sort_values("timestamp").tail(MAX_ROWS_TO_SCORE)
    scored = score_pump_messages(messages[["text", "timestamp", "group_id"]].reset_index(drop=True))
    return {
        "data_source": describe_data_source(),
        "total_messages": int(len(scored)),
        "sentiment": sentiment_shares(scored),
        "trend": daily_trend(scored),
        "pump_signals": pump_signals_by_group(scored),
        "top_keywords": count_hype_keywords(scored),
    }


# Liveness check so the dashboard can tell whether the classifier service is running.
@app.get("/health")
def health():
    return {"status": "ok", "models_loaded": len(loaded_models) == 3, "pump_threshold": PUMP_THRESHOLD,
            "data_source": describe_data_source()}


# Classifies raw texts as positive / neutral / negative.
@app.post("/sentiment/predict", dependencies=[Depends(require_key)])
def sentiment_predict(request: SentimentRequest):
    frame = add_clean_columns(pd.DataFrame({"text": request.texts}))
    labels, confidence = predict_sentiment(frame["clean_for_classical"])
    return [{"text": text, "label": label, "confidence": float(score)}
            for text, label, score in zip(request.texts, labels, confidence)]


# Scores messages for pump-and-dump behaviour; send several from one group to get burst features.
@app.post("/pump/predict", dependencies=[Depends(require_key)])
def pump_predict(request: PumpRequest):
    try:
        scored = score_pump_messages(messages_to_frame(request.messages))
    except ValueError as error:
        raise HTTPException(422, f"could not score these messages: {error}")
    return {"threshold": PUMP_THRESHOLD, "results": [format_pump_result(row) for _, row in scored.iterrows()]}


# Returns the cached summary that the Sentiment & Pump page and dashboard KPI display.
@app.get("/dashboard/summary", dependencies=[Depends(require_key)])
def dashboard_summary(refresh: bool = Query(False)):
    if refresh or "summary" not in summary_cache:
        summary_cache["summary"] = build_dashboard_summary()
    return summary_cache["summary"]