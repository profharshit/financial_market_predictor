"""
Sentiment classifier: text -> {positive, negative, neutral}

Baseline: TF-IDF + Logistic Regression (fast, interpretable, good for a
class project baseline). An XGBoost variant is also provided since it
often edges out logistic regression on this kind of feature set.
"""

import os
import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

from config import RANDOM_STATE, TEST_SIZE, OUTPUT_DIR


def build_logreg_pipeline():
    return Pipeline([
        ("tfidf", TfidfVectorizer(max_features=20000, ngram_range=(1, 2), min_df=2)),
        ("clf", LogisticRegression(max_iter=1000, class_weight="balanced",
                                    random_state=RANDOM_STATE)),
    ])


def build_xgb_pipeline():
    # Imported lazily: xgboost is an optional dependency (see requirements.txt).
    # Install it (`pip install xgboost`) if you want to use model_type="xgb".
    from xgboost import XGBClassifier
    return Pipeline([
        ("tfidf", TfidfVectorizer(max_features=20000, ngram_range=(1, 2), min_df=2)),
        ("clf", XGBClassifier(
            n_estimators=300, max_depth=6, learning_rate=0.1,
            subsample=0.9, colsample_bytree=0.9,
            eval_metric="mlogloss", random_state=RANDOM_STATE,
        )),
    ])


def train_sentiment_model(df, text_col="clean_for_classical", label_col="sentiment",
                           model_type="logreg"):
    X = df[text_col]
    y = df[label_col]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=TEST_SIZE, random_state=RANDOM_STATE, stratify=y
    )

    pipeline = build_logreg_pipeline() if model_type == "logreg" else build_xgb_pipeline()

    if model_type == "xgb":
        # XGBClassifier needs numeric labels
        classes = sorted(y.unique())
        label_map = {c: i for i, c in enumerate(classes)}
        y_train_enc = y_train.map(label_map)
        pipeline.fit(X_train, y_train_enc)
        y_pred_enc = pipeline.predict(X_test)
        inv_map = {v: k for k, v in label_map.items()}
        y_pred = np.array([inv_map[i] for i in y_pred_enc])
    else:
        pipeline.fit(X_train, y_train)
        y_pred = pipeline.predict(X_test)

    return pipeline, X_test, y_test, y_pred


def save_model(pipeline, name="sentiment_model.joblib"):
    path = os.path.join(OUTPUT_DIR, name)
    joblib.dump(pipeline, path)
    print(f"Saved model to {path}")
    return path
