# FILE: financial-market-project/src/fmip_routes.py
# NEW FILE. Place it next to serve_api.py in src/.
# Extra endpoints for the dashboard: /anomaly/scan and /model/importance.
# Put the NSE csv at: financial-market-project/data/nse/nse_cy1_data.csv  (or set FMP_NSE_CSV).

import json
import os

from fastapi import APIRouter, Depends, HTTPException, Query

import anomaly_detector

HERE = os.path.dirname(os.path.abspath(__file__))
NSE_CSV_PATH = os.environ.get("FMP_NSE_CSV", os.path.join(HERE, "../data/nse/nse_cy1_data.csv"))
METRICS_PATH = os.path.join(HERE, "../outputs/price_model_metrics.json")

cached_scan = {}


# Runs the anomaly scan once and reuses it, because the csv does not change between requests.
def get_anomaly_scan(force_refresh):
    if force_refresh or "report" not in cached_scan:
        cached_scan["report"] = anomaly_detector.scan_csv_file(NSE_CSV_PATH)
    return cached_scan["report"]


# Reads the saved XGBoost feature importances and scales them so the top one is 1.0.
def read_top_importances(top_count):
    with open(METRICS_PATH) as metrics_file:
        importances = json.load(metrics_file)["feature_importance_xgb_4h"]
    strongest = max(importances.values()) or 1.0
    ranked = sorted(importances.items(), key=lambda item: item[1], reverse=True)
    return [{"name": name, "weight": value / strongest} for name, value in ranked[:top_count]]


# Builds the router; the API-key check is passed in so this file never imports serve_api.
def build_router(require_key):
    router = APIRouter(tags=["FMIP dashboard"])

    # Returns the Isolation Forest report that feeds the Anomaly page and the dashboard table.
    @router.get("/anomaly/scan", dependencies=[Depends(require_key)])
    def anomaly_scan(refresh: bool = False):
        try:
            return get_anomaly_scan(refresh)
        except FileNotFoundError:
            raise HTTPException(404, f"NSE csv not found at {os.path.abspath(NSE_CSV_PATH)}")
        except ValueError as error:
            raise HTTPException(422, str(error))

    # Returns the top features of the 4h model so the Forecast page shows real importances.
    @router.get("/model/importance", dependencies=[Depends(require_key)])
    def model_importance(top: int = Query(5, ge=1, le=25)):
        if not os.path.exists(METRICS_PATH):
            raise HTTPException(404, "price_model_metrics.json not found - run train_price_model.py")
        return read_top_importances(top)

    return router# FILE: financial-market-project/src/fmip_routes.py
# NEW FILE. Place it next to serve_api.py in src/.
# Extra endpoints for the dashboard: /anomaly/scan and /model/importance.
# Put the NSE csv at: financial-market-project/data/nse/nse_cy1_data.csv  (or set FMP_NSE_CSV).

import json
import os

from fastapi import APIRouter, Depends, HTTPException, Query

import anomaly_detector

HERE = os.path.dirname(os.path.abspath(__file__))
NSE_CSV_PATH = os.environ.get("FMP_NSE_CSV", os.path.join(HERE, "../data/nse/nse_cy1_data.csv"))
METRICS_PATH = os.path.join(HERE, "../outputs/price_model_metrics.json")

cached_scan = {}


# Runs the anomaly scan once and reuses it, because the csv does not change between requests.
def get_anomaly_scan(force_refresh):
    if force_refresh or "report" not in cached_scan:
        cached_scan["report"] = anomaly_detector.scan_csv_file(NSE_CSV_PATH)
    return cached_scan["report"]


# Reads the saved XGBoost feature importances and scales them so the top one is 1.0.
def read_top_importances(top_count):
    with open(METRICS_PATH) as metrics_file:
        importances = json.load(metrics_file)["feature_importance_xgb_4h"]
    strongest = max(importances.values()) or 1.0
    ranked = sorted(importances.items(), key=lambda item: item[1], reverse=True)
    return [{"name": name, "weight": value / strongest} for name, value in ranked[:top_count]]


# Builds the router; the API-key check is passed in so this file never imports serve_api.
def build_router(require_key):
    router = APIRouter(tags=["FMIP dashboard"])

    # Returns the Isolation Forest report that feeds the Anomaly page and the dashboard table.
    @router.get("/anomaly/scan", dependencies=[Depends(require_key)])
    def anomaly_scan(refresh: bool = False):
        try:
            return get_anomaly_scan(refresh)
        except FileNotFoundError:
            raise HTTPException(404, f"NSE csv not found at {os.path.abspath(NSE_CSV_PATH)}")
        except ValueError as error:
            raise HTTPException(422, str(error))

    # Returns the top features of the 4h model so the Forecast page shows real importances.
    @router.get("/model/importance", dependencies=[Depends(require_key)])
    def model_importance(top: int = Query(5, ge=1, le=25)):
        if not os.path.exists(METRICS_PATH):
            raise HTTPException(404, "price_model_metrics.json not found - run train_price_model.py")
        return read_top_importances(top)

    return router