# FILE: financial-market-project/src/anomaly_detector.py
# NEW FILE. Place it next to serve_api.py, inference_engine.py and the other files in src/.
# Isolation Forest anomaly detector for NSE price/volume data (ported from the Flask prototype app.py).

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

REQUIRED_COLUMNS = ["DATE", "SYMBOL", "CLOSE", "VOLUME", "VWAP", "NO_OF_TRADES", "DELIVERY_PCT"]
NUMERIC_COLUMNS = ["CLOSE", "VOLUME", "VWAP", "NO_OF_TRADES", "DELIVERY_PCT"]
MODEL_FEATURES = [
    "PRICE_CHANGE_PCT", "VOLUME_CHANGE_PCT", "VOLUME_RATIO", "VOLATILITY_20",
    "VWAP_DEVIATION_PCT", "TRADE_COUNT_CHANGE_PCT", "DELIVERY_CHANGE_PCT",
]
TRIGGER_NAMES = {
    "PRICE_CHANGE_PCT": "Price jump",
    "VOLUME_CHANGE_PCT": "Volume spike",
    "VOLUME_RATIO": "Volume spike",
    "VOLATILITY_20": "Volatility surge",
    "VWAP_DEVIATION_PCT": "VWAP deviation",
    "TRADE_COUNT_CHANGE_PCT": "Trade count jump",
    "DELIVERY_CHANGE_PCT": "Delivery shift",
}
MIN_ROWS_REQUIRED = 30
HIGH_RISK_VOLUME_RATIO = 3.0
WATCH_SCORE_PERCENTILE = 15
HEATMAP_CELL_COUNT = 60
RANKED_ROW_COUNT = 10


# Reads the NSE csv and fails early with a clear message if a needed column is missing.
def load_nse_data(csv_path):
    raw_data = pd.read_csv(csv_path)
    raw_data.columns = [str(name).strip().upper() for name in raw_data.columns]
    missing_columns = [name for name in REQUIRED_COLUMNS if name not in raw_data.columns]
    if missing_columns:
        raise ValueError("Missing columns: " + ", ".join(missing_columns))
    return raw_data


# Converts text values like "1,234" or "45%" to numbers and removes unusable rows.
def clean_raw_values(stock_data):
    cleaned = stock_data.copy()
    for column in NUMERIC_COLUMNS:
        text = cleaned[column].astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False)
        cleaned[column] = pd.to_numeric(text, errors="coerce")
    cleaned["DATE"] = pd.to_datetime(cleaned["DATE"], errors="coerce")
    cleaned["SYMBOL"] = cleaned["SYMBOL"].astype(str).str.strip()
    cleaned = cleaned.dropna(subset=["DATE", "SYMBOL", "CLOSE", "VOLUME"])
    return cleaned.sort_values(["SYMBOL", "DATE"]).drop_duplicates(["DATE", "SYMBOL"]).reset_index(drop=True)


# Builds the seven per-stock features the Isolation Forest learns from.
def add_model_features(stock_data):
    data = stock_data.copy()
    by_symbol = data.groupby("SYMBOL")
    data["PRICE_CHANGE_PCT"] = by_symbol["CLOSE"].pct_change() * 100
    data["VOLUME_CHANGE_PCT"] = by_symbol["VOLUME"].pct_change() * 100
    average_volume = by_symbol["VOLUME"].transform(lambda volume: volume.rolling(20, min_periods=5).mean())
    data["VOLUME_RATIO"] = data["VOLUME"] / average_volume
    data["VOLATILITY_20"] = data.groupby("SYMBOL")["PRICE_CHANGE_PCT"].transform(
        lambda change: change.rolling(20, min_periods=5).std())
    data["VWAP_DEVIATION_PCT"] = (data["CLOSE"] - data["VWAP"]) / data["VWAP"] * 100
    data["TRADE_COUNT_CHANGE_PCT"] = by_symbol["NO_OF_TRADES"].pct_change() * 100
    data["DELIVERY_CHANGE_PCT"] = by_symbol["DELIVERY_PCT"].diff()
    return data.replace([np.inf, -np.inf], np.nan)


# Keeps only rows with every feature present and refuses to run on too little data.
def select_model_rows(feature_data):
    model_rows = feature_data.dropna(subset=MODEL_FEATURES).copy()
    if len(model_rows) < MIN_ROWS_REQUIRED:
        raise ValueError(f"Need at least {MIN_ROWS_REQUIRED} clean rows, found {len(model_rows)}")
    return model_rows


# Caps the extreme 1% on each side so one bad value cannot dominate the model.
def clip_extreme_values(features):
    clipped = features.copy()
    for column in clipped.columns:
        lower_limit = clipped[column].quantile(0.01)
        upper_limit = clipped[column].quantile(0.99)
        clipped[column] = clipped[column].clip(lower_limit, upper_limit)
    return clipped


# Fits the Isolation Forest and returns its flags, scores and the clipped features it used.
def score_rows(model_rows):
    features = clip_extreme_values(model_rows[MODEL_FEATURES])
    contamination = min(0.10, max(0.03, 5 / len(model_rows)))
    model = IsolationForest(n_estimators=250, contamination=contamination, random_state=42)
    flags = model.fit_predict(features)
    return flags, model.score_samples(features), contamination, features


# Puts every feature on the same scale so we can tell which one drove a flag.
def standardize(features):
    spread = features.std().replace(0, np.nan)
    return ((features - features.mean()) / spread).fillna(0)


# Adds status (Anomaly / Watch / Normal) and the feature that pushed each row furthest from normal.
def label_rows(model_rows, flags, scores, z_scores):
    labeled = model_rows.copy()
    labeled["ANOMALY_SCORE"] = scores
    labeled["IS_ANOMALY"] = flags == -1
    watch_cutoff = np.percentile(scores, WATCH_SCORE_PERCENTILE)
    conditions = [labeled["IS_ANOMALY"], scores <= watch_cutoff]
    labeled["STATUS"] = np.select(conditions, ["Anomaly", "Watch"], default="Normal")
    labeled["TRIGGER"] = z_scores.abs().idxmax(axis=1).map(TRIGGER_NAMES)
    return labeled


# Decides the heatmap colour bucket; a flagged row with a big volume surge counts as high.
def heatmap_level(row):
    if row["STATUS"] == "Anomaly":
        return "high" if row["VOLUME_RATIO"] >= HIGH_RISK_VOLUME_RATIO else "anomaly"
    return "watch" if row["STATUS"] == "Watch" else "normal"


# Converts one scored row into the plain dictionary the dashboard reads.
def row_to_dictionary(row):
    return {
        "symbol": row["SYMBOL"],
        "date": row["DATE"].strftime("%Y-%m-%d"),
        "score": float(row["ANOMALY_SCORE"]),
        "trigger": row["TRIGGER"],
        "price_change_pct": float(row["PRICE_CHANGE_PCT"]),
        "volume_ratio": float(row["VOLUME_RATIO"]),
        "status": row["STATUS"],
        "level": heatmap_level(row),
    }


# Shows which features separate anomalies from normal rows, scaled so the strongest is 1.0.
def calculate_feature_weights(z_scores, anomaly_mask):
    focus_rows = z_scores[anomaly_mask] if anomaly_mask.any() else z_scores
    mean_strength = focus_rows.abs().mean()
    strongest = mean_strength.max() or 1.0
    ranked = (mean_strength / strongest).sort_values(ascending=False)
    return [{"name": name, "weight": float(weight)} for name, weight in ranked.items()]


# Packs everything the dashboard needs (counts, heatmap, ranked rows) into one response.
def build_scan_report(labeled, z_scores, contamination):
    anomaly_count = int(labeled["IS_ANOMALY"].sum())
    ranked_rows = labeled.sort_values("ANOMALY_SCORE").head(RANKED_ROW_COUNT)
    recent_rows = labeled.sort_values("DATE").tail(HEATMAP_CELL_COUNT)
    return {
        "total_rows": int(len(labeled)),
        "symbol_count": int(labeled["SYMBOL"].nunique()),
        "anomaly_count": anomaly_count,
        "anomaly_rate_pct": anomaly_count / len(labeled) * 100,
        "contamination": float(contamination),
        "feature_weights": calculate_feature_weights(z_scores, labeled["IS_ANOMALY"]),
        "ranked_rows": [row_to_dictionary(row) for _, row in ranked_rows.iterrows()],
        "heatmap": [row_to_dictionary(row) for _, row in recent_rows.iterrows()],
    }


# Runs the whole detector on a raw NSE frame, from cleaning to the final report.
def run_anomaly_scan(raw_data):
    model_rows = select_model_rows(add_model_features(clean_raw_values(raw_data)))
    flags, scores, contamination, features = score_rows(model_rows)
    z_scores = standardize(features)
    labeled = label_rows(model_rows, flags, scores, z_scores)
    return build_scan_report(labeled, z_scores, contamination)


# Convenience entry point used by the API: csv path in, report out.
def scan_csv_file(csv_path):
    return run_anomaly_scan(load_nse_data(csv_path))