"""JSON-RPC client over multiple providers with health tracking, circuit breaking and failover.

Read calls fail over to the next healthy endpoint. `sendTransaction` is NOT blindly retried across endpoints by
this layer — the transaction engine decides (re-broadcasting the *same signed bytes* is safe, building a new
transaction is not).
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from tradingagent.common.logging import get_logger, register_secret
from tradingagent.common.metrics import METRICS

log = get_logger("solana.rpc")


class RpcError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, data: Any = None, endpoint: str | None = None):
        super().__init__(message)
        self.code, self.data, self.endpoint = code, data, endpoint


class RpcUnavailable(RpcError):
    """No healthy endpoint could serve the request."""


# JSON-RPC codes that mean "this node can't answer right now" (behind, pruned, min-context-slot) => fail over.
_NODE_LAG_CODES = {-32004, -32005, -32007, -32014, -32016}


def _retryable(code: int | None) -> bool:
    return code is None or code == 429 or code >= 500 or code in _NODE_LAG_CODES


@dataclass
class EndpointHealth:
    url: str
    label: str
    latency_ewma_ms: float = 0.0
    consecutive_errors: int = 0
    total_errors: int = 0
    total_calls: int = 0
    open_until: float = 0.0
    last_ok: float = 0.0
    last_slot: int = 0
    recent: list[float] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return time.monotonic() < self.open_until

    def snapshot(self) -> dict:
        return {
            "label": self.label,
            "latency_ewma_ms": round(self.latency_ewma_ms, 1),
            "consecutive_errors": self.consecutive_errors,
            "error_rate": round(self.total_errors / self.total_calls, 4) if self.total_calls else None,
            "circuit_open": self.is_open,
            "last_slot": self.last_slot,
        }


class RpcPool:
    def __init__(
        self,
        urls: list[str],
        timeout_s: float = 8.0,
        retries: int = 2,
        circuit_open_after: int = 5,
        circuit_cooldown_s: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not urls:
            raise ValueError("RpcPool needs at least one RPC URL")
        for u in urls:
            register_secret(u)  # provider URLs usually embed API keys
        self.endpoints = [EndpointHealth(url=u, label=f"rpc{i}") for i, u in enumerate(urls)]
        self.timeout_s, self.retries = timeout_s, retries
        self.circuit_open_after, self.circuit_cooldown_s = circuit_open_after, circuit_cooldown_s
        self._client = client or httpx.AsyncClient(timeout=timeout_s, http2=False)
        self._ids = itertools.count(1)

    async def close(self) -> None:
        await self._client.aclose()

    def ordered(self) -> list[EndpointHealth]:
        healthy = [e for e in self.endpoints if not e.is_open]
        # prefer fewer consecutive errors, then lower latency; keep configured priority as tiebreaker
        return sorted(healthy, key=lambda e: (e.consecutive_errors, e.latency_ewma_ms // 50))

    @property
    def healthy(self) -> bool:
        return any(not e.is_open for e in self.endpoints)

    def health(self) -> list[dict]:
        return [e.snapshot() for e in self.endpoints]

    def _record(self, ep: EndpointHealth, ok: bool, latency_ms: float) -> None:
        ep.total_calls += 1
        if ok:
            ep.consecutive_errors = 0
            ep.last_ok = time.monotonic()
            ep.latency_ewma_ms = latency_ms if ep.latency_ewma_ms == 0 else 0.8 * ep.latency_ewma_ms + 0.2 * latency_ms
        else:
            ep.consecutive_errors += 1
            ep.total_errors += 1
            if ep.consecutive_errors >= self.circuit_open_after:
                ep.open_until = time.monotonic() + self.circuit_cooldown_s
                log.warning("rpc_circuit_open", endpoint=ep.label, cooldown_s=self.circuit_cooldown_s)
                METRICS.inc("rpc.circuit_open")
        METRICS.observe_ms(f"rpc.latency.{ep.label}", latency_ms)

    async def _call_one(self, ep: EndpointHealth, method: str, params: list) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        t0 = time.perf_counter()
        try:
            resp = await self._client.post(ep.url, json=body, timeout=self.timeout_s)
            latency = (time.perf_counter() - t0) * 1000
            if resp.status_code == 429 or resp.status_code >= 500:
                self._record(ep, False, latency)
                raise RpcError(f"HTTP {resp.status_code}", code=resp.status_code, endpoint=ep.label)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            self._record(ep, False, (time.perf_counter() - t0) * 1000)
            raise RpcError(f"{type(e).__name__}: {e}", endpoint=ep.label) from e
        if "error" in data:
            # A JSON-RPC error is an answer, not an endpoint failure (e.g. simulation errors).
            self._record(ep, True, latency)
            err = data["error"]
            raise RpcError(
                err.get("message", "rpc error"), code=err.get("code"), data=err.get("data"), endpoint=ep.label
            )
        self._record(ep, True, latency)
        METRICS.observe_ms("rpc.latency", latency)
        return data.get("result")

    async def call(self, method: str, params: list | None = None, *, failover: bool = True) -> Any:
        params = params or []
        last: Exception | None = None
        candidates = self.ordered() if failover else self.ordered()[:1]
        if not candidates:
            METRICS.inc("rpc.unavailable")
            raise RpcUnavailable("all RPC endpoints are circuit-open")
        attempts = 0
        for ep in candidates:
            for _ in range(self.retries if failover else 1):
                attempts += 1
                try:
                    return await self._call_one(ep, method, params)
                except RpcError as e:
                    if not _retryable(e.code):
                        raise  # application-level error: retrying elsewhere won't help
                    last = e
                    await asyncio.sleep(min(0.1 * attempts, 1.0))
        METRICS.inc("rpc.unavailable")
        raise RpcUnavailable(f"{method} failed on all endpoints: {last}")

    # ---- typed helpers ---------------------------------------------------------------------------------
    async def get_slot(self, commitment: str = "confirmed") -> int:
        return int(await self.call("getSlot", [{"commitment": commitment}]))

    async def get_account_info(self, pubkey: str, commitment: str = "confirmed") -> dict | None:
        res = await self.call("getAccountInfo", [pubkey, {"encoding": "base64", "commitment": commitment}])
        return res.get("value") if res else None

    async def get_multiple_accounts(self, pubkeys: list[str], commitment: str = "confirmed") -> list[dict | None]:
        res = await self.call("getMultipleAccounts", [pubkeys, {"encoding": "base64", "commitment": commitment}])
        return res.get("value") or []

    async def get_balance(self, pubkey: str, commitment: str = "confirmed") -> int:
        res = await self.call("getBalance", [pubkey, {"commitment": commitment}])
        return int(res["value"])

    async def get_token_account_balance(self, pubkey: str) -> int:
        res = await self.call("getTokenAccountBalance", [pubkey, {"commitment": "confirmed"}])
        return int(res["value"]["amount"])

    async def get_token_largest_accounts(self, mint: str) -> list[dict]:
        res = await self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        return res.get("value") or []

    async def get_latest_blockhash(self, commitment: str = "confirmed") -> tuple[str, int]:
        res = await self.call("getLatestBlockhash", [{"commitment": commitment}])
        v = res["value"]
        return v["blockhash"], int(v["lastValidBlockHeight"])

    async def get_block_height(self, commitment: str = "confirmed") -> int:
        return int(await self.call("getBlockHeight", [{"commitment": commitment}]))

    async def get_signature_statuses(self, signatures: list[str], search_history: bool = False) -> list[dict | None]:
        res = await self.call("getSignatureStatuses", [signatures, {"searchTransactionHistory": search_history}])
        return res.get("value") or []

    async def simulate_transaction(self, tx_b64: str, *, replace_blockhash: bool = False) -> dict:
        res = await self.call(
            "simulateTransaction",
            [
                tx_b64,
                {
                    "encoding": "base64",
                    "commitment": "confirmed",
                    "sigVerify": not replace_blockhash,
                    "replaceRecentBlockhash": replace_blockhash,
                },
            ],
        )
        return res.get("value") or {}

    async def send_transaction(
        self, tx_b64: str, *, skip_preflight: bool = True, endpoint_index: int | None = None
    ) -> str:
        params = [tx_b64, {"encoding": "base64", "skipPreflight": skip_preflight, "maxRetries": 0}]
        if endpoint_index is not None:
            ep = self.endpoints[endpoint_index]
            return str(await self._call_one(ep, "sendTransaction", params))
        return str(await self.call("sendTransaction", params, failover=False))

    async def get_recent_prioritization_fees(self, accounts: list[str]) -> list[int]:
        res = await self.call("getRecentPrioritizationFees", [accounts])
        return [int(r.get("prioritizationFee", 0)) for r in (res or [])]

    async def get_transaction(self, signature: str) -> dict | None:
        return await self.call(
            "getTransaction",
            [signature, {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
        )

    async def get_signatures_for_address(self, address: str, limit: int = 100, before: str | None = None) -> list[dict]:
        opts: dict[str, Any] = {"limit": limit, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        return await self.call("getSignaturesForAddress", [address, opts]) or []
