"""Paused runs: a transfer waiting for staff approval leaves a durable record so the conversation can resume
(in any worker process, after restarts) when the decision event arrives."""
from __future__ import annotations

import json

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from ..common import clock
from ..db import app_paused_runs
from ..security.identity import Identity


class PauseStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def save(self, approval_id: str, session_id: str, identity: Identity, agent: str, extra: dict) -> None:
        state = {"identity": identity.to_claims(), **extra}
        with self.engine.begin() as conn:
            conn.execute(delete(app_paused_runs).where(app_paused_runs.c.approval_id == approval_id))
            conn.execute(app_paused_runs.insert().values(
                approval_id=approval_id, session_id=session_id, user_id=identity.user_id, agent=agent,
                state_json=json.dumps(state), created_ts=clock.now()))

    def pop(self, approval_id: str) -> dict | None:
        """Atomically claim the paused run (delete-returning), so a redelivered event cannot resume it twice."""
        with self.engine.begin() as conn:
            row = conn.execute(select(app_paused_runs).where(app_paused_runs.c.approval_id == approval_id)).mappings().first()
            if not row:
                return None
            res = conn.execute(delete(app_paused_runs).where(app_paused_runs.c.approval_id == approval_id))
            if res.rowcount != 1:
                return None
        return {**dict(row), "state": json.loads(row["state_json"])}
