"""Real provider clients, exercised against fake HTTP servers (no network, no keys): wire format + failure mapping,
and the whole external path (guard + real Anthropic client) proving what actually goes over the wire."""
import json

import httpx
import pytest

from bankai.llm.base import LLMRequest, Message, ToolCall, ToolSpec
from bankai.llm.guarded import GuardedExternalLLM
from bankai.llm.providers.anthropic import AnthropicClient
from bankai.llm.providers.ollama import OllamaClient
from bankai.pii import PIIDetector, Redactor
from bankai.resilience import TransientError

TOOL = ToolSpec("accounts__get_balance", "Get balance", {"type": "object", "properties": {"account_number": {"type": "string"}}})


async def test_ollama_request_and_tool_call_parsing():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"], seen["path"] = json.loads(request.content), request.url.path
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "accounts__get_balance", "arguments": {"account_number": "100200300401"}}}]},
            "prompt_eval_count": 120, "eval_count": 15})

    c = OllamaClient("http://ollama:11434", "qwen2.5", transport=httpx.MockTransport(handler))
    r = await c.chat(LLMRequest([Message("system", "s"), Message("user", "balance?"),
                                 Message("assistant", "", tool_calls=[ToolCall("1", "x", {"a": 1})]),
                                 Message("tool", "{}", name="x", tool_call_id="1")], [TOOL], task="agent"))
    assert seen["path"] == "/api/chat" and seen["body"]["stream"] is False and seen["body"]["tools"][0]["function"]["name"] == "accounts__get_balance"
    assert seen["body"]["messages"][2]["tool_calls"][0]["function"]["arguments"] == {"a": 1} and seen["body"]["messages"][3]["tool_name"] == "x"
    assert r.tool_calls[0].arguments == {"account_number": "100200300401"} and (r.usage.tokens_in, r.usage.tokens_out) == (120, 15)
    assert r.provider == "local" and r.cost_usd > 0
    r2 = await OllamaClient("http://o", "m", transport=httpx.MockTransport(lambda q: httpx.Response(200, json={"message": {"content": "{}"}}))).chat(
        LLMRequest([Message("user", "x")], task="classify"))
    assert r2.text == "{}"


@pytest.mark.parametrize("status", [500, 503])
async def test_ollama_server_errors_are_transient(status):
    c = OllamaClient("http://o", "m", transport=httpx.MockTransport(lambda q: httpx.Response(status)))
    with pytest.raises(TransientError):
        await c.chat(LLMRequest([Message("user", "x")]))


async def test_ollama_connection_failure_is_transient():
    def boom(request):
        raise httpx.ConnectError("refused")
    with pytest.raises(TransientError):
        await OllamaClient("http://o", "m", transport=httpx.MockTransport(boom)).chat(LLMRequest([Message("user", "x")]))


async def test_anthropic_message_conversion_and_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"], seen["headers"] = json.loads(request.content), request.headers
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "tu_1", "name": "accounts__get_balance", "input": {"a": "b"}}],
                                         "usage": {"input_tokens": 50, "output_tokens": 9}})

    c = AnthropicClient("sk-test", "claude-sonnet-5", transport=httpx.MockTransport(handler))
    r = await c.chat(LLMRequest([Message("system", "SYS"), Message("user", "q"),
                                 Message("assistant", "thinking", tool_calls=[ToolCall("tu_0", "accounts__get_balance", {"x": 1})]),
                                 Message("tool", "result", tool_call_id="tu_0", name="accounts__get_balance")], [TOOL]))
    b = seen["body"]
    assert seen["headers"]["x-api-key"] == "sk-test" and seen["headers"]["anthropic-version"] and b["model"] == "claude-sonnet-5" and b["system"] == "SYS"
    assert b["tools"][0]["input_schema"]["type"] == "object"
    assert [m["role"] for m in b["messages"]] == ["user", "assistant", "user"]  # roles alternate; tool_result rides in a user turn
    assert b["messages"][1]["content"][1] == {"type": "tool_use", "id": "tu_0", "name": "accounts__get_balance", "input": {"x": 1}}
    assert b["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu_0", "content": "result"}
    assert r.text == "hi" and r.tool_calls[0].id == "tu_1" and r.provider == "external" and (r.usage.tokens_in, r.usage.tokens_out) == (50, 9)


@pytest.mark.parametrize("status", [429, 500, 529])
async def test_anthropic_rate_limits_and_overload_are_transient(status):
    c = AnthropicClient("k", "m", transport=httpx.MockTransport(lambda q: httpx.Response(status)))
    with pytest.raises(TransientError):
        await c.chat(LLMRequest([Message("user", "x")]))


async def test_real_external_client_behind_the_guard_sends_only_redacted_text_and_restores_it():
    """What the wire actually carries when the REAL client class sits behind the guard."""
    det = PIIDetector(["Alice Johnson"])
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["raw"] = request.content.decode()
        placeholder = next(w for w in json.loads(request.content)["messages"][0]["content"][0]["text"].split() if w.startswith("<PERSON"))
        return httpx.Response(200, json={"content": [{"type": "text", "text": f"Hello {placeholder.strip('.,')}, our hours are 9-5."}], "usage": {}})

    raw = AnthropicClient("k", "m", transport=httpx.MockTransport(handler))
    g = GuardedExternalLLM(raw, Redactor(det))
    r = await g.chat(LLMRequest([Message("system", "FAQ bot"), Message("user", "I am Alice Johnson, alice.johnson@example.com. Hours?")], task="faq_answer"))
    assert "Alice" not in seen["raw"] and "alice.johnson@example.com" not in seen["raw"] and "<PERSON_1>" in seen["raw"] and "<EMAIL_1>" in seen["raw"]
    assert r.text == "Hello Alice Johnson, our hours are 9-5." and r.redactions == 2
