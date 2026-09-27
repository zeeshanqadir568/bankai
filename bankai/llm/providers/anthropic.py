"""External frontier model (Anthropic Messages API).

SECURITY: this class is the raw transport and is NEVER handed to agents. It is wrapped by
`GuardedExternalLLM`, which redacts, verifies and logs every payload before `chat` is reached.
"""
from __future__ import annotations

import time

import httpx

from ...resilience import TransientError
from ..base import LLMRequest, LLMResponse, ToolCall, Usage, cost_of


class AnthropicClient:
    provider = "external"

    def __init__(self, api_key: str, model: str, *, base_url: str = "https://api.anthropic.com", timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.model = model
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport, headers={
            "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})

    async def chat(self, req: LLMRequest) -> LLMResponse:
        system = "\n\n".join(m.content for m in req.messages if m.role == "system")
        body: dict = {"model": self.model, "max_tokens": req.max_tokens, "system": system,
                      "messages": self._messages(req)}
        if req.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in req.tools]
        t0 = time.perf_counter()
        try:
            r = await self._http.post("/v1/messages", json=body)
        except httpx.TransportError as e:
            raise TransientError(f"external llm unreachable: {type(e).__name__}") from e
        if r.status_code in (429, 500, 502, 503, 529):
            raise TransientError(f"external llm {r.status_code}")
        r.raise_for_status()
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        calls = [ToolCall(id=b["id"], name=b["name"], arguments=b.get("input") or {})
                 for b in data.get("content", []) if b.get("type") == "tool_use"]
        u = data.get("usage", {})
        usage = Usage(u.get("input_tokens", 0), u.get("output_tokens", 0))
        return LLMResponse(text=text, tool_calls=calls, usage=usage, model=self.model, provider="external",
                           cost_usd=cost_of("external", usage), latency_ms=(time.perf_counter() - t0) * 1000)

    @staticmethod
    def _messages(req: LLMRequest) -> list[dict]:
        out: list[dict] = []

        def push(role: str, block: dict) -> None:
            if out and out[-1]["role"] == role:
                out[-1]["content"].append(block)
            else:
                out.append({"role": role, "content": [block]})

        for m in req.messages:
            if m.role == "system":
                continue
            if m.role == "user":
                push("user", {"type": "text", "text": m.content})
            elif m.role == "assistant":
                if m.content:
                    push("assistant", {"type": "text", "text": m.content})
                for c in m.tool_calls:
                    push("assistant", {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
            elif m.role == "tool":
                push("user", {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content})
        return out

    async def aclose(self) -> None:
        await self._http.aclose()
