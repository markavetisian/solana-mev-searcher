"""Instruction builders for real Pump / PumpSwap transactions.

Account lists are built by NAME from the vendored IDL (order, writable and signer flags all come from the IDL),
so an IDL refresh that reorders accounts cannot silently produce a wrong transaction; a missing name raises.

Paths:
  * bonding curve buy   -> buy_exact_quote_in_v2(spendable_quote_in, min_tokens_out)
  * bonding curve sell  -> sell_v2(amount, min_sol_output)
  * PumpSwap buy        -> buy_exact_quote_in(spendable_quote_in, min_base_amount_out, track_volume)
                           wrapped in create-wSOL-ATA / transfer / sync_native ... close-wSOL-ATA
  * PumpSwap sell       -> sell(base_amount_in, min_quote_amount_out) + close wSOL ATA

STATUS: built against pump-public-docs @ versions.PUMP_IDL_COMMIT and the official SDK account derivations, but
NOT exercised against mainnet from this repository's CI (no RPC access). LiveExecutionProvider therefore refuses
to broadcast anything that has not passed `simulateTransaction` first (execution.simulation_required).
"""

from __future__ import annotations

import random
import struct
from dataclasses import dataclass

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from tradingagent.pump.borsh import amm_codec, pump_codec
from tradingagent.pump.constants import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    PUMP_AMM_PROGRAM_ID,
    PUMP_FEE_PROGRAM_ID,
    PUMP_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    WSOL_MINT,
    amm_event_authority,
    amm_fee_config,
    amm_global_config,
    amm_global_volume_accumulator,
    amm_user_volume_accumulator,
    associated_token_address,
    bonding_curve_pda,
    coin_creator_vault_authority,
    creator_vault_pda,
    global_pda,
    global_volume_accumulator,
    pool_v2_pda,
    pump_event_authority,
    pump_fee_config,
    sharing_config_pda,
    user_volume_accumulator,
)

_DEFAULT = Pubkey.default()


class InstructionBuildError(ValueError):
    pass


def _build(
    codec_name: str,
    program: Pubkey,
    ix_name: str,
    accounts: dict[str, Pubkey],
    data: bytes,
    remaining: list[AccountMeta] | None = None,
) -> Instruction:
    codec = pump_codec() if codec_name == "pump" else amm_codec()
    metas: list[AccountMeta] = []
    for a in codec.instruction_accounts(ix_name):
        name = a["name"]
        if "address" in a:
            key = Pubkey.from_string(a["address"])
        elif name in accounts:
            key = accounts[name]
        else:
            raise InstructionBuildError(f"{ix_name}: no value for account '{name}'")
        metas.append(AccountMeta(key, is_signer=bool(a.get("signer")), is_writable=bool(a.get("writable"))))
    if remaining:
        metas.extend(remaining)
    return Instruction(program, codec.instruction_discriminator(ix_name) + data, metas)


# ---- SPL helpers ------------------------------------------------------------------------------------------
def create_ata_idempotent(payer: Pubkey, owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Instruction:
    ata = associated_token_address(owner, mint, token_program)
    return Instruction(
        ASSOCIATED_TOKEN_PROGRAM_ID,
        bytes([1]),
        [
            AccountMeta(payer, True, True),
            AccountMeta(ata, False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(token_program, False, False),
        ],
    )


def sync_native(account: Pubkey) -> Instruction:
    return Instruction(TOKEN_PROGRAM_ID, bytes([17]), [AccountMeta(account, False, True)])


def close_account(account: Pubkey, destination: Pubkey, owner: Pubkey, token_program: Pubkey) -> Instruction:
    return Instruction(
        token_program,
        bytes([9]),
        [AccountMeta(account, False, True), AccountMeta(destination, False, True), AccountMeta(owner, True, False)],
    )


def system_transfer(src: Pubkey, dst: Pubkey, lamports: int) -> Instruction:
    from solders.system_program import TransferParams, transfer

    return transfer(TransferParams(from_pubkey=src, to_pubkey=dst, lamports=lamports))


# ---- on-chain config snapshots ---------------------------------------------------------------------------
@dataclass
class PumpGlobal:
    fee_recipients: list[Pubkey]
    reserved_fee_recipients: list[Pubkey]
    buyback_fee_recipients: list[Pubkey]

    @classmethod
    def from_decoded(cls, g: dict) -> PumpGlobal:
        def keys(xs: list[str]) -> list[Pubkey]:
            return [Pubkey.from_string(x) for x in xs if x and x != str(_DEFAULT)]

        return cls(
            fee_recipients=keys([g["fee_recipient"], *g.get("fee_recipients", [])]),
            reserved_fee_recipients=keys([g.get("reserved_fee_recipient", ""), *g.get("reserved_fee_recipients", [])]),
            buyback_fee_recipients=keys(g.get("buyback_fee_recipients", [])),
        )

    def pick_fee_recipient(self, mayhem: bool) -> Pubkey:
        pool = self.reserved_fee_recipients if mayhem else self.fee_recipients
        if not pool:
            raise InstructionBuildError("no fee recipients in Global")
        return random.choice(pool)

    def pick_buyback_recipient(self) -> Pubkey:
        if not self.buyback_fee_recipients:
            raise InstructionBuildError("no buyback fee recipients in Global")
        return random.choice(self.buyback_fee_recipients)


@dataclass
class AmmGlobalConfig:
    protocol_fee_recipients: list[Pubkey]
    reserved_fee_recipients: list[Pubkey]
    buyback_fee_recipients: list[Pubkey]

    @classmethod
    def from_decoded(cls, g: dict) -> AmmGlobalConfig:
        def keys(xs: list[str]) -> list[Pubkey]:
            return [Pubkey.from_string(x) for x in xs if x and x != str(_DEFAULT)]

        return cls(
            protocol_fee_recipients=keys(g["protocol_fee_recipients"]),
            reserved_fee_recipients=keys([g.get("reserved_fee_recipient", ""), *g.get("reserved_fee_recipients", [])]),
            buyback_fee_recipients=keys(g.get("buyback_fee_recipients", [])),
        )


@dataclass
class PoolAccounts:
    pool: Pubkey
    base_mint: Pubkey
    quote_mint: Pubkey
    pool_base_token_account: Pubkey
    pool_quote_token_account: Pubkey
    coin_creator: Pubkey
    is_mayhem_mode: bool
    is_cashback_coin: bool

    @classmethod
    def from_decoded(cls, pool: Pubkey, p: dict) -> PoolAccounts:
        return cls(
            pool=pool,
            base_mint=Pubkey.from_string(p["base_mint"]),
            quote_mint=Pubkey.from_string(p["quote_mint"]),
            pool_base_token_account=Pubkey.from_string(p["pool_base_token_account"]),
            pool_quote_token_account=Pubkey.from_string(p["pool_quote_token_account"]),
            coin_creator=Pubkey.from_string(p["coin_creator"]),
            is_mayhem_mode=bool(p.get("is_mayhem_mode")),
            is_cashback_coin=bool(p.get("is_cashback_coin")),
        )


# ---- bonding curve ----------------------------------------------------------------------------------------
def _curve_accounts(
    user: Pubkey, mint: Pubkey, creator: Pubkey, base_token_program: Pubkey, glob: PumpGlobal, mayhem: bool
) -> dict[str, Pubkey]:
    quote_mint, quote_prog = WSOL_MINT, TOKEN_PROGRAM_ID
    curve = bonding_curve_pda(mint)
    fee_recipient = glob.pick_fee_recipient(mayhem)
    buyback = glob.pick_buyback_recipient()
    cvault = creator_vault_pda(creator)
    uva = user_volume_accumulator(user)
    return {
        "global": global_pda(),
        "base_mint": mint,
        "quote_mint": quote_mint,
        "base_token_program": base_token_program,
        "quote_token_program": quote_prog,
        "fee_recipient": fee_recipient,
        "associated_quote_fee_recipient": associated_token_address(fee_recipient, quote_mint, quote_prog),
        "buyback_fee_recipient": buyback,
        "associated_quote_buyback_fee_recipient": associated_token_address(buyback, quote_mint, quote_prog),
        "bonding_curve": curve,
        "associated_base_bonding_curve": associated_token_address(curve, mint, base_token_program),
        "associated_quote_bonding_curve": associated_token_address(curve, quote_mint, quote_prog),
        "user": user,
        "associated_base_user": associated_token_address(user, mint, base_token_program),
        "associated_quote_user": associated_token_address(user, quote_mint, quote_prog),
        "creator_vault": cvault,
        "associated_creator_vault": associated_token_address(cvault, quote_mint, quote_prog),
        "sharing_config": sharing_config_pda(mint),
        "global_volume_accumulator": global_volume_accumulator(),
        "user_volume_accumulator": uva,
        "associated_user_volume_accumulator": associated_token_address(uva, quote_mint, quote_prog),
        "fee_config": pump_fee_config(),
        "fee_program": PUMP_FEE_PROGRAM_ID,
        "system_program": SYSTEM_PROGRAM_ID,
        "event_authority": pump_event_authority(),
        "program": PUMP_PROGRAM_ID,
    }


def curve_buy_instructions(
    user: Pubkey,
    mint: Pubkey,
    creator: Pubkey,
    base_token_program: Pubkey,
    spend_lamports: int,
    min_tokens_out: int,
    glob: PumpGlobal,
    mayhem: bool = False,
) -> list[Instruction]:
    accts = _curve_accounts(user, mint, creator, base_token_program, glob, mayhem)
    data = struct.pack("<QQ", spend_lamports, min_tokens_out)
    return [
        create_ata_idempotent(user, user, mint, base_token_program),
        _build("pump", PUMP_PROGRAM_ID, "buy_exact_quote_in_v2", accts, data),
    ]


def curve_sell_instructions(
    user: Pubkey,
    mint: Pubkey,
    creator: Pubkey,
    base_token_program: Pubkey,
    tokens: int,
    min_sol_out: int,
    glob: PumpGlobal,
    close_token_account: bool,
    mayhem: bool = False,
) -> list[Instruction]:
    accts = _curve_accounts(user, mint, creator, base_token_program, glob, mayhem)
    ixs = [_build("pump", PUMP_PROGRAM_ID, "sell_v2", accts, struct.pack("<QQ", tokens, min_sol_out))]
    if close_token_account:
        ixs.append(
            close_account(associated_token_address(user, mint, base_token_program), user, user, base_token_program)
        )
    return ixs


# ---- PumpSwap -----------------------------------------------------------------------------------------------
def _amm_accounts(
    user: Pubkey, pool: PoolAccounts, base_token_program: Pubkey, gc: AmmGlobalConfig
) -> dict[str, Pubkey]:
    quote_prog = TOKEN_PROGRAM_ID
    recips = gc.reserved_fee_recipients if pool.is_mayhem_mode else gc.protocol_fee_recipients
    if not recips:
        raise InstructionBuildError("no protocol fee recipients")
    proto = random.choice(recips)
    cva = coin_creator_vault_authority(pool.coin_creator)
    return {
        "pool": pool.pool,
        "user": user,
        "global_config": amm_global_config(),
        "base_mint": pool.base_mint,
        "quote_mint": pool.quote_mint,
        "user_base_token_account": associated_token_address(user, pool.base_mint, base_token_program),
        "user_quote_token_account": associated_token_address(user, pool.quote_mint, quote_prog),
        "pool_base_token_account": pool.pool_base_token_account,
        "pool_quote_token_account": pool.pool_quote_token_account,
        "protocol_fee_recipient": proto,
        "protocol_fee_recipient_token_account": associated_token_address(proto, pool.quote_mint, quote_prog),
        "base_token_program": base_token_program,
        "quote_token_program": quote_prog,
        "system_program": SYSTEM_PROGRAM_ID,
        "associated_token_program": ASSOCIATED_TOKEN_PROGRAM_ID,
        "event_authority": amm_event_authority(),
        "program": PUMP_AMM_PROGRAM_ID,
        "coin_creator_vault_ata": associated_token_address(cva, pool.quote_mint, quote_prog),
        "coin_creator_vault_authority": cva,
        "global_volume_accumulator": amm_global_volume_accumulator(),
        "user_volume_accumulator": amm_user_volume_accumulator(user),
        "fee_config": amm_fee_config(),
        "fee_program": PUMP_FEE_PROGRAM_ID,
    }


def _amm_remaining(user: Pubkey, pool: PoolAccounts, gc: AmmGlobalConfig, is_sell: bool) -> list[AccountMeta]:
    rem: list[AccountMeta] = []
    if pool.is_cashback_coin:
        uva = amm_user_volume_accumulator(user)
        rem.append(AccountMeta(associated_token_address(uva, pool.quote_mint, TOKEN_PROGRAM_ID), False, True))
        if is_sell:
            rem.append(AccountMeta(uva, False, True))
    if pool.coin_creator != _DEFAULT:
        rem.append(AccountMeta(pool_v2_pda(pool.base_mint), False, False))
    if not gc.buyback_fee_recipients:
        raise InstructionBuildError("no buyback fee recipients")
    bb = random.choice(gc.buyback_fee_recipients)
    rem.append(AccountMeta(bb, False, False))
    rem.append(AccountMeta(associated_token_address(bb, pool.quote_mint, TOKEN_PROGRAM_ID), False, True))
    return rem


def amm_buy_instructions(
    user: Pubkey,
    pool: PoolAccounts,
    base_token_program: Pubkey,
    spend_lamports: int,
    min_base_out: int,
    gc: AmmGlobalConfig,
) -> list[Instruction]:
    if pool.quote_mint != WSOL_MINT:
        raise InstructionBuildError("only SOL-quoted pools are supported")
    wsol_ata = associated_token_address(user, WSOL_MINT, TOKEN_PROGRAM_ID)
    accts = _amm_accounts(user, pool, base_token_program, gc)
    data = struct.pack("<QQ", spend_lamports, min_base_out) + b"\x01"  # OptionBool(track_volume=true)
    return [
        create_ata_idempotent(user, user, WSOL_MINT, TOKEN_PROGRAM_ID),
        system_transfer(user, wsol_ata, spend_lamports),
        sync_native(wsol_ata),
        create_ata_idempotent(user, user, pool.base_mint, base_token_program),
        _build("amm", PUMP_AMM_PROGRAM_ID, "buy_exact_quote_in", accts, data, _amm_remaining(user, pool, gc, False)),
        close_account(wsol_ata, user, user, TOKEN_PROGRAM_ID),
    ]


def amm_sell_instructions(
    user: Pubkey,
    pool: PoolAccounts,
    base_token_program: Pubkey,
    base_in: int,
    min_quote_out: int,
    gc: AmmGlobalConfig,
    close_token_account: bool,
) -> list[Instruction]:
    if pool.quote_mint != WSOL_MINT:
        raise InstructionBuildError("only SOL-quoted pools are supported")
    wsol_ata = associated_token_address(user, WSOL_MINT, TOKEN_PROGRAM_ID)
    accts = _amm_accounts(user, pool, base_token_program, gc)
    ixs = [
        create_ata_idempotent(user, user, WSOL_MINT, TOKEN_PROGRAM_ID),
        _build(
            "amm",
            PUMP_AMM_PROGRAM_ID,
            "sell",
            accts,
            struct.pack("<QQ", base_in, min_quote_out),
            _amm_remaining(user, pool, gc, True),
        ),
        close_account(wsol_ata, user, user, TOKEN_PROGRAM_ID),
    ]
    if close_token_account:
        ixs.append(
            close_account(
                associated_token_address(user, pool.base_mint, base_token_program), user, user, base_token_program
            )
        )
    return ixs
