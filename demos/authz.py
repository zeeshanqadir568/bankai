"""DEMO 2 - Authorization: an agent acts on an account it shouldn't, and the TOOL LAYER refuses.

    python -m demos.authz

AuthN answers "who are you"; AuthZ answers "may YOU do THIS to THAT". The agent's word is never evidence of either.
"""
from mcp import Client

from bankai.mcp_layer.gateway import CallAuth
from bankai.testing import World
from demos._common import bot, h1, h2, run, say


async def main():
    h1("DEMO 2: AUTHENTICATION vs AUTHORIZATION")
    w = await World.create()

    h2("1. Alice asks for Bob's balance (ownership check happens in accounts-mcp, from the ledger)")
    say("alice", "What is the balance of account 100200300501?")
    bot(await w.say("alice", "What is the balance of account 100200300501?"))

    h2("2. Same question from a teller: elevated but scoped role -> allowed to READ any account")
    say("tina", "What is the balance of account 100200300501?")
    bot(await w.say("tina", "What is the balance of account 100200300501?"))

    h2("3. The teller tries to MOVE Bob's money (role has accounts:read:any but no transfers:initiate)")
    r = await w.say("tina", "Transfer $10 from 100200300501 to 100200300401")
    r = await w.say("tina", "yes", r.session_id)
    bot(r)

    h2("4. A compromised/hallucinating agent passes someone else's customer_id as an argument")
    alice = w.ident("alice")
    res = await w.app.gateway.call("accounts__list_accounts", {"customer_id": "cust_bob"}, CallAuth(alice, "accounts_agent", ["accounts__list_accounts"]))
    print(f"  accounts__list_accounts(customer_id='cust_bob') as alice -> ok={res.ok} code={res.code}: {res.message}")

    h2("5. An agent calls a tool outside its scope (accounts_agent -> payments tool), even with Alice's valid identity")
    res = await w.app.gateway.call("payments__initiate_transfer", {"from_account": "100200300401", "to_account": "100200300501", "amount": 1},
                                   CallAuth(alice, "accounts_agent", ["accounts__get_balance"]), idempotency_key="k")
    print(f"  payments__initiate_transfer with an accounts_agent token -> ok={res.ok} code={res.code}: {res.message}")

    h2("6. Bypass the agent entirely: raw MCP calls with no token / a forged token")
    for label, meta in (("no token", None), ("forged token", {"auth_token": "eyJhbGciOiJIUzI1NiJ9.e30.forged"})):
        async with Client(w.app.mcp_servers["accounts"]) as c:
            out = await c.call_tool("get_balance", {"account_number": "100200300401"}, meta=meta)
        print(f"  {label:<13} -> {out.content[0].text.replace(chr(10), ' ')}")

    h2("7. What the audit log recorded (append-only, hash-chained, PII-masked)")
    for a in reversed(w.app.audit.recent(9)):
        print(f"  #{a['id']:<3} {a['decision']:<5} actor={a['actor']:<8} agent={a['agent'] or '-':<16} {a['tool']:<30} {a['reason'][:62]}")
    ok, n, msg = w.app.audit.verify_chain()
    print(f"\n  audit chain verification: {'OK' if ok else 'BROKEN'} ({n} rows)")
    await w.close()


if __name__ == "__main__":
    run(main())
