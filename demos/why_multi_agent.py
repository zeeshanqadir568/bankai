"""DEMO 5 - Why one agent with all the tools fails: measured, not asserted.

    python -m demos.why_multi_agent

What THIS demo measures deterministically: prompt bloat (tool-schema tokens paid on every LLM call) and blast radius (tools an
injected agent can reach). What it CANNOT measure with a mock model: tool-selection accuracy - that depends on the real model, so
run `python -m evals.runner --local ollama` (routing/tool assertions) against yours.
"""
import json

from bankai.agents.registry import AGENTS, MONOLITH
from bankai.common import estimate_tokens
from bankai.testing import World
from demos._common import h1, h2, run


def schema_tokens(tools) -> int:
    return sum(estimate_tokens(t.name + t.description + json.dumps(t.schema)) for t in tools)


async def main():
    h1("DEMO 5: ONE AGENT WITH EVERY TOOL vs SPECIALISED AGENTS")
    w = await World.create()
    cat = w.app.gateway
    visible = {n: cat.tools_for(list(a.tool_patterns)) for n, a in AGENTS.items()}
    mono = cat.tools_for(list(MONOLITH.tool_patterns))
    every = [t for t in cat.catalog.values()]

    h2("Tools in the catalog vs tools each agent is given")
    print(f"  MCP catalog (all servers, incl. staff-only tools): {len(every)} tools")
    print(f"  monolith agent                                   : {len(mono):>2} tools, {schema_tokens(mono):>4} schema tokens on EVERY llm call")
    for n, ts in visible.items():
        print(f"  {n:<16}                                 : {len(ts):>2} tools, {schema_tokens(ts):>4} schema tokens   {[t.name for t in ts]}")

    h2("Same request, both designs: 'What is my balance?'")
    for label, agents in (("monolith ", None), ("specialised", None)):
        pass
    w2 = await World.create()
    r = await w2.say("alice", "What's my balance?")
    spec_tokens = sum(l["tokens"] for l in r.log.llm)
    from dataclasses import replace
    w3 = await World.create(agents={**AGENTS, "accounts_agent": replace(MONOLITH, name="accounts_agent", state_keys=("verified_accounts",))})
    r3 = await w3.say("alice", "What's my balance?")
    mono_tokens = sum(l["tokens"] for l in r3.log.llm)
    print(f"  specialised: {spec_tokens} tokens for the turn | monolith: {mono_tokens} tokens ({mono_tokens / spec_tokens:.2f}x) - same answer, more tokens, and the gap "
          f"grows with every tool added\n  (and with every extra LLM step: the schema is re-sent each iteration).")

    h2("Blast radius: what can a prompt-injected agent DO?")
    print(f"  accounts_agent can reach : {sorted(t.fq for t in visible['accounts_agent'])}  -> read-only")
    print(f"  monolith can reach       : {len(mono)} tools including payments__initiate_transfer -> can move money")
    print("  Even if the model is fully compromised, the specialised accounts agent has nothing dangerous to call; and each agent's\n"
          "  delegated token lists only its own tools, so the tool servers refuse anything else (see demos.authz #5).")

    h2("Other reasons (see DECISIONS.md): independent prompts/evals per domain, per-agent cost attribution, per-agent model choice,\n     independent failure (transfers down != balances down).")
    for x in (w, w2, w3):
        await x.close()


if __name__ == "__main__":
    run(main())
