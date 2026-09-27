"""Provider-neutral LLM types. Agents talk to `LLMClient`; which model answers is a routing decision, not agent code."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# Illustrative per-million-token prices (USD). Real deployments load these from config / the provider's price sheet.
PRICING: dict[str, tuple[float, float]] = {
    "local": (0.10, 0.10),  # amortised GPU/infra cost of the in-bank model
    "external": (3.00, 15.00),
}

# Tasks that may leave the bank network AFTER redaction. Everything else is in-bank only.
EXTERNAL_ELIGIBLE_TASKS = {"faq_answer", "general"}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""
    # public | restricted | unclassified. Tool results inherit the tool's data_class; restricted content can never
    # be sent to an external model, whatever the PII detector says.
    data_class: str = "unclassified"


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class LLMRequest:
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    task: str = "agent"  # agent | classify | synthesize | summarize | faq_answer | general
    agent: str = ""
    max_tokens: int = 1024
    trace_id: str = ""


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""  # local | external
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    route_reason: str = ""
    redactions: int = 0


class LLMClient(Protocol):
    provider: str
    model: str

    async def chat(self, req: LLMRequest) -> LLMResponse: ...


def cost_of(provider: str, usage: Usage) -> float:
    pin, pout = PRICING.get(provider, PRICING["external"])
    return (usage.tokens_in * pin + usage.tokens_out * pout) / 1_000_000
