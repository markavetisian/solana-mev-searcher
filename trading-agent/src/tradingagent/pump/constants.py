"""Program IDs and PDA derivations (seeds from pump-public-docs / @pump-fun/pump-sdk 2.0.0 / pump-swap-sdk 1.20.0)."""

from __future__ import annotations

from functools import lru_cache

from solders.pubkey import Pubkey

PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
PUMP_FEE_PROGRAM_ID = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
COMPUTE_BUDGET_PROGRAM_ID = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
DEFAULT_PUBKEY = Pubkey.default()

GLOBAL_ACCOUNT = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")


def _pda(seeds: list[bytes], program: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(seeds, program)[0]


def pump_pda(*seeds: bytes) -> Pubkey:
    return _pda(list(seeds), PUMP_PROGRAM_ID)


def amm_pda(*seeds: bytes) -> Pubkey:
    return _pda(list(seeds), PUMP_AMM_PROGRAM_ID)


def fee_pda(*seeds: bytes) -> Pubkey:
    return _pda(list(seeds), PUMP_FEE_PROGRAM_ID)


@lru_cache(maxsize=1)
def global_pda() -> Pubkey:
    return pump_pda(b"global")


@lru_cache(maxsize=1)
def pump_event_authority() -> Pubkey:
    return pump_pda(b"__event_authority")


@lru_cache(maxsize=1)
def amm_event_authority() -> Pubkey:
    return amm_pda(b"__event_authority")


@lru_cache(maxsize=1)
def global_volume_accumulator() -> Pubkey:
    return pump_pda(b"global_volume_accumulator")


@lru_cache(maxsize=1)
def amm_global_volume_accumulator() -> Pubkey:
    return amm_pda(b"global_volume_accumulator")


@lru_cache(maxsize=1)
def pump_fee_config() -> Pubkey:
    return fee_pda(b"fee_config", bytes(PUMP_PROGRAM_ID))


@lru_cache(maxsize=1)
def amm_fee_config() -> Pubkey:
    return fee_pda(b"fee_config", bytes(PUMP_AMM_PROGRAM_ID))


@lru_cache(maxsize=1)
def amm_global_config() -> Pubkey:
    return amm_pda(b"global_config")


@lru_cache(maxsize=65536)
def bonding_curve_pda(mint: Pubkey) -> Pubkey:
    return pump_pda(b"bonding-curve", bytes(mint))


def creator_vault_pda(creator: Pubkey) -> Pubkey:
    return pump_pda(b"creator-vault", bytes(creator))


def user_volume_accumulator(user: Pubkey) -> Pubkey:
    return pump_pda(b"user_volume_accumulator", bytes(user))


def amm_user_volume_accumulator(user: Pubkey) -> Pubkey:
    return amm_pda(b"user_volume_accumulator", bytes(user))


def sharing_config_pda(mint: Pubkey) -> Pubkey:
    return fee_pda(b"sharing-config", bytes(mint))


def pool_authority_pda(mint: Pubkey) -> Pubkey:
    return pump_pda(b"pool-authority", bytes(mint))


def pool_pda(index: int, owner: Pubkey, base_mint: Pubkey, quote_mint: Pubkey) -> Pubkey:
    return amm_pda(b"pool", index.to_bytes(2, "little"), bytes(owner), bytes(base_mint), bytes(quote_mint))


@lru_cache(maxsize=65536)
def canonical_pool_pda(mint: Pubkey, quote_mint: Pubkey = WSOL_MINT) -> Pubkey:
    return pool_pda(0, pool_authority_pda(mint), mint, quote_mint)


def pool_v2_pda(base_mint: Pubkey) -> Pubkey:
    return amm_pda(b"pool-v2", bytes(base_mint))


def coin_creator_vault_authority(coin_creator: Pubkey) -> Pubkey:
    return amm_pda(b"creator_vault", bytes(coin_creator))


def associated_token_address(owner: Pubkey, mint: Pubkey, token_program: Pubkey = TOKEN_PROGRAM_ID) -> Pubkey:
    return _pda([bytes(owner), bytes(token_program), bytes(mint)], ASSOCIATED_TOKEN_PROGRAM_ID)


def is_canonical_pump_pool(base_mint: Pubkey, pool_creator: Pubkey) -> bool:
    return pool_authority_pda(base_mint) == pool_creator
