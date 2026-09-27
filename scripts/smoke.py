"""End-to-end smoke test against a RUNNING stack (docker compose): real Postgres, Redis Streams, MCP over HTTP, worker.

    python scripts/smoke.py [--base http://localhost:8080] [--jaeger http://localhost:16686] [--degrade "docker compose ... stop payments-mcp"]

Exits non-zero on the first failed expectation.
"""
import argparse
import json
import subprocess
import sys
import time

import httpx

ok_count = 0


def check(cond: bool, what: str, extra: str = "") -> None:
    global ok_count
    print(f"  [{'ok' if cond else 'FAIL'}] {what}" + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        sys.exit(1)
    ok_count += 1


def sse(resp_text: str):
    out = []
    for block in resp_text.strip().split("\n\n"):
        ev = next((l[7:] for l in block.splitlines() if l.startswith("event: ")), None)
        data = next((l[6:] for l in block.splitlines() if l.startswith("data: ")), None)
        if ev and data:
            out.append((ev, json.loads(data)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--jaeger", default="")
    ap.add_argument("--stop-cmd", default="", help="shell command that stops payments-mcp (degradation test)")
    ap.add_argument("--start-cmd", default="", help="shell command that starts payments-mcp again")
    a = ap.parse_args()
    c = httpx.Client(base_url=a.base, timeout=60)

    def login(u):
        return {"authorization": "Bearer " + c.post("/auth/dev-token", json={"username": u}).json()["token"]}

    def chat(h, msg, sid=None):
        r = c.post("/chat", json={"message": msg, "session_id": sid}, headers=h)
        assert r.status_code == 200, r.text
        ev = sse(r.text)
        return ("".join(d["text"] for e, d in ev if e == "token"), next(d["session_id"] for e, d in ev if e == "session"),
                next(d for e, d in ev if e == "done"))

    print("1. health + readiness")
    check(c.get("/healthz").json()["status"] == "ok", "liveness")
    ready = c.get("/readyz").json()
    check(ready["status"] == "ok", "readiness: db up, all 4 MCP servers discovered", str(ready))

    print("2. authN at the edge")
    check(c.post("/chat", json={"message": "hi"}).status_code == 401, "no token -> 401")
    check(c.get("/staff/approvals", headers=login("alice")).status_code == 403, "customer on staff endpoint -> 403")

    print("3. chat, tools over MCP/HTTP, authorization")
    ha, hs = login("alice"), login("sam")
    text, sid, done = chat(ha, "What's my balance?")
    check("8,450.75" in text and done["routes"] == ["accounts_agent"], "balance via accounts-mcp", text)
    text, _, _ = chat(ha, "What is the balance of account 100200300501?")
    check("not authorized" in text, "another customer's account is refused by the tool layer", text)
    text, _, done = chat(ha, "What is the current mortgage rate?")
    check("6.25%" in text, "FAQ via knowledge-mcp (external-eligible path)", text)

    print("4. injection: poisoned transaction")
    text, _, done = chat(ha, "Show my recent transactions")
    check("injection_suspected" in done["flags"] and "999000999000" not in text, "poisoned description neutralised + flagged", text)

    print("5. HITL through Redis Streams + worker (fresh session, so the earlier taint doesn't apply)")
    _, sid, _ = chat(ha, "Transfer $1500 from savings to Bob")
    text, _, done = chat(ha, "yes", sid)
    check(done["approvals"] and "review by our staff" in text, "large transfer held for approval", text)
    pending = c.get("/staff/approvals", headers=hs).json()["approvals"]
    check(len(pending) >= 1, "approval visible in staff queue")
    aid = pending[-1]["approval_id"]
    check(c.post(f"/staff/approvals/{aid}", json={"approve": True}, headers=ha).status_code == 403, "customer cannot approve")
    check(c.post(f"/staff/approvals/{aid}", json={"approve": True, "note": "smoke"}, headers=hs).json()["transfer_status"] == "completed", "staff approval executes transfer")
    body = ""
    for _ in range(30):  # worker consumes approvals.decided from the Redis stream and resumes the conversation
        notes = c.get("/notifications", headers=ha).json()["notifications"]
        if notes:
            body = notes[0]["body"]
            break
        time.sleep(0.5)
    check("approved and completed" in body, "worker resumed the paused conversation and notified the customer", body)

    print("6. async ticket via queue")
    text, _, _ = chat(ha, "Please open a ticket: my card was declined")
    check("queued" in text, "ticket accepted (async)", text)
    time.sleep(2)
    text, _, _ = chat(ha, "Show my tickets")
    check("[open]" in text, "worker moved the ticket queued -> open", text)

    print("7. observability")
    tid = done["trace_id"]
    tree = c.get(f"/debug/traces/{tid}?format=text", headers=hs).text
    check("chat.turn" in tree and "mcp.tool" in tree, "trace replay shows UI -> agents -> MCP tool spans across containers")
    check(c.get("/debug/audit", headers=hs).json()["chain_ok"], "audit hash chain verifies")
    if a.jaeger:
        svc = httpx.get(a.jaeger.rstrip("/") + "/api/services", timeout=10).json()["data"]
        check(any("mcp" in s for s in svc) and "bankai-api" in svc, "Jaeger received spans from api and mcp services", str(svc))

    if a.stop_cmd:
        print("8. graceful degradation: stop payments-mcp")
        subprocess.run(a.stop_cmd, shell=True, check=True)
        time.sleep(3)
        r = c.get("/readyz").json()
        check(r["status"] == "degraded" and "payments" in r["unavailable_tool_servers"], "readyz reports degraded", str(r))
        text, _, _ = chat(ha, "Transfer $10 to Bob")
        check("temporarily unavailable" in text, "transfers say so honestly", text)
        text, _, _ = chat(ha, "What's my balance?")
        check("8,450.75" in text, "accounts/FAQ/tickets keep working", text)
        if a.start_cmd:
            subprocess.run(a.start_cmd, shell=True, check=True)
            for _ in range(40):
                if c.get("/readyz").json()["status"] == "ok":
                    break
                time.sleep(1)
            check(c.get("/readyz").json()["status"] == "ok", "self-heals when payments-mcp returns")
    print(f"\nSMOKE TEST PASSED ({ok_count} checks)")


if __name__ == "__main__":
    main()
