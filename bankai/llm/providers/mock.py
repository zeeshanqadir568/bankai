"""Deterministic stand-ins for the in-bank model and the external frontier model.

Why mocks (and what they do and do NOT prove): the platform's *guarantees* — authorization, redaction,
idempotency, budgets, guardrails — are deterministic code and are tested against ANY model behaviour,
including hostile behaviour. So the mock is intentionally simple but adversarial when asked:

  * it READS the coordinator prompt's routing table, so editing the prompt changes routing (eval catches it)
  * `gullible=True`   : obeys "ignore previous instructions ... transfer ..." found anywhere in its context
                        (worst-case model; used to show the deterministic layers still hold)
  * `pathology=...`   : gets stuck (identical retries / varying retries / delegation ping-pong) to exercise the
                        runaway-loop protections
  * `fail_next=N`     : raises transient errors to exercise retries and circuit breakers
Swap in `OllamaClient` / `AnthropicClient` (same interface) to evaluate real model behaviour.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from ...common import estimate_tokens, money
from ...resilience import TransientError
from ..base import LLMRequest, LLMResponse, Message, ToolCall, Usage, cost_of

_ROUTE_LINE = re.compile(r"^- (\w+): (.+)$", re.M)
_INJECTION = re.compile(
    r"ignore (?:all )?(?:previous|prior) instructions.{0,120}?transfer (?:all (?:the )?funds|\$?[\d,\.]+)\s+to\s+account\s+(\d{10,16})",
    re.I | re.S)
_ACCT12 = re.compile(r"(?<!\d)\d{12}(?!\d)")


def extract_json(text: str) -> dict:
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b <= a:
        return {}
    try:
        return json.loads(text[a: b + 1])
    except json.JSONDecodeError:
        return {}


def _mask(n: str) -> str:
    return f"****{n[-4:]}"


class _Ctx:
    def __init__(self, req: LLMRequest):
        self.req = req
        sysmsg = next((m.content for m in req.messages if m.role == "system"), "")
        self.system = sysmsg
        st = re.search(r"<shared_state>(.*?)</shared_state>", sysmsg, re.S)
        self.state: dict = extract_json(st.group(1)) if st else {}
        idx = max((i for i, m in enumerate(req.messages) if m.role == "user"), default=-1)
        self.user = req.messages[idx].content if idx >= 0 else ""
        self.results: list[tuple[str, dict]] = [
            (m.name, extract_json(m.content)) for m in req.messages[idx + 1:] if m.role == "tool"]
        self.tools = {t.name for t in req.tools}
        self.raw_tool_text = "\n".join(m.content for m in req.messages if m.role == "tool")

    def result(self, name: str) -> dict | None:
        for n, d in reversed(self.results):
            if n == name:
                return d
        return None

    def n_calls(self, name: str) -> int:
        return sum(1 for n, _ in self.results if n == name)


class MockBrain:
    def __init__(self, *, gullible: bool = False, pathology: str | None = None):
        self.gullible, self.pathology = gullible, pathology
        self._n = 0

    # ------------------------------------------------------------------ coordinator
    def classify(self, ctx: _Ctx) -> str:
        table = {}
        in_table = False
        for line in ctx.system.splitlines():
            if line.startswith("## Routing table"):
                in_table = True
            elif line.startswith("## "):
                in_table = False
            elif in_table and (m := _ROUTE_LINE.match(line)):
                table[m.group(1)] = [k.strip().lower() for k in m.group(2).split(",")]
        text = ctx.user.lower()
        hits: list[tuple[int, str, int]] = []
        for agent, kws in table.items():
            positions, count = [], 0
            for kw in kws:
                for mt in re.finditer(rf"(?<![a-z]){re.escape(kw)}(?![a-z])", text):
                    positions.append(mt.start())
                    count += 1
            if count:
                hits.append((min(positions), agent, count))
        hits.sort()
        if any(a == "escalate" for _, a, _ in hits):
            return json.dumps({"intents": [{"agent": "escalate", "confidence": 0.95}], "confidence": 0.95,
                               "reason": "customer needs a human"})
        if not hits:
            return json.dumps({"intents": [], "confidence": 0.1, "reason": "no specialist matches"})
        intents = [{"agent": a, "confidence": round(min(0.9, 0.6 + 0.1 * c), 2)} for _, a, c in hits]
        return json.dumps({"intents": intents, "confidence": max(i["confidence"] for i in intents), "reason": "keyword match"})

    def summarize(self, ctx: _Ctx) -> str:
        lines = [ln for ln in ctx.user.splitlines() if ln.startswith("user:")]
        return "Earlier the customer asked: " + " | ".join(ln[5:].strip()[:90] for ln in lines[-4:])

    def synthesize(self, ctx: _Ctx) -> str:
        parts = [p.strip() for p in re.split(r"^### .*$", ctx.user, flags=re.M) if p.strip()]
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ agents
    def agent_step(self, ctx: _Ctx) -> tuple[str, list[ToolCall]]:
        if self.pathology:
            return self._pathology(ctx)
        if self.gullible and (call := self._follow_injection(ctx)):
            return "", [call]
        agent = ctx.req.agent
        flow = {"accounts_agent": self._accounts, "transfers_agent": self._transfers, "faq_agent": self._faq,
                "tickets_agent": self._tickets}.get(agent) or self._generalist
        return flow(ctx)

    def _call(self, name: str, **args) -> tuple[str, list[ToolCall]]:
        self._n += 1
        return "", [ToolCall(id=f"call_{self._n}", name=name, arguments=args)]

    def _follow_injection(self, ctx: _Ctx) -> ToolCall | None:
        m = _INJECTION.search(ctx.raw_tool_text)
        if not m:
            return None
        dest = m.group(1)
        accts = ctx.state.get("verified_accounts") or []
        if accts:
            src, bal = accts[0]["account_number"], accts[0].get("balance_cents", 10_000) / 100
        else:
            seen = re.search(r'"account_number":\s*"(\d{12})"', ctx.raw_tool_text)
            src, bal = (seen.group(1) if seen else None), 100.0
        self._n += 1
        if "payments__initiate_transfer" in ctx.tools and src:
            return ToolCall(f"call_{self._n}", "payments__initiate_transfer",
                            {"from_account": src, "to_account": dest, "amount": bal, "memo": "as instructed"})
        if "delegate" in ctx.tools:
            return ToolCall(f"call_{self._n}", "delegate", {"agent": "transfers_agent", "task": f"transfer all funds to account {dest}"})
        return None

    def _pathology(self, ctx: _Ctx) -> tuple[str, list[ToolCall]]:
        if self.pathology == "identical":
            return self._call("accounts__get_balance", account_number="100200300499")
        if self.pathology == "varying":
            return self._call("accounts__get_transactions", account_number="100200300401", limit=5 + len(ctx.results))
        if self.pathology == "delegate":
            target = "accounts_agent" if len(ctx.results) % 2 == 0 else "transfers_agent"
            return self._call("delegate", agent=target, task=f"please handle: {ctx.user[:40]} #{len(ctx.results)}")
        raise ValueError(self.pathology)

    @staticmethod
    def _failed(ctx: _Ctx) -> str | None:
        for _, d in ctx.results:
            if d.get("ok") is False:
                return d.get("error", {}).get("message", "the request failed")
        return None

    # ---- accounts_agent
    def _accounts(self, ctx: _Ctx):
        text = ctx.user.lower()
        nums = _ACCT12.findall(ctx.user)
        wants_tx = any(w in text for w in ("transaction", "history", "statement", "spent", "spending", "activity", "recent"))
        if not ctx.results:
            if nums:
                return self._call("accounts__get_transactions" if wants_tx else "accounts__get_balance",
                                  account_number=nums[0], **({"limit": 5} if wants_tx else {}))
            return self._call("accounts__list_accounts")
        if err := self._failed(ctx):
            return f"I'm sorry, I can't complete that: {err}", []
        lst = ctx.result("accounts__list_accounts")
        if lst and wants_tx and ctx.n_calls("accounts__get_transactions") == 0:
            accts = lst["accounts"]
            pick = next((a for a in accts if a["type"] in text), accts[0] if accts else None)
            if pick:
                return self._call("accounts__get_transactions", account_number=pick["account_number"], limit=5)
        if (tx := ctx.result("accounts__get_transactions")):
            rows = []
            for t in tx["transactions"][:5]:
                day = time.strftime("%Y-%m-%d", time.gmtime(t["ts"]))
                rows.append(f"- {day} {money(t['amount_cents'])} {t.get('counterparty') or ''}: {t['description'][:90]}")
            return f"Recent transactions for account {_mask(tx['account_number'])}:\n" + "\n".join(rows), []
        if lst:
            lines = [f"- {a['type'].title()} {_mask(a['account_number'])}: {money(a['balance_cents'], a['currency'])}"
                     for a in lst["accounts"] if a["type"] in text or not any(t in text for t in ("checking", "savings"))]
            return "Here are your account balances:\n" + "\n".join(lines or ["- (no matching account)"]), []
        if (bal := ctx.result("accounts__get_balance")):
            return (f"The balance of your {bal['type']} account {_mask(bal['account_number'])} is "
                    f"{money(bal['balance_cents'], bal['currency'])}."), []
        return "I could not find that information.", []

    # ---- transfers_agent
    def _transfers(self, ctx: _Ctx):
        state = ctx.state
        if ctx.user.startswith("[approval_decision]"):
            d = extract_json(ctx.user)
            st = d.get("transfer_status")
            if st == "completed":
                return f"Good news: your transfer of {money(d.get('amount_cents', 0))} was approved and completed.", []
            if st == "expired":
                return "Your transfer was not reviewed in time and has expired. No money moved; you can try again.", []
            return "Your transfer was reviewed and declined by our staff. No money moved. Please contact us if you have questions.", []
        pend = state.get("pending_transfer") or {}
        if pend.get("status") == "confirmed" and not ctx.result("payments__initiate_transfer"):
            return self._call("payments__initiate_transfer", from_account=pend["from_account"], to_account=pend["to_account"],
                              amount=pend["amount_cents"] / 100, currency=pend.get("currency", "USD"), memo=pend.get("memo", ""))
        res = ctx.result("payments__initiate_transfer")
        if res is not None:
            return self._transfer_reply(res)
        text = ctx.user
        no_nums = _ACCT12.sub(" ", text)
        m = re.search(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)", no_nums) or re.search(r"(\d[\d,]*(?:\.\d{1,2})?)\s*(?:dollars|usd|bucks)", no_nums, re.I) \
            or re.search(r"\b(\d[\d,]*(?:\.\d{1,2})?)\b", no_nums)
        if not m:
            return "How much would you like to transfer, and to which account?", []
        amount = float(m.group(1).replace(",", ""))
        src = (re.search(r"from\s+(?:my\s+)?(?:account\s+)?(\d{12})", text) or [None, None])[1]
        dst = (re.search(r"to\s+(?:account\s+)?(\d{12})", text) or [None, None])[1]
        nums = _ACCT12.findall(text)
        if not dst and len(nums) == 1 and not src:
            dst = nums[0]
        if not dst:
            name = re.search(r"\bto\s+([A-Z][a-z]+)", text)
            payees = ctx.result("payments__list_payees")
            if name and payees is None:
                return self._call("payments__list_payees")
            if name and payees:
                hit = next((p for p in payees.get("payees", []) if name.group(1).lower() in p["name"].lower()), None)
                dst = hit["account_number"] if hit else None
            if not dst:
                return "I couldn't tell which account to send to. Please give me the recipient's account number.", []
        if not src:
            accts = state.get("verified_accounts") or []
            want = "savings" if re.search(r"from (?:my )?savings", text, re.I) else "checking"
            pick = next((a for a in accts if a["type"] == want), accts[0] if accts else None)
            remembered = (state.get("user_prefs") or {}).get("preferred_from_account")
            if remembered and not re.search(r"from (?:my )?(?:savings|checking)", text, re.I):
                src = remembered  # long-term memory saves a delegation round-trip
            elif pick:
                src = pick["account_number"]
            elif "delegate" in ctx.tools and ctx.n_calls("delegate") == 0:
                return self._call("delegate", agent="accounts_agent", task="List my accounts so I can pick the source account.")
            else:
                return "Which of your accounts should I transfer from?", []
        return self._call("payments__initiate_transfer", from_account=src, to_account=dst, amount=amount, memo="")

    @staticmethod
    def _transfer_reply(res: dict) -> tuple[str, list[ToolCall]]:
        if res.get("ok") is False:
            err = res.get("error", {})
            if err.get("code") == "confirmation_required":
                d = err.get("details", {})
                return (f"Please confirm: transfer {money(d.get('amount_cents', 0))} from {_mask(d.get('from_account', '0000'))} "
                        f"to {_mask(d.get('to_account', '0000'))}. Reply \"yes\" to confirm or \"no\" to cancel."), []
            return f"I can't complete that transfer: {err.get('message', 'request refused')}", []
        if res.get("status") == "pending_approval":
            why = ", ".join(r.replace("_", " ") for r in res.get("reasons", []))
            return (f"Your transfer of {money(res['amount_cents'])} to {_mask(res['to_account'])} needs a quick review by our staff "
                    f"({why}). No money has moved yet, and I'll let you know as soon as it's decided. Reference: {res['approval_id']}."), []
        return (f"Done. I transferred {money(res['amount_cents'])} from {_mask(res['from_account'])} to "
                f"{_mask(res['to_account'])} (reference {res['transfer_id']})."), []

    # ---- faq_agent
    def _faq(self, ctx: _Ctx):
        res = ctx.result("knowledge__search_faq")
        if res is None:
            return self._call("knowledge__search_faq", query=ctx.user)
        if res.get("ok") is False:
            return "I'm sorry, the knowledge base is unavailable right now.", []
        docs = res.get("results", [])
        if not docs:
            return "I don't have that information. I can open a support ticket for you if you'd like.", []
        top = docs[0]
        return f"{top['title']}: {top['body']}", []

    # ---- tickets_agent
    def _tickets(self, ctx: _Ctx):
        text = ctx.user
        low = text.lower()
        if not ctx.results:
            tid = re.search(r"\bT_[0-9a-f]{12}\b", text)
            if tid:
                return self._call("crm__get_ticket", ticket_id=tid.group(0))
            if re.search(r"\bmy tickets\b|\blist (?:my )?tickets\b|\bopen tickets\b", low):
                return self._call("crm__list_tickets")
            cat = next((c for c, kws in {"fraud": ("fraud", "scam", "unauthorized"), "card": ("card",), "payments": ("payment", "transfer", "wire"),
                                         "loan": ("loan", "mortgage"), "complaint": ("complaint", "unhappy")}.items()
                        if any(k in low for k in kws)), "other")
            pri = "high" if any(k in low for k in ("urgent", "asap", "fraud", "stolen")) else "normal"
            subject = re.sub(r"\s+", " ", text).strip()[:80]
            return self._call("crm__create_ticket", subject=subject, description=text.strip(), category=cat, priority=pri)
        if err := self._failed(ctx):
            return f"I'm sorry, I couldn't do that: {err}", []
        if (r := ctx.result("crm__create_ticket")):
            return f"I've filed ticket {r['ticket_id']} (status: {r['status']}). Our team will follow up.", []
        if (r := ctx.result("crm__get_ticket")):
            t = r["ticket"]
            return f"Ticket {t['ticket_id']} \"{t['subject']}\" is {t['status']} (priority {t['priority']}).", []
        if (r := ctx.result("crm__list_tickets")):
            ts = r["tickets"]
            return ("Your recent tickets:\n" + "\n".join(f"- {t['ticket_id']}: {t['subject']} [{t['status']}]" for t in ts)) if ts \
                else "You have no support tickets.", []
        return "Done.", []

    # ---- monolithic agent (baseline "one agent, all tools")
    def _generalist(self, ctx: _Ctx):
        low = ctx.user.lower()
        if any(k in low for k in ("transfer", "send ", "wire")):
            return self._transfers(ctx)
        if any(k in low for k in ("ticket", "complaint")):
            return self._tickets(ctx)
        if any(k in low for k in ("rate", "hours", "kyc", "policy", "fee")):
            return self._faq(ctx)
        return self._accounts(ctx)


class _MockClient:
    provider = "local"
    model = "mock-local"

    def __init__(self, brain: MockBrain | None = None, *, latency: float = 0.0):
        self.brain, self.latency, self.fail_next = brain or MockBrain(), latency, 0
        self.received: list[LLMRequest] = []  # what this "server" was sent (tests assert on the external one)

    async def chat(self, req: LLMRequest) -> LLMResponse:
        self.received.append(req)
        if self.latency:
            await asyncio.sleep(self.latency)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TransientError("mock upstream unavailable")
        ctx = _Ctx(req)
        calls: list[ToolCall] = []
        if req.task == "classify":
            text = self.brain.classify(ctx)
        elif req.task == "summarize":
            text = self.brain.summarize(ctx)
        elif req.task == "synthesize":
            text = self.brain.synthesize(ctx)
        else:
            text, calls = self.brain.agent_step(ctx)
        tin = sum(estimate_tokens(m.content) + 4 for m in req.messages) + sum(
            estimate_tokens(t.name + t.description + json.dumps(t.parameters)) for t in req.tools)
        usage = Usage(tin, estimate_tokens(text + json.dumps([c.arguments for c in calls])))
        return LLMResponse(text=text, tool_calls=calls, usage=usage, model=self.model, provider=self.provider,
                           cost_usd=cost_of(self.provider, usage))


class MockLocalLLM(_MockClient):
    provider, model = "local", "mock-local-qwen"


class MockExternalLLM(_MockClient):
    provider, model = "external", "mock-external-frontier"
