"""AuthN (who are you) and AuthZ (what may you do) are different questions, tested separately."""
import jwt
import pytest

from bankai.common import clock
from bankai.security.authn import JWTAuthenticator
from bankai.security.authz import authorize, authorize_approval
from bankai.security.identity import AuthenticationError, Identity
from bankai.security.tool_tokens import ToolTokenService

KEY = "k" * 40


@pytest.fixture
def authn():
    return JWTAuthenticator(KEY, "HS256", "iss", "aud")


def mint(claims_override=None, key=KEY, alg="HS256"):
    now = int(clock.now())
    c = {"sub": "alice", "roles": ["customer"], "cust": "cust_alice", "iss": "iss", "aud": "aud", "iat": now, "exp": now + 60}
    c.update(claims_override or {})
    return jwt.encode(c, key, algorithm=alg)


# ---------------------------------------------------------------- AuthN
def test_valid_token_yields_identity(authn):
    who = authn.verify(mint())
    assert who.user_id == "alice" and who.customer_id == "cust_alice" and who.has_role("customer")


@pytest.mark.parametrize("bad", [
    pytest.param(lambda: mint(key="x" * 40), id="wrong_signature"),
    pytest.param(lambda: mint({"exp": int(clock.now()) - 10}), id="expired"),
    pytest.param(lambda: mint({"aud": "other"}), id="wrong_audience"),
    pytest.param(lambda: mint({"iss": "evil"}), id="wrong_issuer"),
    pytest.param(lambda: jwt.encode({"sub": "alice", "iss": "iss", "aud": "aud", "iat": 1, "exp": 9999999999}, None, algorithm="none"), id="alg_none"),
    pytest.param(lambda: "", id="missing"),
    pytest.param(lambda: "not.a.jwt", id="garbage"),
])
def test_bad_tokens_rejected(authn, bad):
    with pytest.raises(AuthenticationError):
        authn.verify(bad())


def test_token_missing_required_claim_rejected(authn):
    now = int(clock.now())
    tok = jwt.encode({"sub": "alice", "iss": "iss", "aud": "aud", "iat": now}, KEY, algorithm="HS256")  # no exp
    with pytest.raises(AuthenticationError):
        authn.verify(tok)


def test_role_claims_cannot_be_forged_without_key(authn):
    forged = mint({"roles": ["approver"]}, key="attacker" * 6)
    with pytest.raises(AuthenticationError):
        authn.verify(forged)


# ---------------------------------------------------------------- AuthZ
ALICE = Identity("alice", frozenset({"customer"}), "cust_alice")
TELLER = Identity("tina", frozenset({"teller"}), None)
APPROVER = Identity("sam", frozenset({"approver"}), None)


def test_customer_reads_only_own_accounts():
    assert authorize(ALICE, "accounts:read", "cust_alice")
    d = authorize(ALICE, "accounts:read", "cust_bob")
    assert not d and "own" in d.reason
    assert not authorize(ALICE, "accounts:read", None)  # unknown owner never authorizes


def test_teller_elevated_but_scoped():
    assert authorize(TELLER, "accounts:read", "cust_bob")  # elevated: any account
    assert authorize(TELLER, "tickets:create", "cust_bob")
    assert not authorize(TELLER, "transfers:initiate", "cust_bob")  # ...but cannot move money
    assert not authorize(TELLER, "approvals:decide")  # ...or approve


def test_approver_cannot_browse_accounts_or_initiate():
    assert authorize(APPROVER, "approvals:decide")
    assert not authorize(APPROVER, "accounts:read", "cust_alice")
    assert not authorize(APPROVER, "transfers:initiate", "cust_alice")


def test_separation_of_duties():
    assert authorize_approval(APPROVER, "alice")
    d = authorize_approval(Identity("sam", frozenset({"approver"}), None), "sam")
    assert not d and "separation" in d.reason
    assert not authorize_approval(ALICE, "bob")


def test_unknown_role_has_no_permissions():
    assert not authorize(Identity("x", frozenset({"superuser"}), None), "accounts:read", "cust_alice")


# ---------------------------------------------------------------- delegated tool tokens
def test_tool_token_bound_to_server_and_scope():
    svc = ToolTokenService(KEY)
    tok = svc.mint(ALICE, agent="accounts_agent", server="accounts", scopes=["accounts__get_balance"], taint=["injection_suspected"])
    c = svc.verify(tok, server="accounts")
    assert c.identity.user_id == "alice" and c.agent == "accounts_agent" and "accounts__get_balance" in c.scopes
    assert c.taint == ("injection_suspected",)
    with pytest.raises(AuthenticationError):  # replay against another server
        svc.verify(tok, server="payments")


def test_tool_token_expires_and_rejects_tampering():
    svc = ToolTokenService(KEY)
    tok = svc.mint(ALICE, agent="a", server="accounts", scopes=[])
    clock.advance(120)
    with pytest.raises(AuthenticationError):
        svc.verify(tok, server="accounts")
    with pytest.raises(AuthenticationError):
        ToolTokenService("z" * 40).verify(svc.mint(ALICE, agent="a", server="accounts", scopes=[]), server="accounts")
    with pytest.raises(AuthenticationError):
        svc.verify(None, server="accounts")
