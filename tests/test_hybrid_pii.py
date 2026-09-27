"""Hybrid routing + PII: in-bank model for anything sensitive; the external model only ever sees redacted, PII-free text."""
import pytest
from sqlalchemy import select

from bankai.db import app_egress_log
from bankai.llm.base import LLMRequest, Message
from bankai.llm.guarded import GuardedExternalLLM, PIILeakBlocked
from bankai.llm.providers.mock import MockExternalLLM
from bankai.pii import PIIDetector, RedactionState, Redactor, StreamReinjector
from bankai.resilience import TransientError
from bankai.testing import World, make_settings

DET = PIIDetector(["Alice Johnson", "Bob Martinez"])
RED = Redactor(DET)
VALID_CARD = "4111 1111 1111 1111"  # Luhn-valid test number


def kinds(text):
    return {m.kind for m in DET.find(text)}


# ---------------------------------------------------------------- detection / redaction
def test_detector_finds_each_kind():
    assert "EMAIL" in kinds("mail me at jane.doe@example.com")
    assert "PHONE" in kinds("call +1 415-555-0142 or (415) 555-0199")
    assert "SSN" in kinds("ssn 123-45-6789")
    assert "ACCOUNT" in kinds("send to 100200300401")
    assert "CARD" in kinds(f"card {VALID_CARD}")
    assert "IBAN" in kinds("iban GB82WEST12345698765432")
    assert "PERSON" in kinds("this is alice johnson")


def test_luhn_invalid_card_shaped_number_is_not_a_card_but_is_still_flagged_as_account():
    assert "CARD" not in kinds("1234 5678 9012 3456")
    assert "ACCOUNT" in kinds("1234567890123456")


def test_masked_and_short_numbers_are_not_pii():
    assert not DET.find("****0401 and order 12345 and $8,450.75 at 9:00")


def test_redact_then_reinject_round_trips_and_is_consistent():
    st = RedactionState()
    src = "Alice Johnson (alice.johnson@example.com) pays 100200300401; again 100200300401"
    red = RED.redact(src, st)
    assert not DET.find(red)
    assert red.count("<ACCOUNT_1>") == 2  # same value -> same placeholder
    assert RED.reinject(red, st) == src


def test_mask_is_irreversible_and_keeps_last_four():
    assert RED.mask("acct 100200300401 email a.b@example.com") == "acct ****0401 email a***@example.com"


def test_stream_reinjector_handles_placeholder_split_across_chunks():
    st = RedactionState()
    ph = st.placeholder_for("ACCOUNT", "100200300401")
    r = StreamReinjector(RED, st)
    out = "".join(r.feed(c) for c in ["Your acc", "ount is <ACC", "OUNT_", "1> ok"]) + r.flush()
    assert out == "Your account is 100200300401 ok" and ph == "<ACCOUNT_1>"


# ---------------------------------------------------------------- routing policy
@pytest.fixture
async def router_world():
    w = await World.create()
    yield w
    await w.close()


def req(text, task="faq_answer", data_class="unclassified"):
    return LLMRequest([Message("system", "You answer bank FAQs."), Message("user", text, data_class=data_class)], task=task)


async def test_routing_decisions(router_world):
    d = router_world.app.router.decide
    assert d(req("What are your branch hours?")).target == "external"
    assert d(req("What are your branch hours?", task="agent")).reason == "task_not_eligible:agent"
    assert d(req("What is my balance?", task="classify")).target == "local"
    assert d(req("mortgage rates?", data_class="restricted")).reason == "restricted_data_class"
    assert d(req("I'm 100200300401, what's the rate?")).reason == "high_sensitivity_pii"
    assert d(req(f"my card {VALID_CARD}, what's the rate?")).target == "local"
    assert d(req("I'm Alice Johnson, what are the branch hours?")).target == "external"  # incidental PII: masked
    assert d(req("Alice Johnson alice.johnson@example.com +1 415-555-0142 bob.martinez@example.com hours?")).reason == "too_much_pii"


async def test_kill_switch_keeps_everything_in_bank():
    w = await World.create(make_settings(external_llm_enabled=False))
    try:
        assert w.app.router.decide(req("branch hours?")).reason == "external_disabled"
        await w.say("alice", "What are your branch hours?")
        assert w.external_payloads() == []
    finally:
        await w.close()


async def test_incidental_pii_is_masked_on_the_wire_and_restored_on_return(router_world):
    r = await router_world.app.router.chat(req("I'm Alice Johnson, what are the branch hours?"))
    sent = router_world.external_payloads()
    assert sent and not any("Alice" in s or "Johnson" in s for s in sent) and any("<PERSON_" in s for s in sent)
    assert r.provider == "external" and r.redactions >= 1


# ---------------------------------------------------------------- guard: defence in depth
async def test_guard_refuses_restricted_class_even_if_router_were_bypassed(router_world):
    app = router_world.app
    guarded = GuardedExternalLLM(MockExternalLLM(), app.redactor, app.engine)
    with pytest.raises(PIILeakBlocked):
        await guarded.chat(req("harmless text", data_class="restricted"))
    with app.engine.connect() as conn:
        assert conn.execute(select(app_egress_log.c.blocked)).scalars().all()[-1] is True


async def test_guard_fails_closed_when_redaction_leaves_residual_pii(router_world):
    class LeakyRedactor(Redactor):  # simulates a redaction bug / detector regression
        def redact(self, text, state):
            return text

    app = router_world.app
    raw = MockExternalLLM()
    guarded = GuardedExternalLLM(raw, LeakyRedactor(app.detector), app.engine)
    with pytest.raises(PIILeakBlocked):
        await guarded.chat(req("please look at 100200300401"))
    assert raw.received == []  # nothing reached the provider


async def test_egress_log_records_every_external_attempt_with_verification(router_world):
    await router_world.say("alice", "What is the mortgage rate?")
    with router_world.app.engine.connect() as conn:
        rows = conn.execute(select(app_egress_log)).mappings().all()
    assert rows and all(r["verified_clean"] and not r["blocked"] and len(r["payload_sha256"]) == 64 for r in rows)
    assert not router_world.pii_in([r["payload_preview"] for r in rows])


# ---------------------------------------------------------------- THE test: PII never reaches the external client
PII_PROMPTS = [
    "What is the mortgage rate? My account is 100200300401",
    f"What are your branch hours? my card is {VALID_CARD}",
    "Tell me about KYC rules, I'm Alice Johnson, alice.johnson@example.com, ssn 123-45-6789",
    "overdraft fee policy? call me on +1 415-555-0142",
    "What is the savings interest rate for account 100200300402?",
    "wire cut-off times? iban GB82WEST12345698765432",
    "I'm Bob Martinez, what are the personal loan rates",
    "Is there a fee for a lost card 5500 0000 0000 0004 replacement?",
]


async def test_no_raw_pii_ever_reaches_the_external_client(router_world):
    for i, p in enumerate(PII_PROMPTS):
        await router_world.say("alice", p)
    await router_world.say("alice", "Show my recent transactions")  # restricted tool data
    await router_world.say("alice", "What is the mortgage rate?")  # a clean one, so the external path is exercised
    assert router_world.external_payloads(), "vacuous test: nothing was ever sent to the external model"
    leaked = router_world.pii_in(router_world.external_payloads())
    assert leaked == [], f"PII reached the external client: {leaked}"


def test_the_leak_assertion_has_teeth():
    """Meta-test: if a client WERE handed raw PII, the assertion used above would catch it."""
    raw = MockExternalLLM()
    import asyncio
    asyncio.run(raw.chat(req("my account is 100200300401")))
    payloads = [m.content for r in raw.received for m in r.messages]
    assert DET.scan_obj(payloads) != []


# ---------------------------------------------------------------- failure behaviour
async def test_external_outage_degrades_to_local_for_public_tasks(router_world):
    router_world.app.external_raw.fail_next = 99
    r = await router_world.say("alice", "What are your branch hours?")
    assert "Branches are open" in r.text
    assert any("external_failed" in l["route_reason"] for l in r.log.llm)


async def test_local_outage_never_falls_back_to_external_for_sensitive_work(router_world):
    router_world.app.local.fail_next = 99
    r = await router_world.say("alice", "What's my balance?")
    assert "trouble" in r.text.lower()
    assert router_world.external_payloads() == []
