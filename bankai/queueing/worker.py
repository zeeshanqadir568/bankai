"""Async work off the request path: ticket creation, human-handoff tickets, approval-decision resume, approval
expiry sweeper, session purge. Every handler is idempotent (the queue is at-least-once)."""
from __future__ import annotations

import asyncio
import logging

from ..container import App

log = logging.getLogger("bankai.worker")
TOPICS = ("tickets.created", "escalations.requested", "approvals.decided")


class Worker:
    def __init__(self, app: App):
        self.app = app
        self.handlers = {"tickets.created": self.on_ticket, "escalations.requested": self.on_escalation,
                         "approvals.decided": self.on_decided}

    async def on_ticket(self, ev: dict) -> None:
        self.app.crm.process_ticket(ev["ticket_id"])  # queued -> open, only if still queued

    async def on_escalation(self, ev: dict) -> None:
        if not ev.get("customer_id"):
            return
        body = f"Handoff from AI assistant (trace {ev['trace_id']}). Reason: {ev['reason']}.\nLast messages (PII masked):\n" + "\n".join(ev["transcript_tail"])
        self.app.crm.system_ticket(ev["customer_id"], f"AI assistant handoff {ev['ref']}", body,
                                   "fraud" if "human_or_high_risk" in ev["reason"] else "other", "high", "system:coordinator")

    async def on_decided(self, ev: dict) -> None:
        await self.app.orchestrator.resume_after_decision(ev)

    async def pump(self) -> int:
        """Deterministically process everything queued (in-memory queue only): tests, demos, evals."""
        total = 0
        while True:
            n = 0
            for t in TOPICS:
                n += await self.app.queue.drain(t, self.handlers[t])  # type: ignore[attr-defined]
            total += n
            if not n:
                return total

    async def run(self, stop: asyncio.Event, sweep_every: float = 5.0) -> None:
        tasks = [asyncio.create_task(self.app.queue.consume_loop(t, self.handlers[t], stop)) for t in TOPICS]
        tasks.append(asyncio.create_task(self._sweeper(stop, sweep_every)))
        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _sweeper(self, stop: asyncio.Event, every: float) -> None:
        n = 0
        while not stop.is_set():
            try:
                await self.app.payments.expire_overdue()
                n += 1
                if n % 12 == 0:
                    self.app.sessions.purge_expired()
            except Exception as e:  # noqa: BLE001
                log.warning("sweeper error: %s", type(e).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=every)
            except asyncio.TimeoutError:
                pass
