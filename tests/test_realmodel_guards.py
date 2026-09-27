"""Guards added after driving the system with a REAL model (qwen3:8b), which made mistakes a scripted mock never makes:
guessing a destination, inventing a source account, promising to look something up and stopping."""
from bankai.guardrails.actions import ActionGuard, GuardConfig
from bankai.llm.base import LLMResponse, ToolCall, Usage
from bankai.llm.providers.mock import MockLocalLLM
from bankai.state.blackboard import Blackboard
from bankai.testing import World, make_settings

CHK, SAV, BOB = "100200300401", "100200300402", "100200300501"
TRANSFER = {"properties": {"from_account": {}, "to_account": {}, "amount": {}, "currency": {}, "memo": {}},
            "required": ["from_account", "to_account", "amount"]}
ALLOWED = {"payments__initiate_transfer": TRANSFER}


def board():
    b = Blackboard()
    b.verified_accounts = [{"account_number": CHK, "type": "checking", "currency": "USD", "balance_cents": 845_075},
                           {"account_number": SAV, "type": "savings", "currency": "USD", "balance_cents": 2_500_000}]
    b.known_payees = [{"name": "Bob Martinez", "account_number": BOB}]
    return b


def validate(**args):
    return ActionGuard(make_settings(), GuardConfig()).validate(ToolCall("c", "payments__initiate_transfer", args), allowed=ALLOWED,
                                                                bb=board(), trusted_user_text=args.pop("_text", "") or "")


def test_destination_must_match_the_payee_the_customer_named():
    g = ActionGuard(make_settings(), GuardConfig())
    call = ToolCall("c", "payments__initiate_transfer", {"from_account": CHK, "to_account": SAV, "amount": 25})  # model 'guessed' own savings
    v = g.validate(call, allowed=ALLOWED, bb=board(), trusted_user_text="send $25 to bob from checking")
    assert not v.allow and v.code == "destination_mismatch" and BOB in v.message
    ok = ToolCall("c", "payments__initiate_transfer", {"from_account": CHK, "to_account": BOB, "amount": 25})
    assert g.validate(ok, allowed=ALLOWED, bb=board(), trusted_user_text="send $25 to bob from checking").code == "confirmation_required"


def test_payee_name_matching_is_case_insensitive_and_whole_word():
    g = ActionGuard(make_settings(), GuardConfig())
    call = ToolCall("c", "payments__initiate_transfer", {"from_account": CHK, "to_account": SAV, "amount": 25})
    assert g.validate(call, allowed=ALLOWED, bb=board(), trusted_user_text="BOB ko 25 bhej do").code == "destination_mismatch"
    # 'bobcat' / 'jobs' must not be mistaken for the payee 'Bob'
    assert g.validate(call, allowed=ALLOWED, bb=board(), trusted_user_text="move 25 to my savings, the bobcat fund").code != "destination_mismatch"


def test_source_account_must_be_one_of_the_customers_own():
    g = ActionGuard(make_settings(), GuardConfig())
    bogus = ToolCall("c", "payments__initiate_transfer", {"from_account": "ount", "to_account": BOB, "amount": 25})  # a real model invented this
    v = g.validate(bogus, allowed=ALLOWED, bb=board(), trusted_user_text="send 25 to bob from checking")
    assert not v.allow and v.code == "unknown_source_account" and CHK in v.message


async def test_runtime_prefetches_accounts_and_payees_so_the_model_never_has_to_guess(world):
    seen = {}

    class Spy(MockLocalLLM):
        async def chat(self, req):
            if req.agent == "transfers_agent":
                seen["system"] = req.messages[0].content
            return await super().chat(req)

    w = await World.create(local=Spy())
    try:
        await w.say("alice", "Transfer $25 to Bob")  # brand-new session: nothing cached
        assert '"verified_accounts"' in seen["system"] and CHK in seen["system"] and "Bob Martinez" in seen["system"]
    finally:
        await w.close()


async def test_accounts_agent_always_starts_from_a_fresh_balance_snapshot():
    seen = []

    class Spy(MockLocalLLM):
        async def chat(self, req):
            if req.agent == "accounts_agent":
                seen.append(req.messages[0].content)
            return await super().chat(req)

    w = await World.create(local=Spy())
    try:
        r = await w.say("alice", "What's my balance?")
        await w.say("alice", "Transfer $1500 from savings to Bob", r.session_id)
        await w.say("alice", "yes", r.session_id)
        await w.app.staff_decide(w.ident("sam"), (await w.app.staff_list_approvals(w.ident("sam"))).data["approvals"][0]["approval_id"], True)
        await w.say("alice", "What's my balance?", r.session_id)
        assert "2350000" in seen[-1]  # savings 25,000 - 1,500 approved later by staff: the prompt shows the CURRENT balance
    finally:
        await w.close()


async def test_a_model_that_promises_to_look_something_up_and_stops_is_nudged_once():
    calls = {"n": 0}

    class Promiser(MockLocalLLM):
        async def chat(self, req):
            if req.agent != "accounts_agent":
                return await super().chat(req)
            self.received.append(req)
            calls["n"] += 1
            if not any(m.role == "user" and m.content.startswith("Reminder:") for m in req.messages):
                return LLMResponse(text="I have your accounts. Let me check your recent transactions.", provider="local", model="p", usage=Usage(5, 5))
            if not any(m.role == "tool" for m in req.messages):
                return LLMResponse(tool_calls=[ToolCall("t1", "accounts__get_transactions", {"account_number": CHK, "limit": 3})],
                                   provider="local", model="p", usage=Usage(5, 5))
            return LLMResponse(text="Here are your transactions.", provider="local", model="p", usage=Usage(5, 5))

    w = await World.create(local=Promiser())
    try:
        r = await w.say("alice", "Show my recent transactions")
        assert "Here are your transactions" in r.text and any(t["tool"] == "accounts__get_transactions" for t in r.log.tools)
        assert calls["n"] == 3  # promise -> nudge -> tool call -> answer, never more
    finally:
        await w.close()


def test_transaction_results_carry_real_dates_because_models_misread_epoch_seconds():
    import asyncio

    async def go():
        w = await World.create()
        try:
            from bankai.mcp_layer.gateway import CallAuth
            r = await w.app.gateway.call("accounts__get_transactions", {"account_number": CHK, "limit": 2},
                                         CallAuth(w.ident("alice"), "accounts_agent", ["accounts__get_transactions"]))
            return r.data["transactions"][0]
        finally:
            await w.close()

    tx = asyncio.run(go())
    assert len(tx["date"]) == 10 and tx["date"][4] == "-" and tx["date"].startswith("20")
