"""Blackboard: structured state shared between the coordinator and sub-agents.

Agents do NOT pass state to each other as chat text ("the user has a checking account ending in..."); text is
lossy, injectable and unauditable. They read/write typed fields here. Who may write what:
  * verified_accounts / known_payees : ONLY the runtime, from system-of-record tool results (never from model text)
  * pending_transfer                 : ONLY the runtime guardrail (proposal) and the coordinator (user confirmation)
  * risk_flags                       : ONLY guardrails/sanitizers
Each agent is shown only the slice it declares in `state_keys` (least privilege: the FAQ agent, whose LLM calls may go
external, never sees account data at all).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class Blackboard:
    verified_accounts: list[dict] = field(default_factory=list)  # {account_number,type,currency,balance_cents}
    known_payees: list[dict] = field(default_factory=list)  # {name,account_number}
    pending_transfer: dict | None = None  # {from_account,to_account,amount_cents,currency,memo,status}
    risk_flags: list[str] = field(default_factory=list)  # sticky for the session (taint)
    pending_approvals: list[str] = field(default_factory=list)
    user_prefs: dict = field(default_factory=dict)  # long-term memory slice (hints only, never authority)
    last_agent: str = ""

    def flag(self, name: str) -> bool:
        """Returns True if newly raised."""
        if name in self.risk_flags:
            return False
        self.risk_flags.append(name)
        return True

    def slice(self, keys: list[str]) -> dict:
        d = asdict(self)
        return {k: d[k] for k in keys if k in d and d[k] not in (None, [], {}, "")}

    def prompt_block(self, keys: list[str]) -> str:
        return "<shared_state>" + json.dumps(self.slice(keys), sort_keys=True) + "</shared_state>"

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "Blackboard":
        d = json.loads(s or "{}")
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
