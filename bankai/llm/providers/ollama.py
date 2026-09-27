"""In-bank model served by Ollama (or any Ollama-compatible /api/chat server such as llama.cpp's). Never leaves the network."""
from __future__ import annotations

import json
import re
import time

import httpx

from ...resilience import TransientError
from ..base import LLMRequest, LLMResponse, ToolCall, Usage, cost_of


class OllamaClient:
    provider = "local"

    def __init__(self, base_url: str, model: str, *, timeout: float = 60.0, transport: httpx.AsyncBaseTransport | None = None,
                 think: bool = False):
        self.model, self.think = model, think
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def chat(self, req: LLMRequest) -> LLMResponse:
        body: dict = {
            "model": self.model, "stream": False,
            "messages": [self._msg(m) for m in req.messages], "think": self.think, "keep_alive": "30m",  # reasoning models (qwen3...) otherwise burn the token budget thinking
            "options": {"num_predict": req.max_tokens, "temperature": 0.0},
        }
        if req.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                "parameters": t.parameters}} for t in req.tools]
        if req.task == "classify":
            body["format"] = "json"
        t0 = time.perf_counter()
        try:
            r = await self._http.post("/api/chat", json=body)
        except httpx.TransportError as e:
            raise TransientError(f"ollama unreachable: {type(e).__name__}") from e
        if r.status_code >= 500:
            raise TransientError(f"ollama {r.status_code}")
        r.raise_for_status()
        data = r.json()
        msg = data.get("message", {})
        calls = [ToolCall(id=f"call_{i}", name=c["function"]["name"], arguments=c["function"].get("arguments") or {})
                 for i, c in enumerate(msg.get("tool_calls") or [])]
        usage = Usage(data.get("prompt_eval_count", 0), data.get("eval_count", 0))
        text = re.sub(r"<think>.*?</think>", "", msg.get("content", ""), flags=re.S).strip()  # older servers inline the reasoning
        return LLMResponse(text=text, tool_calls=calls, usage=usage, model=self.model, provider="local",
                           cost_usd=cost_of("local", usage), latency_ms=(time.perf_counter() - t0) * 1000)

    @staticmethod
    def _msg(m) -> dict:  # noqa: ANN001
        out: dict = {"role": m.role, "content": m.content}
        if m.tool_calls:
            out["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls]
        if m.role == "tool":
            out["tool_name"] = m.name
        return out

    async def aclose(self) -> None:
        await self._http.aclose()
