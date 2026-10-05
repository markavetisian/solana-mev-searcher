# Setup

## Requirements

* Python 3.12+, Node 22 (dashboard), PostgreSQL 16
* A paid Solana RPC provider with WebSocket support for live ingestion (public RPC rate limits make it unusable)
* Optional: Anthropic API key (AI analyst), Telegram bot (alerts)

## Local (no Docker)

```bash
cd trading-agent
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env && chmod 600 .env          # fill DATABASE_URL, SOLANA_*_URLS, API tokens
createdb trading_agent
ta migrate                                       # alembic upgrade head (tables + append-only triggers)
ta config-check                                  # prints mode, configuration_version, readiness
ta security-check                                # pre-flight secret/permission review

ta --config config/research.yaml worker --source ws   # Phase 1-2: record + label, no orders
ta api                                                # http://127.0.0.1:8080/docs

cd apps/dashboard && npm ci && npm run build
TA_API_URL=http://127.0.0.1:8080 TA_API_READ_TOKEN=... DASHBOARD_BASIC_AUTH=op:pass npm start   # :3000
```

Without RPC credentials you can still exercise everything end-to-end on a **synthetic** feed (clearly labelled,
never usable as evidence of edge):

```bash
ta --config config/paper.yaml worker --source synthetic
```

## Docker

```bash
cp .env.example .env && chmod 600 .env   # set POSTGRES_PASSWORD, API tokens, DASHBOARD_BASIC_AUTH, RPC URLs
docker compose up -d --build             # postgres, migrate (one-shot), worker (PAPER), api, dashboard
TA_MODE_CONFIG=config/research.yaml docker compose up -d worker   # switch the worker's mode
```

API and dashboard bind to `127.0.0.1` only. Expose them through a TLS reverse proxy with its own
authentication if you need remote access. PostgreSQL is not published to the host.

The Dockerfiles were written for this repository but not built in the authoring environment (no Docker daemon
was available); build them once locally before relying on them.

## Tests

```bash
pytest                                            # unit, integration, failure tests (SQLite)
TA_TEST_POSTGRES_URL=postgresql+asyncpg://...  pytest -m postgres   # migrations + immutability triggers
ruff check src tests
cd apps/dashboard && npx tsc --noEmit && npm run build
```

Regenerating the SDK golden vectors (requires npm access):
`cd scripts && npm i @pump-fun/pump-sdk@2.0.0 @pump-fun/pump-swap-sdk@1.20.0 bn.js @solana/web3.js @solana/spl-token && node gen_pump_golden.js`
then copy `golden.json` to `tests/fixtures/pump_sdk_golden.json`.

## Development phases → commands

| Phase | Mode / command | Orders |
|---|---|---|
| 1 Data infrastructure | `ta --config config/research.yaml worker --source ws` | none |
| 2 Research engine | same + `ta research --db` | none |
| 3 Backtester | `ta backtest --db`, `ta walkforward --db` | none |
| 4 Paper trader | `ta --config config/paper.yaml worker` | hypothetical |
| 5 Shadow trader | `ta --config config/shadow.yaml worker` (+ wallet for simulateTransaction) | built + simulated, never sent |
| 6 Live adapter | implemented; refuses to start without the full LIVE checklist | — |
| 7 Controlled live | see `LIVE_EXECUTION.md` | real, tiny, capped |
