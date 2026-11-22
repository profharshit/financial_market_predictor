"""
Fake-hype / pump-and-dump classifier: text + behavior -> {0: normal, 1: pump signal}

Combines:
  - TF-IDF text representation (captures hype wording)
  - Behavioral/coordination features from features.py (captures burst/duplicate posting)

Model: Random Forest by default (handles the mixed sparse+dense feature set
well and gives feature importances, which is useful for your limitations
discussion). class_weight="balanced" / scale_pos_weight compensates for the
fact that real pump windows are a small minority of all messages.
"""

import os
import joblib
import numpy as np
import scipy.sparse as sp
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.model_selection import train_test_split

from config import RANDOM_STATE, TEST_SIZE, OUTPUT_DIR
from features import add_lexical_features, add_behavioral_features, build_feature_matrix


def prepare_features(df, text_col="clean_for_classical"):
    df = add_lexical_features(df, text_col=text_col)
    df = add_behavioral_features(df, text_col=text_col)
    return df


def build_combined_matrix(df, tfidf: TfidfVectorizer, fit=False, text_col="clean_for_classical"):
    if fit:
        text_matrix = tfidf.fit_transform(df[text_col])
    else:
        text_matrix = tfidf.transform(df[text_col])
    behavioral_matrix = build_feature_matrix(df)
    return sp.hstack([text_matrix, behavioral_matrix]).tocsr()


def train_pump_dump_model(df, label_col="is_pump_announcement", text_col="clean_for_classical"):
    df = prepare_features(df, text_col=text_col)

    train_df, test_df = train_test_split(
        df, test_size=TEST_SIZE, random_state=RANDOM_STATE, stratify=df[label_col]
    )

    tfidf = TfidfVectorizer(max_features=5000, ngram_range=(1, 2), min_df=1)
    X_train = build_combined_matrix(train_df, tfidf, fit=True, text_col=text_col)
    X_test = build_combined_matrix(test_df, tfidf, fit=False, text_col=text_col)

    y_train = train_df[label_col].to_numpy()
    y_test = test_df[label_col].to_numpy()

    clf = RandomForestClassifier(
        n_estimators=400, max_depth=None, min_samples_leaf=2,
        class_weight="balanced", random_state=RANDOM_STATE, n_jobs=-1,
    )
    clf.fit(X_train, y_train)
    y_pred = clf.predict(X_test)
    y_proba = clf.predict_proba(X_test)[:, 1]

    return {
        "tfidf": tfidf,
        "clf": clf,
        "X_test": X_test,
        "y_test": y_test,
        "y_pred": y_pred,
        "y_proba": y_proba,
        "test_df": test_df,
    }


def top_feature_importances(result, behavioral_cols, top_n=15):
    """Returns the most important features (mix of TF-IDF terms and behavioral signals)."""
    feature_names = list(result["tfidf"].get_feature_names_out()) + behavioral_cols
    importances = result["clf"].feature_importances_
    order = np.argsort(importances)[::-1][:top_n]
    return [(feature_names[i], float(importances[i])) for i in order]


def save_artifacts(result, prefix="pump_dump"):
    joblib.dump(result["tfidf"], os.path.join(OUTPUT_DIR, f"{prefix}_tfidf.joblib"))
    joblib.dump(result["clf"], os.path.join(OUTPUT_DIR, f"{prefix}_model.joblib"))
    print(f"Saved {prefix}_tfidf.joblib and {prefix}_model.joblib to {OUTPUT_DIR}")
