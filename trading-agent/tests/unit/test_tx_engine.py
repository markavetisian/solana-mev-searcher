"""Transaction state machine against a scripted fake RPC (no network)."""

from __future__ import annotations

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer

from tradingagent.common.config import ExecutionConfig
from tradingagent.common.types import ExecStatus
from tradingagent.solana.rpc import RpcError
from tradingagent.solana.tx import DuplicateExecution, TransactionEngine, parse_fill

BH = "11111111111111111111111111111111"


class FakeRpc:
    def __init__(self, *, sim_err=None, statuses=None, heights=None, send_error=False, status_error=False, final=None):
        self.sim_err, self.statuses, self.heights = sim_err, list(statuses or []), list(heights or [100])
        self.send_error, self.status_error, self.final = send_error, status_error, final
        self.sent: list[str] = []
        self.calls: list[str] = []

    async def get_latest_blockhash(self, commitment: str = "confirmed"):
        self.calls.append("blockhash")
        return BH, 150

    async def simulate_transaction(self, tx_b64: str, replace_blockhash: bool = False):
        self.calls.append("simulate")
        return {"err": self.sim_err, "logs": ["Program log: ok"], "unitsConsumed": 60_000}

    async def send_transaction(self, tx_b64: str, skip_preflight: bool = True, endpoint_index=None):
        self.calls.append("send")
        if self.send_error:
            raise RpcError("send failed", code=503)
        self.sent.append(tx_b64)
        return "sig"

    async def get_signature_statuses(self, sigs, search_history: bool = False):
        self.calls.append("status_hist" if search_history else "status")
        if self.status_error:
            raise RpcError("down", code=503)
        if search_history:
            return [self.final]
        return [self.statuses.pop(0) if self.statuses else None]

    async def get_block_height(self, commitment: str = "confirmed") -> int:
        if self.status_error:
            raise RpcError("down", code=503)
        return self.heights.pop(0) if len(self.heights) > 1 else self.heights[0]


def ixs(kp: Keypair):
    return [transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Pubkey.new_unique(), lamports=1))]


def cfg(**kw) -> ExecutionConfig:
    base = dict(status_poll_interval_s=0.0, confirm_timeout_s=0.05, max_send_attempts=2)
    base.update(kw)
    return ExecutionConfig(**base)


async def test_confirmed_happy_path_and_persist_before_send():
    kp = Keypair()
    rpc = FakeRpc(statuses=[None, {"confirmationStatus": "confirmed", "slot": 7, "err": None}])
    order: list[str] = []

    async def on_update(rec):
        order.append(f"{rec.status.value}:{len(rpc.sent)}")

    eng = TransactionEngine(rpc, kp, cfg(), on_update)
    rec = await eng.execute("ex1", ixs(kp), 1000)
    assert rec.status is ExecStatus.CONFIRMED and rec.slot == 7 and rec.signature
    assert order[0] == "SIGNED:0"  # persisted with signature BEFORE anything was broadcast
    assert rpc.calls.index("simulate") < rpc.calls.index("send")
    assert rec.compute_unit_limit < cfg().compute_unit_limit  # tuned from simulation


async def test_simulation_failure_is_never_broadcast():
    kp = Keypair()
    rpc = FakeRpc(sim_err={"InstructionError": [2, {"Custom": 6003}]})
    rec = await TransactionEngine(rpc, kp, cfg()).execute("ex2", ixs(kp), 1000)
    assert rec.status is ExecStatus.REJECTED and "simulation error" in rec.error
    assert "send" not in rpc.calls


async def test_landed_with_error_is_failed():
    kp = Keypair()
    rpc = FakeRpc(statuses=[{"confirmationStatus": "confirmed", "slot": 9, "err": {"InstructionError": [3, "x"]}}])
    rec = await TransactionEngine(rpc, kp, cfg()).execute("ex3", ixs(kp), 1000)
    assert rec.status is ExecStatus.FAILED


async def test_expired_blockhash_is_provably_not_landed():
    kp = Keypair()
    rpc = FakeRpc(statuses=[None, None], heights=[100, 151], final=None)
    rec = await TransactionEngine(rpc, kp, cfg()).execute("ex4", ixs(kp), 1000)
    assert rec.status is ExecStatus.EXPIRED
    assert len(rpc.sent) <= 2  # only the same signed bytes were rebroadcast


async def test_expired_but_found_in_history_is_confirmed():
    kp = Keypair()
    rpc = FakeRpc(statuses=[None], heights=[151], final={"confirmationStatus": "finalized", "slot": 5, "err": None})
    rec = await TransactionEngine(rpc, kp, cfg()).execute("ex5", ixs(kp), 1000)
    assert rec.status is ExecStatus.CONFIRMED


async def test_rpc_outage_after_send_is_uncertain_and_never_resent():
    kp = Keypair()
    rpc = FakeRpc(status_error=True)
    eng = TransactionEngine(rpc, kp, cfg())
    rec = await eng.execute("ex6", ixs(kp), 1000)
    assert rec.status is ExecStatus.UNCERTAIN
    with pytest.raises(DuplicateExecution):
        await eng.execute("ex6", ixs(kp), 1000)


async def test_same_execution_id_is_idempotent():
    kp = Keypair()
    rpc = FakeRpc(statuses=[{"confirmationStatus": "confirmed", "slot": 1, "err": None}])
    eng = TransactionEngine(rpc, kp, cfg())
    a = await eng.execute("ex7", ixs(kp), 1000)
    b = await eng.execute("ex7", ixs(kp), 1000)
    assert a is b and rpc.calls.count("blockhash") == 1


async def test_dry_run_never_sends():
    kp = Keypair()
    rpc = FakeRpc()
    rec = await TransactionEngine(rpc, kp, cfg()).execute("ex8", ixs(kp), 1000, dry_run=True)
    assert rec.status is ExecStatus.SIMULATED and "send" not in rpc.calls


def test_parse_fill_from_transaction_meta():
    user, mint = "User1111111111111111111111111111111111111", "Mint111111111111111111111111111111111111"
    tx = {
        "meta": {
            "fee": 5000,
            "preBalances": [10_000_000_000, 0],
            "postBalances": [8_994_995_000, 0],
            "preTokenBalances": [],
            "postTokenBalances": [{"owner": user, "mint": mint, "uiTokenAmount": {"amount": "123456"}}],
        },
        "transaction": {"message": {"accountKeys": [user, "Other"]}},
    }
    sol, tok, fee = parse_fill(tx, user, mint)
    assert fee == 5000 and tok == 123456 and sol == 8_994_995_000 - 10_000_000_000 + 5000
