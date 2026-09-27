"""Session store (software engineering) vs conversation memory (AI engineering) vs blackboard (inter-agent state)."""
import pytest

from bankai.common import clock
from bankai.llm.base import LLMRequest
from bankai.state.blackboard import Blackboard
from bankai.state.sessions import SessionConflict, SessionExpired, SessionNotFound
from bankai.testing import World, make_settings


# ---------------------------------------------------------------- session store: SWE concerns
async def test_session_is_bound_to_its_owner(world):
    s = world.app.sessions.create("alice")
    assert world.app.sessions.get(s.id, "alice").id == s.id
    with pytest.raises(SessionNotFound):  # another user's valid token cannot load it; same error as a bogus id
        world.app.sessions.get(s.id, "bob")
    with pytest.raises(SessionNotFound):
        world.app.sessions.get("ses_doesnotexist", "alice")


async def test_ttl_expiry_and_sliding_window(world):
    ttl = world.app.settings.session_ttl_seconds
    s = world.app.sessions.create("alice")
    clock.advance(ttl - 10)
    live = world.app.sessions.get(s.id, "alice")
    world.app.sessions.save(live)  # activity slides the expiry forward
    clock.advance(ttl - 10)
    assert world.app.sessions.get(s.id, "alice")
    clock.advance(ttl + 10)  # idle past the TTL
    with pytest.raises(SessionExpired):
        world.app.sessions.get(s.id, "alice")


async def test_absolute_lifetime_cap_even_with_constant_activity(world):
    ttl = world.app.settings.session_ttl_seconds
    s = world.app.sessions.create("alice")
    for _ in range(20):
        clock.advance(ttl - 5)
        try:
            world.app.sessions.save(world.app.sessions.get(s.id, "alice"))
        except SessionExpired:
            break
    else:
        pytest.fail("session never hit its absolute lifetime cap")


async def test_purge_removes_expired_sessions_and_their_transcripts(world):
    r = await world.say("alice", "What's my balance?")
    clock.advance(world.app.settings.session_ttl_seconds + 5)
    assert world.app.sessions.purge_expired() == 1
    assert world.app.memory._all(r.session_id) == []


async def test_optimistic_concurrency_rejects_lost_updates(world):
    s = world.app.sessions.create("alice")
    a, b = world.app.sessions.get(s.id, "alice"), world.app.sessions.get(s.id, "alice")
    a.blackboard.flag("x")
    world.app.sessions.save(a)
    b.blackboard.flag("y")
    with pytest.raises(SessionConflict):
        world.app.sessions.save(b)  # b started from stale state


async def test_expired_session_is_rejected_by_the_orchestrator(world):
    r = await world.say("alice", "What's my balance?")
    clock.advance(world.app.settings.session_ttl_seconds + 5)
    with pytest.raises(SessionExpired):
        await world.say("alice", "and again?", r.session_id)


async def test_another_users_session_id_is_rejected(world):
    r = await world.say("alice", "What's my balance?")
    with pytest.raises(SessionNotFound):
        await world.say("bob", "What's my balance?", r.session_id)


# ---------------------------------------------------------------- blackboard: inter-agent state
async def test_blackboard_persists_structured_state_across_turns_and_agents(world):
    r = await world.say("alice", "What's my balance?")  # accounts_agent verifies accounts
    bb = world.app.sessions.get(r.session_id, "alice").blackboard
    assert {a["account_number"] for a in bb.verified_accounts} == {"100200300401", "100200300402"}
    r2 = await world.say("alice", "Transfer $10 to Bob", r.session_id)  # transfers_agent reuses it: no delegation needed
    assert not any(t["tool"] == "accounts__list_accounts" for t in r2.log.tools)
    assert world.app.sessions.get(r.session_id, "alice").blackboard.pending_transfer["status"] == "awaiting_confirmation"


def test_agents_only_see_their_declared_slice_of_the_blackboard():
    bb = Blackboard()
    bb.verified_accounts = [{"account_number": "100200300401", "type": "checking", "currency": "USD", "balance_cents": 1}]
    bb.risk_flags = ["injection_suspected"]
    from bankai.agents.registry import AGENTS
    assert bb.slice(list(AGENTS["faq_agent"].state_keys)) == {}  # the external-eligible FAQ agent sees no account data at all
    assert "100200300401" in bb.prompt_block(list(AGENTS["transfers_agent"].state_keys))
    assert "risk_flags" not in bb.prompt_block(list(AGENTS["transfers_agent"].state_keys))  # models never see/edit their own taint


# ---------------------------------------------------------------- conversation memory: AI-engineering concerns
async def test_short_term_memory_summarises_beyond_the_window():
    w = await World.create(make_settings(memory_window_messages=4))
    try:
        r = await w.say("alice", "What's my balance?")
        for i in range(4):
            await w.say("alice", f"What are your branch hours? ({i})", r.session_id)
        summary, recent = await w.app.memory.context(r.session_id, w.app.router)
        assert len(recent) == 4 and summary.startswith("Earlier the customer asked")  # older turns compressed, recent verbatim
        assert any(q.task == "summarize" and q.agent == "memory" for q in w.app.local.received)  # summarised in-bank, never external
        assert not any(q.task == "summarize" for q in w.app.external_raw.received)
        calls = len(w.app.local.received)
        await w.app.memory.context(r.session_id, w.app.router)  # already summarised: no new LLM call
        assert len(w.app.local.received) == calls
    finally:
        await w.close()


async def test_long_term_memory_is_learned_from_trusted_signals_and_survives_sessions(world):
    r = await world.say("alice", "What's my balance? Please call me Ally")
    assert world.app.memory.prefs("alice")["preferred_name"] == "Ally"
    r = await world.say("alice", "Transfer $10 from savings to Bob", r.session_id)
    await world.say("alice", "yes", r.session_id)
    assert world.app.memory.prefs("alice")["preferred_from_account"] == "100200300402"
    # a brand-new session (e.g. next week) benefits from the remembered source account: no account lookup needed
    r2 = await world.say("alice", "Transfer $5 to Bob")
    assert not any(t["tool"] == "accounts__list_accounts" for t in r2.log.tools) and "****0402" in r2.text
    assert "bob" not in world.app.memory.prefs("alice")  # and nothing leaks across users
    assert world.app.memory.prefs("bob") == {}


async def test_memory_hints_never_grant_authority(world):
    """Even a poisoned/wrong memory (another customer's account as 'preferred') cannot move money: ownership is
    re-checked by the tool layer on every call."""
    world.app.memory.remember("bob", "preferred_from_account", "100200300401")
    r = await world.say("bob", "Transfer $5 to 100200300402")
    r2 = await world.say("bob", "yes", r.session_id)
    # refused EARLY by the source-account guard (not one of Bob's own accounts) or, failing that, by the tool layer's ownership check
    assert any(t["tool"] == "payments__initiate_transfer" and t["code"] in ("access_denied", "unknown_source_account") for t in [*r.log.tools, *r2.log.tools])
    assert world.balance("100200300401") == 845_075 and world.balance("100200300402") == 2_500_000


async def test_right_to_erasure_hook(world):
    await world.say("alice", "Please call me Ally")
    world.app.memory.forget_all("alice")
    assert world.app.memory.prefs("alice") == {}
