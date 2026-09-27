"""The tool layer (MCP servers) enforces authn, scope, ownership, idempotency and policy — whatever the agent asks."""
import pytest
from sqlalchemy import update

from bankai.db import core_audit
from bankai.mcp_layer.gateway import CallAuth

ALICE_CHK, ALICE_SAV, BOB_CHK, MALLORY = "100200300401", "100200300402", "100200300501", "999000999000"


async def call(w, who, tool, args, *, agent="test", scopes=None, taint=(), idem=None):
    return await w.app.gateway.call(tool, args, CallAuth(w.ident(who), agent, scopes or [tool], list(taint)), idempotency_key=idem)


async def test_agents_discover_tools_over_mcp_and_schemas_hide_auth(world):
    cat = world.app.gateway.catalog
    assert {"accounts__get_balance", "payments__initiate_transfer", "crm__create_ticket", "knowledge__search_faq"} <= set(cat)
    for td in cat.values():  # identity / idempotency travel in _meta, never in the model-visible schema
        assert not {"auth_token", "idempotency_key", "token"} & set(td.schema.get("properties", {}))
    assert cat["accounts__get_balance"].data_class == "restricted" and cat["knowledge__search_faq"].data_class == "public"
    assert cat["payments__initiate_transfer"].mutating and not cat["accounts__get_balance"].mutating


async def test_customer_reads_own_account_but_is_refused_for_anothers(world):
    ok = await call(world, "alice", "accounts__get_balance", {"account_number": ALICE_CHK})
    assert ok.ok and ok.data["balance_cents"] == 845_075
    denied = await call(world, "alice", "accounts__get_balance", {"account_number": BOB_CHK})
    assert not ok.denied and denied.denied and denied.code == "access_denied"
    missing = await call(world, "alice", "accounts__get_balance", {"account_number": "100200399999"})
    assert missing.message == denied.message  # no enumeration oracle: "not yours" == "does not exist"
    # the refusal is in the audit log with the true reason
    rows = world.app.audit.recent(5)
    assert any(r["decision"] == "deny" and r["actor"] == "alice" and "access_denied" in r["reason"] for r in rows)


async def test_teller_can_read_any_account_but_not_transfer(world):
    assert (await call(world, "tina", "accounts__get_balance", {"account_number": BOB_CHK})).ok
    r = await call(world, "tina", "payments__initiate_transfer",
                   {"from_account": BOB_CHK, "to_account": ALICE_CHK, "amount": 1}, idem="k")
    assert r.code == "access_denied" and world.balance(BOB_CHK) == 320_000


async def test_agent_cannot_call_tool_outside_its_scope(world):
    r = await call(world, "alice", "payments__initiate_transfer",
                   {"from_account": ALICE_CHK, "to_account": BOB_CHK, "amount": 5}, agent="accounts_agent",
                   scopes=["accounts__get_balance"], idem="k1")
    assert r.code == "out_of_scope" and world.transfers() == []


async def test_call_without_or_with_forged_identity_is_refused(world):
    from mcp import Client
    server = world.app.mcp_servers["accounts"]
    async with Client(server) as c:  # a raw MCP client with no token at all
        res = await c.call_tool("get_balance", {"account_number": ALICE_CHK})
    assert '"unauthenticated"' in res.content[0].text
    async with Client(server) as c:
        res = await c.call_tool("get_balance", {"account_number": ALICE_CHK}, meta={"auth_token": "forged.jwt.value"})
    assert '"unauthenticated"' in res.content[0].text


async def test_arguments_cannot_impersonate_another_user(world):
    """Even if a model passes customer_id of someone else, ownership is checked against the verified identity."""
    r = await call(world, "alice", "accounts__list_accounts", {"customer_id": "cust_bob"})
    assert r.code == "access_denied"
    r = await call(world, "alice", "crm__create_ticket", {"subject": "x", "description": "y", "customer_id": "cust_bob"}, idem="t1")
    assert r.code == "access_denied"


async def test_transfer_idempotency_same_key_replays_once(world):
    args = {"from_account": ALICE_CHK, "to_account": BOB_CHK, "amount": 10}
    a = await call(world, "alice", "payments__initiate_transfer", args, idem="same")
    b = await call(world, "alice", "payments__initiate_transfer", args, idem="same")
    assert a.data["transfer_id"] == b.data["transfer_id"] and b.data["replayed"] is True
    assert world.balance(BOB_CHK) == 321_000 and len(world.transfers()) == 1  # debited ONCE despite two calls


async def test_idempotency_key_reuse_with_different_payload_conflicts(world):
    await call(world, "alice", "payments__initiate_transfer", {"from_account": ALICE_CHK, "to_account": BOB_CHK, "amount": 10}, idem="k")
    r = await call(world, "alice", "payments__initiate_transfer", {"from_account": ALICE_CHK, "to_account": BOB_CHK, "amount": 99}, idem="k")
    assert r.code == "idempotency_conflict" and world.balance(BOB_CHK) == 321_000


async def test_mutations_require_an_idempotency_key(world):
    r = await call(world, "alice", "payments__initiate_transfer", {"from_account": ALICE_CHK, "to_account": BOB_CHK, "amount": 10})
    assert r.code == "idempotency_key_required" and world.transfers() == []
    r = await call(world, "alice", "crm__create_ticket", {"subject": "hi", "description": "there"})
    assert r.code == "idempotency_key_required"


@pytest.mark.parametrize("amount,code", [(0, "invalid_amount"), (-5, "invalid_amount"), (1.999, "invalid_amount"),
                                           (9_999_999, "over_hard_limit"), (60_000, "over_hard_limit"),
                                           (30_000, "insufficient_funds")])  # savings holds $25,000
async def test_invalid_or_impossible_amounts(world, amount, code):
    r = await call(world, "alice", "payments__initiate_transfer", {"from_account": ALICE_SAV, "to_account": BOB_CHK, "amount": amount}, idem="a")
    assert r.code == code


async def test_hard_limit_is_a_denial_not_an_approval(world):
    with world.app.engine.begin() as conn:
        conn.execute(update(__import__("bankai.db", fromlist=["core_accounts"]).core_accounts).where(
            __import__("bankai.db", fromlist=["core_accounts"]).core_accounts.c.number == ALICE_SAV).values(balance_cents=9_000_000))
    r = await call(world, "alice", "payments__initiate_transfer", {"from_account": ALICE_SAV, "to_account": BOB_CHK, "amount": 51_000}, idem="h")
    assert r.code == "over_hard_limit"


async def test_transfer_policy_routes_risky_transfers_to_human_approval(world):
    small = await call(world, "alice", "payments__initiate_transfer", {"from_account": ALICE_CHK, "to_account": BOB_CHK, "amount": 25}, idem="s")
    assert small.data["status"] == "completed"  # known payee, small, not draining
    new_payee = await call(world, "alice", "payments__initiate_transfer", {"from_account": ALICE_CHK, "to_account": MALLORY, "amount": 25}, idem="n")
    assert new_payee.data["status"] == "pending_approval" and "new_payee" in new_payee.data["reasons"]
    big = await call(world, "alice", "payments__initiate_transfer", {"from_account": ALICE_SAV, "to_account": BOB_CHK, "amount": 1500}, idem="b")
    assert big.data["status"] == "pending_approval" and "amount_over_threshold" in big.data["reasons"]
    assert world.balance(MALLORY) == 1_000  # held transfers move nothing


async def test_tainted_context_forces_review_even_for_a_normally_safe_transfer(world):
    r = await call(world, "alice", "payments__initiate_transfer", {"from_account": ALICE_CHK, "to_account": BOB_CHK, "amount": 5},
                   taint=["injection_suspected"], idem="t")
    assert r.data["status"] == "pending_approval" and r.data["reasons"] == ["tainted_context"]


async def test_velocity_flag_after_three_quick_transfers(world):
    for i in range(3):
        assert (await call(world, "alice", "payments__initiate_transfer", {"from_account": ALICE_CHK, "to_account": BOB_CHK, "amount": 1 + i}, idem=f"v{i}")).data["status"] == "completed"
    r = await call(world, "alice", "payments__initiate_transfer", {"from_account": ALICE_CHK, "to_account": BOB_CHK, "amount": 4}, idem="v3")
    assert r.data["status"] == "pending_approval" and "velocity" in r.data["reasons"]


async def test_every_call_is_audited_and_the_chain_detects_tampering(world):
    await call(world, "alice", "accounts__get_balance", {"account_number": ALICE_CHK})
    await call(world, "alice", "accounts__get_balance", {"account_number": BOB_CHK})
    ok, n, _ = world.app.audit.verify_chain()
    assert ok and n >= 2
    with world.app.engine.begin() as conn:  # an insider quietly rewrites a denial into an allow
        conn.execute(update(core_audit).where(core_audit.c.decision == "deny").values(decision="allow"))
    ok, _, msg = world.app.audit.verify_chain()
    assert not ok and "chain broken" in msg


async def test_audit_log_does_not_store_raw_account_numbers(world):
    await call(world, "alice", "accounts__get_balance", {"account_number": BOB_CHK})
    blob = " ".join(str(r["args_json"]) for r in world.app.audit.recent(5))
    assert BOB_CHK not in blob and "0501" in blob


async def test_ticket_creation_is_async_and_idempotent(world):
    a = await call(world, "alice", "crm__create_ticket", {"subject": "Card issue", "description": "declined", "category": "card"}, idem="tk")
    b = await call(world, "alice", "crm__create_ticket", {"subject": "Card issue", "description": "declined", "category": "card"}, idem="tk")
    assert a.data["status"] == "queued" and a.data["ticket_id"] == b.data["ticket_id"]
    assert (await call(world, "alice", "crm__get_ticket", {"ticket_id": a.data["ticket_id"]})).data["ticket"]["status"] == "queued"
    await world.pump()  # the worker consumes tickets.created
    assert (await call(world, "alice", "crm__get_ticket", {"ticket_id": a.data["ticket_id"]})).data["ticket"]["status"] == "open"
    assert (await call(world, "bob", "crm__get_ticket", {"ticket_id": a.data["ticket_id"]})).code == "access_denied"
