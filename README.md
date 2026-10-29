# Algo-Flat Sensex

Intraday **SENSEX** weekly options trading system (paper-first). Independent replica of Algo-Flat, retargeted from NIFTY 50 → SENSEX (BSE cash / BFO options).

This repo has **no runtime link** to `algo-flat`. It uses its own Docker Compose project, database, volumes, host ports, Traefik path, and browser auth token — so both stacks can run on the same server at once.

## Market identity

| Setting | Value |
|---------|-------|
| Underlying | SENSEX |
| Spot exchange | BSE (token `1`) |
| Options exchange | BFO |
| Lot size | **20** |
| Weekly expiry | **Thursday** |
| Strike step | 100 |
| Watch / trade band | **ATM ± 5** CE/PE |

## Quick start (Docker)

```bash
cp .env.example .env
# Edit .env — POSTGRES_PASSWORD, WEB_USERNAME, WEB_PASSWORD, JWT_SECRET, broker creds

docker compose up -d --build
```

Open **http://localhost:8081**

| Service | Internal | Host port (vs NIFTY) |
|---------|----------|----------------------|
| web-app | 8080 | **8081** (NIFTY: 8080) |
| trading-engine | 8001 | **8002** (NIFTY: 8001) |
| postgres | 5432 | **5433** (NIFTY: 5432) |

Compose project name: `algo-flat-sensex` (volumes/network isolated from `algo-flat`).

### Server / Traefik

```bash
./scripts/deploy.sh prod
# or: docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.proxy.yml up -d --build
```

Public path: **`/algo-flat-sensex`** (NIFTY uses `/algo-flat`).

## Broker

Default: `config/broker_config.yaml` → `provider: flattrade` (or Upstox).

OAuth callback in `.env.example` uses port **8010** so it does not clash with the NIFTY stack callback.

```bash
docker compose exec trading-engine python scripts/flattrade_login.py
# or
docker compose exec trading-engine python scripts/upstox_login.py
```

## Useful commands

```bash
docker compose logs -f trading-engine
docker compose ps
make health   # checks :8081
```

## Config highlights

- `config/symbols_config.yaml` — SENSEX / BSE / BFO / ATM±5 / lot 20
- `config/strategy_config.yaml` — `option_lift.atm_band_steps: 5`
- Weekly calendar weekday for SENSEX = Thursday (`contract_selector/expiry.py`)
- BFO scripmaster: `Bfo_Index_Derivatives.csv`
