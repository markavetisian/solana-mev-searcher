"""Transaction engine: build -> simulate -> sign -> persist -> send -> confirm, with a strict state machine.

    CREATED -> SIMULATED -> SIGNED -> SUBMITTED -> CONFIRMED | FAILED | EXPIRED | UNCERTAIN
            \\-> REJECTED (simulation failed / pre-checks, never broadcast)

Safety rules:
  * One execution_id = one signed transaction. Re-broadcasting the SAME signed bytes while its blockhash is
    valid is safe (same signature, at most one can land). Building a NEW transaction for the same intent is only
    allowed once the old one is provably EXPIRED (block height > lastValidBlockHeight and the signature is
    unknown even with searchTransactionHistory).
  * If landing can be neither proven nor disproven (RPC down, timeout) the status is UNCERTAIN. UNCERTAIN is
    never auto-resent; it escalates to the kill switch and waits for reconciliation.
  * The signed transaction and its signature are persisted (on_update) BEFORE the first broadcast, so a crash
    after sending can always be reconciled by signature on restart.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from tradingagent.common.config import ExecutionConfig
from tradingagent.common.logging import get_logger, log_context
from tradingagent.common.metrics import METRICS
from tradingagent.common.types import ExecStatus
from tradingagent.solana.rpc import RpcError, RpcPool

log = get_logger("solana.tx")

TERMINAL = {ExecStatus.CONFIRMED, ExecStatus.FAILED, ExecStatus.EXPIRED, ExecStatus.REJECTED}


class DuplicateExecution(RuntimeError):
    pass


@dataclass
class TxRecord:
    execution_id: str
    status: ExecStatus = ExecStatus.CREATED
    signature: str | None = None
    blockhash: str | None = None
    last_valid_block_height: int | None = None
    raw_b64: str | None = None
    compute_unit_limit: int = 0
    priority_micro_lamports: int = 0
    units_consumed: int | None = None
    sim_logs: list[str] = field(default_factory=list)
    attempts: int = 0
    submitted_at: float | None = None
    confirmed_at: float | None = None
    slot: int | None = None
    error: str | None = None
    history: list[tuple[float, str]] = field(default_factory=list)

    def set(self, status: ExecStatus, error: str | None = None) -> None:
        self.status = status
        if error:
            self.error = error
        self.history.append((time.time(), status.value))

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "raw_b64"}
        d["status"] = self.status.value
        return d


class TransactionEngine:
    def __init__(
        self,
        rpc: RpcPool,
        keypair: Keypair,
        cfg: ExecutionConfig,
        on_update: Callable[[TxRecord], Awaitable[None]] | None = None,
    ) -> None:
        self.rpc, self.keypair, self.cfg = rpc, keypair, cfg
        self.on_update = on_update
        self.records: dict[str, TxRecord] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def payer(self):
        return self.keypair.pubkey()

    async def _persist(self, rec: TxRecord) -> None:
        if self.on_update:
            await self.on_update(rec)

    def _compile(self, ixs: list[Instruction], blockhash: str, cu_limit: int, price: int) -> VersionedTransaction:
        budget = [set_compute_unit_limit(cu_limit), set_compute_unit_price(price)]
        msg = MessageV0.try_compile(self.payer, budget + ixs, [], Hash.from_string(blockhash))
        return VersionedTransaction(msg, [self.keypair])

    async def execute(
        self, execution_id: str, ixs: list[Instruction], priority_micro_lamports: int, dry_run: bool = False
    ) -> TxRecord:
        """dry_run=True stops after simulation (SHADOW mode): nothing is ever broadcast."""
        lock = self._locks.setdefault(execution_id, asyncio.Lock())
        async with lock, _ctx(execution_id):
            existing = self.records.get(execution_id)
            if existing is not None:
                if existing.status is ExecStatus.UNCERTAIN:
                    raise DuplicateExecution(f"{execution_id} is UNCERTAIN; reconcile before any retry")
                if existing.status not in (ExecStatus.EXPIRED, ExecStatus.REJECTED):
                    return existing  # idempotent: never build a second tx for a live/landed intent
            rec = TxRecord(
                execution_id=execution_id,
                priority_micro_lamports=priority_micro_lamports,
                compute_unit_limit=self.cfg.compute_unit_limit,
            )
            self.records[execution_id] = rec
            rec.set(ExecStatus.CREATED)
            try:
                blockhash, lvbh = await self.rpc.get_latest_blockhash()
            except RpcError as e:
                rec.set(ExecStatus.REJECTED, f"blockhash unavailable: {e}")
                await self._persist(rec)
                return rec
            rec.blockhash, rec.last_valid_block_height = blockhash, lvbh
            tx = self._compile(ixs, blockhash, rec.compute_unit_limit, priority_micro_lamports)
            if self.cfg.simulation_required or dry_run:
                ok = await self._simulate(rec, tx)
                if not ok:
                    await self._persist(rec)
                    return rec
                if rec.units_consumed:
                    tuned = int(rec.units_consumed * self.cfg.compute_unit_buffer) + 1_000
                    if tuned < rec.compute_unit_limit:
                        rec.compute_unit_limit = tuned
                        tx = self._compile(ixs, blockhash, tuned, priority_micro_lamports)
            if dry_run:
                await self._persist(rec)
                return rec
            rec.signature = str(tx.signatures[0])
            rec.raw_b64 = base64.b64encode(bytes(tx)).decode()
            rec.set(ExecStatus.SIGNED)
            await self._persist(rec)  # must hit storage BEFORE broadcast
            await self._send_and_confirm(rec)
            await self._persist(rec)
            return rec

    async def _simulate(self, rec: TxRecord, tx: VersionedTransaction) -> bool:
        b64 = base64.b64encode(bytes(tx)).decode()
        try:
            res = await self.rpc.simulate_transaction(b64)
        except RpcError as e:
            rec.set(ExecStatus.REJECTED, f"simulation unavailable: {e}")
            METRICS.inc("tx.simulation_unavailable")
            return False
        rec.sim_logs = (res.get("logs") or [])[-40:]
        rec.units_consumed = res.get("unitsConsumed")
        if res.get("err") is not None:
            rec.set(ExecStatus.REJECTED, f"simulation error: {res.get('err')}")
            METRICS.inc("tx.simulation_failed")
            log.warning("tx_simulation_failed", execution_id=rec.execution_id, error=str(res.get("err")))
            return False
        rec.set(ExecStatus.SIMULATED)
        return True

    async def _send_and_confirm(self, rec: TxRecord) -> None:
        assert rec.raw_b64 and rec.signature and rec.last_valid_block_height is not None
        rec.submitted_at = time.time()
        deadline = rec.submitted_at + self.cfg.confirm_timeout_s
        last_send = 0.0
        rpc_errors = 0
        rec.set(ExecStatus.SUBMITTED)
        while True:
            now = time.time()
            if rec.attempts < self.cfg.max_send_attempts and now - last_send >= 2.0:
                try:
                    await self.rpc.send_transaction(rec.raw_b64, skip_preflight=True)
                    rec.attempts += 1
                    METRICS.inc("tx.sent")
                except RpcError as e:
                    rpc_errors += 1
                    log.warning("tx_send_error", execution_id=rec.execution_id, error=str(e))
                last_send = now
            try:
                st = (await self.rpc.get_signature_statuses([rec.signature]))[0]
                rpc_errors = 0
            except RpcError:
                st, rpc_errors = None, rpc_errors + 1
            if st is not None and st.get("confirmationStatus") in ("confirmed", "finalized"):
                rec.slot = st.get("slot")
                rec.confirmed_at = time.time()
                if st.get("err") is not None:
                    rec.set(ExecStatus.FAILED, f"landed with error: {st.get('err')}")
                    METRICS.inc("tx.failed_onchain")
                else:
                    rec.set(ExecStatus.CONFIRMED)
                    METRICS.inc("tx.confirmed")
                    METRICS.observe_ms("tx.confirm_latency", (rec.confirmed_at - rec.submitted_at) * 1000)
                return
            try:
                height = await self.rpc.get_block_height()
            except RpcError:
                height = None
            if height is not None and height > rec.last_valid_block_height:
                # Blockhash expired. One last authoritative lookup before declaring it dead.
                try:
                    final = (await self.rpc.get_signature_statuses([rec.signature], search_history=True))[0]
                except RpcError:
                    rec.set(ExecStatus.UNCERTAIN, "expired blockhash but final status lookup failed")
                    METRICS.inc("tx.uncertain")
                    return
                if final is None:
                    rec.set(ExecStatus.EXPIRED, "blockhash expired; signature never landed")
                    METRICS.inc("tx.expired")
                    return
                rec.slot = final.get("slot")
                rec.set(
                    ExecStatus.FAILED if final.get("err") else ExecStatus.CONFIRMED,
                    str(final.get("err")) if final.get("err") else None,
                )
                return
            if now > deadline + 90 or rpc_errors >= 10:
                rec.set(ExecStatus.UNCERTAIN, "could not prove landed or expired (timeout / RPC errors)")
                METRICS.inc("tx.uncertain")
                log.critical("tx_uncertain", execution_id=rec.execution_id, signature=rec.signature)
                return
            await asyncio.sleep(self.cfg.status_poll_interval_s)

    async def reconcile(self, rec: TxRecord) -> TxRecord:
        """Re-check an UNCERTAIN record by signature (e.g. on restart). Never re-sends."""
        if rec.signature is None:
            rec.set(ExecStatus.EXPIRED, "never signed/broadcast")
            return rec
        st = (await self.rpc.get_signature_statuses([rec.signature], search_history=True))[0]
        if st is not None:
            rec.slot = st.get("slot")
            rec.set(ExecStatus.FAILED if st.get("err") else ExecStatus.CONFIRMED)
        elif (
            rec.last_valid_block_height is not None and await self.rpc.get_block_height() > rec.last_valid_block_height
        ):
            rec.set(ExecStatus.EXPIRED, "reconciled: never landed")
        await self._persist(rec)
        return rec


def parse_fill(tx: dict, user: str, mint: str) -> tuple[int, int, int]:
    """(lamports delta for user excl. fee, token delta for user, network fee) from a getTransaction result."""
    meta = tx.get("meta") or {}
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    keys = [k if isinstance(k, str) else k.get("pubkey") for k in keys]
    fee = int(meta.get("fee") or 0)
    sol_delta = 0
    if user in keys:
        i = keys.index(user)
        sol_delta = int(meta["postBalances"][i]) - int(meta["preBalances"][i]) + (fee if i == 0 else 0)

    def tok(bals: list[dict]) -> int:
        return sum(int(b["uiTokenAmount"]["amount"]) for b in bals if b.get("owner") == user and b.get("mint") == mint)

    token_delta = tok(meta.get("postTokenBalances") or []) - tok(meta.get("preTokenBalances") or [])
    return sol_delta, token_delta, fee


class _ctx:
    def __init__(self, execution_id: str) -> None:
        self.cm = log_context(execution_id=execution_id)

    async def __aenter__(self) -> None:
        self.cm.__enter__()

    async def __aexit__(self, *a: object) -> None:
        self.cm.__exit__(*a)
