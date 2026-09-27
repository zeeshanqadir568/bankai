"""Session store — this is SOFTWARE engineering, not AI engineering.

Concerns here are the ones any web backend has: identity binding (a session belongs to exactly one user; another
user's token can never load it), sliding TTL + absolute lifetime, expiry/purge, atomic persistence, and optimistic
concurrency (two tabs racing on one session cannot silently overwrite each other's state).

What goes INTO the model's context (window size, summarisation, long-term memory) is a different problem — see memory.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import delete, select, update
from sqlalchemy.engine import Engine

from ..common import clock, new_id
from ..db import app_messages, app_sessions, app_summaries
from .blackboard import Blackboard

ABSOLUTE_LIFETIME_FACTOR = 16  # absolute cap = 16 x sliding TTL (8h at the default 30 min)


class SessionError(Exception):
    pass


class SessionNotFound(SessionError):
    pass


class SessionExpired(SessionError):
    pass


class SessionConflict(SessionError):
    pass


@dataclass
class Session:
    id: str
    user_id: str
    created_ts: float
    last_seen_ts: float
    expires_ts: float
    turn_count: int = 0
    blackboard: Blackboard = field(default_factory=Blackboard)
    _loaded_version: int = 0


class SessionStore:
    def __init__(self, engine: Engine, ttl_seconds: int):
        self.engine, self.ttl = engine, ttl_seconds

    def create(self, user_id: str) -> Session:
        now = clock.now()
        s = Session(new_id("ses"), user_id, now, now, now + self.ttl)
        with self.engine.begin() as conn:
            conn.execute(app_sessions.insert().values(
                id=s.id, user_id=user_id, created_ts=now, last_seen_ts=now, expires_ts=s.expires_ts,
                blackboard_json=s.blackboard.to_json(), turn_count=0))
        return s

    def get(self, session_id: str, user_id: str) -> Session:
        with self.engine.connect() as conn:
            r = conn.execute(select(app_sessions).where(app_sessions.c.id == session_id)).mappings().first()
        # Wrong owner is indistinguishable from "does not exist": no session-id probing.
        if not r or r["user_id"] != user_id:
            raise SessionNotFound("session not found")
        now = clock.now()
        if now > r["expires_ts"] or now > r["created_ts"] + self.ttl * ABSOLUTE_LIFETIME_FACTOR:
            raise SessionExpired("session expired")
        return Session(r["id"], r["user_id"], r["created_ts"], r["last_seen_ts"], r["expires_ts"], r["turn_count"],
                       Blackboard.from_json(r["blackboard_json"]), _loaded_version=r["turn_count"])

    def save(self, s: Session) -> None:
        """Persist blackboard + slide the TTL. Fails with SessionConflict if someone else saved first."""
        now = clock.now()
        with self.engine.begin() as conn:
            res = conn.execute(update(app_sessions).where(
                app_sessions.c.id == s.id, app_sessions.c.turn_count == s._loaded_version).values(
                blackboard_json=s.blackboard.to_json(), last_seen_ts=now, expires_ts=now + self.ttl,
                turn_count=s._loaded_version + 1))
            if res.rowcount != 1:
                raise SessionConflict("session was modified concurrently")
        s._loaded_version += 1
        s.turn_count, s.last_seen_ts, s.expires_ts = s._loaded_version, now, now + self.ttl

    def delete(self, session_id: str) -> None:
        with self.engine.begin() as conn:
            for t in (app_messages, app_summaries):
                conn.execute(delete(t).where(t.c.session_id == session_id))
            conn.execute(delete(app_sessions).where(app_sessions.c.id == session_id))

    def purge_expired(self) -> int:
        """Housekeeping job: drop expired sessions AND their transcripts (data minimisation)."""
        with self.engine.connect() as conn:
            ids = [r[0] for r in conn.execute(select(app_sessions.c.id).where(app_sessions.c.expires_ts < clock.now()))]
        for i in ids:
            self.delete(i)
        return len(ids)
