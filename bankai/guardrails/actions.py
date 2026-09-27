"""Guardrail layer 3 — the action guard: what an agent is ALLOWED TO ATTEMPT, validated in trusted code before any
tool call is even sent. (Layer 2, the tool servers' own authz/policy, is the non-negotiable backstop behind it.)

Checks, in order:
  1. allow-list        the tool exists in this agent's allowed set (the model can hallucinate tool names)
  2. argument hygiene  no unknown arguments (a model must not smuggle `auth_token`, `idempotency_key`, ...)
  3. provenance        any account-number-like argument must be grounded in what the CUSTOMER typed, their verified
                       accounts, or their saved payees. A number that only ever appeared in a tool result / document
                       is, by construction, not something the customer asked for.
  4. confirmation gate money movement executes only after the customer confirmed the EXACT details in a later turn
                       (state kept in the blackboard by trusted code; the model cannot set it)
  5. anomaly notes     amount / new-payee / drain heuristics, surfaced in the confirmation prompt and risk flags
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Settings
from ..llm.base import ToolCall
from ..pii import PIIDetector, Redactor
from ..state.blackboard import Blackboard

_NUM_RUN = re.compile(r"(?<![\d])\d(?:[ -]?\d){9,15}(?!\d)")
AFFIRM = re.compile(r"^\s*(?:yes|yep|yeah|confirm(?:ed)?|approve|go ahead|do it|proceed|ok(?:ay)?|sure)\b[\s.!]*$", re.I)
DENY = re.compile(r"^\s*(?:no|nope|cancel|stop|abort|don'?t|do not)\b[\s.!]*$", re.I)


def digit_runs(text: str) -> set[str]:
    return {re.sub(r"\D", "", m.group(0)) for m in _NUM_RUN.finditer(text or "")}


@dataclass
class GuardConfig:
    """Layers can be switched off ONLY in demos/evals that show what each layer buys. Production: all True."""

    sanitize: bool = True
    provenance: bool = True
    confirmation: bool = True
    taint: bool = True
    output: bool = True


@dataclass
class Verdict:
    allow: bool
    code: str = ""
    message: str = ""
    details: dict = field(default_factory=dict)
    flag: str | None = None


ALLOW = Verdict(True)
MONEY_TOOL = "payments__initiate_transfer"


class ActionGuard:
    def __init__(self, settings: Settings, cfg: GuardConfig | None = None):
        self.s, self.cfg = settings, cfg or GuardConfig()

    def validate(self, call: ToolCall, *, allowed: dict[str, dict], bb: Blackboard, trusted_user_text: str) -> Verdict:
        spec = allowed.get(call.name)
        if spec is None:
            return Verdict(False, "not_allowed", f"'{call.name}' is not an available tool for this agent.", flag="hallucinated_tool")
        props = set((spec.get("properties") or {}).keys())
        extra = set(call.arguments) - props
        if extra:
            return Verdict(False, "invalid_arguments", f"Unexpected arguments: {sorted(extra)}", flag="argument_smuggling")
        missing = [k for k in spec.get("required", []) if k not in call.arguments]
        if missing:
            return Verdict(False, "invalid_arguments", f"Missing required arguments: {missing}")

        if self.cfg.provenance:
            grounded = digit_runs(trusted_user_text) | {a["account_number"] for a in bb.verified_accounts} \
                | {p["account_number"] for p in bb.known_payees}
            grounded |= digit_runs(" ".join(str(v) for v in bb.user_prefs.values()))  # long-term memory: trusted-signal writes only
            for k, v in call.arguments.items():
                for run in digit_runs(str(v)):
                    if run not in grounded:
                        return Verdict(False, "blocked_ungrounded",
                                       "The account number in this request did not come from the customer, so it was blocked.",
                                       flag="ungrounded_account_number")
        if call.name == MONEY_TOOL:
            return self._money(call, bb, trusted_user_text)
        return ALLOW

    def _money(self, call: ToolCall, bb: Blackboard, trusted_user_text: str = "") -> Verdict:
        a = call.arguments
        try:
            cents = int(round(float(a["amount"]) * 100))
        except (TypeError, ValueError):
            return Verdict(False, "invalid_arguments", "Amount is not a number.")
        params = {"from_account": a["from_account"], "to_account": a["to_account"], "amount_cents": cents,
                  "currency": a.get("currency", "USD"), "memo": a.get("memo", "")}
        named = [x for x in bb.known_payees if any(len(t) >= 3 and re.search(rf"\b{re.escape(t)}\b", trusted_user_text, re.I)
                                                  for t in x["name"].split())]
        if named and a["to_account"] not in {x["account_number"] for x in named} and a["to_account"] not in digit_runs(trusted_user_text):
            who = named[0]
            return Verdict(False, "destination_mismatch",
                           f"The customer named the payee '{who['name']}' but this destination is a different account. "
                           f"Use that payee's account number {who['account_number']}.", flag=None)
        own = {x["account_number"] for x in bb.verified_accounts}
        if own and a["from_account"] not in own and a["from_account"] not in digit_runs(trusted_user_text):
            return Verdict(False, "unknown_source_account",
                           "from_account must be one of the customer's own accounts: " + ", ".join(sorted(own)) + ".")
        if not self.cfg.confirmation:
            return ALLOW
        pend = bb.pending_transfer
        key = ("from_account", "to_account", "amount_cents", "currency")
        if pend and pend.get("status") == "confirmed" and all(pend.get(k) == params[k] for k in key):
            pend["status"] = "submitted"
            return ALLOW
        notes = self.assess(params, bb)
        bb.pending_transfer = {**params, "status": "awaiting_confirmation", "notes": notes}
        return Verdict(False, "confirmation_required",
                       "Ask the customer to confirm these exact details before any money moves.",
                       details={**params, "notes": notes})

    def assess(self, p: dict, bb: Blackboard) -> list[str]:
        """Runtime-side anomaly notes (early warning). Enforcement lives in payments-mcp, which recomputes from the ledger."""
        notes: list[str] = []
        own = {x["account_number"] for x in bb.verified_accounts}
        if p["to_account"] not in own and p["to_account"] not in {x["account_number"] for x in bb.known_payees}:
            notes.append("new_payee")
        if p["amount_cents"] >= self.s.approval_threshold_cents:
            notes.append("amount_over_threshold")
        src = next((x for x in bb.verified_accounts if x["account_number"] == p["from_account"]), None)
        if src and p["amount_cents"] >= 0.8 * src["balance_cents"]:
            notes.append("drains_account")
        return notes


class OutputGuard:
    """Final-response check: never show an account/card number the customer has no claim to (exfiltration via prose)."""

    def __init__(self, detector: PIIDetector, redactor: Redactor):
        self.detector, self.redactor = detector, redactor

    def check(self, text: str, *, allowed_numbers: set[str]) -> tuple[str, list[str]]:
        leaks, out, last = [], [], 0
        for m in self.detector.find(text):
            digits = re.sub(r"\D", "", m.value)
            if m.kind in ("ACCOUNT", "CARD") and digits not in allowed_numbers:
                leaks.append(m.kind)
                out.append(text[last:m.start])
                out.append(f"****{digits[-4:]}")
                last = m.end
        out.append(text[last:])
        return "".join(out), leaks
