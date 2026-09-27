"""Timeouts, retries with backoff, circuit breakers, graceful degradation, secrets/config."""
import asyncio

import pytest

from bankai.common import clock
from bankai.config import Settings, SecretStore
from bankai.llm.base import LLMRequest, Message
from bankai.llm.providers.mock import MockBrain, MockLocalLLM
from bankai.resilience import CircuitBreaker, CircuitOpenError, TransientError, resilient_call
from bankai.testing import World, make_settings

DEAD = "http://127.0.0.1:9/mcp"  # nothing listens here: connection refused


# ---------------------------------------------------------------- primitives
def test_breaker_opens_half_opens_and_recovers():
    b = CircuitBreaker("x", failure_threshold=3, reset_seconds=10)
    for _ in range(3):
        b.before_call()
        b.on_failure()
    assert b.state == "open"
    with pytest.raises(CircuitOpenError):
        b.before_call()  # fail fast, dependency is not hammered
    clock.advance(11)
    assert b.state == "half_open"
    b.before_call()  # exactly one probe is allowed
    with pytest.raises(CircuitOpenError):
        b.before_call()
    b.on_failure()  # probe failed -> open again
    assert b.state == "open"
    clock.advance(11)
    b.before_call()
    b.on_success()
    assert b.state == "closed"


async def test_retry_with_backoff_then_success_and_non_transient_errors_are_not_retried():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("blip")
        return "ok"

    assert await resilient_call(flaky, attempts=3, base_delay=0.001) == "ok" and calls["n"] == 3

    calls["n"] = 0

    async def logic_error():
        calls["n"] += 1
        raise ValueError("not transient")

    with pytest.raises(ValueError):
        await resilient_call(logic_error, attempts=3, base_delay=0.001)
    assert calls["n"] == 1


async def test_timeout_is_enforced_and_retried_then_raised():
    async def slow():
        await asyncio.sleep(1)

    with pytest.raises(asyncio.TimeoutError):
        await resilient_call(slow, timeout=0.02, attempts=2, base_delay=0.001)


# ---------------------------------------------------------------- LLM failure modes
async def test_llm_breaker_opens_and_stops_hammering_a_dead_model(world):
    world.app.local.fail_next = 10_000
    req = LLMRequest([Message("system", "s"), Message("user", "hi")], task="classify")
    for _ in range(2):  # the breaker trips part-way through the 2nd call's retries
        with pytest.raises((TransientError, CircuitOpenError)):
            await world.app.router.chat(req)
    before = len(world.app.local.received)
    with pytest.raises(CircuitOpenError):
        await world.app.router.chat(req)
    assert len(world.app.local.received) == before  # no call reached the dead model
    assert world.app.router.breakers["local"].state == "open"
    r = await world.say("alice", "What's my balance?")  # users get a message, not a stack trace
    assert r.outcome == "llm_unavailable" and "trouble" in r.text.lower()


async def test_llm_timeout_degrades_gracefully():
    w = await World.create(make_settings(llm_timeout_seconds=0.05), local=MockLocalLLM(MockBrain(), latency=0.5))
    try:
        r = await w.say("alice", "What's my balance?")
        assert r.outcome == "llm_unavailable"
    finally:
        await w.close()


# ---------------------------------------------------------------- MCP failure modes: blast radius of an outage
async def test_transfers_service_down_other_agents_still_work():
    w = await World.create(mcp_targets={"payments": DEAD})
    try:
        assert "payments" in w.app.gateway.unavailable
        bal = await w.say("alice", "What's my balance?")
        faq = await w.say("alice", "What are your branch hours?")
        tk = await w.say("alice", "Please open a ticket: my card was declined")
        assert "8,450.75" in bal.text and "Branches are open" in faq.text and "ticket" in tk.text.lower()
        tr = await w.say("alice", "Transfer $10 to Bob")
        assert "Transfers are temporarily unavailable" in tr.text and w.transfers() == []
        assert not any(t["tool"].startswith("payments__") for t in tr.log.tools)  # not even attempted
    finally:
        await w.close()


async def test_mid_flight_mcp_outage_opens_the_breaker_and_recovers():
    w = await World.create()
    try:
        healthy = w.app.gateway.targets["payments"]
        w.app.gateway.targets["payments"] = DEAD  # server dies after startup
        from bankai.agents.registry import AGENTS
        from bankai.mcp_layer.gateway import CallAuth
        scopes = [t.fq for t in w.app.gateway.tools_for(list(AGENTS["transfers_agent"].tool_patterns))]
        auth = CallAuth(w.ident("alice"), "transfers_agent", scopes)
        codes = [(await w.app.gateway.call("payments__list_payees", {}, auth)).code for _ in range(4)]
        assert set(codes) == {"service_unavailable"} and w.app.gateway.breakers["payments"].state == "open"
        w.app.gateway.targets["payments"] = healthy  # server comes back
        clock.advance(w.app.settings.breaker_reset_seconds + 1)
        assert (await w.app.gateway.call("payments__list_payees", {}, auth)).ok  # half-open probe succeeds
        assert w.app.gateway.breakers["payments"].state == "closed"
    finally:
        await w.close()


async def test_mutating_retry_after_transient_failure_cannot_double_spend(world):
    """The transport retried; the server already committed. The idempotency key makes the replay harmless."""
    from bankai.mcp_layer.gateway import CallAuth
    auth = CallAuth(world.ident("alice"), "transfers_agent", ["payments__initiate_transfer"])
    args = {"from_account": "100200300401", "to_account": "100200300501", "amount": 10}
    for _ in range(3):
        await world.app.gateway.call("payments__initiate_transfer", args, auth, idempotency_key="retry-1")
    assert world.balance("100200300501") == 321_000 and len(world.transfers()) == 1


# ---------------------------------------------------------------- config & secrets
def test_secrets_never_default_outside_dev(tmp_path):
    with pytest.raises(RuntimeError, match="jwt_signing_key"):
        SecretStore("prod", str(tmp_path)).get("jwt_signing_key")
    (tmp_path / "jwt_signing_key").write_text("s3cret\n")
    assert SecretStore("prod", str(tmp_path)).get("jwt_signing_key") == "s3cret"  # mounted secret file wins, trimmed
    assert SecretStore("dev", str(tmp_path / "none")).get("jwt_signing_key").startswith("dev-only")


def test_settings_come_from_environment(monkeypatch):
    monkeypatch.setenv("MAX_ITERATIONS", "9")
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "false")
    monkeypatch.setenv("APPROVAL_THRESHOLD_CENTS", "5000")
    s = Settings.from_env()
    assert s.max_iterations == 9 and s.external_llm_enabled is False and s.approval_threshold_cents == 5000


def test_no_secret_material_in_source_outside_the_dev_defaults():
    import pathlib
    root = pathlib.Path(__file__).parent.parent / "bankai"
    offenders = [str(p) for p in root.rglob("*.py") if "not-for-production" in p.read_text(encoding="utf-8") and p.name != "config.py"]
    assert offenders == []


async def test_active_probe_detects_a_dead_tool_server_and_notices_recovery():
    """/readyz relies on this: health is discovered by probing, not only by a user's request failing."""
    w = await World.create()
    try:
        healthy = w.app.gateway.targets["crm"]
        w.app.gateway.targets["crm"] = DEAD
        await w.app.gateway.probe()
        assert w.app.gateway.unavailable == {"crm"} and not w.app.gateway.tools_for(["crm__*"])
        tk = await w.say("alice", "Please open a ticket: my card was declined")  # no LLM wasted; honest message
        assert "Support tickets are temporarily unavailable" in tk.text and not tk.log.tools
        w.app.gateway.targets["crm"] = healthy
        await w.app.gateway.probe()
        assert w.app.gateway.unavailable == set() and w.app.gateway.tools_for(["crm__*"])
    finally:
        await w.close()
