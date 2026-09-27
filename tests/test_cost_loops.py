"""Cost tracking and runaway-loop protection: the 'bill that tripled' scenarios."""
from dataclasses import replace

from bankai.agents.registry import AGENTS
from bankai.testing import World, make_settings


async def test_identical_retry_loop_is_stopped_by_the_loop_detector():
    w = await World.create(pathology="identical")
    try:
        r = await w.say("alice", "Show balance of account 100200300499")
        assert r.outcome == "budget_stopped" and r.log.budget_stopped == "loop"
        assert len(r.log.tools) == 3  # max_identical_calls: the 4th identical attempt never ran
        assert "human" in r.text.lower() and "repeating" in r.text
        assert len(w.app.local.received) <= 6  # not 6+ iterations of tokens
    finally:
        await w.close()


async def test_varying_retry_loop_is_stopped_by_the_iteration_cap():
    w = await World.create(make_settings(max_iterations=4), pathology="varying")
    try:
        r = await w.say("alice", "Show my recent transactions for 100200300401")
        assert r.log.budget_stopped == "iterations" and len(r.log.tools) == 4
        assert w.app.cost.spent_since("alice") > 0  # spend was recorded even for the aborted turn
    finally:
        await w.close()


async def test_delegation_ping_pong_is_stopped_by_the_depth_cap():
    agents = {**AGENTS, "accounts_agent": replace(AGENTS["accounts_agent"], can_delegate_to=("transfers_agent",)),
              "transfers_agent": replace(AGENTS["transfers_agent"], can_delegate_to=("accounts_agent",))}
    w = await World.create(pathology="delegate", agents=agents)
    try:
        r = await w.say("alice", "Show my balance")
        assert r.log.budget_stopped == "depth" and "hand-offs" in r.text
    finally:
        await w.close()


async def test_token_budget_per_request():
    w = await World.create(make_settings(max_request_tokens=900))
    try:
        r = await w.say("alice", "What's my balance?")
        assert r.log.budget_stopped == "tokens" and "processing" in r.text
    finally:
        await w.close()


async def test_cost_budget_per_request():
    w = await World.create(make_settings(max_request_cost_usd=1e-6))
    try:
        r = await w.say("alice", "What's my balance?")
        assert r.log.budget_stopped == "cost"
    finally:
        await w.close()


async def test_with_per_loop_limits_lifted_the_token_and_call_budgets_still_bound_the_bill():
    """Defence in depth: even if iteration/identical-call limits were misconfigured, context growth trips the token
    budget (or the LLM-call cap) long before the spend is unbounded."""
    w = await World.create(make_settings(max_iterations=50, max_identical_calls=50), pathology="varying")
    try:
        r = await w.say("alice", "Show my recent transactions for 100200300401")
        assert r.log.budget_stopped in {"tokens", "llm_calls", "cost"} and len(w.app.local.received) <= 26
    finally:
        await w.close()


async def test_per_user_daily_cap_blocks_before_any_llm_call():
    w = await World.create(make_settings(user_daily_cost_usd=0.0002))
    try:
        for _ in range(6):
            await w.say("alice", "What's my balance?")
        calls_before = len(w.app.local.received)
        r = await w.say("alice", "What's my balance?")
        assert r.log.budget_stopped == "daily_cost" and "today's usage limit" in r.text
        assert len(w.app.local.received) == calls_before  # refused up front: zero marginal cost
        assert (await w.say("bob", "What's my balance?")).outcome == "ok"  # other users unaffected
    finally:
        await w.close()


async def test_session_is_usable_after_a_runaway_is_stopped():
    w = await World.create(make_settings(max_iterations=3), pathology="varying")
    try:
        r = await w.say("alice", "Show my recent transactions for 100200300401")
        assert r.outcome == "budget_stopped"
        w.app.local.brain.pathology = None  # the model recovers
        ok = await w.say("alice", "What's my balance?", r.session_id)
        assert ok.outcome == "ok" and "8,450.75" in ok.text
    finally:
        await w.close()


async def test_usage_is_tracked_per_user_session_agent_and_provider(world):
    r = await world.say("alice", "What's my balance?")
    await world.say("alice", "What are your branch hours?", r.session_id)
    await world.say("bob", "What's my balance?")
    by_agent = {row["key"]: row for row in world.app.cost.report("agent", user_id="alice")}
    assert {"coordinator", "accounts_agent", "faq_agent"} <= set(by_agent)
    assert all(row["tokens"] > 0 and row["cost_usd"] > 0 for row in by_agent.values())
    by_provider = {row["key"]: row for row in world.app.cost.report("provider")}
    assert by_provider["external"]["cost_usd"] > by_provider["local"]["cost_usd"] / 1000  # both providers metered
    assert {row["key"] for row in world.app.cost.report("user")} == {"alice", "bob"}
    assert world.app.cost.report("session", session_id=r.session_id)[0]["calls"] >= 5


async def test_one_specialist_means_no_extra_synthesis_call(world):
    r = await world.say("alice", "What's my balance?")
    assert not any(l["task"] == "synthesize" for l in r.log.llm)
    r2 = await world.say("alice", "What's my balance and what are your branch hours?")
    assert r2.log.routes == ["accounts_agent", "faq_agent"] and any(l["task"] == "synthesize" for l in r2.log.llm)
    assert "8,450.75" in r2.text and "Branches are open" in r2.text
