-- Upstox API credentials (Settings → Broker)
CREATE TABLE IF NOT EXISTS upstox_credentials (
    id INTEGER PRIMARY KEY,
    api_key TEXT NOT NULL,
    api_secret TEXT NOT NULL,
    redirect_url TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
