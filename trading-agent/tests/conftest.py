from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
os.chdir(ROOT)

# Never pick up a developer's real secrets during tests.
for var in (
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "WALLET_KEYPAIR_PATH",
    "WALLET_PRIVATE_KEY_B58",
    "LIVE_TRADING_CONFIRM",
    "SOLANA_RPC_URLS",
    "SOLANA_WS_URLS",
):
    os.environ.pop(var, None)


@pytest.fixture
def cfg():
    from tradingagent.common.config import AppConfig

    return AppConfig()


@pytest.fixture
def sched(cfg):
    from tradingagent.pump.fees import FeeSchedule

    return FeeSchedule.from_rows(cfg.pump.fee_tiers, cfg.pump.flat_fees)


@pytest.fixture
async def sqlite_db(tmp_path):
    from tradingagent.storage.db import Database

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await db.create_all()
    yield db
    await db.close()
