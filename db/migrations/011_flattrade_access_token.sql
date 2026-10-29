-- Persist Flattrade session token in DB (mirrors Upstox access_token pattern).
-- File cache (.flattrade/session.json) remains primary for engine; DB is backup + UI status.
ALTER TABLE flattrade_credentials
    ADD COLUMN IF NOT EXISTS access_token TEXT,
    ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMPTZ;
