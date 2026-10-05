"""Untrusted-text handling (token name/symbol/uri, descriptions, social text, external API strings).

These strings are attacker-controlled. They are:
  * never interpolated into instructions — only placed in clearly-labelled data fields;
  * stripped of control / zero-width / bidi characters and truncated;
  * scanned for instruction-like content, which becomes a deterministic risk feature.
"""

from __future__ import annotations

import re
import unicodedata

_INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompts?|rules)",
    r"disregard (all |the )?(previous|prior|above|system)",
    r"\bsystem prompt\b",
    r"\byou are (now )?(an?|the) ",
    r"\b(buy|ape|purchase) (this|now|immediately)\b",
    r"\bassessment\b.*\bpositive\b",
    r"\bconfidence\b\s*[:=]",
    r"</?(system|assistant|user|instructions?)>",
    r"\b(approve|execute) (the )?(trade|transaction|order)\b",
    r"\bset (the )?(score|confidence|risk)\b",
    r"\bnew instructions?\b",
    r"\bdeveloper mode\b",
    r"\bjailbreak\b",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)
_STRIP_CATEGORIES = {"Cc", "Cf", "Co", "Cs"}  # control, format (zero-width, bidi), private use, surrogates


def clean_untrusted(text: str | None, max_chars: int = 200) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in _STRIP_CATEGORIES)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


def looks_like_injection(*texts: str | None) -> bool:
    joined = " ".join(unicodedata.normalize("NFKC", t) for t in texts if t)
    return bool(_INJECTION_RE.search(joined))
