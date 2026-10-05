"""AI analyst clients. Every failure mode degrades to "no AI opinion" — the trading loop never blocks on the AI.

Results carry a status: OK | DISABLED | TIMEOUT | ERROR | MALFORMED | REFUSED | RATE_LIMITED.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ValidationError

from tradingagent.ai.prompt import PROMPT_VERSION, SYSTEM_PROMPT, render_user_message
from tradingagent.ai.schema import AIAssessment, json_schema
from tradingagent.common.config import AIConfig
from tradingagent.common.logging import get_logger
from tradingagent.common.metrics import METRICS

log = get_logger("ai.analyst")


@dataclass
class AIResult:
    status: str
    assessment: AIAssessment | None = None
    model: str | None = None
    prompt_version: str = PROMPT_VERSION
    latency_ms: float = 0.0
    error: str | None = None
    input_hash: str | None = None
    request_id: str | None = None
    cached: bool = False
    raw_excerpt: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "OK" and self.assessment is not None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
            "input_hash": self.input_hash,
            "request_id": self.request_id,
            "cached": self.cached,
            "assessment": self.assessment.model_dump() if self.assessment else None,
        }


def parse_assessment(text: str) -> AIAssessment:
    """Strictly parse model text into AIAssessment. Raises ValueError on anything malformed."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("AI output is not a JSON object")
    return AIAssessment.model_validate(data)


class Analyst(Protocol):
    async def analyze(self, payload: dict) -> AIResult: ...


class NullAnalyst:
    async def analyze(self, payload: dict) -> AIResult:
        return AIResult(status="DISABLED")


class StaticAnalyst:
    """Deterministic analyst for tests: returns fixed text (valid or not) after an optional delay."""

    def __init__(self, text: str, delay_s: float = 0.0) -> None:
        self.text, self.delay_s = text, delay_s

    async def analyze(self, payload: dict) -> AIResult:
        t0 = time.perf_counter()
        await asyncio.sleep(self.delay_s)
        try:
            a = parse_assessment(self.text)
            return AIResult(status="OK", assessment=a, model="static", latency_ms=(time.perf_counter() - t0) * 1000)
        except (ValueError, ValidationError) as e:
            return AIResult(status="MALFORMED", error=str(e)[:300], model="static")


class GuardedAnalyst:
    """Wraps any analyst with timeout, rate limit, cache and metrics."""

    def __init__(self, inner: Analyst, cfg: AIConfig) -> None:
        self.inner, self.cfg = inner, cfg
        self._calls: deque[float] = deque()
        self._cache: dict[str, tuple[float, AIResult]] = {}

    async def analyze(self, payload: dict) -> AIResult:
        if not self.cfg.enabled:
            return AIResult(status="DISABLED")
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]
        mint_key = str(payload.get("token"))
        hit = self._cache.get(mint_key)
        if hit and time.time() - hit[0] < self.cfg.cache_ttl_s:
            r = hit[1]
            return AIResult(**{**r.__dict__, "cached": True})
        now = time.time()
        while self._calls and self._calls[0] < now - 60:
            self._calls.popleft()
        if len(self._calls) >= self.cfg.max_calls_per_minute:
            METRICS.inc("ai.rate_limited")
            return AIResult(status="RATE_LIMITED", input_hash=key)
        self._calls.append(now)
        t0 = time.perf_counter()
        try:
            res = await asyncio.wait_for(self.inner.analyze(payload), timeout=self.cfg.timeout_s)
        except TimeoutError:
            METRICS.inc("ai.timeout")
            res = AIResult(status="TIMEOUT", error=f"no response within {self.cfg.timeout_s}s")
        except Exception as e:
            METRICS.inc("ai.error")
            res = AIResult(status="ERROR", error=f"{type(e).__name__}: {str(e)[:200]}")
        res.latency_ms = (time.perf_counter() - t0) * 1000
        res.input_hash = key
        METRICS.observe_ms("ai.latency", res.latency_ms)
        METRICS.inc(f"ai.status.{res.status}")
        if res.status == "OK":
            self._cache[mint_key] = (time.time(), res)
            if len(self._cache) > 5000:
                self._cache.clear()
        return res


class AnthropicAnalyst:
    """Claude via the official Anthropic SDK with a JSON-schema structured-output constraint.

    Server-side refusal fallback ("default" routing) is enabled by default (ai.server_side_fallback) so a policy
    decline on the primary model is retried on a fallback model inside the same call; a final refusal is
    reported as status REFUSED and treated like any other unavailable AI result.
    """

    def __init__(self, cfg: AIConfig, api_key: str | None) -> None:
        import anthropic

        self.cfg = cfg
        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=cfg.timeout_s, max_retries=1)

    async def analyze(self, payload: dict) -> AIResult:
        a = self._anthropic
        kwargs: dict = dict(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": render_user_message(payload)}],
            output_config={"effort": self.cfg.effort, "format": {"type": "json_schema", "schema": json_schema()}},
        )
        try:
            if self.cfg.server_side_fallback:
                resp = await self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            else:
                resp = await self.client.messages.create(**kwargs)
        except a.RateLimitError as e:
            return AIResult(status="RATE_LIMITED", error=str(e)[:200], model=self.cfg.model)
        except a.APITimeoutError as e:
            return AIResult(status="TIMEOUT", error=str(e)[:200], model=self.cfg.model)
        except a.APIStatusError as e:
            return AIResult(status="ERROR", error=f"HTTP {e.status_code}: {str(e)[:200]}", model=self.cfg.model)
        except a.APIConnectionError as e:
            return AIResult(status="ERROR", error=f"connection: {str(e)[:200]}", model=self.cfg.model)
        rid = getattr(resp, "_request_id", None)
        if resp.stop_reason == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            return AIResult(status="REFUSED", error=f"refusal ({cat})", model=resp.model, request_id=rid)
        if resp.stop_reason == "max_tokens":
            return AIResult(status="MALFORMED", error="truncated at max_tokens", model=resp.model, request_id=rid)
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        try:
            assessment = parse_assessment(text)
        except (ValueError, ValidationError) as e:
            log.warning("ai_malformed_output", error=str(e)[:200])
            return AIResult(
                status="MALFORMED", error=str(e)[:300], model=resp.model, request_id=rid, raw_excerpt=text[:200]
            )
        return AIResult(status="OK", assessment=assessment, model=resp.model, request_id=rid)


def build_analyst(cfg: AIConfig, api_key: str | None) -> GuardedAnalyst:
    if not cfg.enabled or cfg.provider == "none":
        return GuardedAnalyst(NullAnalyst(), cfg)
    if not api_key:
        log.warning("ai_enabled_without_key", detail="ANTHROPIC_API_KEY missing; AI disabled")
        return GuardedAnalyst(NullAnalyst(), cfg)
    return GuardedAnalyst(AnthropicAnalyst(cfg, api_key), cfg)
