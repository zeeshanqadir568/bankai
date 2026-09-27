"""Cost tracking and runaway-loop protection.

Two different failures both look like "the bill tripled":
  * a loop  (agent retries the same failing tool call, or delegates back and forth)  -> caught by iteration caps, the
            identical-call detector and the delegation-depth cap, usually long before any money is spent
  * a slow leak (many legitimate-looking calls, each a bit bigger)                   -> caught by the per-request token/cost
            budget and the per-user daily cap
Every limit raises BudgetExceeded carrying a human-readable message, so the customer gets an explanation and a
handoff instead of a 500 or a silent hang.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from .common import clock
from .config import Settings
from .db import app_usage
from .llm.base import LLMResponse


class BudgetExceeded(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail

    @property
    def user_message(self) -> str:
        return {
            "iterations": "I wasn't able to finish that in a reasonable number of steps, so I've stopped to avoid repeating myself.",
            "loop": "I noticed I was repeating the same action without making progress, so I've stopped.",
            "tokens": "This request needed more processing than I'm allowed to use in one go, so I've stopped.",
            "cost": "This request hit its processing budget, so I've stopped.",
            "depth": "This request required too many nested hand-offs between my specialists, so I've stopped.",
            "llm_calls": "This request required too many steps, so I've stopped.",
            "daily_cost": "You've reached today's usage limit for the assistant.",
        }.get(self.kind, "I've hit a safety limit and stopped.") + " A member of our team can pick this up — just say \"talk to a human\"."


@dataclass
class RequestBudget:
    """One per user turn. Shared by the coordinator and every sub-agent it spawns."""

    max_iterations: int
    max_tokens: int
    max_cost: float
    max_depth: int
    max_identical: int
    max_llm_calls: int = 24
    tokens: int = 0
    cost: float = 0.0
    llm_calls: int = 0
    depth: int = 0
    seen: Counter = field(default_factory=Counter)
    stopped: str = ""

    @classmethod
    def from_settings(cls, s: Settings) -> "RequestBudget":
        return cls(s.max_iterations, s.max_request_tokens, s.max_request_cost_usd, s.max_delegation_depth, s.max_identical_calls,
                   max_llm_calls=s.max_llm_calls_per_request)

    def check_iteration(self, i: int) -> None:
        if i >= self.max_iterations:
            self._stop("iterations", f"agent loop reached {self.max_iterations} steps")

    def charge(self, resp: LLMResponse) -> None:
        self.llm_calls += 1
        self.tokens += resp.usage.tokens_in + resp.usage.tokens_out
        self.cost += resp.cost_usd
        if self.llm_calls > self.max_llm_calls:
            self._stop("llm_calls", f"{self.llm_calls} LLM calls in one request")
        if self.tokens > self.max_tokens:
            self._stop("tokens", f"{self.tokens} tokens > {self.max_tokens}")
        if self.cost > self.max_cost:
            self._stop("cost", f"${self.cost:.4f} > ${self.max_cost:.2f}")

    def enter_delegation(self) -> None:
        if self.depth + 1 > self.max_depth:
            self._stop("depth", f"delegation depth would exceed {self.max_depth}")
        self.depth += 1

    def leave_delegation(self) -> None:
        self.depth -= 1

    def note_tool_call(self, fq: str, args: dict) -> None:
        """Identical (tool, args) repeated N times in one request == a loop, whatever the model believes."""
        key = fq + hashlib.sha1(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:12]
        self.seen[key] += 1
        if self.seen[key] > self.max_identical:
            self._stop("loop", f"{fq} called {self.seen[key]} times with identical arguments")

    def _stop(self, kind: str, detail: str) -> None:
        self.stopped = kind
        raise BudgetExceeded(kind, detail)


class CostTracker:
    """Persistent per-user / per-session / per-agent usage ledger."""

    def __init__(self, engine: Engine, settings: Settings):
        self.engine, self.s = engine, settings

    def record(self, *, user_id: str, session_id: str, agent: str, resp: LLMResponse, trace_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(app_usage.insert().values(
                ts=clock.now(), user_id=user_id, session_id=session_id, agent=agent, provider=resp.provider, model=resp.model,
                tokens_in=resp.usage.tokens_in, tokens_out=resp.usage.tokens_out, cost_usd=resp.cost_usd, trace_id=trace_id))

    def spent_since(self, user_id: str, seconds: float = 86400) -> float:
        with self.engine.connect() as conn:
            return float(conn.execute(select(func.coalesce(func.sum(app_usage.c.cost_usd), 0.0)).where(
                app_usage.c.user_id == user_id, app_usage.c.ts >= clock.now() - seconds)).scalar() or 0.0)

    def check_daily(self, user_id: str) -> None:
        spent = self.spent_since(user_id)
        if spent >= self.s.user_daily_cost_usd:
            raise BudgetExceeded("daily_cost", f"${spent:.2f} spent in 24h")

    def report(self, by: str = "agent", user_id: str | None = None, session_id: str | None = None) -> list[dict]:
        col = {"agent": app_usage.c.agent, "user": app_usage.c.user_id, "session": app_usage.c.session_id,
               "provider": app_usage.c.provider}[by]
        q = select(col.label("key"), func.count().label("calls"), func.sum(app_usage.c.tokens_in + app_usage.c.tokens_out).label("tokens"),
                   func.sum(app_usage.c.cost_usd).label("cost_usd")).group_by(col).order_by(func.sum(app_usage.c.cost_usd).desc())
        if user_id:
            q = q.where(app_usage.c.user_id == user_id)
        if session_id:
            q = q.where(app_usage.c.session_id == session_id)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]
