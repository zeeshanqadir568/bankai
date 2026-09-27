"""Small shared helpers: injectable clock, ids, hashing, canonical JSON, token estimate."""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Any


class _Clock:
    """Injectable clock so TTL / timeout / velocity logic is testable without sleeping."""

    def __init__(self) -> None:
        self._offset = 0.0

    def now(self) -> float:
        return time.time() + self._offset

    def advance(self, seconds: float) -> None:
        self._offset += seconds

    def reset(self) -> None:
        self._offset = 0.0


clock = _Clock()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def estimate_tokens(text: str) -> int:
    """~4 chars/token. Providers that report real usage override this."""
    return max(1, math.ceil(len(text) / 4))


def money(cents: int, currency: str = "USD") -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}" if currency == "USD" else f"{sign}{cents / 100:,.2f} {currency}"
