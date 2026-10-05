"""Trading wallet loading and balance sanity checks.

Rules: a DEDICATED trading wallet only; key material comes from a 0600 keypair file (preferred) or an env var;
it is never logged, stored in the database, sent to the dashboard or included in any API response. Only the
public key leaves this module.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from solders.keypair import Keypair

from tradingagent.common.config import Secrets


class WalletError(RuntimeError):
    pass


def load_keypair(secrets: Secrets) -> Keypair:
    if secrets.wallet_keypair_path:
        p = Path(secrets.wallet_keypair_path)
        if not p.exists():
            raise WalletError("wallet keypair file not found")
        mode = stat.S_IMODE(os.stat(p).st_mode)
        if mode & 0o077:
            raise WalletError(f"wallet keypair file permissions too open ({oct(mode)}); chmod 600")
        try:
            data = json.loads(p.read_text())
            kp = Keypair.from_bytes(bytes(data))
        except (ValueError, TypeError) as e:
            raise WalletError("wallet keypair file is not a valid 64-byte JSON array") from e
        return kp
    if secrets.wallet_private_key_b58:
        try:
            return Keypair.from_base58_string(secrets.wallet_private_key_b58.get_secret_value())
        except ValueError as e:
            raise WalletError("WALLET_PRIVATE_KEY_B58 is not a valid base58 keypair") from e
    raise WalletError("no wallet configured")


def public_key_only(secrets: Secrets) -> str | None:
    try:
        return str(load_keypair(secrets).pubkey())
    except WalletError:
        return None


class BalanceGuard:
    """Compares on-chain SOL with what the portfolio believes. A mismatch beyond tolerance means something moved
    funds that this system did not account for (manual transfer, compromised key, accounting bug) => kill switch."""

    def __init__(self, tolerance_lamports: int, min_balance_lamports: int) -> None:
        self.tolerance, self.min_balance = tolerance_lamports, min_balance_lamports
        self.baseline_offset: int | None = None  # on-chain - portfolio cash at start (rent, dust...)

    def check(self, onchain_lamports: int, portfolio_cash_lamports: int, pending: bool) -> str | None:
        if onchain_lamports < self.min_balance:
            return f"wallet balance {onchain_lamports / 1e9:.4f} SOL below minimum {self.min_balance / 1e9:.4f}"
        if pending:
            return None  # transactions in flight: balances legitimately disagree
        diff = onchain_lamports - portfolio_cash_lamports
        if self.baseline_offset is None:
            self.baseline_offset = diff
            return None
        drift = diff - self.baseline_offset
        if abs(drift) > self.tolerance:
            return f"unexpected wallet balance change: {drift / 1e9:+.6f} SOL vs accounting"
        return None
