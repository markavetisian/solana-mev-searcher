"""Convert decoded Pump / PumpSwap Anchor events into normalized MarketEvents."""

from __future__ import annotations

from typing import Any

from tradingagent.common.events import MarketEvent
from tradingagent.common.types import EventKind, Side, Venue
from tradingagent.pump.constants import WSOL_MINT

_DEFAULT = "11111111111111111111111111111111"
_WSOL = str(WSOL_MINT)


def _quote_mint(v: str | None) -> str:
    return _WSOL if (not v or v == _DEFAULT) else v


def from_pump_event(
    name: str, ev: dict[str, Any], signature: str, slot: int, seq: int, observed_at: float, source: str = "live"
) -> MarketEvent | None:
    ts = float(ev.get("timestamp") or 0)
    base = dict(signature=signature, slot=slot, seq=seq, observed_at=observed_at, chain_time=ts, source=source)
    if name == "CreateEvent":
        return MarketEvent(
            kind=EventKind.CREATE,
            mint=ev["mint"],
            venue=Venue.BONDING_CURVE,
            user=ev.get("user"),
            creator=ev.get("creator"),
            name=ev.get("name"),
            symbol=ev.get("symbol"),
            uri=ev.get("uri"),
            token_program=ev.get("token_program"),
            quote_mint=_quote_mint(ev.get("quote_mint")),
            is_mayhem=bool(ev.get("is_mayhem_mode")),
            virtual_token_reserves=ev.get("virtual_token_reserves"),
            virtual_quote_reserves=ev.get("virtual_quote_reserves") or ev.get("virtual_sol_reserves"),
            real_token_reserves=ev.get("real_token_reserves"),
            real_quote_reserves=0,
            extra={
                "bonding_curve": ev.get("bonding_curve"),
                "token_total_supply": ev.get("token_total_supply"),
                "is_holder_reward": ev.get("is_holder_reward"),
                "creator_fee_bps": ev.get("creator_fee_bps"),
            },
            **base,
        )
    if name == "TradeEvent":
        # Since the quote-mint upgrade both legacy (sol_*) and new (quote_*) fields exist; prefer quote_* when set.
        quote_amount = ev.get("quote_amount") or ev.get("sol_amount") or 0
        vq = ev.get("virtual_quote_reserves") or ev.get("virtual_sol_reserves")
        rq = ev.get("real_quote_reserves") if ev.get("real_quote_reserves") else ev.get("real_sol_reserves")
        return MarketEvent(
            kind=EventKind.TRADE,
            mint=ev["mint"],
            venue=Venue.BONDING_CURVE,
            side=Side.BUY if ev.get("is_buy") else Side.SELL,
            user=ev.get("user"),
            sol_amount=int(quote_amount),
            token_amount=int(ev.get("token_amount") or 0),
            fee_lamports=int(ev.get("fee") or 0) + int(ev.get("creator_fee") or 0),
            creator_fee_lamports=int(ev.get("creator_fee") or 0),
            virtual_token_reserves=ev.get("virtual_token_reserves"),
            virtual_quote_reserves=vq,
            real_token_reserves=ev.get("real_token_reserves"),
            real_quote_reserves=rq,
            creator=ev.get("creator"),
            quote_mint=_quote_mint(ev.get("quote_mint")),
            is_mayhem=bool(ev.get("mayhem_mode")),
            extra={
                "ix_name": ev.get("ix_name"),
                "fee_bps": ev.get("fee_basis_points"),
                "creator_fee_bps": ev.get("creator_fee_basis_points"),
            },
            **base,
        )
    if name == "CompleteEvent":
        return MarketEvent(
            kind=EventKind.COMPLETE,
            mint=ev["mint"],
            venue=Venue.BONDING_CURVE,
            user=ev.get("user"),
            extra={"bonding_curve": ev.get("bonding_curve")},
            **base,
        )
    if name == "CompletePumpAmmMigrationEvent":
        return MarketEvent(
            kind=EventKind.MIGRATE,
            mint=ev["mint"],
            venue=Venue.PUMPSWAP,
            pool=ev.get("pool"),
            user=ev.get("user"),
            sol_amount=int(ev.get("sol_amount") or 0),
            token_amount=int(ev.get("mint_amount") or 0),
            quote_mint=_quote_mint(ev.get("quote_mint")),
            extra={"pool_migration_fee": ev.get("pool_migration_fee")},
            **base,
        )
    return None


def from_amm_event(
    name: str,
    ev: dict[str, Any],
    signature: str,
    slot: int,
    seq: int,
    observed_at: float,
    pool_to_mint: dict[str, str],
    source: str = "live",
) -> MarketEvent | None:
    ts = float(ev.get("timestamp") or 0)
    base = dict(signature=signature, slot=slot, seq=seq, observed_at=observed_at, chain_time=ts, source=source)
    if name == "CreatePoolEvent":
        pool, mint = ev["pool"], ev["base_mint"]
        pool_to_mint[pool] = mint
        return MarketEvent(
            kind=EventKind.POOL_CREATE,
            mint=mint,
            venue=Venue.PUMPSWAP,
            pool=pool,
            user=ev.get("creator"),
            creator=ev.get("coin_creator"),
            quote_mint=ev.get("quote_mint"),
            pool_base_reserves=ev.get("pool_base_amount"),
            pool_quote_reserves=ev.get("pool_quote_amount"),
            pool_virtual_quote_reserves=0,
            is_mayhem=bool(ev.get("is_mayhem_mode")),
            extra={"pool_creator": ev.get("creator"), "lp_mint": ev.get("lp_mint")},
            **base,
        )
    if name in ("BuyEvent", "SellEvent"):
        pool = ev["pool"]
        mint = pool_to_mint.get(pool)
        is_buy = name == "BuyEvent"
        base_amt = int(ev["base_amount_out"] if is_buy else ev["base_amount_in"])
        # pool_*_token_reserves in Buy/Sell events are the vault balances BEFORE the swap (the SDK prices
        # against them). Reconstruct the post-trade state the same way the program moves the vaults.
        pre_base = int(ev.get("pool_base_token_reserves") or 0)
        pre_quote = int(ev.get("pool_quote_token_reserves") or 0)
        lp_fee = int(ev.get("lp_fee") or 0)
        if is_buy:
            gross = int(ev.get("quote_amount_in") or 0)
            post_base, post_quote = pre_base - base_amt, pre_quote + gross + lp_fee
        else:
            gross = int(ev.get("quote_amount_out") or 0)
            post_base, post_quote = pre_base + base_amt, pre_quote - (gross - lp_fee)
        fees = lp_fee + int(ev.get("protocol_fee") or 0) + int(ev.get("coin_creator_fee") or 0)
        return MarketEvent(
            kind=EventKind.AMM_TRADE,
            mint=mint,
            venue=Venue.PUMPSWAP,
            side=Side.BUY if is_buy else Side.SELL,
            user=ev.get("user"),
            sol_amount=gross,
            token_amount=base_amt,
            fee_lamports=fees,
            creator_fee_lamports=int(ev.get("coin_creator_fee") or 0),
            pool=pool,
            pool_base_reserves=post_base,
            pool_quote_reserves=post_quote,
            pool_virtual_quote_reserves=int(ev.get("virtual_quote_reserves") or 0),
            creator=ev.get("coin_creator"),
            extra={
                "unresolved_pool": mint is None,
                "pre_base": pre_base,
                "pre_quote": pre_quote,
                "base_supply": ev.get("base_supply"),
            },
            **base,
        )
    return None
