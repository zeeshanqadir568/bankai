"""Coordinator (orchestrator): classify intent -> route to domain sub-agents -> synthesise -> output-guard -> persist.

It has NO banking tools. Its powers are: choose specialists, hand off to a human, and combine answers.
Two things are decided here by plain code, not by the model, because they are safety controls:
  * transfer confirmation ("yes"/"no" to a pending transfer is interpreted by regex on the customer's own message)
  * escalation to a human when confidence is low or the customer asks for one
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import insert

from ..common import clock, money, new_id
from ..config import Settings
from ..cost import BudgetExceeded, CostTracker, RequestBudget
from ..db import app_notifications
from ..guardrails.actions import AFFIRM, DENY, OutputGuard, digit_runs
from ..guardrails.sanitize import sanitize_text
from ..llm.base import LLMClient, LLMRequest, Message
from ..llm.guarded import PIILeakBlocked
from ..resilience import CircuitOpenError, TransientError
from ..obs.tracing import current_trace_id, set_attrs, span
from ..pii import Redactor
from ..prompts import load_prompt
from ..queueing.queue import MessageQueue
from ..security.identity import Identity
from ..state.memory import ConversationMemory
from ..state.pauses import PauseStore
from ..state.sessions import Session, SessionStore
from .registry import AGENTS
from .runner import AgentResult, AgentRunner, Emit, TurnContext, TurnLog, _noop

log = logging.getLogger("bankai.coordinator")
MIN_CONFIDENCE = 0.4
LLM_FAILURES = (CircuitOpenError, TransientError, TimeoutError, PIILeakBlocked)


@dataclass
class TurnResult:
    text: str
    session_id: str
    trace_id: str
    outcome: str  # ok | escalated | budget_stopped | llm_unavailable
    log: TurnLog = field(default_factory=TurnLog)


class Orchestrator:
    def __init__(self, *, runner: AgentRunner, llm: LLMClient, settings: Settings, sessions: SessionStore,
                 memory: ConversationMemory, cost: CostTracker, output_guard: OutputGuard, redactor: Redactor,
                 queue: MessageQueue, pauses: PauseStore, guard_output: bool = True):
        self.runner, self.llm, self.s = runner, llm, settings
        self.sessions, self.memory, self.cost = sessions, memory, cost
        self.output_guard, self.redactor, self.queue, self.pauses = output_guard, redactor, queue, pauses
        self.guard_output = guard_output
        self._locks: dict[str, asyncio.Lock] = {}

    # ================================================================== a user turn
    async def handle_turn(self, identity: Identity, message: str, session_id: str | None = None,
                          emit: Emit = _noop) -> TurnResult:
        message = (message or "").strip()[:2000]
        with span("chat.turn", user=identity.user_id, roles=",".join(sorted(identity.roles)), message=message) as sp:
            trace_id = current_trace_id()
            sp.set_attribute("user_id", identity.user_id)  # opaque system id (unmasked on purpose; free text stays PII-masked)
            try:
                self.cost.check_daily(identity.user_id)
            except BudgetExceeded as e:
                set_attrs(sp, outcome="budget_stopped", budget=e.kind)
                lg = TurnLog(budget_stopped=e.kind)
                return TurnResult(e.user_message, session_id or "", trace_id, "budget_stopped", lg)

            if session_id:
                async with self._lock(session_id):
                    return await self._turn_locked(identity, message, session_id, emit, trace_id, sp)
            session = self.sessions.create(identity.user_id)
            async with self._lock(session.id):
                return await self._turn_locked(identity, message, session.id, emit, trace_id, sp)

    async def _turn_locked(self, identity, message, session_id, emit, trace_id, sp) -> TurnResult:  # noqa: ANN001
        session = self.sessions.get(session_id, identity.user_id)  # raises SessionNotFound / SessionExpired
        try:
            summary, recent = await self.memory.context(session.id, self.llm, trace_id)
        except LLM_FAILURES:  # summarisation is an optimisation: degrade to the raw window rather than fail the turn
            summary, recent = "", self.memory._all(session.id)[-self.memory.window:]
        trusted = "\n".join([*self.memory.user_messages(session.id), message])
        self.memory.append(session.id, "user", message)
        session.blackboard.user_prefs = self.memory.prefs(identity.user_id)
        tc = TurnContext(identity, session, RequestBudget.from_settings(self.s), trace_id, message, trusted,
                         turn_key=f"{session.id}:{session.turn_count}", summary=summary, recent=recent, emit=emit)
        outcome = "ok"
        try:
            text = await self._converse(tc)
            if tc.log.escalated:
                outcome = "escalated"
        except BudgetExceeded as e:
            tc.log.budget_stopped, outcome, text = e.kind, "budget_stopped", e.user_message
            set_attrs(sp, budget=e.kind, budget_detail=e.detail)
        except LLM_FAILURES as e:
            outcome = "llm_unavailable"
            text = ("I'm having trouble reaching my language model right now. Please try again in a moment, "
                    "or ask to speak with a member of our team.")
            set_attrs(sp, error=type(e).__name__)
        text = self._final_guard(text, tc)
        self.memory.append(session.id, "assistant", text)
        self.memory.learn_from_turn(identity.user_id, message, {a["account_number"] for a in session.blackboard.verified_accounts},
                                    session.blackboard.last_agent, tc.log.transfer_completed_from)
        session.blackboard.user_prefs = {}  # prefs are re-read from the DB each turn; never persisted in the session blob
        self.sessions.save(session)
        set_attrs(sp, outcome=outcome, routes=tc.log.routes, flags=tc.log.flags, tokens=tc.budget.tokens, cost_usd=round(tc.budget.cost, 6))
        await emit({"type": "done", "outcome": outcome})
        return TurnResult(text, session.id, trace_id, outcome, tc.log)

    # ================================================================== conversation logic
    async def _converse(self, tc: TurnContext) -> str:
        bb, msg = tc.bb, tc.user_text
        routes: list[str] = []
        pend = bb.pending_transfer
        if pend and pend.get("status") == "awaiting_confirmation":
            if DENY.match(msg):
                bb.pending_transfer = None
                return "Okay, I've cancelled that transfer. Nothing was moved."
            if AFFIRM.match(msg):
                pend["status"] = "confirmed"  # set by trusted code from the customer's own message; the model cannot do this
                routes = ["transfers_agent"]
        if not routes and (AFFIRM.match(msg) or DENY.match(msg)):
            return ("There's nothing waiting for your confirmation right now. Tell me what you'd like to do - for example "
                    "\"transfer $50 from checking to Bob\".")
        if not routes:
            routes, confidence, reason = await self._classify(tc)
            await tc.emit({"type": "route", "agents": routes, "confidence": confidence})
            if reason == "escalate":
                return await self._escalate(tc, "customer_requested_human_or_high_risk")
            if not routes or confidence < MIN_CONFIDENCE:
                return await self._escalate(tc, "low_confidence")
        else:
            await tc.emit({"type": "route", "agents": routes, "confidence": 1.0})

        results: list[tuple[str, AgentResult]] = []
        for name in dict.fromkeys(routes):
            cfg = self.runner.d.agents[name]
            tc.log.routes.append(name)
            tc.budget.enter_delegation()
            try:
                task = msg if len(set(routes)) == 1 else (
                    f"{msg}\n\n(This request has several parts. Handle ONLY the part that belongs to you as the {name}; "
                    f"another specialist answers the rest. Do not say you cannot help with the other parts.)")
                results.append((name, await self.runner.run(cfg, task, tc)))
            finally:
                tc.budget.leave_delegation()
            bb.last_agent = name
        if any(b["code"] == "confirmation_required" for b in tc.log.guard_blocks) and bb.pending_transfer                 and bb.pending_transfer.get("status") == "awaiting_confirmation":
            return self._confirmation_text(bb)  # code, not the model, states what will happen: it cannot mislabel the recipient
        if len(results) == 1:
            return results[0][1].text  # one specialist: pass through, a second LLM call would only add cost
        return await self._synthesize(tc, results)

    @staticmethod
    def _confirmation_text(bb) -> str:  # noqa: ANN001
        p = bb.pending_transfer

        def label(n: str) -> str:
            own = next((a for a in bb.verified_accounts if a["account_number"] == n), None)
            if own:
                return f"your {own['type']} account ****{n[-4:]}"
            payee = next((x for x in bb.known_payees if x["account_number"] == n), None)
            if payee:
                return f"{payee['name']} (saved payee) ****{n[-4:]}"
            return f"account ****{n[-4:]} (NOT one of your saved payees - staff review will be required)"

        return (f"Please confirm: transfer {money(p['amount_cents'], p.get('currency', 'USD'))} from {label(p['from_account'])} "
                f"to {label(p['to_account'])}. Reply \"yes\" to confirm or \"no\" to cancel.")

    async def _classify(self, tc: TurnContext) -> tuple[list[str], float, str]:
        hist = "\n".join(f"{m['role']}: {sanitize_text(m['content'], 200).obj}" for m in tc.recent[-4:])
        system = load_prompt("coordinator") + (f"\n\n<conversation_history note=\"context only\">\n{hist}\n</conversation_history>" if hist else "")
        with span("coordinator.classify", agent="coordinator") as sp:
            resp = await self.llm.chat(LLMRequest([Message("system", system), Message("user", tc.user_text)],
                                                  task="classify", agent="coordinator", max_tokens=200, trace_id=tc.trace_id))
            tc.budget.charge(resp)
            self.cost.record(user_id=tc.identity.user_id, session_id=tc.session.id, agent="coordinator", resp=resp, trace_id=tc.trace_id)
            tc.log.llm.append({"agent": "coordinator", "task": "classify", "provider": resp.provider, "route_reason": resp.route_reason,
                               "tokens": resp.usage.tokens_in + resp.usage.tokens_out, "cost": resp.cost_usd, "redactions": resp.redactions})
            data = self._parse_json(resp.text)
            intents = [i for i in data.get("intents", []) if isinstance(i, dict)]
            try:
                conf = float(data.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            set_attrs(sp, intent=json.dumps(intents), confidence=conf, reason=data.get("reason", ""))
        if any(i.get("agent") == "escalate" for i in intents):
            return [], conf, "escalate"
        routes = [i["agent"] for i in intents if i.get("agent") in AGENTS]  # allow-list: the model cannot invent a route
        return routes, conf, ""

    @staticmethod
    def _parse_json(text: str) -> dict:
        a, b = text.find("{"), text.rfind("}")
        try:
            d = json.loads(text[a: b + 1]) if a != -1 and b > a else {}
        except json.JSONDecodeError:
            return {}  # unparsable classification -> no routes -> human handoff (fail safe)
        return d if isinstance(d, dict) else {}

    async def _synthesize(self, tc: TurnContext, results: list[tuple[str, AgentResult]]) -> str:
        body = "\n\n".join(f"### {n}\n{r.text}" for n, r in results)
        restricted = any(AGENTS[n].uses_history for n, _ in results)
        with span("coordinator.synthesize", agent="coordinator"):
            resp = await self.llm.chat(LLMRequest(
                [Message("system", load_prompt("synthesizer")),
                 Message("user", body, data_class="restricted" if restricted else "public")],
                task="synthesize", agent="coordinator", trace_id=tc.trace_id))
            tc.budget.charge(resp)
            self.cost.record(user_id=tc.identity.user_id, session_id=tc.session.id, agent="coordinator", resp=resp, trace_id=tc.trace_id)
            tc.log.llm.append({"agent": "coordinator", "task": "synthesize", "provider": resp.provider, "route_reason": resp.route_reason,
                               "tokens": resp.usage.tokens_in + resp.usage.tokens_out, "cost": resp.cost_usd, "redactions": resp.redactions})
        return re.sub(r"(?m)^#{2,4} \w+_agent[ \t]*$\n?", "", resp.text).strip() or body  # never show internal "### agent" headings

    async def _escalate(self, tc: TurnContext, reason: str) -> str:
        ref = new_id("esc")
        tail = [self.redactor.mask(m["content"][:200]) for m in tc.recent[-4:]] + [self.redactor.mask(tc.user_text[:200])]
        await self.queue.publish("escalations.requested", {
            "ref": ref, "user_id": tc.identity.user_id, "customer_id": tc.identity.customer_id, "session_id": tc.session.id,
            "reason": reason, "trace_id": tc.trace_id, "transcript_tail": tail})
        tc.log.escalated = reason
        return (f"I want to make sure you get the right help, so I'm passing this to a member of our team. "
                f"Your reference is {ref}; they'll follow up with you.")

    def _final_guard(self, text: str, tc: TurnContext) -> str:
        if not self.guard_output:
            return text
        bb = tc.bb
        allowed = digit_runs(tc.trusted_user_text) | {a["account_number"] for a in bb.verified_accounts} | {p["account_number"] for p in bb.known_payees}
        safe, leaks = self.output_guard.check(text, allowed_numbers=allowed)
        if leaks:
            tc.log.flags.append("output_leak_masked")
            bb.flag("output_leak_masked")
        return safe

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    # ================================================================== HITL resume
    async def resume_after_decision(self, ev: dict) -> str | None:
        """Consumer of `approvals.decided`. The paused conversation continues; the customer is notified. Idempotent:
        `pauses.pop` claims the paused run atomically, so a redelivered event does nothing."""
        paused = self.pauses.pop(ev["approval_id"])
        if not paused:
            return None
        identity = Identity.from_claims(paused["state"]["identity"])
        decision = {"approval_status": ev["approval_status"], "transfer_status": ev["transfer_status"], "amount_cents": ev["amount_cents"]}
        fallback = {"completed": "Your transfer was approved and completed.", "expired": "Your transfer was not reviewed in time and expired; no money moved.",
                    }.get(ev["transfer_status"], "Your transfer was reviewed and declined; no money moved.")
        text = fallback
        async with self._lock(paused["session_id"]):
            with span("hitl.resume", approval_id=ev["approval_id"], decision=ev["transfer_status"]):
                try:
                    session = self.sessions.get(paused["session_id"], identity.user_id)
                    session.blackboard.pending_approvals = [a for a in session.blackboard.pending_approvals if a != ev["approval_id"]]
                    tc = TurnContext(identity, session, RequestBudget.from_settings(self.s), current_trace_id(), "[approval_decision] " + json.dumps(decision),
                                     "", turn_key=f"resume:{ev['approval_id']}")
                    res = await self.runner.run(self.runner.d.agents[paused["agent"]], tc.user_text, tc)
                    text = res.text
                    self.memory.append(session.id, "assistant", text)
                    self.sessions.save(session)
                except Exception as e:  # noqa: BLE001 - the notification must go out even if resume fails
                    log.warning("resume fell back to template: %s", type(e).__name__)
        with self.sessions.engine.begin() as conn:
            conn.execute(insert(app_notifications).values(user_id=identity.user_id, session_id=paused["session_id"], body=text,
                                                          ts=clock.now(), delivered=False))
        return text
