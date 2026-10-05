"""Strict schema for AI output. The AI returns an assessment — never an action, size, price or instruction."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MAX_ITEMS = 8
_MAX_ITEM_LEN = 240


class AIAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment: Literal["positive", "neutral", "negative"]
    confidence: float = Field(ge=0.0, le=1.0)
    risk_flags: list[str] = Field(default_factory=list, max_length=_MAX_ITEMS)
    observations: list[str] = Field(default_factory=list, max_length=_MAX_ITEMS)
    thesis: str = Field(default="", max_length=800)
    contradictions: list[str] = Field(default_factory=list, max_length=_MAX_ITEMS)

    @field_validator("risk_flags", "observations", "contradictions")
    @classmethod
    def _short_items(cls, v: list[str]) -> list[str]:
        return [s.strip()[:_MAX_ITEM_LEN] for s in v if isinstance(s, str) and s.strip()]


def json_schema() -> dict:
    """JSON schema for the Messages API structured-output constraint (all fields required, no extras)."""
    str_list = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "assessment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
            "confidence": {"type": "number"},
            "risk_flags": str_list,
            "observations": str_list,
            "thesis": {"type": "string"},
            "contradictions": str_list,
        },
        "required": ["assessment", "confidence", "risk_flags", "observations", "thesis", "contradictions"],
        "additionalProperties": False,
    }
