"""
Train + test the EUR/USD direction predictor (the "Predictor" block of the
pipeline).

Run:  python3 train_model.py

Pipeline position:
    data/processed/eurusd_features.csv  ->  train_model.py  ->  models/*.joblib
                                                            ->  outputs/*.json

Three things happen here, in order:
  1. Chronological train/val/test split (NEVER random - see DOCUMENTATION.md)
  2. Walk-forward validation on the training portion (more honest than a
     single split for time series)
  3. Final fit + evaluation on the untouched held-out test set

Everything is compared against a majority-class baseline. On liquid FX,
next-bar direction is close to a coin flip, so a model that does not clearly
beat that baseline has not learned anything useful - the script says so
explicitly rather than quietly reporting a number near 50%.
"""

import json
import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    roc_auc_score, confusion_matrix, classification_report,
)
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier

INPUT_CSV = "../data/processed/eurusd_features.csv"
MODEL_OUT = "../models/eurusd_direction_model.joblib"
METRICS_OUT = "../outputs/training_metrics.json"

TRAIN_RATIO = 0.70
VAL_RATIO = 0.15          # remainder (~0.15) becomes the test set
N_WALK_FORWARD_FOLDS = 5

# Columns that are labels or metadata, never model inputs.
NON_FEATURE_COLS = ["timestamp", "target_return", "target_direction"]

# Columns that leak the answer or are non-stationary. See DOCUMENTATION.md
# section "Leakage and non-stationarity" for why each one is dropped.
LEAKY_OR_NONSTATIONARY = [
    "open", "high", "low", "close",          # raw price level, not stationary
    "bb_upper", "bb_lower", "bb_mid",         # price-level derived
    "sma_10", "sma_50",                       # price-level derived
    "atr_trailing_stop",                      # price-level derived
]

MODEL_PARAMS = dict(
    n_estimators=300,
    max_depth=3,
    learning_rate=0.03,
    subsample=0.8,
    colsample_bytree=0.8,
    min_child_weight=5,
    reg_lambda=2.0,
    eval_metric="logloss",
    random_state=42,
)


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------

def load_dataset(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def select_features(df: pd.DataFrame, drop_nonstationary: bool = True) -> list[str]:
    cols = [c for c in df.columns if c not in NON_FEATURE_COLS]
    if drop_nonstationary:
        cols = [c for c in cols if c not in LEAKY_OR_NONSTATIONARY]
    return cols


def chronological_split(df: pd.DataFrame):
    """Split a time-ordered frame into train/val/test WITHOUT shuffling."""
    n = len(df)
    train_end = int(n * TRAIN_RATIO)
    val_end = int(n * (TRAIN_RATIO + VAL_RATIO))
    return df.iloc[:train_end], df.iloc[train_end:val_end], df.iloc[val_end:]


# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------

def evaluate(model, X, y, split_name: str, verbose: bool = True) -> dict:
    preds = model.predict(X)
    try:
        proba = model.predict_proba(X)[:, 1]
        auc = roc_auc_score(y, proba) if y.nunique() > 1 else float("nan")
    except Exception:
        auc = float("nan")

    baseline = max(y.mean(), 1 - y.mean())
    metrics = {
        "split": split_name,
        "n_samples": int(len(y)),
        "accuracy": accuracy_score(y, preds),
        "precision": precision_score(y, preds, zero_division=0),
        "recall": recall_score(y, preds, zero_division=0),
        "f1": f1_score(y, preds, zero_division=0),
        "roc_auc": auc,
        "majority_class_baseline": baseline,
        "edge_over_baseline": accuracy_score(y, preds) - baseline,
    }

    if verbose:
        print(f"\n--- {split_name} ---")
        print(classification_report(y, preds, zero_division=0))
        print("Confusion matrix:\n", confusion_matrix(y, preds))
        print(f"ROC-AUC: {auc:.4f}   (0.50 = no skill)")
        print(f"Accuracy {metrics['accuracy']:.4f} vs baseline {baseline:.4f} "
              f"-> edge {metrics['edge_over_baseline']:+.4f}")
    return metrics


def walk_forward_validation(X: pd.DataFrame, y: pd.Series, n_folds: int) -> dict:
    """
    Expanding-window validation: fold k trains on everything before it and
    tests on the block immediately after. This is the time-series analogue of
    cross-validation and is far more honest than a single val split, because
    it tests the model across several different market regimes.
    """
    splitter = TimeSeriesSplit(n_splits=n_folds)
    fold_rows = []

    for i, (tr_idx, te_idx) in enumerate(splitter.split(X), start=1):
        model = XGBClassifier(**MODEL_PARAMS)
        model.fit(X.iloc[tr_idx], y.iloc[tr_idx], verbose=False)

        preds = model.predict(X.iloc[te_idx])
        y_te = y.iloc[te_idx]
        acc = accuracy_score(y_te, preds)
        base = max(y_te.mean(), 1 - y_te.mean())

        fold_rows.append({
            "fold": i,
            "train_size": int(len(tr_idx)),
            "test_size": int(len(te_idx)),
            "accuracy": acc,
            "baseline": base,
            "edge": acc - base,
        })
        print(f"  fold {i}: train={len(tr_idx):>5}  test={len(te_idx):>5}  "
              f"acc={acc:.4f}  baseline={base:.4f}  edge={acc - base:+.4f}")

    accs = [r["accuracy"] for r in fold_rows]
    edges = [r["edge"] for r in fold_rows]
    summary = {
        "folds": fold_rows,
        "mean_accuracy": float(np.mean(accs)),
        "std_accuracy": float(np.std(accs)),
        "mean_edge_over_baseline": float(np.mean(edges)),
    }
    print(f"  mean accuracy {summary['mean_accuracy']:.4f} "
          f"(+/- {summary['std_accuracy']:.4f})   "
          f"mean edge {summary['mean_edge_over_baseline']:+.4f}")
    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    df = load_dataset(INPUT_CSV)
    feature_cols = select_features(df)

    print(f"Dataset: {len(df)} rows, {len(feature_cols)} features")
    print(f"Date range: {df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]}")
    print(f"Dropped as non-stationary/leak-prone: {LEAKY_OR_NONSTATIONARY}")

    train_df, val_df, test_df = chronological_split(df)
    print(f"\nChronological split -> train={len(train_df)}  "
          f"val={len(val_df)}  test={len(test_df)}")

    X_train, y_train = train_df[feature_cols], train_df["target_direction"]
    X_val, y_val = val_df[feature_cols], val_df["target_direction"]
    X_test, y_test = test_df[feature_cols], test_df["target_direction"]

    # --- 1. Walk-forward validation on train+val (test stays untouched) ---
    print(f"\nWalk-forward validation ({N_WALK_FORWARD_FOLDS} folds):")
    wf_df = pd.concat([train_df, val_df])
    wf_summary = walk_forward_validation(
        wf_df[feature_cols].reset_index(drop=True),
        wf_df["target_direction"].reset_index(drop=True),
        N_WALK_FORWARD_FOLDS,
    )

    # --- 2. Final fit on train, early-stopping reference on val ---
    model = XGBClassifier(**MODEL_PARAMS)
    model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)

    all_metrics = {
        "dataset": {
            "n_rows": int(len(df)),
            "n_features": len(feature_cols),
            "features": feature_cols,
            "dropped_features": LEAKY_OR_NONSTATIONARY,
            "start": str(df["timestamp"].iloc[0]),
            "end": str(df["timestamp"].iloc[-1]),
        },
        "model_params": {k: v for k, v in MODEL_PARAMS.items()},
        "walk_forward": wf_summary,
        "train": evaluate(model, X_train, y_train, "train"),
        "validation": evaluate(model, X_val, y_val, "validation"),
        "test": evaluate(model, X_test, y_test, "test (held out)"),
    }

    # --- 3. Feature importance ---
    importance = sorted(zip(feature_cols, model.feature_importances_),
                        key=lambda x: x[1], reverse=True)
    all_metrics["feature_importance"] = {n: float(s) for n, s in importance}
    print("\nTop 10 features by gain:")
    for name, score in importance[:10]:
        print(f"  {name:<22} {score:.4f}")

    # --- 4. Persist ---
    joblib.dump({"model": model, "feature_cols": feature_cols}, MODEL_OUT)
    with open(METRICS_OUT, "w") as f:
        json.dump(all_metrics, f, indent=2, default=float)
    print(f"\nModel   -> {MODEL_OUT}")
    print(f"Metrics -> {METRICS_OUT}")

    # --- 5. Honest verdict ---
    test_edge = all_metrics["test"]["edge_over_baseline"]
    train_acc = all_metrics["train"]["accuracy"]
    test_acc = all_metrics["test"]["accuracy"]

    print("\n" + "=" * 62)
    if test_edge <= 0:
        print("VERDICT: the model does NOT beat the majority-class baseline on")
        print("the held-out test set. Report this honestly - it is the expected")
        print("result for next-bar FX direction and is a finding, not a failure.")
        print("See DOCUMENTATION.md -> 'Why accuracy sits near 50%'.")
    elif test_edge < 0.02:
        print(f"VERDICT: marginal edge ({test_edge:+.4f}). Within noise for a")
        print("test set this size - do not present it as predictive skill.")
    else:
        print(f"VERDICT: test edge {test_edge:+.4f} over baseline. Confirm it")
        print("survives walk-forward validation before trusting it.")

    if train_acc - test_acc > 0.15:
        print(f"\nOVERFITTING: train {train_acc:.3f} vs test {test_acc:.3f} "
              f"(gap {train_acc - test_acc:.3f}).")
        print("Reduce max_depth / n_estimators, or cut correlated features.")
    print("=" * 62)
