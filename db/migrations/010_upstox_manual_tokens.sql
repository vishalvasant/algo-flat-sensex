-- Manual Upstox tokens (Settings → Broker paste from UpStox.txt)
ALTER TABLE upstox_credentials
    ADD COLUMN IF NOT EXISTS access_token TEXT,
    ADD COLUMN IF NOT EXISTS analytic_token TEXT;
