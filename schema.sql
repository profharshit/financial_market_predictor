-- EUR/USD forecast API - reference schema (SQLite dialect; Postgres port: INTEGER PRIMARY KEY AUTOINCREMENT -> BIGSERIAL,
-- TEXT timestamps -> TIMESTAMPTZ, REAL -> DOUBLE PRECISION, partial index syntax is identical).
-- Kept in sync with store.py::DDL (the API creates these tables itself on first start).

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

-- ---------------------------------------------------------------------------
-- OPTIONAL (recommended for the DB teammates): persist raw bars so the API can
-- warm up from the database instead of the CSV. Not created by store.py.
-- ---------------------------------------------------------------------------
-- CREATE TABLE IF NOT EXISTS bars (
--     symbol TEXT NOT NULL, timeframe TEXT NOT NULL, ts TEXT NOT NULL,   -- bar OPEN time, UTC
--     open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
--     PRIMARY KEY (symbol, timeframe, ts)
-- );
