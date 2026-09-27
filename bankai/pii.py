"""PII detection, reversible redaction (for external LLM calls) and irreversible masking (for logs/traces).

Detectors are a deterministic baseline: regex + Luhn + a dictionary of known customer names supplied by
the bank's directory. A production deployment adds an NER model (e.g. Presidio) *behind the same
interface*, and keeps this layer as the fail-closed verifier: after redaction we RE-SCAN the output and
refuse to send if anything still matches.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# Order == priority when spans overlap (earlier wins).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("CARD", re.compile(r"(?<![\d*])(?:\d[ -]?){12,18}\d(?!\d)")),  # Luhn-verified below
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("SSN", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")),
    ("PHONE", re.compile(r"(?<![\w*])\+\d[\d\s().-]{7,14}\d(?!\d)|(?<![\w*])\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")),
    ("ACCOUNT", re.compile(r"(?<![\d*])\d{10,16}(?!\d)")),
]
_PLACEHOLDER = re.compile(r"<([A-Z]+_\d+)>")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


@dataclass(frozen=True)
class PIIMatch:
    kind: str
    start: int
    end: int
    value: str


@dataclass
class RedactionState:
    """Per-request mapping so the same value always gets the same placeholder and can be restored."""

    forward: dict[str, str] = field(default_factory=dict)  # original -> placeholder
    reverse: dict[str, str] = field(default_factory=dict)  # placeholder -> original
    counters: dict[str, int] = field(default_factory=dict)

    def placeholder_for(self, kind: str, value: str) -> str:
        if value in self.forward:
            return self.forward[value]
        n = self.counters[kind] = self.counters.get(kind, 0) + 1
        ph = f"<{kind}_{n}>"
        self.forward[value] = ph
        self.reverse[ph] = value
        return ph

    @property
    def count(self) -> int:
        return len(self.forward)


class PIIDetector:
    def __init__(self, known_names: Iterable[str] = ()):
        self._names: set[str] = set()
        self._name_re: re.Pattern[str] | None = None
        self.add_names(known_names)

    def add_names(self, names: Iterable[str]) -> None:
        for n in names:
            n = n.strip()
            if n:
                self._names.add(n)
                self._names.update(t for t in n.split() if len(t) >= 3)
        if self._names:
            alt = "|".join(re.escape(t) for t in sorted(self._names, key=len, reverse=True))
            self._name_re = re.compile(rf"\b(?:{alt})\b", re.IGNORECASE)

    def find(self, text: str) -> list[PIIMatch]:
        if not text:
            return []
        cands: list[PIIMatch] = []
        for prio, (kind, pat) in enumerate(_PATTERNS):
            for m in pat.finditer(text):
                val = m.group(0)
                if kind == "CARD":
                    digits = re.sub(r"\D", "", val)
                    if not (13 <= len(digits) <= 19 and _luhn_ok(digits)):
                        continue
                cands.append(PIIMatch(kind, m.start(), m.end(), val))
        if self._name_re is not None:
            cands += [PIIMatch("PERSON", m.start(), m.end(), m.group(0)) for m in self._name_re.finditer(text)]
        # resolve overlaps: kind priority first, then longer span
        order = {k: i for i, (k, _) in enumerate(_PATTERNS)}
        order["PERSON"] = len(_PATTERNS)
        cands.sort(key=lambda c: (order[c.kind], -(c.end - c.start), c.start))
        taken: list[PIIMatch] = []
        for c in cands:
            if all(c.end <= t.start or c.start >= t.end for t in taken):
                taken.append(c)
        return sorted(taken, key=lambda c: c.start)

    def has_pii(self, text: str) -> bool:
        return bool(self.find(text))

    def scan_obj(self, obj: Any) -> list[PIIMatch]:
        out: list[PIIMatch] = []
        for s in _iter_strings(obj):
            out.extend(self.find(s))
        return out


def _iter_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_strings(k)
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_strings(v)


def _map_strings(obj: Any, fn):
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, dict):
        return {(_map_strings(k, fn) if isinstance(k, str) else k): _map_strings(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_map_strings(v, fn) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_map_strings(v, fn) for v in obj)
    return obj


class Redactor:
    def __init__(self, detector: PIIDetector):
        self.detector = detector

    # ---- reversible: external LLM calls -------------------------------------------------
    def redact(self, text: str, state: RedactionState) -> str:
        out, last = [], 0
        for m in self.detector.find(text):
            out.append(text[last:m.start])
            out.append(state.placeholder_for(m.kind, m.value))
            last = m.end
        out.append(text[last:])
        return "".join(out)

    def redact_obj(self, obj: Any, state: RedactionState) -> Any:
        return _map_strings(obj, lambda s: self.redact(s, state))

    @staticmethod
    def reinject(text: str, state: RedactionState) -> str:
        return _PLACEHOLDER.sub(lambda m: state.reverse.get(f"<{m.group(1)}>", m.group(0)), text)

    def reinject_obj(self, obj: Any, state: RedactionState) -> Any:
        return _map_strings(obj, lambda s: self.reinject(s, state))

    # ---- irreversible: logs, traces, eval reports ---------------------------------------
    def mask(self, text: str) -> str:
        out, last = [], 0
        for m in self.detector.find(text):
            out.append(text[last:m.start])
            out.append(_mask_value(m))
            last = m.end
        out.append(text[last:])
        return "".join(out)

    def mask_obj(self, obj: Any) -> Any:
        return _map_strings(obj, self.mask)


def _mask_value(m: PIIMatch) -> str:
    digits = re.sub(r"\D", "", m.value)
    if m.kind in ("ACCOUNT", "CARD", "SSN", "PHONE"):
        return f"****{digits[-4:]}"
    if m.kind == "EMAIL":
        user, _, dom = m.value.partition("@")
        return f"{user[:1]}***@{dom}"
    if m.kind == "IBAN":
        return f"{m.value[:2]}**…{m.value[-4:]}"
    return "[NAME]"


class StreamReinjector:
    """Restores placeholders in a token stream even when `<ACCOUNT_1>` is split across chunks."""

    def __init__(self, redactor: Redactor, state: RedactionState):
        self._r, self._s, self._buf = redactor, state, ""

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        cut = self._buf.rfind("<")
        if cut != -1 and ">" not in self._buf[cut:] and len(self._buf) - cut <= 24:
            ready, self._buf = self._buf[:cut], self._buf[cut:]
        else:
            ready, self._buf = self._buf, ""
        return self._r.reinject(ready, self._s)

    def flush(self) -> str:
        ready, self._buf = self._buf, ""
        return self._r.reinject(ready, self._s)
