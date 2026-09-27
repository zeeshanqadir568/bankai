"""DEMO 4 - Human-in-the-loop: risky transfers pause the conversation, staff decide, the conversation resumes.

    python -m demos.hitl

Flow: customer asks -> customer confirms exact details (runtime-enforced) -> payments-mcp policy says "needs a human" ->
approval request is queued + pushed to the staff console, the paused run is persisted -> staff approve/deny (or the
window expires) -> the decision event resumes the conversation and notifies the customer. Money moves ONLY on approval.
"""
from sqlalchemy import select

from bankai.common import clock
from bankai.db import app_notifications
from bankai.testing import World, make_settings
from demos._common import bot, h1, h2, money, run, say

BOB, ALICE_SAV = "100200300501", "100200300402"


async def notifications(w):
    with w.app.engine.connect() as conn:
        return [r[0] for r in conn.execute(select(app_notifications.c.body))]


async def main():
    h1("DEMO 4: HUMAN-IN-THE-LOOP APPROVAL")
    w = await World.create(make_settings(approval_timeout_seconds=120))

    h2("1. Alice asks for a $1,500 transfer (above the $1,000 review threshold)")
    say("alice", "Transfer $1500 from savings to Bob")
    r = await w.say("alice", "Transfer $1500 from savings to Bob")
    bot(r)
    say("alice", "yes")
    r = await w.say("alice", "yes", r.session_id)
    bot(r)
    print(f"\n  balances now: alice savings {money(w.balance(ALICE_SAV))}, bob {money(w.balance(BOB))}  (nothing moved; agent run is PAUSED)")

    h2("2. The approval request lands in the staff queue (pushed over SSE to /staff; also listable via the tool layer)")
    sam = w.ident("sam")
    q = await w.app.staff_list_approvals(sam)
    for a in q.data["approvals"]:
        print(f"  {a['approval_id']}: {money(a['amount_cents'])} ****{a['from_account'][-4:]} -> ****{a['to_account'][-4:]}  requested_by={a['requested_by']}  reasons={a['reasons']}")
    aid = q.data["approvals"][0]["approval_id"]

    h2("3. Who may decide? Customers, tellers, and the requester themselves are refused by the tool layer")
    for who in ("alice", "tina"):
        d = await w.app.staff_decide(w.ident(who), aid, True)
        print(f"  {who} tries to approve -> {d.code}: {d.message}")

    h2("4. Sam approves; the decision event resumes the paused conversation")
    d = await w.app.staff_decide(sam, aid, True, "verified with customer by phone")
    print(f"  sam approves -> {d.data['status']} / transfer {d.data['transfer_status']}   balances: bob {money(w.balance(BOB))}, alice savings {money(w.balance(ALICE_SAV))}")
    d2 = await w.app.staff_decide(sam, aid, True, "verified with customer by phone")
    print(f"  double-click (same idempotency key) -> replayed={d2.data['replayed']}, still one debit: bob {money(w.balance(BOB))}")
    await w.pump()
    for n in await notifications(w):
        print(f"  [notification to alice] {n}")

    h2("5. Denial path")
    r = await w.say("alice", "Transfer $2000 from savings to Bob")
    r = await w.say("alice", "yes", r.session_id)
    d = await w.app.staff_decide(sam, r.log.approvals[0], False, "does not match customer's usual behaviour")
    await w.pump()
    print(f"  sam denies -> {d.data['transfer_status']}; bob {money(w.balance(BOB))} (unchanged)")
    print(f"  [notification to alice] {(await notifications(w))[-1]}")

    h2("6. Timeout path (nobody reviews within the window)")
    r = await w.say("alice", "Transfer $2500 from savings to Bob")
    r = await w.say("alice", "yes", r.session_id)
    clock.advance(180)
    n = await w.app.payments.expire_overdue()
    late = await w.app.staff_decide(sam, r.log.approvals[0], True)
    await w.pump()
    print(f"  sweeper expired {n} approval(s); a late approval attempt -> {late.code}: {late.message}; bob {money(w.balance(BOB))} (unchanged)")
    print(f"  [notification to alice] {(await notifications(w))[-1]}")

    h2("7. New payee (small amount, but never paid before) also needs review")
    r = await w.say("alice", "Transfer $20 to 999000999000")
    r = await w.say("alice", "yes", r.session_id)
    bot(r)
    await w.close()


if __name__ == "__main__":
    run(main())
