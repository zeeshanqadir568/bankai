"""Guardrail layer 1 — input sanitisation and content isolation.

Everything a tool returns is treated as UNTRUSTED DATA: normalised, stripped of invisible/control characters,
scanned for instruction-like text (which is removed and reported), and wrapped in <untrusted_data> delimiters that
the system prompt tells the model never to obey.

Honest limits: this is a heuristic, best-effort layer. A determined attacker can phrase around any regex, and a model
can ignore delimiters. That is why it is layer 1 of 4 and the system's safety does NOT depend on it — layers 2 (tool-side
authorization / policy) and 3 (action guard) hold even against a fully compromised model.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [(n, re.compile(p, re.I | re.S)) for n, p in [
    ("override_instructions", r"(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+)*(?:previous|prior|above|earlier|system|original)\s+(?:instructions?|prompts?|rules?|directives?)"),
    ("role_hijack", r"\byou\s+are\s+now\b|\bnew\s+instructions?\s*:|\bact\s+as\b.{0,20}\b(?:admin|system|developer)\b"),
    ("fake_system_turn", r"(?:^|\n|\.\s)\s*(?:system|assistant|developer)\s*:"),
    ("concealment", r"do\s+not\s+(?:tell|inform|notify|alert)\s+the\s+(?:user|customer)"),
    ("money_movement_directive", r"(?:transfer|send|wire|move|pay)\s+(?:all\s+(?:the\s+|of\s+the\s+)?)?(?:funds|money|balance|savings)\b.{0,60}?\baccount(?:\s+(?:number\s+)?[\d*-]{4,20})?"),
    ("prompt_exfiltration", r"(?:reveal|print|show|repeat)\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions)"),
    ("delimiter_spoof", r"</?\s*(?:untrusted_data|shared_state|system|assistant)\b[^>]*>"),
]]
_INVISIBLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
REMOVED = "[removed: suspected instruction]"


@dataclass
class Sanitized:
    obj: Any
    findings: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.findings)


def _clean_str(s: str, max_len: int, findings: list[str]) -> str:
    s = unicodedata.normalize("NFKC", s)  # folds full-width / compatibility look-alikes before matching
    s = _INVISIBLE.sub("", s)
    if len(s) > max_len:
        s = s[:max_len] + "…[truncated]"
    for name, pat in INJECTION_PATTERNS:
        if pat.search(s):
            findings.append(name)
            s = pat.sub(REMOVED, s)
    return s


def sanitize_obj(obj: Any, *, max_len: int = 2000) -> Sanitized:
    """Sanitise every string value of a JSON-like object, keeping the structure (and JSON validity) intact."""
    findings: list[str] = []

    def walk(o: Any) -> Any:
        if isinstance(o, str):
            return _clean_str(o, max_len, findings)
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o

    return Sanitized(walk(obj), sorted(set(findings)))


def sanitize_text(text: str, max_len: int = 4000) -> Sanitized:
    findings: list[str] = []
    return Sanitized(_clean_str(text, max_len, findings), sorted(set(findings)))


def wrap_untrusted(source: str, obj: Any, findings: list[str] | None = None) -> str:
    """What the model actually sees for a tool result."""
    body = json.dumps(obj, ensure_ascii=False, default=str)
    flag = f' flagged="{",".join(findings)}"' if findings else ""
    return f'<untrusted_data source="{source}"{flag}>\n{body}\n</untrusted_data>'
