"""
Evaluation for both models: metrics, confusion matrices, and a limitations
summary suitable for the "Model Test Results and Limitations" section of
the group report.
"""

import os
import json
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.metrics import (
    classification_report, confusion_matrix, f1_score,
    precision_score, recall_score, roc_auc_score, accuracy_score,
)

from config import OUTPUT_DIR


def evaluate_classification(y_true, y_pred, labels=None, y_proba=None, name="model"):
    report = classification_report(y_true, y_pred, labels=labels, output_dict=True, zero_division=0)
    metrics = {
        "accuracy": accuracy_score(y_true, y_pred),
        "precision_macro": precision_score(y_true, y_pred, average="macro", zero_division=0),
        "recall_macro": recall_score(y_true, y_pred, average="macro", zero_division=0),
        "f1_macro": f1_score(y_true, y_pred, average="macro", zero_division=0),
    }
    if y_proba is not None and labels is not None and len(labels) == 2:
        try:
            metrics["roc_auc"] = roc_auc_score(y_true, y_proba)
        except ValueError:
            pass  # e.g. only one class present in a small synthetic test split

    print(f"\n=== {name} — classification report ===")
    print(classification_report(y_true, y_pred, labels=labels, zero_division=0))
    print(f"Summary metrics: {json.dumps(metrics, indent=2)}")

    with open(os.path.join(OUTPUT_DIR, f"{name}_report.json"), "w") as f:
        json.dump({"metrics": metrics, "per_class_report": report}, f, indent=2)

    return metrics, report


def plot_confusion_matrix(y_true, y_pred, labels, name="model"):
    cm = confusion_matrix(y_true, y_pred, labels=labels)
    plt.figure(figsize=(5, 4))
    sns.heatmap(cm, annot=True, fmt="d", cmap="Blues", xticklabels=labels, yticklabels=labels)
    plt.xlabel("Predicted")
    plt.ylabel("Actual")
    plt.title(f"Confusion Matrix — {name}")
    plt.tight_layout()
    out_path = os.path.join(OUTPUT_DIR, f"{name}_confusion_matrix.png")
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"Saved confusion matrix to {out_path}")
    return out_path


LIMITATIONS = {
    "sentiment_model": [
        "Trained primarily on English-language text; performance on other languages "
        "or heavy crypto slang/emoji-only posts is untested.",
        "TF-IDF + Logistic Regression baseline ignores word order and context, so "
        "sarcasm or negation ('not bullish at all') can be misclassified.",
        "Neutral class is often the hardest to separate from mild positive/negative "
        "sentiment; expect more confusion there than in the extremes.",
        "Sentiment labels reflect the training data's time period; sentiment "
        "expression evolves quickly in crypto communities (new slang, memes).",
    ],
    "pump_dump_model": [
        "Severe class imbalance: real pump events are a small fraction of all "
        "messages, so recall on the minority class is the metric to watch, not "
        "raw accuracy.",
        "Behavioral features (posting velocity, near-duplicate bursts) assume "
        "group-level timestamps are accurate and messages are attributable to a "
        "group; this may not transfer directly to platforms without clear group "
        "structure (e.g. Twitter/X).",
        "The model is trained on known historical pump phrasing; sophisticated "
        "manipulators can adapt language to evade keyword/embedding-based detection "
        "(an arms-race limitation common to this literature).",
        "Near-duplicate detection uses TF-IDF cosine similarity within a group, "
        "which can miss paraphrased (but semantically identical) coordinated posts.",
        "Synthetic data (used when real datasets aren't loaded) simplifies real-world "
        "noise; results on real Telegram/Binance data should be reported separately "
        "from any synthetic sanity-check runs.",
    ],
}


def print_limitations(model_key):
    print(f"\n=== Known limitations — {model_key} ===")
    for i, item in enumerate(LIMITATIONS.get(model_key, []), 1):
        print(f"{i}. {item}")
