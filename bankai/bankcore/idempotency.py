"""Idempotency for every mutating operation.

(scope, key) is a primary key. The operation and the idempotency record commit in ONE transaction, so
either both exist or neither does. Same key + same payload -> the stored response is replayed and the
side effect is NOT repeated. Same key + different payload -> conflict (a retry bug or an attack).
"""
from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from ..common import canonical_json, clock, sha256_hex
from ..db import core_idempotency


class IdempotencyConflict(Exception):
    pass


def run_idempotent(
    engine: Engine, scope: str, key: str, payload: Any, fn: Callable[[Connection], dict],
) -> tuple[dict, bool]:
    """Returns (response, replayed)."""
    req_hash = sha256_hex(canonical_json(payload))
    for _ in range(2):  # 2nd pass only if we lost a race to an identical concurrent request
        try:
            with engine.begin() as conn:
                row = conn.execute(select(core_idempotency).where(
                    core_idempotency.c.scope == scope, core_idempotency.c.key == key)).mappings().first()
                if row:
                    if row["request_hash"] != req_hash:
                        raise IdempotencyConflict("idempotency key reused with a different request")
                    return json.loads(row["response_json"]), True
                response = fn(conn)
                conn.execute(core_idempotency.insert().values(
                    scope=scope, key=key, request_hash=req_hash, response_json=canonical_json(response), ts=clock.now()))
                return response, False
        except IntegrityError:
            continue
    raise IdempotencyConflict("concurrent duplicate request could not be resolved")
