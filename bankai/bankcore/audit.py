"""Append-only, hash-chained audit log: every tool call (allowed, denied or failed) is recorded by the
tool layer itself. Each row commits to its predecessor, so deleting or editing a row is detectable."""
from __future__ import annotations

import threading

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..common import canonical_json, clock, sha256_hex
from ..db import core_audit
from ..pii import Redactor

_GENESIS = "0" * 64
_lock = threading.Lock()


class AuditLog:
    def __init__(self, engine: Engine, redactor: Redactor):
        self.engine, self.redactor = engine, redactor

    def record(
        self, *, actor: str, tool: str, decision: str, reason: str = "", agent: str = "", trace_id: str = "",
        args: dict | None = None,
    ) -> int:
        safe_args = canonical_json(self.redactor.mask_obj(args or {}))
        ts = clock.now()
        with _lock, self.engine.begin() as conn:
            prev = conn.execute(select(core_audit.c.hash).order_by(core_audit.c.id.desc()).limit(1)).scalar()
            prev = prev or _GENESIS
            body = {"ts": ts, "trace": trace_id, "actor": actor, "agent": agent, "tool": tool,
                    "decision": decision, "reason": reason, "args": safe_args}
            h = sha256_hex(prev + canonical_json(body))
            res = conn.execute(core_audit.insert().values(
                ts=ts, trace_id=trace_id, actor=actor, agent=agent, tool=tool, decision=decision,
                reason=reason, args_json=safe_args, prev_hash=prev, hash=h))
            return res.inserted_primary_key[0]

    def verify_chain(self) -> tuple[bool, int, str]:
        """Returns (ok, rows_checked, message). Recomputes every hash from the stored fields."""
        prev = _GENESIS
        with self.engine.connect() as conn:
            rows = conn.execute(select(core_audit).order_by(core_audit.c.id)).mappings().all()
        for r in rows:
            body = {"ts": r["ts"], "trace": r["trace_id"], "actor": r["actor"], "agent": r["agent"], "tool": r["tool"],
                    "decision": r["decision"], "reason": r["reason"], "args": r["args_json"]}
            if r["prev_hash"] != prev or r["hash"] != sha256_hex(prev + canonical_json(body)):
                return False, len(rows), f"chain broken at audit row {r['id']}"
            prev = r["hash"]
        return True, len(rows), "ok"

    def recent(self, limit: int = 50) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(core_audit).order_by(core_audit.c.id.desc()).limit(limit)).mappings().all()
        return [dict(r) for r in rows]
