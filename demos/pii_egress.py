"""DEMO 6 - Hybrid LLM routing and PII redaction: what actually leaves the bank.

    python -m demos.pii_egress
"""
from sqlalchemy import select

from bankai.db import app_egress_log
from bankai.testing import World
from demos._common import bot, h1, h2, run, say

PROMPTS = [
    "What are your branch hours?",
    "I'm Alice Johnson (alice.johnson@example.com), what are your branch hours?",
    "My account 100200300401 wants to know the mortgage rate",
    "My card 4111 1111 1111 1111 was charged an overdraft fee, what is the fee policy?",
    "What's my balance?",
]


async def main():
    h1("DEMO 6: HYBRID ROUTING AND PII EGRESS")
    w = await World.create()
    h2("Routing decisions (per LLM call, by task + data class + PII scan; default is LOCAL)")
    for p in PROMPTS:
        say("alice", p)
        r = await w.say("alice", p)
        route = ", ".join(f"{l['agent']}->{l['provider']}({l['route_reason']})" for l in r.log.llm)
        print(f"      {route}")
    h2("What the external model server ACTUALLY received (ground truth captured at the provider boundary)")
    for i, payload in enumerate(w.external_payloads(), 1):
        if payload.strip():
            print(f"  {i:>2}. {payload[:110]!r}")
    leaked = w.pii_in(w.external_payloads())
    print(f"\n  PII detected in everything the external model received: {leaked or 'NONE'}")
    h2("Egress log (every external attempt: hash, redaction count, re-verification result)")
    with w.app.engine.connect() as conn:
        for r in conn.execute(select(app_egress_log)).mappings():
            print(f"  redactions={r['redactions']} verified_clean={r['verified_clean']} blocked={r['blocked']} sha256={r['payload_sha256'][:12]}.. preview={r['payload_preview'][:60]!r}")
    await w.close()


if __name__ == "__main__":
    run(main())
