"""
Prediction log + forward-test scorer (SQLite reference implementation).

The DB teammates can port schema.sql to Postgres/MySQL; the columns are the
contract. Every forecast row is later filled with the REALISED price once the
target bar arrives - that is what turns the API into a live, honest test of
the model on upcoming data.
"""

import sqlite3
import threading
from datetime import datetime, timezone

import numpy as np

DDL = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,
    bar_ts          TEXT    NOT NULL,          -- ISO-8601 UTC: bar the prediction was made at the CLOSE of
    last_close      REAL    NOT NULL,
    stop_level      REAL    NOT NULL,
    stop_trend      TEXT    NOT NULL,          -- up | down | flat
    stop_flip       TEXT,                      -- buy | sell | NULL
    stop_distance_pips REAL,
    atr_pips        REAL,
    key_multiplier  REAL,
    signal_action   TEXT    NOT NULL,          -- BUY | SELL | HOLD
    signal_basis    TEXT    NOT NULL,          -- stop_only | stop+model
    model_bias      TEXT,                      -- up | down | neutral
    edge_verified   INTEGER NOT NULL DEFAULT 0,
    model_version   TEXT    NOT NULL,
    mode            TEXT    NOT NULL,          -- live | replay
    warnings        TEXT,
    created_at      TEXT    NOT NULL,
    UNIQUE (symbol, timeframe, bar_ts, model_version)
);
CREATE TABLE IF NOT EXISTS forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER NOT NULL REFERENCES signals(id),
    horizon_bars    INTEGER NOT NULL,
    target_ts_est   TEXT,
    price_p10       REAL    NOT NULL,
    price_p50       REAL    NOT NULL,
    price_p90       REAL    NOT NULL,
    model_kind      TEXT,
    realised_price  REAL,                      -- filled in when the target bar arrives
    realised_bar_ts TEXT,
    resolved_at     TEXT,
    UNIQUE (signal_id, horizon_bars)
);
CREATE INDEX IF NOT EXISTS idx_forecasts_pending ON forecasts (realised_price) WHERE realised_price IS NULL;
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (symbol, timeframe, bar_ts);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(DDL)
            self.conn.commit()

    # ------------------------------------------------------------------ write
    def log(self, results: list[dict], mode: str) -> None:
        with self.lock:
            for r in results:
                ts, s, sig = r["trailing_stop"], None, r["signal"]
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO signals
                       (symbol,timeframe,bar_ts,last_close,stop_level,stop_trend,stop_flip,
                        stop_distance_pips,atr_pips,key_multiplier,signal_action,signal_basis,
                        model_bias,edge_verified,model_version,mode,warnings,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["symbol"], r["timeframe"], r["as_of"], r["last_close"], ts["level"], ts["trend"],
                     ts["flip"], ts["distance_pips"], ts["atr_pips"], ts["key_multiplier"],
                     sig["action"], sig["basis"], sig["model_bias"], int(sig["edge_verified"]),
                     r["model_version"], mode, ",".join(r["warnings"]) or None, _now()))
                if cur.rowcount == 0:
                    continue
                sid = cur.lastrowid
                for f in r["forecasts"]:
                    self.conn.execute(
                        """INSERT OR IGNORE INTO forecasts
                           (signal_id,horizon_bars,target_ts_est,price_p10,price_p50,price_p90,model_kind)
                           VALUES (?,?,?,?,?,?,?)""",
                        (sid, f["horizon_bars"], f["target_ts_est"], f["price_p10"], f["price_p50"],
                         f["price_p90"], f["model"]))
            self.conn.commit()

    def pending(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                """SELECT f.id, f.horizon_bars, s.bar_ts FROM forecasts f
                   JOIN signals s ON s.id = f.signal_id WHERE f.realised_price IS NULL""")]

    def resolve(self, forecast_id: int, price: float, bar_ts: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE forecasts SET realised_price=?, realised_bar_ts=?, resolved_at=? WHERE id=?",
                (price, bar_ts, _now(), forecast_id))
            self.conn.commit()

    # ------------------------------------------------------------------- read
    def history(self, limit: int = 100, mode: str | None = None):
        q = ("SELECT s.*, f.horizon_bars, f.price_p10, f.price_p50, f.price_p90, f.target_ts_est, "
             "f.realised_price, f.realised_bar_ts FROM signals s JOIN forecasts f ON f.signal_id=s.id ")
        args: list = []
        if mode:
            q += "WHERE s.mode=? "; args.append(mode)
        q += "ORDER BY s.bar_ts DESC, f.horizon_bars ASC LIMIT ?"
        args.append(limit)
        with self.lock:
            return [dict(r) for r in self.conn.execute(q, args)]

    def forward_stats(self, mode: str | None = None) -> dict:
        q = ("SELECT s.last_close, f.horizon_bars, f.price_p10, f.price_p50, f.price_p90, f.realised_price "
             "FROM forecasts f JOIN signals s ON s.id=f.signal_id WHERE f.realised_price IS NOT NULL")
        args: list = []
        if mode:
            q += " AND s.mode=?"; args.append(mode)
        with self.lock:
            rows = [dict(r) for r in self.conn.execute(q, args)]
            pend = self.conn.execute("SELECT COUNT(*) c FROM forecasts WHERE realised_price IS NULL").fetchone()["c"]
        out = {"pending_unresolved": pend, "horizons": {}}
        for h in sorted({r["horizon_bars"] for r in rows}):
            rs = [r for r in rows if r["horizon_bars"] == h]
            c = np.array([r["last_close"] for r in rs]); real = np.array([r["realised_price"] for r in rs])
            p10 = np.array([r["price_p10"] for r in rs]); p50 = np.array([r["price_p50"] for r in rs])
            p90 = np.array([r["price_p90"] for r in rs])
            moved = real != c
            call = (p50 != c) & moved
            out["horizons"][str(h)] = {
                "n_resolved": len(rs),
                "band_coverage_p10_p90": float(((real >= p10) & (real <= p90)).mean()),   # target 0.80
                "mae_pips_model": float(np.abs(real - p50).mean() * 1e4),
                "mae_pips_naive_no_change": float(np.abs(real - c).mean() * 1e4),
                "directional_accuracy": float((np.sign(p50 - c)[call] == np.sign(real - c)[call]).mean())
                                        if call.any() else None,
                "mean_band_width_pips": float((p90 - p10).mean() * 1e4),
            }
        return out
