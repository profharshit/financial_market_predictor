"""
Model bake-off + training for the LIVE price forecaster.

    python3 train_price_model.py

What it does (all chronological, no shuffling anywhere):
  1. Builds stationary features + volatility-scaled targets for 1h / 4h / 24h.
  2. For each horizon, walk-forward validates three candidates on the first
     85% of history (6 expanding folds, with a `horizon`-bar embargo gap):
         vol_only     - no-skill benchmark (zero drift, ATR-scaled bands)
         ridge        - linear mean forecast + residual quantiles
         xgb_quantile - gradient-boosted quantile regression
  3. Picks the winner PER HORIZON with a rule fixed in advance (below).
  4. Scores the winner on the untouched last 15% (holdout).
  5. Backtests trailing-stop strategies (fixed vs adaptive stop, with and
     without the model gate) on the holdout, WITH transaction costs.
  6. Refits the winners on ALL data and saves the deployable artifact.

Selection rule (fixed before looking at results):
  A complex model replaces the vol_only benchmark only if, across the
  walk-forward folds, it has lower mean pinball loss AND wins >= 4 of 6 folds.
  It is allowed to drive BUY/SELL direction only if additionally its median
  forecast beats vol_only's median forecast in >= 4 of 6 folds AND its
  directional accuracy is significantly above the majority-sign rate
  (one-sided binomial p < 0.05, counts divided by the horizon because
  overlapping h-bar targets are not independent).
"""

import json
import sys
import time
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
from scipy.stats import binomtest
from sklearn.model_selection import TimeSeriesSplit

from features import (FEATURE_COLS, HORIZONS, PIP, SIGNAL_HORIZON,
                       build_features, load_ohlcv, make_targets)
from indicators import atr_trailing_stop, atr_trailing_stop_adaptive
from price_models import CANDIDATES, QUANTILES

INPUT_CSV = "../data/raw/eurusd_ohlcv.csv"
MODEL_OUT = "../models/eurusd_price_model.joblib"
METRICS_OUT = "../outputs/price_model_metrics.json"

HOLDOUT_FRAC = 0.15
N_FOLDS = 6
MIN_FOLD_WINS = 4
CONF_PERCENTILE = 70          # "confident" = top 30% of |median forecast|
COST_PIPS = 1.0               # per unit of position change (spread + slippage)
BARS_PER_YEAR = 24 * 5 * 52


# ---------------------------------------------------------------- metrics ---
def pinball(y, qpred):
    """Per-quantile pinball loss, shape (3,)."""
    q = np.asarray(QUANTILES)
    e = y[:, None] - qpred
    return np.maximum(q * e, (q - 1) * e).mean(axis=0)


def score(y, qpred, sigma_scale):
    """All reporting metrics for one (y, predictions) pair. sigma_scale = sigma*sqrt(h)."""
    pb = pinball(y, qpred)
    p50 = qpred[:, 1]
    cover = float(((y >= qpred[:, 0]) & (y <= qpred[:, 2])).mean())
    # return-space MAE vs the "no change" forecast
    mae_model = float(np.mean(np.abs((y - p50) * sigma_scale)))
    mae_naive = float(np.mean(np.abs(y * sigma_scale)))
    called = (p50 != 0) & (y != 0)
    dir_acc = float((np.sign(p50[called]) == np.sign(y[called])).mean()) if called.any() else float("nan")
    return {
        "n": int(len(y)),
        "pinball_mean": float(pb.mean()),
        "pinball_q10": float(pb[0]), "pinball_q50": float(pb[1]), "pinball_q90": float(pb[2]),
        "coverage_10_90": cover,
        "mae_ratio_vs_naive": mae_model / mae_naive,
        "dir_acc": dir_acc,
    }


def dir_test(y, p50, h=1):
    """
    Directional accuracy vs the majority-sign rate, one-sided binomial test.
    Targets for horizon h overlap (bar t and t+1 share h-1 of their h hours),
    so the h-bar observations are NOT independent. Counts are divided by h
    (effective sample size) - without this the p-value is overstated.
    """
    called = (p50 != 0) & (y != 0)
    y, p50 = y[called], p50[called]
    n = len(y)
    if n == 0:
        return {"dir_acc": float("nan"), "majority_rate": float("nan"), "p_value": 1.0, "n_eff": 0}
    correct = int((np.sign(p50) == np.sign(y)).sum())
    p0 = max((y > 0).mean(), (y < 0).mean())
    n_eff = max(int(round(n / h)), 1)
    k_eff = int(round(correct * n_eff / n))
    return {"dir_acc": correct / n, "majority_rate": float(p0), "n_eff": n_eff,
            "p_value": float(binomtest(k_eff, n_eff, p0, alternative="greater").pvalue)}


# -------------------------------------------------------------- walk-forward
def walk_forward(name, X, y, scale, h):
    splitter = TimeSeriesSplit(n_splits=N_FOLDS, gap=h)
    folds, oos_idx, oos_pred = [], [], []
    for k, (tr, te) in enumerate(splitter.split(X), 1):
        m = CANDIDATES[name]().fit(X.iloc[tr], y[tr])
        pred = m.predict(X.iloc[te])
        s = score(y[te], pred, scale[te]); s["fold"] = k
        folds.append(s); oos_idx.append(te); oos_pred.append(pred)
    return folds, np.concatenate(oos_idx), np.vstack(oos_pred)


def summarize(folds):
    pb = np.array([f["pinball_mean"] for f in folds])
    p50 = np.array([f["pinball_q50"] for f in folds])
    return {"pinball_mean": float(pb.mean()), "pinball_std": float(pb.std()),
            "pinball_q50_mean": float(p50.mean()),
            "coverage_mean": float(np.mean([f["coverage_10_90"] for f in folds])),
            "mae_ratio_mean": float(np.mean([f["mae_ratio_vs_naive"] for f in folds])),
            "dir_acc_mean": float(np.mean([f["dir_acc"] for f in folds]))}


# ----------------------------------------------------------------- backtest
def perf(pos, ret, close):
    """pos[t] is decided at close t and earns ret[t+1]. Costs on position changes."""
    pos = np.asarray(pos, float)
    gross = pos[:-1] * ret[1:]
    turnover = np.abs(np.diff(pos, prepend=0.0))[:-1]
    cost = turnover * COST_PIPS * PIP / close[:-1]
    net = gross - cost
    eq = np.cumsum(net)
    dd = float((np.exp(eq) / np.maximum.accumulate(np.exp(eq)) - 1).min())
    years = len(net) / BARS_PER_YEAR
    sd = net.std()
    sharpe = float(net.mean() / sd * np.sqrt(BARS_PER_YEAR)) if sd > 0 else 0.0
    return {"total_return_pct": float((np.exp(eq[-1]) - 1) * 100),
            "sharpe": sharpe, "sharpe_se": float(np.sqrt(1 / years)),
            "max_drawdown_pct": dd * 100, "position_changes": int((turnover > 0).sum()),
            "time_in_market_pct": float((pos != 0).mean() * 100),
            "gross_return_pct": float((np.exp(gross.sum()) - 1) * 100),
            "bars": int(len(net))}


def trend_from(close, stop):
    return np.sign(close - stop)


# --------------------------------------------------------------------- main
def main():
    t0 = time.time()
    ohlcv = load_ohlcv(INPUT_CSV)
    feat = build_features(ohlcv)
    tgt = make_targets(feat)
    ok = feat[FEATURE_COLS].notna().all(axis=1)
    F, T = feat[ok], tgt[ok]
    n = len(F)
    n_dev = int(n * (1 - HOLDOUT_FRAC))
    print(f"Data: {n} usable hourly bars  {F.index[0]} -> {F.index[-1]}")
    print(f"Dev (walk-forward) = first {n_dev} | Holdout = last {n - n_dev} "
          f"({F.index[n_dev]} -> {F.index[-1]})\n")

    results = {"data": {"rows": n, "start": str(F.index[0]), "end": str(F.index[-1]),
                        "holdout_start": str(F.index[n_dev]), "n_dev": n_dev,
                        "n_holdout": n - n_dev},
               "config": {"horizons": list(HORIZONS), "n_folds": N_FOLDS,
                          "min_fold_wins": MIN_FOLD_WINS, "cost_pips": COST_PIPS,
                          "quantiles": list(QUANTILES)},
               "horizons": {}}
    winners, hold_preds, conf_thr, dir_skill, oos_signal = {}, {}, {}, {}, None

    for h in HORIZONS:
        z = T[f"z_{h}"].to_numpy()
        scale = (F["sigma"].to_numpy() * np.sqrt(h))
        lab = ~np.isnan(z)
        dev_i = np.where(lab[:n_dev])[0]
        hold_i = np.arange(n_dev, n)[lab[n_dev:]]
        Xd, yd, sd = F.iloc[dev_i][FEATURE_COLS], z[dev_i], scale[dev_i]

        print(f"=== horizon {h}h ===")
        wf, oos = {}, {}
        for name in CANDIDATES:
            folds, idx, pred = walk_forward(name, Xd, yd, sd, h)
            wf[name] = {"folds": folds, "summary": summarize(folds)}
            oos[name] = (idx, pred)
            s = wf[name]["summary"]
            print(f"  {name:<13} pinball {s['pinball_mean']:.4f} (+/-{s['pinball_std']:.4f})  "
                  f"cover {s['coverage_mean']:.3f}  MAE/naive {s['mae_ratio_mean']:.4f}  "
                  f"dir {s['dir_acc_mean']:.3f}")

        # ---- selection (rule fixed in the module docstring)
        base = wf["vol_only"]
        best_name, best_gain = None, 0.0
        for name in ("ridge", "xgb_quantile"):
            g = 1 - wf[name]["summary"]["pinball_mean"] / base["summary"]["pinball_mean"]
            wins = sum(a["pinball_mean"] < b["pinball_mean"]
                       for a, b in zip(wf[name]["folds"], base["folds"]))
            wf[name]["gain_vs_vol_only"], wf[name]["fold_wins"] = float(g), int(wins)
            if g > best_gain and wins >= MIN_FOLD_WINS:
                best_name, best_gain = name, g
        winner = best_name or "vol_only"

        # ---- directional skill of the winner (median forecast)
        skill, dtest = False, None
        if winner != "vol_only":
            w = wf[winner]
            med_wins = sum(a["pinball_q50"] < b["pinball_q50"]
                           for a, b in zip(w["folds"], base["folds"]))
            idx, pred = oos[winner]
            dtest = dir_test(yd[idx], pred[:, 1], h)
            skill = bool(med_wins >= MIN_FOLD_WINS and dtest["p_value"] < 0.05)
            dtest["median_fold_wins"] = int(med_wins)
        print(f"  -> winner: {winner}"
              + (f"  (pinball gain {best_gain*100:.2f}%, folds won {wf[winner]['fold_wins']}/{N_FOLDS}, "
                 f"directional skill: {skill}, overlap-adj dir p={dtest['p_value']:.3f})" if winner != 'vol_only' else
                 "  (no complex model beat the benchmark consistently)"))

        # ---- holdout: fit ALL candidates on dev (embargo h rows), score untouched holdout
        emb = dev_i[:-h] if h else dev_i
        hold = {}
        hp = {}
        for name in CANDIDATES:
            m = CANDIDATES[name]().fit(F.iloc[emb][FEATURE_COLS], z[emb])
            p = m.predict(F.iloc[hold_i][FEATURE_COLS])
            hold[name] = score(z[hold_i], p, scale[hold_i]); hp[name] = (hold_i, p)
        print("  holdout: " + "  ".join(
            f"{k}: pinball {v['pinball_mean']:.4f} cover {v['coverage_10_90']:.3f} "
            f"MAE/naive {v['mae_ratio_vs_naive']:.4f} dir {v['dir_acc']:.3f}" for k, v in hold.items()))

        # confident-direction check on holdout for the best complex model
        cx = best_name or "xgb_quantile"
        idx_o, pr_o = oos[cx]
        thr = float(np.percentile(np.abs(pr_o[:, 1]), CONF_PERCENTILE))
        ph = hp[cx][1][:, 1]
        conf = np.abs(ph) >= thr
        conf_dir = None
        if conf.sum() > 0:
            conf_dir = {"n": int(conf.sum()), **dir_test(z[hold_i][conf], ph[conf], h)}
            print(f"  holdout confident-direction ({cx}, |p50|>={thr:.3f}): n={conf_dir['n']} "
                  f"acc={conf_dir['dir_acc']:.3f} vs majority {conf_dir['majority_rate']:.3f} "
                  f"overlap-adj p={conf_dir['p_value']:.3f}")

        winners[h], conf_thr[h], dir_skill[h] = winner, thr, skill
        hold_preds[h] = {k: v[1] for k, v in hp.items()}
        if h == SIGNAL_HORIZON:
            hold_preds["idx"] = hold_i
        results["horizons"][str(h)] = {
            "walk_forward": {k: {"summary": v["summary"], "folds": v["folds"],
                                 **{a: v[a] for a in ("gain_vs_vol_only", "fold_wins") if a in v}}
                             for k, v in wf.items()},
            "winner": winner, "directional_skill": skill, "direction_test": dtest,
            "holdout": hold, "holdout_confident_direction": conf_dir,
            "conf_threshold_abs_p50_z": thr,
        }
        print()

    # ------------------------------------------------ feature importance (xgb, 4h)
    z4 = T[f"z_{SIGNAL_HORIZON}"].to_numpy()
    d4 = np.where(~np.isnan(z4[:n_dev]))[0]
    xgb4 = CANDIDATES["xgb_quantile"]().fit(F.iloc[d4][FEATURE_COLS], z4[d4])
    imp = sorted(zip(FEATURE_COLS, xgb4.feature_importances_), key=lambda x: -x[1])
    results["feature_importance_xgb_4h"] = {k: float(v) for k, v in imp}
    print("Top features (xgb, 4h):", ", ".join(f"{k} {v:.3f}" for k, v in imp[:6]), "\n")

    # ----------------------------------------------------------- backtest
    hi = hold_preds["idx"]
    close_all = ohlcv["close"].reindex(F.index).to_numpy()
    lr = np.log(close_all[1:] / close_all[:-1])
    ret = np.r_[np.nan, lr]                                   # ret[t] = return INTO bar t

    fixed = atr_trailing_stop(ohlcv)["atr_trailing_stop"].reindex(F.index).to_numpy()
    adapt = atr_trailing_stop_adaptive(ohlcv)["atr_trailing_stop"].reindex(F.index).to_numpy()
    tr_fixed, tr_adapt = trend_from(close_all, fixed), trend_from(close_all, adapt)

    def run(pos_full, sl):
        return perf(pos_full[sl], ret[sl], close_all[sl])

    full_sl = slice(1, n)
    hold_sl = slice(hi[0], n)
    bt = {"cost_pips_per_position_change": COST_PIPS, "full_sample": {}, "holdout": {}}
    bh = np.ones(n)
    for label, sl in (("full_sample", full_sl), ("holdout", hold_sl)):
        bt[label]["buy_and_hold"] = run(bh, sl)
        bt[label]["stop_fixed_pine"] = run(tr_fixed, sl)
        bt[label]["stop_adaptive"] = run(tr_adapt, sl)

    # model-gated variant on holdout (uses the signal-horizon forecasts)
    cx = winners[SIGNAL_HORIZON] if winners[SIGNAL_HORIZON] != "vol_only" else "xgb_quantile"
    p50 = np.full(n, np.nan)
    p50[hi] = hold_preds[SIGNAL_HORIZON][cx][:, 1]
    thr = results["horizons"][str(SIGNAL_HORIZON)]["conf_threshold_abs_p50_z"]
    bias = np.where(p50 >= thr, 1, np.where(p50 <= -thr, -1, 0))
    gated = np.where(bias == tr_adapt, tr_adapt, 0)
    bt["holdout"][f"stop_adaptive_gated_by_{cx}"] = run(np.nan_to_num(gated), hold_sl)

    print(f"Backtest (cost {COST_PIPS} pip per position change; Sharpe s.e. ~ +/-x):")
    for label in ("full_sample", "holdout"):
        print(f"  [{label}]")
        for k, v in bt[label].items():
            print(f"    {k:<32} ret {v['total_return_pct']:+7.2f}%  gross {v['gross_return_pct']:+7.2f}%  "
                  f"Sharpe {v['sharpe']:+5.2f} (+/-{v['sharpe_se']:.2f})  maxDD {v['max_drawdown_pct']:6.2f}%  "
                  f"changes {v['position_changes']:>4}  in-mkt {v['time_in_market_pct']:.0f}%")
    results["backtest"] = bt

    # A trading edge is "verified" only if a strategy beats zero NET of costs by > 2 s.e. on holdout
    edge_verified = any(v["total_return_pct"] > 0 and v["sharpe"] > 2 * v["sharpe_se"]
                        for k, v in bt["holdout"].items() if k != "buy_and_hold")
    bt["edge_verified"] = bool(edge_verified)
    print(f"\nTrading edge verified net of costs on holdout: {edge_verified}")

    # -------------------------------------------------------- final refit + save
    print("\nRefitting winners on ALL data for deployment ...")
    final = {}
    for h in HORIZONS:
        z = T[f"z_{h}"].to_numpy()
        lab = np.where(~np.isnan(z))[0]
        final[h] = CANDIDATES[winners[h]]().fit(F.iloc[lab][FEATURE_COLS], z[lab])
    version = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M") + "-" + "+".join(winners[h] for h in HORIZONS)
    artifact = {
        "version": version, "trained_at": datetime.now(timezone.utc).isoformat(),
        "models": final, "kinds": winners, "feature_cols": FEATURE_COLS,
        "horizons": list(HORIZONS), "quantiles": list(QUANTILES),
        "signal_horizon": SIGNAL_HORIZON,
        "directional_skill": dir_skill, "conf_threshold": conf_thr,
        "edge_verified": bool(edge_verified),
        "data_range": [str(F.index[0]), str(F.index[-1])], "n_rows": n,
    }
    joblib.dump(artifact, MODEL_OUT)
    results["artifact"] = {"version": version, "kinds": winners,
                           "directional_skill": {str(k): v for k, v in dir_skill.items()},
                           "edge_verified": bool(edge_verified)}
    with open(METRICS_OUT, "w") as f:
        json.dump(results, f, indent=2, default=float)
    print(f"Model   -> {MODEL_OUT}  (version {version})")
    print(f"Metrics -> {METRICS_OUT}   [{time.time()-t0:.0f}s]")


if __name__ == "__main__":
    sys.exit(main())
