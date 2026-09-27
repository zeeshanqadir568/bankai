"""Agent definitions. Each sub-agent owns ONLY its tools (glob patterns resolved against the MCP catalog) and its prompt.

Why not one agent with all ~12 tools? Three measurable reasons (see demos/why_multi_agent.py):
  * prompt bloat   : every request pays for every tool schema, and the bill grows with the tool count
  * selection error: models pick the wrong tool more often as the menu grows (evaluate with `tool_selection` evals on your model)
  * blast radius   : a prompt-injected agent can only misuse the tools it owns — the accounts agent has no transfer tool at all
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentConfig:
    name: str
    prompt: str
    tool_patterns: tuple[str, ...]
    state_keys: tuple[str, ...] = ()
    task: str = "agent"  # llm task class; 'faq_answer' makes the agent's calls external-eligible
    can_delegate_to: tuple[str, ...] = ()
    uses_history: bool = True
    prefetch: tuple[str, ...] = ()  # trusted read-only calls the RUNTIME makes before the model runs (real state beats model guesses)
    prefetch_always: tuple[str, ...] = ()  # like prefetch, but refreshed on EVERY run (balances change)
    action_tool: str = ""  # tool the agent must CALL (not merely talk about) once it has the details; see runner nudge
    unavailable_message: str = "That service is temporarily unavailable. I can still help with other requests."


AGENTS: dict[str, AgentConfig] = {a.name: a for a in [
    AgentConfig("accounts_agent", "accounts_agent", ("accounts__*",), state_keys=("verified_accounts",), prefetch_always=("accounts__list_accounts",),
                unavailable_message="Account information is temporarily unavailable. Please try again shortly."),
    AgentConfig("transfers_agent", "transfers_agent",
                ("payments__list_payees", "payments__initiate_transfer", "payments__get_transfer_status"),
                state_keys=("verified_accounts", "known_payees", "pending_transfer", "user_prefs"),
                can_delegate_to=("accounts_agent",), action_tool="payments__initiate_transfer",
                prefetch=("payments__list_payees", "accounts__list_accounts"),
                unavailable_message="Transfers are temporarily unavailable, so I can't move money right now. Balances, FAQs and tickets still work."),
    AgentConfig("faq_agent", "faq_agent", ("knowledge__*",), task="faq_answer", uses_history=False,
                unavailable_message="The policy knowledge base is temporarily unavailable."),
    AgentConfig("tickets_agent", "tickets_agent", ("crm__*",),
                unavailable_message="Support tickets are temporarily unavailable. Please try again shortly."),
]}

# The anti-pattern, kept only for the comparison demo and the blast-radius test.
MONOLITH = AgentConfig("naive_agent", "naive_agent", ("accounts__*", "payments__list_payees", "payments__initiate_transfer",
                                                        "payments__get_transfer_status", "crm__*", "knowledge__*"),
                       state_keys=("verified_accounts", "known_payees", "pending_transfer", "user_prefs"))

DELEGATE_TOOL = "delegate"
