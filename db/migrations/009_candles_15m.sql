-- Materialized 15m candles (aggregated from 5m, IST-aligned buckets).
CREATE TABLE IF NOT EXISTS candles_15m (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_token TEXT NOT NULL,
    ts               TIMESTAMPTZ NOT NULL,
    open             NUMERIC(12,4) NOT NULL,
    high             NUMERIC(12,4) NOT NULL,
    low              NUMERIC(12,4) NOT NULL,
    close            NUMERIC(12,4) NOT NULL,
    volume           BIGINT,
    UNIQUE (instrument_token, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_15m_token_ts ON candles_15m (instrument_token, ts DESC);
