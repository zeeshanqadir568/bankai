"""Human-in-the-loop approvals, queues, and async workers."""
import asyncio

import fakeredis
import pytest
from sqlalchemy import select

from bankai.common import clock
from bankai.db import app_notifications, app_paused_runs
from bankai.queueing.queue import MAX_ATTEMPTS, InMemoryQueue, RedisQueue
from bankai.testing import World, make_settings

MALLORY, BOB = "999000999000", "100200300501"


async def request_big_transfer(w, text="Transfer $1500 from savings to Bob"):
    r = await w.say("alice", text)
    r = await w.say("alice", "yes", r.session_id)
    return r


# ---------------------------------------------------------------- pause -> approve -> resume
async def test_large_transfer_pauses_then_resumes_on_approval(world):
    r = await request_big_transfer(world)
    assert r.log.approvals and "review by our staff" in r.text
    assert world.balance(BOB) == 320_000  # nothing moved while waiting
    with world.app.engine.connect() as conn:
        assert conn.execute(select(app_paused_runs)).mappings().all()  # durable pause record

    approvals = (await world.app.staff_list_approvals(world.ident("sam"))).data["approvals"]
    assert len(approvals) == 1 and approvals[0]["reasons"] == ["amount_over_threshold"] and approvals[0]["requested_by"] == "alice"
    decision = await world.app.staff_decide(world.ident("sam"), approvals[0]["approval_id"], True)
    assert decision.data["transfer_status"] == "completed"
    assert world.balance(BOB) == 470_000  # $1,500 moved only now

    await world.pump()  # approvals.decided -> resume the paused conversation, notify the customer
    with world.app.engine.connect() as conn:
        note = conn.execute(select(app_notifications)).mappings().one()
    assert note["user_id"] == "alice" and "approved and completed" in note["body"]
    bb = world.app.sessions.get(r.session_id, "alice").blackboard
    assert bb.pending_approvals == []
    with world.app.engine.connect() as conn:
        assert conn.execute(select(app_paused_runs)).mappings().all() == []  # pause consumed


async def test_denial_moves_nothing_and_notifies(world):
    r = await request_big_transfer(world)
    aid = r.log.approvals[0]
    d = await world.app.staff_decide(world.ident("sam"), aid, False, "suspicious")
    assert d.data["transfer_status"] == "denied" and world.balance(BOB) == 320_000
    await world.pump()
    with world.app.engine.connect() as conn:
        body = conn.execute(select(app_notifications.c.body)).scalar()
    assert "declined" in body and "No money moved" in body


async def test_approval_timeout_expires_the_transfer_and_tells_the_customer():
    w = await World.create(make_settings(approval_timeout_seconds=60))
    try:
        r = await request_big_transfer(w)
        clock.advance(120)
        assert await w.app.payments.expire_overdue() == 1  # the sweeper
        assert w.balance(BOB) == 320_000 and [t["status"] for t in w.transfers()] == ["expired"]
        await w.pump()
        with w.app.engine.connect() as conn:
            assert "expired" in conn.execute(select(app_notifications.c.body)).scalar()
        late = await w.app.staff_decide(w.ident("sam"), r.log.approvals[0], True)  # too late: cannot approve after expiry
        assert not late.ok and late.code == "already_decided" and w.balance(BOB) == 320_000
    finally:
        await w.close()


async def test_approving_after_deadline_without_sweeper_still_expires_instead_of_executing():
    w = await World.create(make_settings(approval_timeout_seconds=60))
    try:
        r = await request_big_transfer(w)
        clock.advance(120)
        d = await w.app.staff_decide(w.ident("sam"), r.log.approvals[0], True)
        assert d.data["status"] == "expired" and w.balance(BOB) == 320_000
    finally:
        await w.close()


async def test_only_approvers_can_decide_and_never_their_own_request(world):
    r = await request_big_transfer(world)
    aid = r.log.approvals[0]
    for who in ("alice", "bob", "tina"):  # customers and tellers are refused by the tool layer
        assert (await world.app.staff_decide(world.ident(who), aid, True)).code in {"access_denied", "out_of_scope"}
    assert world.balance(BOB) == 320_000
    # separation of duties: an approver who is also the initiator cannot self-approve
    from bankai.security.identity import Identity
    sam_customer = Identity("alice", frozenset({"approver", "customer"}), "cust_alice")
    d = await world.app.staff_decide(sam_customer, aid, True)
    assert d.code == "access_denied" and "separation" in d.message.lower()


async def test_agents_cannot_reach_the_approval_tools_even_with_an_approver_identity(world):
    """The staff tools live on payments-mcp but no agent's scope includes them."""
    from bankai.agents.registry import AGENTS
    from bankai.mcp_layer.gateway import CallAuth
    scopes = [t.fq for t in world.app.gateway.tools_for(list(AGENTS["transfers_agent"].tool_patterns))]
    r = await world.app.gateway.call("payments__decide_approval", {"approval_id": "x", "approve": True},
                                     CallAuth(world.ident("sam"), "transfers_agent", scopes), idempotency_key="k")
    assert r.code == "out_of_scope"


async def test_decision_event_redelivery_resumes_only_once(world):
    r = await request_big_transfer(world)
    await world.app.staff_decide(world.ident("sam"), r.log.approvals[0], True)
    ev = next(p for t, p in world.app.queue.published if t == "approvals.decided")
    await world.pump()
    await world.app.orchestrator.resume_after_decision(ev)  # at-least-once delivery: the broker redelivers
    with world.app.engine.connect() as conn:
        assert len(conn.execute(select(app_notifications)).all()) == 1


async def test_staff_decision_is_idempotent(world):
    r = await request_big_transfer(world)
    a = await world.app.staff_decide(world.ident("sam"), r.log.approvals[0], True)
    b = await world.app.staff_decide(world.ident("sam"), r.log.approvals[0], True)  # double click
    assert a.data["transfer_status"] == b.data["transfer_status"] == "completed" and b.data["replayed"] and world.balance(BOB) == 470_000


async def test_new_payee_needs_approval_and_approved_recipient_becomes_known():
    w = await World.create()
    try:
        r = await w.say("alice", f"Transfer $20 to {MALLORY}")
        r = await w.say("alice", "yes", r.session_id)
        assert r.log.approvals and w.balance(MALLORY) == 1_000
        await w.app.staff_decide(w.ident("sam"), r.log.approvals[0], True)
        assert w.balance(MALLORY) == 3_000
        r2 = await w.say("alice", f"Transfer $20 to {MALLORY}")  # now a known payee: no review needed
        r2 = await w.say("alice", "yes", r2.session_id)
        assert not r2.log.approvals and w.balance(MALLORY) == 5_000
    finally:
        await w.close()


# ---------------------------------------------------------------- queues
async def test_ticket_worker_is_idempotent_under_redelivery(world):
    r = await world.say("alice", "Please open a ticket: my card was declined")
    ev = next(p for t, p in world.app.queue.published if t == "tickets.created")
    assert world.app.crm.process_ticket(ev["ticket_id"]) is True
    assert world.app.crm.process_ticket(ev["ticket_id"]) is False  # second delivery: no-op
    assert r.log.tools[0]["ok"]


async def test_escalation_creates_a_masked_handoff_ticket(world):
    r = await world.say("alice", "I think there is fraud on my card 4111 1111 1111 1111, I need a human")
    assert r.outcome == "escalated" and "esc_" in r.text
    ev = next(p for t, p in world.app.queue.published if t == "escalations.requested")
    assert "4111 1111 1111 1111" not in " ".join(ev["transcript_tail"])  # transcript tail is PII-masked
    await world.pump()
    tickets = (await world.say("alice", "Show my tickets")).text
    assert "AI assistant handoff" in tickets


async def test_low_confidence_request_escalates_instead_of_guessing(world):
    r = await world.say("alice", "hmm what about the thing from before")
    assert r.outcome == "escalated" and r.log.routes == []


async def test_in_memory_queue_retries_then_dead_letters():
    q, seen = InMemoryQueue(), []

    async def flaky(payload):
        seen.append(payload)
        raise RuntimeError("boom")

    await q.publish("t", {"n": 1})
    await q.drain("t", flaky)
    assert len(seen) == MAX_ATTEMPTS and (await q.dead_letters("t"))[0]["payload"] == {"n": 1}


async def test_redis_streams_queue_delivers_acks_retries_and_dead_letters():
    q = RedisQueue(client=fakeredis.FakeAsyncRedis(decode_responses=True))
    got, calls, stop = [], {"n": 0}, asyncio.Event()

    async def ok(p):
        got.append(p)

    async def bad(p):
        calls["n"] += 1
        raise RuntimeError("boom")

    await q.publish("good", {"a": 1})
    await q.publish("bad", {"b": 2})
    tasks = [asyncio.create_task(q.consume_loop("good", ok, stop)), asyncio.create_task(q.consume_loop("bad", bad, stop))]
    for _ in range(60):
        await asyncio.sleep(0.05)
        if got and len(await q.dead_letters("bad")) == 1:
            break
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert got == [{"a": 1}] and calls["n"] == MAX_ATTEMPTS
    assert (await q.dead_letters("bad"))[0]["payload"] == {"b": 2}
