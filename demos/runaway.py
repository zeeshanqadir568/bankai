"""DEMO 3 - The bill that tripled: an agent stuck in a retry/tool loop, unprotected vs protected.

    python -m demos.runaway

Failure: the model keeps calling a tool that can never succeed (or delegates back and forth between agents), re-sending an
ever-growing context every step. Nothing crashes; the only symptom is the invoice.
"""
from dataclasses import replace

from bankai.agents.registry import AGENTS
from bankai.testing import World, make_settings
from demos._common import bot, h1, h2, run

UNPROTECTED = dict(max_iterations=40, max_identical_calls=10**6, max_request_tokens=10**9, max_request_cost_usd=10**6,
                   max_delegation_depth=10**6, max_llm_calls_per_request=10**6)


async def once(label, pathology, message, settings=None, agents=None):
    w = await World.create(make_settings(**(settings or {})), pathology=pathology, agents=agents)
    try:
        r = await w.say("alice", message)
        calls = len(w.app.local.received)
        tokens = sum(l["tokens"] for l in r.log.llm)
        cost = w.app.cost.spent_since("alice")
        print(f"  {label:<40} LLM calls={calls:<3} tokens={tokens:<7} cost=${cost:.5f}  stopped_by={r.log.budget_stopped or 'nothing (ran to the cap)'}")
        return r, calls, tokens, cost
    finally:
        await w.close()


async def main():
    h1("DEMO 3: RUNAWAY LOOPS AND COST CONTROL")
    msg = "What is the balance of account 100200300499?"
    h2("Scenario A - agent retries the same failing tool call (account does not exist), forever")
    _, c1, t1, k1 = await once("UNPROTECTED (limits effectively off)", "identical", msg, UNPROTECTED)
    r, c2, t2, k2 = await once("PROTECTED (default limits)", "identical", msg)
    print(f"\n  => {c1 / c2:.1f}x fewer LLM calls, {t1 / t2:.1f}x fewer tokens, {k1 / k2:.1f}x lower cost. Customer sees:")
    bot(r)

    h2("Scenario B - the retries are never identical (limit=5,6,7...), so the loop detector cannot see them")
    msg2 = "Show my recent transactions for 100200300401"
    _, c1, t1, k1 = await once("UNPROTECTED", "varying", msg2, UNPROTECTED)
    r, c2, t2, k2 = await once("PROTECTED (iteration cap)", "varying", msg2)
    print(f"\n  => {c1 / c2:.1f}x fewer calls, {t1 / t2:.1f}x fewer tokens. Context grows every step, so cost is QUADRATIC in loop length:")
    print(f"     unprotected tokens/call grows from ~{t1 // c1 // 2} to ~{t1 // c1 * 2}; the cap stops it at {c2}.")
    r, *_ = await once("PROTECTED, iteration cap lifted (token budget)", "varying", msg2, dict(max_iterations=200, max_identical_calls=10**6))
    print("     (defence in depth: with the iteration cap misconfigured, the per-request TOKEN budget still stops it)")

    h2("Scenario C - two agents delegate to each other in a ping-pong (A -> B -> A -> B ...)")
    agents = {**AGENTS, "accounts_agent": replace(AGENTS["accounts_agent"], can_delegate_to=("transfers_agent",)),
              "transfers_agent": replace(AGENTS["transfers_agent"], can_delegate_to=("accounts_agent",))}
    _, c1, t1, k1 = await once("UNPROTECTED (depth cap raised 3 -> 25)", "delegate", "Show my balance", dict(UNPROTECTED, max_delegation_depth=25), agents)  # fully lifted = unbounded recursion
    r, c2, *_ = await once("PROTECTED (max delegation depth 3)", "delegate", "Show my balance", None, agents)
    bot(r)

    h2("Scenario D - per-user daily budget: a customer (or a script) hammering the assistant")
    w = await World.create(make_settings(user_daily_cost_usd=0.0004))
    for i in range(1, 12):
        r = await w.say("alice", "What's my balance?")
        if r.log.budget_stopped:
            print(f"  request #{i}: refused BEFORE any LLM call -> {r.text}")
            break
    print(f"  bob is unaffected: {(await w.say('bob', 'What is my balance?')).outcome}")

    h2("Cost tracking per agent / provider for this run")
    for row in w.app.cost.report("agent"):
        print(f"  {str(row['key']):<14} calls={row['calls']:<3} tokens={row['tokens']:<6} cost=${row['cost_usd']:.6f}")
    await w.close()


if __name__ == "__main__":
    run(main())
