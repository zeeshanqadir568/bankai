"""HTTP edge: authN at the door, role gating, SSE streaming, staff approvals, replayable traces."""
import asyncio
import json

import jwt
import pytest
from starlette.testclient import TestClient

from bankai.api.main import create_api
from bankai.testing import World


@pytest.fixture
def client():
    w = asyncio.run(World.create())
    with TestClient(create_api(w.app, run_workers=False)) as c:
        c.world = w
        yield c


def token(c, user):
    r = c.post("/auth/dev-token", json={"username": user})
    assert r.status_code == 200
    return {"authorization": f"Bearer {r.json()['token']}"}


def parse_sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        ev = next((l[7:] for l in block.splitlines() if l.startswith("event: ")), None)
        data = next((l[6:] for l in block.splitlines() if l.startswith("data: ")), None)
        if ev and data:
            out.append((ev, json.loads(data)))
    return out


def chat(c, headers, message, session_id=None):
    r = c.post("/chat", json={"message": message, "session_id": session_id}, headers=headers)
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    text = "".join(d["text"] for e, d in events if e == "token")
    sid = next(d["session_id"] for e, d in events if e == "session")
    done = next(d for e, d in events if e == "done")
    return text, sid, done, events


def test_health_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/readyz").json()
    assert r["status"] == "ok" and r["llm_breakers"] == {"local": "closed", "external": "closed"}


def test_security_headers_present(client):
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff" and "default-src 'self'" in r.headers["content-security-policy"]


@pytest.mark.parametrize("hdr", [
    {}, {"authorization": "Bearer garbage"}, {"authorization": "Basic abc"},
    {"authorization": "Bearer " + jwt.encode({"sub": "alice", "roles": ["approver"]}, "attacker-key" * 4, algorithm="HS256")},
])
def test_unauthenticated_or_forged_requests_get_401(client, hdr):
    for method, path in (("post", "/chat"), ("get", "/notifications"), ("get", "/staff/approvals"), ("get", "/usage")):
        r = getattr(client, method)(path, headers=hdr, **({"json": {"message": "hi"}} if method == "post" else {}))
        assert r.status_code == 401, (path, r.status_code)


def test_chat_streams_progress_then_tokens_then_done(client):
    h = token(client, "alice")
    text, sid, done, events = chat(client, h, "What's my balance?")
    kinds = [e for e, _ in events]
    assert kinds[0] == "progress" and "token" in kinds and kinds[-1] == "done"
    assert "8,450.75" in text and done["routes"] == ["accounts_agent"] and done["outcome"] == "ok" and len(done["trace_id"]) == 32
    assert any(d.get("type") == "tool" for e, d in events if e == "progress")


def test_session_continuity_and_isolation(client):
    ha, hb = token(client, "alice"), token(client, "bob")
    _, sid, _, _ = chat(client, ha, "What's my balance?")
    text, sid2, _, _ = chat(client, ha, "Show my recent transactions", sid)
    assert sid2 == sid and "Recent transactions" in text
    r = client.post("/chat", json={"message": "hi", "session_id": sid}, headers=hb)
    assert r.status_code == 404  # bob cannot attach to alice's session


def test_input_validation(client):
    h = token(client, "alice")
    assert client.post("/chat", json={"message": "x" * 2001}, headers=h).status_code == 422
    assert client.post("/chat", json={"message": ""}, headers=h).status_code == 422


def test_authorization_denial_surfaces_politely_over_http(client):
    text, _, done, _ = chat(client, token(client, "alice"), "Show balance of account 100200300501")
    assert "not authorized" in text and "100200300501" not in text


def test_role_gating_on_staff_and_debug_endpoints(client):
    for user in ("alice", "tina"):
        h = token(client, user)
        assert client.get("/staff/approvals", headers=h).status_code == 403
        assert client.get("/debug/audit", headers=h).status_code == 403
        assert client.post("/staff/approvals/apr_x", json={"approve": True}, headers=h).status_code == 403
    assert client.get("/staff/approvals", headers=token(client, "sam")).status_code == 200


def test_staff_approval_flow_over_http_and_customer_notification(client):
    ha, hs = token(client, "alice"), token(client, "sam")
    _, sid, _, _ = chat(client, ha, "Transfer $1500 from savings to Bob")
    text, _, done, _ = chat(client, ha, "yes", sid)
    assert done["approvals"] and "review by our staff" in text
    pending = client.get("/staff/approvals", headers=hs).json()["approvals"]
    assert len(pending) == 1 and pending[0]["reasons"] == ["amount_over_threshold"]
    r = client.post(f"/staff/approvals/{pending[0]['approval_id']}", json={"approve": True, "note": "verified by phone"}, headers=hs)
    assert r.status_code == 200 and r.json()["transfer_status"] == "completed"
    assert client.post(f"/staff/approvals/{pending[0]['approval_id']}", json={"approve": False}, headers=hs).status_code in (200, 409)
    asyncio.run(client.world.pump())  # the resume worker
    notes = client.get("/notifications", headers=ha).json()["notifications"]
    assert len(notes) == 1 and "approved and completed" in notes[0]["body"]
    assert client.get("/notifications", headers=ha).json()["notifications"] == []  # delivered once
    assert client.get("/notifications", headers=token(client, "bob")).json()["notifications"] == []  # and only to the owner
    assert client.world.balance("100200300501") == 470_000


def test_trace_replay_endpoint_shows_the_full_agent_tree(client):
    _, _, done, _ = chat(client, token(client, "alice"), "What's my balance?")
    hs = token(client, "sam")
    txt = client.get(f"/debug/traces/{done['trace_id']}?format=text", headers=hs).text
    for expected in ("chat.turn", "coordinator.classify", "agent.run accounts_agent", "llm.call", "mcp.call accounts__list_accounts",
                     "mcp.tool accounts__list_accounts"):
        assert expected in txt, expected
    spans = client.get(f"/debug/traces/{done['trace_id']}", headers=hs).json()["spans"]
    assert len({s["trace_id"] for s in spans}) == 1  # UI -> coordinator -> agent -> LLM -> MCP tool: one trace id throughout
    assert client.get("/debug/traces/" + "0" * 32, headers=hs).status_code == 404


def test_traces_and_logs_contain_no_raw_pii(client):
    _, _, done, _ = chat(client, token(client, "alice"), "Show my recent transactions and email me at alice.johnson@example.com")
    spans = client.get(f"/debug/traces/{done['trace_id']}", headers=token(client, "sam")).json()["spans"]
    blob = json.dumps(spans)
    for raw in ("alice.johnson@example.com", "100200300401", "Alice Johnson", "999000999000"):
        assert raw not in blob, f"raw PII in trace: {raw}"


def test_usage_is_scoped_by_role(client):
    chat(client, token(client, "alice"), "What's my balance?")
    chat(client, token(client, "bob"), "What's my balance?")
    mine = client.get("/usage", headers=token(client, "alice")).json()
    assert "by_user" not in mine and mine["spent_24h_usd"] > 0
    ops = client.get("/usage", headers=token(client, "sam")).json()
    assert {r["key"] for r in ops["by_user"]} == {"alice", "bob"}


def test_audit_endpoint_verifies_the_chain(client):
    chat(client, token(client, "alice"), "Show balance of account 100200300501")
    a = client.get("/debug/audit", headers=token(client, "sam")).json()
    assert a["chain_ok"] and any(r["decision"] == "deny" for r in a["recent"])


def test_static_ui_served_with_untrusted_output_rendering_rules(client):
    assert "Bank AI Assistant" in client.get("/").text and "Staff" in client.get("/staff").text
    js = client.get("/static/chat.js").text
    assert "innerHTML" not in js  # model output is only ever inserted with textContent


def test_pages_use_no_inline_styles_or_scripts_so_the_strict_csp_never_breaks_layout(client):
    """A CSP of default-src 'self' silently drops inline style="..." attributes; the staff console was visibly mis-laid-out."""
    for path in ("/", "/staff"):
        html = client.get(path).text
        assert "style=" not in html and "onclick=" not in html and "<script>" not in html, path


@pytest.mark.parametrize("script", ["chat.js", "staff.js"])
def test_ui_scripts_are_syntactically_valid_javascript(script):
    """A broken string literal in staff.js once would have silently killed the whole staff console."""
    import pathlib
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    path = pathlib.Path(__file__).parent.parent / "bankai" / "api" / "static" / script
    r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_staff_can_list_recent_conversations_and_replay_one_by_click(client):
    chat(client, token(client, "alice"), "What's my balance?")
    hs = token(client, "sam")
    traces = client.get("/debug/traces", headers=hs).json()["traces"]
    assert traces and traces[0]["user"] == "alice" and "balance" in traces[0]["message"] and traces[0]["outcome"] == "ok"
    assert "chat.turn" in client.get(f"/debug/traces/{traces[0]['trace_id']}?format=text", headers=hs).text
    assert client.get("/debug/traces", headers=token(client, "alice")).status_code == 403  # customers cannot list conversations


def test_chat_ui_offers_confirm_and_cancel_buttons_for_transfers(client):
    js = client.get("/static/chat.js").text
    assert "confirmation_required" in js and '"yes"' in js and '"no"' in js
