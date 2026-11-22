"""
End-to-end pipeline for the Fake-Sentiment / Pump-and-Dump Classifier module.

Run:
    cd src
    python main.py

This will (using real data if present in ../data, else synthetic sample data):
  1. Train + evaluate the sentiment classifier (positive/negative/neutral)
  2. Train + evaluate the pump-and-dump classifier (text + behavioral features)
  3. Save models, metrics (JSON), and confusion matrix plots to ../outputs
  4. Print a limitations summary for the report
"""

from data_loader import load_sentiment_data, load_pump_dump_data
from preprocessing import add_clean_columns
from sentiment_model import train_sentiment_model, save_model
from pump_dump_model import train_pump_dump_model, save_artifacts, top_feature_importances
from evaluate import evaluate_classification, plot_confusion_matrix, print_limitations
from features import BEHAVIORAL_FEATURE_COLS


def run_sentiment_pipeline():
    print("\n" + "=" * 60)
    print("SENTIMENT CLASSIFIER")
    print("=" * 60)

    df = load_sentiment_data()
    df = add_clean_columns(df)

    pipeline, X_test, y_test, y_pred = train_sentiment_model(df, model_type="logreg")
    labels = sorted(df["sentiment"].unique())

    evaluate_classification(y_test, y_pred, labels=labels, name="sentiment_model")
    plot_confusion_matrix(y_test, y_pred, labels=labels, name="sentiment_model")
    save_model(pipeline)
    print_limitations("sentiment_model")


def run_pump_dump_pipeline():
    print("\n" + "=" * 60)
    print("PUMP-AND-DUMP / FAKE-HYPE CLASSIFIER")
    print("=" * 60)

    df = load_pump_dump_data()
    df = add_clean_columns(df)

    result = train_pump_dump_model(df)

    evaluate_classification(
        result["y_test"], result["y_pred"], labels=[0, 1],
        y_proba=result["y_proba"], name="pump_dump_model",
    )
    plot_confusion_matrix(result["y_test"], result["y_pred"], labels=[0, 1], name="pump_dump_model")
    save_artifacts(result)

    print("\nTop feature importances (text terms + behavioral signals):")
    for feat, score in top_feature_importances(result, BEHAVIORAL_FEATURE_COLS):
        print(f"  {feat:<25} {score:.4f}")

    print_limitations("pump_dump_model")


if __name__ == "__main__":
    run_sentiment_pipeline()
    run_pump_dump_pipeline()
    print("\nDone. See the outputs/ folder for saved models, metrics, and plots.")
