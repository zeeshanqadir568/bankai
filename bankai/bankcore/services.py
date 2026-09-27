"""Business logic of the mocked core-banking systems. MCP servers are thin wrappers around these classes.

Every public method receives a *verified* ToolCaller and re-derives ownership from the database:
the arguments an agent supplies are requests, never facts.
"""
from __future__ import annotations

import json
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable

from sqlalchemy import func, select, update
from sqlalchemy.engine import Connection, Engine

from ..common import clock, new_id
from ..config import Settings
from ..db import (
    core_accounts, core_approvals, core_customers, core_kb, core_payees, core_tickets, core_transactions,
    core_transfers,
)
from ..security.authz import authorize, authorize_approval
from ..security.tool_tokens import ToolCaller
from .idempotency import run_idempotent

Publish = Callable[[str, dict], Awaitable[None]]
# Risk flags raised by the agent runtime that force human review of any money movement in the same session.
TAINT_ESCALATORS = {"injection_suspected", "ungrounded_account_number", "argument_smuggling"}


class ToolFailure(Exception):
    """A refusal or business-rule failure. `denied=True` => authorization refusal (audited as 'deny')."""

    def __init__(self, code: str, message: str, *, denied: bool = False):
        super().__init__(message)
        self.code, self.message, self.denied = code, message, denied


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def clean_text(s: str, max_len: int) -> str:
    """Write-side hygiene for free text that will later be read by an LLM (stored-injection defense in depth)."""
    return _CTRL.sub("", s or "").strip()[:max_len]


def to_cents(amount: float | int | str) -> int:
    try:
        d = Decimal(str(amount))
    except InvalidOperation as e:
        raise ToolFailure("invalid_amount", "amount is not a number") from e
    if d <= 0 or d != d.quantize(Decimal("0.01")):
        raise ToolFailure("invalid_amount", "amount must be positive with at most 2 decimals")
    return int(d * 100)


def _account(conn: Connection, number: str):
    return conn.execute(select(core_accounts).where(core_accounts.c.number == number)).mappings().first()


# ============================================================================ accounts
class AccountsService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def list_accounts(self, caller: ToolCaller, customer_id: str | None = None) -> dict:
        target = customer_id or caller.identity.customer_id
        if not target:
            raise ToolFailure("customer_required", "customer_id is required for staff callers")
        d = authorize(caller.identity, "accounts:read", target)
        if not d:
            raise ToolFailure("access_denied", "You are not authorized to view these accounts.", denied=True)
        with self.engine.connect() as conn:
            rows = conn.execute(select(core_accounts).where(core_accounts.c.customer_id == target)
                                .order_by(core_accounts.c.number)).mappings().all()
        return {"customer_id": target, "accounts": [
            {"account_number": r["number"], "type": r["type"], "currency": r["currency"],
             "balance_cents": r["balance_cents"], "balance": r["balance_cents"] / 100, "status": r["status"]} for r in rows]}

    def _authorized_account(self, conn: Connection, caller: ToolCaller, number: str):
        acct = _account(conn, number)
        owner = acct["customer_id"] if acct else None
        d = authorize(caller.identity, "accounts:read", owner)
        if not acct or not d:
            # Same answer for "missing" and "not yours": no account-number enumeration. The audit log keeps the true reason.
            raise ToolFailure(
                "access_denied", "You are not authorized to access this account, or it does not exist.", denied=True)
        return acct

    def get_balance(self, caller: ToolCaller, account_number: str) -> dict:
        with self.engine.connect() as conn:
            a = self._authorized_account(conn, caller, account_number)
        return {"account_number": a["number"], "type": a["type"], "currency": a["currency"],
                "balance_cents": a["balance_cents"], "balance": a["balance_cents"] / 100, "status": a["status"]}

    def get_transactions(self, caller: ToolCaller, account_number: str, limit: int = 10) -> dict:
        limit = max(1, min(int(limit), 50))
        with self.engine.connect() as conn:
            a = self._authorized_account(conn, caller, account_number)
            rows = conn.execute(select(core_transactions).where(core_transactions.c.account_number == a["number"])
                                .order_by(core_transactions.c.ts.desc(), core_transactions.c.id.desc())
                                .limit(limit)).mappings().all()
        return {"account_number": a["number"], "transactions": [
            {"id": r["id"], "ts": r["ts"], "date": time.strftime("%Y-%m-%d", time.gmtime(r["ts"])), "amount_cents": r["amount_cents"], "amount": r["amount_cents"] / 100, "kind": r["kind"],
             "counterparty": r["counterparty"], "description": r["description"]} for r in rows]}


# ============================================================================ payments
class PaymentsService:
    def __init__(self, engine: Engine, settings: Settings, publish: Publish):
        self.engine, self.s, self.publish = engine, settings, publish

    # ---- reads ---------------------------------------------------------------------------
    def list_payees(self, caller: ToolCaller) -> dict:
        cust = caller.identity.customer_id
        if not cust or not authorize(caller.identity, "payees:read", cust):
            raise ToolFailure("access_denied", "Only account holders can view saved payees.", denied=True)
        with self.engine.connect() as conn:
            rows = conn.execute(select(core_payees).where(core_payees.c.customer_id == cust)).mappings().all()
        return {"payees": [{"name": r["name"], "account_number": r["account_number"]} for r in rows]}

    def get_transfer_status(self, caller: ToolCaller, transfer_id: str) -> dict:
        with self.engine.connect() as conn:
            t = conn.execute(select(core_transfers).where(core_transfers.c.id == transfer_id)).mappings().first()
        if not t or not authorize(caller.identity, "transfers:read", t["customer_id"]):
            raise ToolFailure("access_denied", "Transfer not found or not accessible.", denied=True)
        return self._transfer_view(t)

    # ---- mutation: initiate -----------------------------------------------------------------
    async def initiate_transfer(
        self, caller: ToolCaller, *, from_account: str, to_account: str, amount: float | str,
        currency: str = "USD", memo: str = "",
    ) -> dict:
        if not caller.idempotency_key:
            raise ToolFailure("idempotency_key_required", "mutating calls require an idempotency key")
        cents = to_cents(amount)
        events: list[tuple[str, dict]] = []
        payload = {"f": from_account, "t": to_account, "a": cents, "c": currency, "m": memo}

        def op(conn: Connection) -> dict:
            src = _account(conn, from_account)
            owner = src["customer_id"] if src else None
            if not src or not authorize(caller.identity, "transfers:initiate", owner):
                raise ToolFailure("access_denied", "You are not authorized to transfer from this account.", denied=True)
            dst = _account(conn, to_account)
            if not dst:
                raise ToolFailure("unknown_destination", "Destination account does not exist at this bank.")
            if from_account == to_account:
                raise ToolFailure("invalid_transfer", "Source and destination are the same account.")
            if src["status"] != "active" or dst["status"] != "active":
                raise ToolFailure("account_inactive", "Source or destination account is not active.")
            if currency != src["currency"] or currency != dst["currency"]:
                raise ToolFailure("currency_mismatch", "Cross-currency transfers are not supported.")
            if cents > self.s.hard_limit_cents:
                raise ToolFailure("over_hard_limit", "Amount exceeds the per-transfer limit and cannot be processed.")
            if src["balance_cents"] < cents:
                raise ToolFailure("insufficient_funds", "Insufficient funds.")
            since = clock.now() - 86400
            day_total = conn.execute(select(func.coalesce(func.sum(core_transfers.c.amount_cents), 0)).where(
                core_transfers.c.customer_id == owner, core_transfers.c.created_ts >= since,
                core_transfers.c.status.in_(["completed", "pending_approval"]))).scalar() or 0
            if day_total + cents > self.s.daily_limit_cents:
                raise ToolFailure("daily_limit_exceeded", "Daily transfer limit would be exceeded.")

            reasons = self._risk_reasons(conn, caller, src, dst, cents)
            now = clock.now()
            tid = new_id("trf")
            base = dict(id=tid, initiator_id=caller.identity.user_id, customer_id=owner, from_account=from_account,
                        to_account=to_account, amount_cents=cents, currency=currency, memo=clean_text(memo, 140),
                        risk_json=json.dumps(reasons), created_ts=now, updated_ts=now)
            if reasons:
                aid = new_id("apr")
                conn.execute(core_transfers.insert().values(**base, status="pending_approval", approval_id=aid))
                conn.execute(core_approvals.insert().values(
                    id=aid, transfer_id=tid, requested_by=caller.identity.user_id, session_id=caller.session_id,
                    status="pending", reasons_json=json.dumps(reasons), created_ts=now,
                    expires_ts=now + self.s.approval_timeout_seconds))
                events.append(("approvals.requested", {
                    "approval_id": aid, "transfer_id": tid, "user_id": caller.identity.user_id,
                    "session_id": caller.session_id, "amount_cents": cents, "reasons": reasons, "trace_id": caller.trace_id}))
                return {"ok": True, "transfer_id": tid, "status": "pending_approval", "approval_id": aid,
                        "amount_cents": cents, "from_account": from_account, "to_account": to_account,
                        "reasons": reasons, "message": "Held for staff approval; no funds have moved."}
            self._execute(conn, tid, base)
            events.append(("transfers.completed", {"transfer_id": tid, "user_id": caller.identity.user_id,
                                                  "amount_cents": cents, "trace_id": caller.trace_id}))
            return {"ok": True, "transfer_id": tid, "status": "completed", "amount_cents": cents,
                    "from_account": from_account, "to_account": to_account, "reasons": [],
                    "message": "Transfer completed."}

        resp, replayed = run_idempotent(self.engine, f"initiate_transfer:{caller.identity.user_id}",
                                        caller.idempotency_key, payload, op)
        if not replayed:
            for topic, ev in events:
                await self.publish(topic, ev)
        return {**resp, "replayed": replayed}

    def _risk_reasons(self, conn: Connection, caller: ToolCaller, src, dst, cents: int) -> list[str]:
        reasons: list[str] = []
        own = dst["customer_id"] == src["customer_id"]
        if not own:
            known = conn.execute(select(core_payees.c.id).where(
                core_payees.c.customer_id == src["customer_id"], core_payees.c.account_number == dst["number"])).first()
            if not known:
                reasons.append("new_payee")
        if cents >= self.s.approval_threshold_cents:
            reasons.append("amount_over_threshold")
        if cents >= 0.8 * src["balance_cents"]:
            reasons.append("drains_account")
        recent = conn.execute(select(func.count()).select_from(core_transfers).where(
            core_transfers.c.customer_id == src["customer_id"], core_transfers.c.created_ts >= clock.now() - 600,
            core_transfers.c.status.in_(["completed", "pending_approval"]))).scalar() or 0
        if recent >= 3:
            reasons.append("velocity")
        if set(caller.taint) & TAINT_ESCALATORS:
            reasons.append("tainted_context")
        return reasons

    def _execute(self, conn: Connection, tid: str, base: dict) -> None:
        amt = base["amount_cents"]
        res = conn.execute(update(core_accounts).where(
            core_accounts.c.number == base["from_account"], core_accounts.c.balance_cents >= amt,
            core_accounts.c.status == "active").values(balance_cents=core_accounts.c.balance_cents - amt))
        if res.rowcount != 1:
            raise ToolFailure("insufficient_funds", "Insufficient funds at execution time.")
        conn.execute(update(core_accounts).where(core_accounts.c.number == base["to_account"])
                     .values(balance_cents=core_accounts.c.balance_cents + amt))
        now = clock.now()
        memo = base.get("memo") or ""
        conn.execute(core_transactions.insert().values(
            account_number=base["from_account"], ts=now, amount_cents=-amt, kind="transfer_out",
            counterparty=base["to_account"][-4:], description=f"Transfer to ****{base['to_account'][-4:]} {memo}".strip()))
        conn.execute(core_transactions.insert().values(
            account_number=base["to_account"], ts=now, amount_cents=amt, kind="transfer_in",
            counterparty=base["from_account"][-4:], description=f"Transfer from ****{base['from_account'][-4:]} {memo}".strip()))
        if "status" not in base:
            conn.execute(core_transfers.insert().values(**base, status="completed"))
        else:
            conn.execute(update(core_transfers).where(core_transfers.c.id == tid).values(status="completed", updated_ts=now))
        # a transfer that cleared review registers the recipient as a known payee
        dst_owner = conn.execute(select(core_accounts.c.customer_id).where(
            core_accounts.c.number == base["to_account"])).scalar()
        if dst_owner != base["customer_id"]:
            name = conn.execute(select(core_customers.c.name).where(core_customers.c.id == dst_owner)).scalar() or "Payee"
            exists = conn.execute(select(core_payees.c.id).where(
                core_payees.c.customer_id == base["customer_id"], core_payees.c.account_number == base["to_account"])).first()
            if not exists:
                conn.execute(core_payees.insert().values(
                    customer_id=base["customer_id"], name=name, account_number=base["to_account"], added_ts=now))

    # ---- approvals (human-in-the-loop) -------------------------------------------------------
    def list_pending_approvals(self, caller: ToolCaller) -> dict:
        if not authorize(caller.identity, "approvals:read"):
            raise ToolFailure("access_denied", "Approvals are visible to approvers only.", denied=True)
        with self.engine.connect() as conn:
            rows = conn.execute(select(core_approvals, core_transfers.c.amount_cents, core_transfers.c.from_account,
                                       core_transfers.c.to_account, core_transfers.c.memo)
                                .join(core_transfers, core_transfers.c.id == core_approvals.c.transfer_id)
                                .where(core_approvals.c.status == "pending")
                                .order_by(core_approvals.c.created_ts)).mappings().all()
        return {"approvals": [{
            "approval_id": r["id"], "transfer_id": r["transfer_id"], "requested_by": r["requested_by"],
            "amount_cents": r["amount_cents"], "from_account": r["from_account"], "to_account": r["to_account"],
            "memo": r["memo"], "reasons": json.loads(r["reasons_json"]), "expires_ts": r["expires_ts"]} for r in rows]}

    async def decide_approval(self, caller: ToolCaller, *, approval_id: str, approve: bool, note: str = "") -> dict:
        if not caller.idempotency_key:
            raise ToolFailure("idempotency_key_required", "mutating calls require an idempotency key")
        events: list[tuple[str, dict]] = []

        def op(conn: Connection) -> dict:
            a = conn.execute(select(core_approvals).where(core_approvals.c.id == approval_id)).mappings().first()
            if not a:
                raise ToolFailure("not_found", "Approval not found.")
            d = authorize_approval(caller.identity, a["requested_by"])
            if not d:
                raise ToolFailure("access_denied", d.reason.capitalize() + ".", denied=True)
            if a["status"] != "pending":
                raise ToolFailure("already_decided", f"Approval already {a['status']}.")
            now = clock.now()
            t = conn.execute(select(core_transfers).where(core_transfers.c.id == a["transfer_id"])).mappings().first()
            if now > a["expires_ts"]:
                self._finalize(conn, a, "expired", "expired", None, now, events, t)
                return {"ok": True, "approval_id": approval_id, "status": "expired", "transfer_status": "expired"}
            if approve:
                try:
                    self._execute(conn, t["id"], {**dict(t), "status": "pending_approval"})
                except ToolFailure as e:
                    self._finalize(conn, a, "approved", "rejected", caller.identity.user_id, now, events, t, note=e.message)
                    return {"ok": True, "approval_id": approval_id, "status": "approved", "transfer_status": "rejected",
                            "message": e.message}
                self._finalize(conn, a, "approved", "completed", caller.identity.user_id, now, events, t, note=note)
                return {"ok": True, "approval_id": approval_id, "status": "approved", "transfer_status": "completed"}
            self._finalize(conn, a, "denied", "denied", caller.identity.user_id, now, events, t, note=note)
            return {"ok": True, "approval_id": approval_id, "status": "denied", "transfer_status": "denied"}

        resp, replayed = run_idempotent(self.engine, f"decide_approval:{caller.identity.user_id}",
                                        caller.idempotency_key, {"a": approval_id, "d": approve}, op)
        if not replayed:
            for topic, ev in events:
                await self.publish(topic, ev)
        return {**resp, "replayed": replayed}

    def _finalize(self, conn, a, approval_status, transfer_status, decided_by, now, events, t, note=""):
        conn.execute(update(core_approvals).where(core_approvals.c.id == a["id"]).values(
            status=approval_status, decided_by=decided_by, decided_ts=now, note=note))
        if transfer_status != "completed":  # _execute already wrote 'completed'
            conn.execute(update(core_transfers).where(core_transfers.c.id == t["id"]).values(
                status=transfer_status, updated_ts=now))
        events.append(("approvals.decided", {
            "approval_id": a["id"], "transfer_id": t["id"], "user_id": a["requested_by"], "session_id": a["session_id"],
            "approval_status": approval_status, "transfer_status": transfer_status, "amount_cents": t["amount_cents"],
            "to_account": t["to_account"], "note": note}))

    async def expire_overdue(self) -> int:
        """System sweeper (worker, not an agent): pending approvals past their deadline expire; no funds ever move."""
        events: list[tuple[str, dict]] = []
        now = clock.now()
        with self.engine.begin() as conn:
            rows = conn.execute(select(core_approvals).where(
                core_approvals.c.status == "pending", core_approvals.c.expires_ts < now)).mappings().all()
            for a in rows:
                t = conn.execute(select(core_transfers).where(core_transfers.c.id == a["transfer_id"])).mappings().first()
                self._finalize(conn, a, "expired", "expired", None, now, events, t, note="approval window elapsed")
        for topic, ev in events:
            await self.publish(topic, ev)
        return len(events)

    @staticmethod
    def _transfer_view(t) -> dict:
        return {"transfer_id": t["id"], "status": t["status"], "amount_cents": t["amount_cents"],
                "from_account": t["from_account"], "to_account": t["to_account"], "reasons": json.loads(t["risk_json"]),
                "approval_id": t["approval_id"]}


# ============================================================================ CRM / tickets
_STOPWORDS = {"the", "and", "for", "are", "you", "your", "what", "whats", "how", "can", "does", "about", "tell", "with", "our", "have", "any", "this", "that", "current", "please"}
TICKET_CATEGORIES = {"card", "account", "payments", "fraud", "loan", "complaint", "other"}


class CRMService:
    def __init__(self, engine: Engine, publish: Publish):
        self.engine, self.publish = engine, publish

    async def create_ticket(
        self, caller: ToolCaller, *, subject: str, description: str, category: str = "other", priority: str = "normal",
        customer_id: str | None = None,
    ) -> dict:
        if not caller.idempotency_key:
            raise ToolFailure("idempotency_key_required", "mutating calls require an idempotency key")
        target = customer_id or caller.identity.customer_id
        if not target or not authorize(caller.identity, "tickets:create", target):
            raise ToolFailure("access_denied", "You may only create tickets for your own account.", denied=True)
        category = category if category in TICKET_CATEGORIES else "other"
        priority = priority if priority in {"low", "normal", "high"} else "normal"
        subject_c, body_c = clean_text(subject, 200), clean_text(description, 4000)
        if not subject_c:
            raise ToolFailure("invalid_ticket", "A ticket needs a subject.")
        events: list[tuple[str, dict]] = []

        def op(conn: Connection) -> dict:
            tid, now = new_id("T"), clock.now()
            conn.execute(core_tickets.insert().values(
                id=tid, customer_id=target, subject=subject_c, body=body_c, category=category, priority=priority,
                status="queued", created_by=caller.identity.user_id, created_ts=now, updated_ts=now))
            events.append(("tickets.created", {"ticket_id": tid, "customer_id": target, "trace_id": caller.trace_id}))
            return {"ok": True, "ticket_id": tid, "status": "queued", "category": category, "priority": priority,
                    "message": "Ticket accepted and queued for processing."}

        resp, replayed = run_idempotent(self.engine, f"create_ticket:{caller.identity.user_id}", caller.idempotency_key,
                                        {"s": subject_c, "d": body_c, "c": category, "p": priority, "cu": target}, op)
        if not replayed:
            for topic, ev in events:
                await self.publish(topic, ev)
        return {**resp, "replayed": replayed}

    def get_ticket(self, caller: ToolCaller, ticket_id: str) -> dict:
        with self.engine.connect() as conn:
            t = conn.execute(select(core_tickets).where(core_tickets.c.id == ticket_id)).mappings().first()
        if not t or not authorize(caller.identity, "tickets:read", t["customer_id"]):
            raise ToolFailure("access_denied", "Ticket not found or not accessible.", denied=True)
        return {"ticket": self._view(t)}

    def list_tickets(self, caller: ToolCaller, customer_id: str | None = None) -> dict:
        target = customer_id or caller.identity.customer_id
        if not target or not authorize(caller.identity, "tickets:read", target):
            raise ToolFailure("access_denied", "You may only list your own tickets.", denied=True)
        with self.engine.connect() as conn:
            rows = conn.execute(select(core_tickets).where(core_tickets.c.customer_id == target)
                                .order_by(core_tickets.c.created_ts.desc()).limit(20)).mappings().all()
        return {"tickets": [self._view(r) for r in rows]}

    @staticmethod
    def _view(t) -> dict:
        return {"ticket_id": t["id"], "subject": t["subject"], "category": t["category"], "priority": t["priority"],
                "status": t["status"], "created_ts": t["created_ts"]}

    def system_ticket(self, customer_id: str, subject: str, body: str, category: str, priority: str, created_by: str) -> str:
        """Internal (worker) path, e.g. human-handoff tickets. Not reachable through MCP, so not reachable by any agent."""
        tid, now = new_id("T"), clock.now()
        with self.engine.begin() as conn:
            conn.execute(core_tickets.insert().values(
                id=tid, customer_id=customer_id, subject=clean_text(subject, 200), body=clean_text(body, 4000), category=category,
                priority=priority, status="open", created_by=created_by, created_ts=now, updated_ts=now))
        return tid

    def process_ticket(self, ticket_id: str) -> bool:
        """Worker step (idempotent): queued -> open. Returns True if it transitioned."""
        with self.engine.begin() as conn:
            res = conn.execute(update(core_tickets).where(
                core_tickets.c.id == ticket_id, core_tickets.c.status == "queued").values(
                status="open", updated_ts=clock.now()))
            return res.rowcount == 1


# ============================================================================ knowledge base
class KnowledgeService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def search_faq(self, caller: ToolCaller, query: str, limit: int = 3) -> dict:
        if not authorize(caller.identity, "kb:read"):
            raise ToolFailure("access_denied", "Knowledge base is not available to this role.", denied=True)
        terms = {t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2 and t not in _STOPWORDS}
        with self.engine.connect() as conn:
            docs = conn.execute(select(core_kb)).mappings().all()
        scored = []
        for d in docs:
            hay = re.findall(r"[a-z0-9]+", (d["title"] + " " + d["body"]).lower())
            title = set(re.findall(r"[a-z0-9]+", d["title"].lower()))
            score = sum(1 for t in terms if t in hay) + 2 * sum(1 for t in terms if t in title)
            if score:
                scored.append((score, d))
        scored.sort(key=lambda x: -x[0])
        return {"query": query, "results": [
            {"doc_id": d["id"], "title": d["title"], "body": d["body"], "source": d["source"], "score": s}
            for s, d in scored[: max(1, min(int(limit), 5))]]}
