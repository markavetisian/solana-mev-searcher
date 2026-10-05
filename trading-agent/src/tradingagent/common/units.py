"""Unit constants. All on-chain amounts are integers in base units; floats only appear in analytics."""

LAMPORTS_PER_SOL = 1_000_000_000
PUMP_TOKEN_DECIMALS = 6
TOKEN_BASE_UNITS = 10**PUMP_TOKEN_DECIMALS
PUMP_TOTAL_SUPPLY = 1_000_000_000 * TOKEN_BASE_UNITS  # 1e15 base units

BASE_FEE_LAMPORTS_PER_SIGNATURE = 5_000
# Rent-exempt minimum for a 165-byte SPL token account. Refunded when the account is closed.
SPL_TOKEN_ACCOUNT_RENT_LAMPORTS = 2_039_280
# Token-2022 account with the ImmutableOwner extension (170 bytes), which create_v2 coins use.
TOKEN2022_ACCOUNT_RENT_LAMPORTS = 2_074_080
# Pump user_volume_accumulator (137 bytes), created once per wallet on first trade. Not refunded per trade.
USER_VOLUME_ACCUMULATOR_RENT_LAMPORTS = 1_844_400


def lamports_to_sol(lamports: int | float) -> float:
    return float(lamports) / LAMPORTS_PER_SOL


def sol_to_lamports(sol: float) -> int:
    return int(round(sol * LAMPORTS_PER_SOL))


def tokens_to_ui(base_units: int | float) -> float:
    return float(base_units) / TOKEN_BASE_UNITS
