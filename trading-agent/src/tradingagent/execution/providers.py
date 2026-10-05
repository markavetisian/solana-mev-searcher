"""Execution providers. The strategy calls `provider.execute(intent, get_adapter)` and never knows which one it has.

PaperExecutionProvider   — hypothetical fill against the LIVE venue state after a realistic latency, with fees,
                           exact price impact, extra adverse drift, random failures and on-chain-style min_out
                           reverts (a failed tx still pays network fees).
ShadowExecutionProvider  — same fill model, plus (when a wallet + RPC are configured) the REAL transaction is
                           built and passed to simulateTransaction. Nothing is ever broadcast.
LiveExecutionProvider    — real transactions through the TransactionEngine, with its own independent live
                           limits that the strategy and the AI cannot change.
"""

from __future__ import annotations

import asyncio
import random
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from tradingagent.common.config import ExecutionConfig, LiveLimits
from tradingagent.common.logging import get_logger
from tradingagent.common.metrics import METRICS
from tradingagent.common.types import ExecStatus, Side, Venue
from tradingagent.common.units import BASE_FEE_LAMPORTS_PER_SIGNATURE
from tradingagent.execution.orders import ExecutionReport, OrderIntent
from tradingagent.pump.adapters import MarketAdapter, QuoteError
from tradingagent.pump.borsh import amm_codec, pump_codec
from tradingagent.pump.constants import (
    GLOBAL_ACCOUNT,
    TOKEN_PROGRAM_ID,
    amm_global_config,
    canonical_pool_pda,
)
from tradingagent.pump.instructions import (
    AmmGlobalConfig,
    PoolAccounts,
    PumpGlobal,
    amm_buy_instructions,
    amm_sell_instructions,
    curve_buy_instructions,
    curve_sell_instructions,
)
from tradingagent.solana.rpc import RpcError, RpcPool
from tradingagent.solana.tx import DuplicateExecution, TransactionEngine, TxRecord, parse_fill

log = get_logger("execution.providers")

AdapterFn = Callable[[str], MarketAdapter | None]


class ExecutionRejected(RuntimeError):
    pass


class PaperFillModel:
    def __init__(self, cfg: ExecutionConfig, seed: int | None = None) -> None:
        self.cfg = cfg
        self.rng = random.Random(seed)

    def network_fee(self, intent: OrderIntent) -> tuple[int, int]:
        prio = min(
            self.cfg.compute_unit_limit * intent.priority_micro_lamports // 1_000_000,
            self.cfg.priority_fee.max_priority_fee_lamports,
        )
        return BASE_FEE_LAMPORTS_PER_SIGNATURE + prio, prio

    def fill(
        self,
        intent: OrderIntent,
        adapter: MarketAdapter | None,
        now: float,
        status_ok: ExecStatus = ExecStatus.FILLED_PAPER,
    ) -> ExecutionReport:
        net_fee, prio = self.network_fee(intent)
        rep = ExecutionReport(
            execution_id=intent.execution_id,
            status=ExecStatus.FAILED,
            mint=intent.mint,
            side=intent.side,
            venue=intent.venue,
            requested_amount=intent.amount_in,
            expected_out=intent.expected_out,
            expected_price=intent.expected_price,
            network_fees=net_fee,
            priority_fee=prio,
            submitted_at=intent.created_at,
            completed_at=now,
        )
        if adapter is None or adapter.venue is not intent.venue:
            rep.error = "venue unavailable at fill time (state changed)"
            rep.network_fees = 0  # never built
            rep.status = ExecStatus.REJECTED
            return rep
        if self.rng.random() < self.cfg.paper_failure_rate:
            rep.error = "simulated transaction failure"
            return rep
        try:
            q = adapter.quote_buy(intent.amount_in) if intent.side is Side.BUY else adapter.quote_sell(intent.amount_in)
        except QuoteError as e:
            rep.error = f"quote failed at fill: {e}"
            return rep
        out = int(q.amount_out * (1 - self.cfg.paper_extra_adverse_bps / 10_000))
        if out < intent.min_out:
            rep.error = f"slippage limit: out {out} < min_out {intent.min_out}"
            METRICS.inc("paper.slippage_revert")
            return rep
        rep.status = status_ok
        rep.actual_in = q.amount_in if intent.side is Side.BUY else intent.amount_in
        rep.actual_out = out
        rep.platform_fees = q.total_fee
        rep.spot_at_fill = q.spot_price_before
        tokens = out if intent.side is Side.BUY else intent.amount_in
        quote_amt = rep.actual_in if intent.side is Side.BUY else out
        rep.actual_price = quote_amt / tokens if tokens else 0.0
        if intent.expected_price:
            rep.slippage_vs_expected = (
                (rep.actual_price / intent.expected_price - 1)
                if intent.side is Side.BUY
                else (1 - rep.actual_price / intent.expected_price)
            )
        return rep


class ExecutionProvider(ABC):
    name = "abstract"

    def __init__(self) -> None:
        self._inflight: dict[str, str] = {}  # mint -> execution_id
        self._seen: set[str] = set()

    def busy(self, mint: str) -> bool:
        return mint in self._inflight

    @property
    def pending(self) -> bool:
        return bool(self._inflight)

    async def execute(self, intent: OrderIntent, get_adapter: AdapterFn) -> ExecutionReport:
        if intent.execution_id in self._seen:
            raise DuplicateExecution(intent.execution_id)
        if intent.mint in self._inflight:
            raise DuplicateExecution(f"order already in flight for {intent.mint}: {self._inflight[intent.mint]}")
        self._seen.add(intent.execution_id)
        self._inflight[intent.mint] = intent.execution_id
        try:
            rep = await self._execute(intent, get_adapter)
        finally:
            self._inflight.pop(intent.mint, None)
        METRICS.inc(f"exec.{self.name}.{rep.status.value}")
        return rep

    @abstractmethod
    async def _execute(self, intent: OrderIntent, get_adapter: AdapterFn) -> ExecutionReport: ...


class PaperExecutionProvider(ExecutionProvider):
    name = "paper"

    def __init__(
        self,
        cfg: ExecutionConfig,
        clock_now: Callable[[], float] = time.time,
        seed: int | None = None,
        simulate_latency: bool = True,
    ) -> None:
        super().__init__()
        self.cfg, self.now = cfg, clock_now
        self.model = PaperFillModel(cfg, seed)
        self.simulate_latency = simulate_latency

    async def _execute(self, intent: OrderIntent, get_adapter: AdapterFn) -> ExecutionReport:
        if self.simulate_latency:
            await asyncio.sleep(self.cfg.assumed_latency_s)
        return self.model.fill(intent, get_adapter(intent.mint), self.now())


# ---------------------------------------------------------------------------------------------------------------
@dataclass
class ChainContext:
    rpc: RpcPool
    keypair: Keypair
    pump_global: PumpGlobal | None = None
    amm_config: AmmGlobalConfig | None = None
    loaded_at: float = 0.0
    pools: dict[str, PoolAccounts] | None = None

    async def refresh(self, max_age_s: float = 300.0) -> None:
        if self.pump_global and time.time() - self.loaded_at < max_age_s:
            return
        g, a = await self.rpc.get_multiple_accounts([str(GLOBAL_ACCOUNT), str(amm_global_config())])
        if not g or not a:
            raise ExecutionRejected("could not load Pump Global / PumpSwap GlobalConfig")
        import base64

        _, gd = pump_codec().decode_account(base64.b64decode(g["data"][0]), "Global")
        _, ad = amm_codec().decode_account(base64.b64decode(a["data"][0]), "GlobalConfig")
        self.pump_global, self.amm_config = PumpGlobal.from_decoded(gd), AmmGlobalConfig.from_decoded(ad)
        self.loaded_at = time.time()
        self.pools = self.pools or {}

    async def pool(self, mint: str, pool_addr: str | None) -> PoolAccounts:
        import base64

        addr = pool_addr or str(canonical_pool_pda(Pubkey.from_string(mint)))
        if self.pools and addr in self.pools:
            return self.pools[addr]
        info = await self.rpc.get_account_info(addr)
        if not info:
            raise ExecutionRejected(f"pool {addr} not found")
        _, pd = amm_codec().decode_account(base64.b64decode(info["data"][0]), "Pool")
        pa = PoolAccounts.from_decoded(Pubkey.from_string(addr), pd)
        self.pools = self.pools or {}
        self.pools[addr] = pa
        return pa


async def build_instructions(ctx: ChainContext, intent: OrderIntent, close_on_full_exit: bool = True) -> list:
    await ctx.refresh()
    assert ctx.pump_global and ctx.amm_config
    user = ctx.keypair.pubkey()
    mint = Pubkey.from_string(intent.mint)
    tprog = Pubkey.from_string(intent.token_program) if intent.token_program else TOKEN_PROGRAM_ID
    full_exit = bool(intent.meta.get("full_exit"))
    if intent.venue is Venue.BONDING_CURVE:
        if not intent.creator:
            raise ExecutionRejected("bonding-curve trade needs the curve creator (creator_vault PDA)")
        creator = Pubkey.from_string(intent.creator)
        mayhem = bool(intent.meta.get("mayhem"))
        if intent.side is Side.BUY:
            return curve_buy_instructions(
                user, mint, creator, tprog, intent.amount_in, intent.min_out, ctx.pump_global, mayhem
            )
        return curve_sell_instructions(
            user,
            mint,
            creator,
            tprog,
            intent.amount_in,
            intent.min_out,
            ctx.pump_global,
            close_on_full_exit and full_exit,
            mayhem,
        )
    pool = await ctx.pool(intent.mint, intent.pool)
    if intent.side is Side.BUY:
        return amm_buy_instructions(user, pool, tprog, intent.amount_in, intent.min_out, ctx.amm_config)
    return amm_sell_instructions(
        user, pool, tprog, intent.amount_in, intent.min_out, ctx.amm_config, close_on_full_exit and full_exit
    )


class ShadowExecutionProvider(ExecutionProvider):
    name = "shadow"

    def __init__(
        self,
        cfg: ExecutionConfig,
        clock_now: Callable[[], float] = time.time,
        ctx: ChainContext | None = None,
        seed: int | None = None,
    ) -> None:
        super().__init__()
        self.cfg, self.now, self.ctx = cfg, clock_now, ctx
        self.model = PaperFillModel(cfg, seed)
        self.engine = TransactionEngine(ctx.rpc, ctx.keypair, cfg) if ctx else None

    async def _execute(self, intent: OrderIntent, get_adapter: AdapterFn) -> ExecutionReport:
        sim: dict[str, Any] = {"performed": False}
        if self.ctx and self.engine:
            try:
                ixs = await build_instructions(self.ctx, intent)
                rec: TxRecord = await self.engine.execute(
                    intent.execution_id, ixs, intent.priority_micro_lamports, dry_run=True
                )
                sim = {
                    "performed": True,
                    "status": rec.status.value,
                    "error": rec.error,
                    "units_consumed": rec.units_consumed,
                    "logs_tail": rec.sim_logs[-8:],
                }
            except (ExecutionRejected, RpcError, ValueError) as e:
                sim = {"performed": False, "error": f"{type(e).__name__}: {e}"}
        await asyncio.sleep(self.cfg.assumed_latency_s)
        rep = self.model.fill(intent, get_adapter(intent.mint), self.now(), status_ok=ExecStatus.FILLED_SHADOW)
        rep.extra["onchain_simulation"] = sim
        rep.extra["broadcast"] = False
        if sim.get("performed") and sim.get("status") == ExecStatus.REJECTED.value and rep.filled:
            rep.status, rep.error = ExecStatus.FAILED, f"on-chain simulation rejected: {sim.get('error')}"
        log.info(
            "shadow_would_send",
            execution_id=intent.execution_id,
            token=intent.mint,
            side=intent.side.value,
            amount_in=intent.amount_in,
            min_out=intent.min_out,
            status=rep.status.value,
            simulation=sim,
        )
        return rep


class LiveExecutionProvider(ExecutionProvider):
    name = "live"

    def __init__(self, cfg: ExecutionConfig, limits: LiveLimits, ctx: ChainContext, engine: TransactionEngine) -> None:
        super().__init__()
        self.cfg, self.limits, self.ctx, self.engine = cfg, limits, ctx, engine
        self._trade_times: deque[float] = deque(maxlen=1000)
        self.enabled = True

    def _check_limits(self, intent: OrderIntent) -> None:
        lim = self.limits
        if not self.enabled:
            raise ExecutionRejected("live provider disabled")
        if intent.venue.value not in lim.allowed_venues:
            raise ExecutionRejected(f"venue {intent.venue.value} not allowed in live limits")
        if intent.side is Side.BUY:
            if intent.amount_in > lim.max_position_sol * 1e9:
                raise ExecutionRejected(f"order {intent.amount_in / 1e9:.4f} SOL > live max_position_sol")
            now = time.time()
            if sum(1 for t in self._trade_times if now - t < 3600) >= lim.max_trades_per_hour:
                raise ExecutionRejected("live max_trades_per_hour reached")
        slip_bps = int(round((1 - intent.min_out / intent.expected_out) * 10_000)) if intent.expected_out else 10_000
        if slip_bps > lim.max_slippage_bps + 1:
            raise ExecutionRejected(f"slippage tolerance {slip_bps}bps > live max {lim.max_slippage_bps}bps")
        prio = self.cfg.compute_unit_limit * intent.priority_micro_lamports // 1_000_000
        if prio > lim.max_priority_fee_lamports:
            raise ExecutionRejected("priority fee above live cap")

    async def _execute(self, intent: OrderIntent, get_adapter: AdapterFn) -> ExecutionReport:
        rep = ExecutionReport(
            execution_id=intent.execution_id,
            status=ExecStatus.REJECTED,
            mint=intent.mint,
            side=intent.side,
            venue=intent.venue,
            requested_amount=intent.amount_in,
            expected_out=intent.expected_out,
            expected_price=intent.expected_price,
            submitted_at=time.time(),
        )
        try:
            self._check_limits(intent)
            ixs = await build_instructions(self.ctx, intent)
        except (ExecutionRejected, RpcError, ValueError) as e:
            rep.error = str(e)
            return rep
        if intent.side is Side.BUY:
            self._trade_times.append(time.time())
        rec = await self.engine.execute(intent.execution_id, ixs, intent.priority_micro_lamports)
        rep.status, rep.error, rep.signature = rec.status, rec.error, rec.signature
        rep.blockhash, rep.last_valid_block_height, rep.slot = rec.blockhash, rec.last_valid_block_height, rec.slot
        rep.priority_fee = rec.compute_unit_limit * rec.priority_micro_lamports // 1_000_000
        rep.completed_at = time.time()
        if rec.status.value == "CONFIRMED" and rec.signature:
            try:
                tx = await self.ctx.rpc.get_transaction(rec.signature)
                user = str(self.ctx.keypair.pubkey())
                sol_delta, tok_delta, fee = parse_fill(tx or {}, user, intent.mint)
                rep.network_fees = fee
                if intent.side is Side.BUY:
                    rep.actual_in, rep.actual_out = -sol_delta, tok_delta
                else:
                    rep.actual_in, rep.actual_out = -tok_delta, sol_delta
                tokens = rep.actual_out if intent.side is Side.BUY else rep.actual_in
                quote = rep.actual_in if intent.side is Side.BUY else rep.actual_out
                rep.actual_price = quote / tokens if tokens else 0.0
                if intent.expected_price and rep.actual_price:
                    rep.slippage_vs_expected = (
                        (rep.actual_price / intent.expected_price - 1)
                        if intent.side is Side.BUY
                        else (1 - rep.actual_price / intent.expected_price)
                    )
                rep.spot_at_fill = intent.spot_price
            except RpcError as e:
                rep.extra["fill_parse_error"] = str(e)  # landed; amounts reconciled from balances later
        elif rec.status.value == "FAILED":
            rep.network_fees = BASE_FEE_LAMPORTS_PER_SIGNATURE + rep.priority_fee
        return rep
