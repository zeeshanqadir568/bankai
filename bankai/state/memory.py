"""Conversation memory — this is AI engineering: deciding what the model gets to see and what it may remember.

Short-term  : the last N messages verbatim + a rolling summary of everything older (bounded context, bounded cost).
              Summarisation is an LLM call (task='summarize' -> always the in-bank model: transcripts contain PII).
Long-term   : a few typed user preferences in the DB, surviving sessions. Deliberately tiny and typed, not a vector
              dump of every conversation: fewer things to leak, go stale, or be poisoned.

Memory-poisoning rule: long-term memory is written ONLY from the customer's own messages and from system-of-record
tool results. Text that came from third parties (transaction descriptions, FAQ documents) can never become a memory.
"""
from __future__ import annotations

import re

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from ..common import clock
from ..db import app_messages, app_summaries, app_user_memory
from ..llm.base import LLMClient, LLMRequest, Message
from ..prompts import load_prompt

PREF_KEYS = {"preferred_from_account", "preferred_name", "last_account_viewed"}
_ACCT = re.compile(r"(?<!\d)\d{12}(?!\d)")


class ConversationMemory:
    def __init__(self, engine: Engine, window: int):
        self.engine, self.window = engine, window

    # ---------------------------------------------------------------- short-term
    def append(self, session_id: str, role: str, content: str) -> int:
        with self.engine.begin() as conn:
            return conn.execute(app_messages.insert().values(
                session_id=session_id, role=role, content=content, ts=clock.now())).inserted_primary_key[0]

    def _all(self, session_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(app_messages).where(
                app_messages.c.session_id == session_id).order_by(app_messages.c.id)).mappings()]

    def user_messages(self, session_id: str) -> list[str]:
        """Everything the CUSTOMER typed in this session: the provenance ground truth for the action guard."""
        return [m["content"] for m in self._all(session_id) if m["role"] == "user"]

    async def context(self, session_id: str, llm: LLMClient, trace_id: str = "") -> tuple[str, list[dict]]:
        """(summary_of_older_turns, last_N_messages). Summarises lazily, only when the window overflows."""
        msgs = self._all(session_id)
        recent = msgs[-self.window:]
        older = msgs[: -self.window] if len(msgs) > self.window else []
        with self.engine.connect() as conn:
            row = conn.execute(select(app_summaries).where(app_summaries.c.session_id == session_id)).mappings().first()
        summary, upto = (row["summary"], row["upto_message_id"]) if row else ("", 0)
        fresh = [m for m in older if m["id"] > upto]
        if fresh:
            transcript = ("Previous summary: " + summary + "\n" if summary else "") + "\n".join(
                f"{m['role']}: {m['content'][:400]}" for m in fresh)
            resp = await llm.chat(LLMRequest(
                messages=[Message("system", load_prompt("summarizer")), Message("user", transcript, data_class="restricted")],
                task="summarize", agent="memory", max_tokens=200, trace_id=trace_id))
            summary, upto = resp.text.strip(), fresh[-1]["id"]
            with self.engine.begin() as conn:
                conn.execute(delete(app_summaries).where(app_summaries.c.session_id == session_id))
                conn.execute(app_summaries.insert().values(session_id=session_id, summary=summary, upto_message_id=upto))
        return summary, recent

    # ---------------------------------------------------------------- long-term
    def prefs(self, user_id: str) -> dict:
        with self.engine.connect() as conn:
            rows = conn.execute(select(app_user_memory).where(app_user_memory.c.user_id == user_id)).mappings().all()
        return {r["key"]: r["value"] for r in rows}

    def remember(self, user_id: str, key: str, value: str) -> None:
        if key not in PREF_KEYS:
            raise ValueError(f"'{key}' is not a permitted long-term memory key")
        with self.engine.begin() as conn:
            conn.execute(delete(app_user_memory).where(app_user_memory.c.user_id == user_id, app_user_memory.c.key == key))
            conn.execute(app_user_memory.insert().values(user_id=user_id, key=key, value=value[:200], updated_ts=clock.now()))

    def forget_all(self, user_id: str) -> None:
        """Right-to-erasure hook."""
        with self.engine.begin() as conn:
            conn.execute(delete(app_user_memory).where(app_user_memory.c.user_id == user_id))

    def learn_from_turn(self, user_id: str, user_text: str, owned_accounts: set[str], agent_used: str, completed_transfer_from: str | None) -> None:
        """Deterministic extraction from TRUSTED inputs only (customer's own words + verified results)."""
        if completed_transfer_from and completed_transfer_from in owned_accounts:
            self.remember(user_id, "preferred_from_account", completed_transfer_from)
        if agent_used == "accounts_agent":
            for n in _ACCT.findall(user_text):
                if n in owned_accounts:
                    self.remember(user_id, "last_account_viewed", n)
                    break
        if m := re.search(r"\bcall me ([A-Z][a-z]{1,20})\b", user_text):
            self.remember(user_id, "preferred_name", m.group(1))
