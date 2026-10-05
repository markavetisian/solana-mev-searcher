# trading-agent

A quantitative research and execution system for newly launched and actively traded **Pump.fun** tokens
(bonding curve and PumpSwap), with a constrained AI analysis layer.

It is built to **prove or disprove its own edge**. It does not assume one. With insufficient evidence its
output is `NO TRADE`, and in the controls run so far (synthetic data only) that is what it produced.

```text
events ─► discovery ─► market data ─► features ─► filter ─► score ─► regime ─► AI (veto only) ─► EV after costs
       ─► size ─► execution simulation ─► risk engine ─► APPROVE / REJECT ─► Paper | Shadow | Live provider
```

Full diagram: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Non-negotiables, as implemented

| Principle | Implementation |
|---|---|
| AI is not the execution authority | AI output is schema-validated JSON with no action fields; it can only veto (`strategy/decision.py`) |
| Never trade on displayed price | Exact on-chain quote math (verified vs official SDKs, 1800 vectors), entry + exit impact, fees by market-cap tier, network + priority fees, latency risk; positions marked at executable sell value |
| Bonding curve ≠ PumpSwap | Separate adapters and lifecycle FSM; GRADUATING has no venue |
| EV from evidence | Empirical outcomes per score bucket/regime, Wilson lower bound; `< ev.min_samples` ⇒ INSUFFICIENT DATA ⇒ no trade |
| Risk-based sizing with ceilings | Risk-per-trade / stop distance, capped by position %, exposure, cash, liquidity share, live limits; score only scales down |
| Never blindly resend | Transaction state machine; UNCERTAIN ⇒ kill switch |
| LIVE off by default | 7-condition `live_guard`, independent `live_limits.yaml` |
| Every decision auditable | Append-only tables (DB triggers); strategy/feature/config/model/AI versions on every decision, order, trade |
| No look-ahead | Structural: ordered replay, forward-only clock, `as_of` guards that raise, latency-delayed fills |

## Quickstart

```bash
cd trading-agent
python3.12 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
cp .env.example .env && chmod 600 .env        # DATABASE_URL, SOLANA_RPC_URLS, SOLANA_WS_URLS, API tokens
ta migrate && ta config-check && ta security-check
ta --config config/research.yaml worker --source ws   # record + label (no orders)
ta api                                                # API on 127.0.0.1:8080
cd apps/dashboard && npm ci && npm run build && \
  TA_API_URL=http://127.0.0.1:8080 TA_API_READ_TOKEN=... DASHBOARD_BASIC_AUTH=op:pass npm start
pytest                                                # 693 tests
```

No RPC credentials yet? `ta --config config/paper.yaml worker --source synthetic` runs the whole stack on a
clearly labelled synthetic feed (useful for the UI and drills; worthless as evidence).

## Documentation

| Topic | File |
|---|---|
| Architecture & look-ahead design | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Database schema | [docs/DATABASE.md](docs/DATABASE.md) |
| Environment variables & configuration | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Setup, Docker, migrations, tests | [docs/SETUP.md](docs/SETUP.md) |
| Backtesting & walk-forward | [docs/BACKTESTING.md](docs/BACKTESTING.md) |
| Paper & shadow trading | [docs/PAPER_TRADING.md](docs/PAPER_TRADING.md) |
| Live execution & PAPER → SHADOW → LIVE procedure | [docs/LIVE_EXECUTION.md](docs/LIVE_EXECUTION.md) |
| Security | [docs/SECURITY.md](docs/SECURITY.md) |
| Strategy research | [docs/RESEARCH.md](docs/RESEARCH.md) |
| Final report (what's real, simulated, experimental, failure modes) | [docs/FINAL_REPORT.md](docs/FINAL_REPORT.md) |

## Layout

```text
trading-agent/
├── apps/{api,worker,cli}/main.py   thin entrypoints (same as `ta api|worker|...`)
├── apps/dashboard/                  Next.js dashboard (server-side API proxy, basic auth)
├── src/tradingagent/
│   ├── common/ solana/ pump/ ingestion/ discovery/ market/ features/ wallets/ scoring/
│   ├── strategy/ risk/ execution/ portfolio/ ai/ backtest/ research/ paper/ alerts/
│   └── storage/ api/ worker/ cli/
├── config/       default.yaml + research/paper/shadow/live overlays + live_limits.yaml
├── migrations/   Alembic (schema + append-only triggers)
├── tests/        unit / integration / failure (+ SDK golden vectors)
├── docker/ docker-compose.yml  scripts/  docs/
```
