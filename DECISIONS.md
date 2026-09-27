# Design decisions

Format per component: **Problem** - **Design** - **Rejected** - **Without it** (what actually breaks). Where a decision was forced by something that went wrong while building this, the note says so: those are the ones I trust most.

## Cross-cutting choices

**Custom agent loop, not LangGraph.** The loop (`agents/runner.py`, ~200 lines) is where the safety controls live: guard before every call, sanitise after, budget ticks, blackboard hooks, identity minting. A graph framework would put that logic in node callbacks and edges I would then have to audit for bypasses, and it adds a dependency whose semantics change faster than a bank wants. *Rejected:* LangGraph (good for durable, branching workflows; I do not need its checkpointing because the paused state is an explicit table, `app_paused_runs`); autogen/crew-style role-play (non-deterministic control flow around money). *Cost of this choice:* no visual graph tooling; I write retries and state persistence myself.

**Python + FastAPI + official `mcp` SDK (2.2).** One language for agents, tool servers and tests; the SDK's in-process `Client(server)` lets the *same* servers run inside tests/evals and as separate containers. The 2.x SDK differs from 1.x (`MCPServer`, `Client`, `_meta` on requests); I verified its behaviour by probing it before building on it. *Rejected:* TypeScript/NestJS (no advantage here, and a second toolchain).

**SQLAlchemy Core over SQLite (tests) and Postgres (compose).** One schema, one code path, hermetic tests that need no Docker. `core_*` tables simulate other teams' systems of record; `app_*` tables are this service's own. Money is integer cents, time is epoch floats behind an injectable clock. *Rejected:* an ORM (unneeded weight), separate mock bank service (the MCP servers already are the boundary).

**Mock LLMs by default, real clients included.** No model or API key was available, and the guarantees do not depend on model quality. The mocks read the routing table from the prompt file (so prompt edits change behaviour and evals can catch them), can be `gullible` (obey any injected instruction), can loop, and can fail. Honest consequence: tool-selection accuracy and real injection resistance are **unmeasured**; the harness runs unchanged against Ollama (`--local ollama`).

**Delegated tool tokens travel in MCP `_meta`.** Found by probing the SDK: `ctx.request_context.meta` carries arbitrary keys from `Client.call_tool(meta=...)`, so identity, idempotency key and W3C `traceparent` reach the server without being tool arguments. *Rejected:* a hidden `auth_token` argument (leaks into schemas and traces, must be stripped everywhere); HTTP headers (not available over stdio/in-process, so tests would exercise a different path than production).

---

## 1. Baseline: chat UI, agent loop, swappable LLM
**Problem.** A demo chatbot fuses UI, model and logic; you cannot change the model, test the loop, or stream safely.
**Design.** `LLMClient` protocol (`chat(LLMRequest) -> LLMResponse`) with provider-neutral messages/tools/usage/cost; four implementations (mock local/external, Ollama, Anthropic). Agent loop = plan (LLM) -> act (tool) -> observe -> repeat, bounded. UI is static HTML+JS over SSE with **no inline script** (strict CSP) and model text inserted only via `textContent`.
**Streaming, honestly.** The UI streams progress events live (route, tool, guardrail chips) and then the *completed, output-guarded* answer in chunks. Token-level streaming from the model is not forwarded because the output guard must inspect the whole text before any of it is released. *Rejected:* streaming raw tokens (a leaked account number would already be on screen).
**Without it.** Swapping a model is a rewrite; the loop cannot be tested without a live model; a compromised model's text becomes HTML.

## 2. Multi-agent design
**Problem.** One agent with ~12 tools: bigger prompt on every call, worse tool selection, and a hijacked agent can reach everything.
**Design.** Coordinator (no banking tools) classifies to JSON, routes via an allow-list, and passes single-specialist answers straight through (a second LLM call would only add cost); synthesises only for multi-intent requests. Four specialists each own a glob of MCP tools and a prompt; `transfers_agent` may delegate to `accounts_agent` (depth-capped). Confirmation and escalation are decided by **plain code** in the coordinator, not by the model.
**Measured, not asserted** (`make demo-multi`): 10 tools = 845 schema tokens on *every* call vs 74-281 per specialist; the same request costs 1.66x the tokens on the monolith; the accounts agent can reach three read-only tools vs ten including `initiate_transfer`. Tool-*selection accuracy* is the third argument and needs a real model to measure - I did not fake it.
**Rejected:** one agent (above); a router that is itself tool-using (blast radius); agents calling each other freely (unbounded loops - hence the depth cap and the `delegate` tool being explicit).
**Without it.** Cost and error rate grow with every tool added; one injection reaches the money tools.

## 3. Tools via MCP
**Problem.** Tools imported into agents cannot be scoped, audited or deployed independently, and agents can call anything they import.
**Design.** Four servers (`accounts`, `payments`, `crm`, `knowledge`); agents hold **no tool code**. `MCPGateway.discover()` lists tools at startup and lazily re-discovers; each agent gets the subset its config globs allow. Tool `_meta` advertises `data_class` (restricted/public) and `mutating`, which drive LLM routing and idempotency; unknown defaults to restricted. Servers run in-process (tests/evals) or as streamable-HTTP containers with `/health` and DNS-rebinding protection. Staff tools (`decide_approval`) live on `payments-mcp` because they share transfer state, but **no agent scope includes them** (tested).
**Rejected:** stdio subprocess per tool (poor fit for containers); a shared tool library (no boundary); one giant MCP server (one blast radius, one deploy).
**Without it.** No boundary at which to enforce anything, and no way to run a tool outage without a code change.

## 4. Authentication and authorization
**Problem.** "The agent said it's Alice" is not evidence; conflating who-you-are with what-you-may-do is how confused-deputy bugs ship.
**Design.**
*AuthN*: JWT verified once at the edge; algorithm pinned (defeats `alg=none` and algorithm confusion), `exp/iss/aud/sub` required. Expiry is checked against the injectable clock (a bug I found: PyJWT checks wall time, so time-travel tests could not expire tokens).
*Delegation*: the user token is **not** forwarded. The runtime mints a 60 s per-call token bound to one MCP server (`aud`), the calling agent, that agent's tool scopes, and the turn's taint.
*AuthZ*: pure function `authorize(identity, "accounts:read", owner)` with `:own`/`:any` scopes; ownership is resolved by the **tool layer from the ledger**, never from arguments. Roles: customer (own), teller (read any, tickets any, **no** money movement), approver (decide approvals; cannot browse accounts). Separation of duties: nobody approves their own request. Missing and not-yours return the same message (no account-enumeration oracle); the audit log keeps the true reason.
*Audit*: every call, allowed/denied/errored, in a hash-chained table (arguments PII-masked); tampering is detectable (tested by rewriting a denial into an allow).
**Rejected:** forwarding the edge JWT to tools (replay + over-broad); trusting `customer_id` arguments; RBAC-only without ownership (a customer role would read every account).
**Without it.** Any prompt that names another customer's account succeeds; there is no forensic record.

## 5. Sessions, memory, blackboard - the SWE / AI-engineering split
**Problem.** "Memory" gets used for three different problems.
**Session store (software engineering)** - `state/sessions.py`: owner binding (another user's session id is indistinguishable from a missing one), sliding TTL plus an absolute lifetime cap, purge that also deletes transcripts (data minimisation), optimistic concurrency via a version column so two tabs cannot silently overwrite each other. None of this involves a model.
**Conversation memory (AI engineering)** - `state/memory.py`: *what the model gets to see and what it may remember.* Short-term = last N messages verbatim + rolling summary of older ones, produced lazily by the **in-bank** model (transcripts hold PII) and cached so it is not recomputed. Long-term = a tiny typed key set (`preferred_from_account`, `preferred_name`, `last_account_viewed`), written **only from the customer's own words and system-of-record results** - never from transaction text or FAQ documents (memory-poisoning defence, tested) - and treated as a *hint, never authority*: a poisoned "preferred account" still cannot move money because the tool layer re-checks ownership (tested). While building I found the provenance guard was rejecting remembered accounts, silently defeating the feature; the fix was to count trusted memory as grounded.
**Blackboard (inter-agent state)** - `state/blackboard.py`: typed fields (`verified_accounts`, `known_payees`, `pending_transfer`, `risk_flags`) instead of chat-text hand-offs. Only runtime hooks write them from system-of-record results; each agent is shown only its declared slice (the FAQ agent, whose calls may go external, sees no account data at all) and models never see their own taint flags.
**Rejected:** a vector store of every conversation (leaks, stales, poisons); putting session logic in the prompt; letting the model write the blackboard.
**Without it.** Session fixation/hijack; unbounded context and cost; agents re-deriving facts from lossy prose that an attacker can edit.

## 6. Hybrid LLM strategy and PII
**Problem.** Sensitive data must not leave; some tasks (FAQ rephrasing) benefit from a frontier model.
**Design.** Router default is **local**. External requires *all* of: task on an allow-list (`faq_answer`, `general`); no message with `data_class=restricted` (anything that came from accounts/payments/crm tools); no high-sensitivity PII (account/card/IBAN/SSN); at most three incidental PII items (names/emails/phones); kill switch on. The only path to the external provider is `GuardedExternalLLM`: reject restricted, redact reversibly (`<PERSON_1>`), **re-scan the outbound payload and refuse if anything still matches**, write an egress-log row (hash, redaction count, verification result, redacted preview), call, then re-inject placeholders in text and tool arguments. Local outage never falls back to external for sensitive work (it fails instead); external outage degrades to local. A stream-safe re-injector handles placeholders split across chunks.
**Proof.** `no_raw_pii_ever_reaches_the_external_client` inspects what the (mock) external *server* actually received across eight PII-laden prompts, and is not vacuous (it asserts something was sent). A meta-test shows the assertion catches a leaky client; an eval invariant runs the same check in **every** golden case; a test simulates a developer bypassing the guard and confirms the evals go red.
**Rejected:** redact-and-send for everything (a regex miss becomes a leak); trusting the router alone (hence the guard's independent refusals); sending tool results externally "because they look public".
**Without it.** One misrouted balance question is a reportable data breach.

## 7. Prompt-injection guardrails
**Problem.** The model reads text written by strangers (transaction descriptions, FAQ documents, tickets) and cannot reliably tell it from instructions.
**Design - four layers, ordered by how much I trust them:**
1. *Sanitise + isolate* (`guardrails/sanitize.py`): NFKC, strip invisible/bidi characters, remove instruction-shaped text (override, role hijack, fake system turns, concealment, money-movement directives incl. the following account number, prompt extraction, delimiter spoofing), wrap in `<untrusted_data>`. Heuristic and bypassable; it is layer 1 of 4 so nothing depends on it.
2. *Tool-layer policy* (non-negotiable): ownership + scope + risk policy recomputed from the ledger (new payee, over threshold, drains 80%+, velocity, tainted session) -> pending human approval. Holds even if every agent-side guard is off (tested: `layer2_tool_policy_alone...`).
3. *Action guard in trusted code*: allow-list; no smuggled arguments; **provenance** (an account number in a call must be traceable to the customer's own words, verified accounts, saved payees or trusted memory); **explicit confirmation** of the exact transfer, set by regex on the customer's own next message (the model cannot set it; changed parameters require re-confirmation); loop counting includes blocked calls. Output guard masks account numbers the customer has no claim to.
4. *Canaries*: `make canary` reruns the attack corpus (10 phrasings + real end-to-end scenarios against a `gullible` model) on every change; a layer-ablation table in `demos/attack.py` shows which layer catches what when the others are removed.
**Where a naive system breaks.** Tools that map a model's call onto a DB function with agent-supplied ids: the model reads the poisoned description and drains the account, and nobody asked the customer (demo part A).
**Honest limit.** An injection aimed at an already-known payee with a small amount is indistinguishable from intent *at the ledger*; layers 1 and 3 (and taint escalation) cover it. Sticky taint is conservative by design.
**Rejected:** "just tell the model to ignore injections" (a prompt is a request, not a control); a classifier as the only defence (probabilistic, attackable); blocking all third-party text (removes the product).
**Without it.** Anyone who can put a sentence in a transaction memo can move your customers' money.

## 8. Agent evaluation pipeline
**Problem.** Prompt and model changes regress silently; "it seemed fine" does not scale.
**Design.** 44 golden conversations (+2 deliberate known failures) across accounts, authz, transfers, HITL, FAQ, tickets, routing, escalation, injection, PII, budgets and outages, each in a fresh isolated system. Assertions: routing, tool inclusion/exclusion/exactness, **tool-layer denial vs guard block** (distinguished), flags, escalation *reason*, which budget fired, providers used, state post-conditions (balances, transfers, tickets, notifications), plus a per-turn quality rubric (non-empty, concise, no internal-detail leak, no account number the customer did not type) and a global "no PII reached the external model" invariant. Reports: per-category pass rate, critical-failure gate, tokens, cost, latency, prompts hash. Known-fail cases render as XFAIL; one that starts passing is XPASS and fails the suite.
**Proven to catch regressions** (`docs/eval-regression-report.md`): deleting one routing line from the coordinator prompt turns **12 cases red** (and it failed *safe* - requests escalated, no money moved). `tests/test_evals.py` also sabotages authz, the action guard, the PII guard and loop protection and asserts the suite fails each time.
**The meta-tests found four weaknesses in my own evals** before I trusted them: escalation cases passed for the wrong reason (low-confidence fallback also escalates, so I added `escalated: <reason>`); the loop case passed with the loop detector removed (the iteration cap backstops it, so I added `budget: <which>`); `--only` did not match categories; and one sabotage test never ran the case it named. A green suite that cannot go red is decoration.
**Rejected:** LLM-as-judge for the security assertions (non-deterministic); snapshot-testing full responses (brittle).
**Without it.** You find out from customers.

## 9. Observability
**Problem.** Agent failures are sequences of small decisions; logs of the final answer explain nothing.
**Design.** OpenTelemetry. One trace per turn: `chat.turn` -> `coordinator.classify` -> `agent.run` -> `llm.call` / `tool.request` -> `mcp.call` -> `mcp.tool` (server side, in another container). The W3C `traceparent` rides in MCP `_meta`, so the tool span joins the caller's trace (verified across containers, and Jaeger receives spans from the API and MCP services). Attributes: agent, intent + confidence, route reason, tool args/result, tokens, latency, cost, authz decision, guardrail. Two exporters: OTLP (Jaeger) and a DB span store that powers `/debug/traces/{id}`, the staff-console replay and `python -m bankai.cli replay`. **Every string attribute is PII-masked before it is stored or exported** (tested: raw email, account numbers and names never appear in a stored trace).
**Rejected:** logging prompts verbatim; a vendor-only tracing setup with no local replay.
**Without it.** "The bot did something weird" is not debuggable, or provable to an auditor.

## 10. Cost control and runaway-loop protection
**Problem.** Loops and slow leaks both look like a tripled bill; neither crashes anything.
**Design.** Per-request `RequestBudget` shared by the coordinator and every sub-agent: iteration cap, identical-call detector (counts blocked attempts too), delegation-depth cap, LLM-call cap, token cap, cost cap; every stop raises `BudgetExceeded` with a human-readable message and a human hand-off. Per-user daily cap checked **before** any LLM call. Persistent ledger by user/session/agent/provider.
**Measured** (`make demo-runaway`, real numbers): identical-retry loop 8.2x fewer LLM calls and 23x fewer tokens with protection; a *varying* loop (invisible to the identical-call detector) is stopped by the iteration cap at 7 calls vs 41, ~30x fewer tokens - context grows every step, so cost is quadratic in loop length; with the iteration cap deliberately lifted, the token budget still stops it. Lifting the depth cap entirely produced unbounded recursion that crashed the demo - which is the point.
**Rejected:** a single global rate limit (does not stop one expensive request); timeouts alone (a fast loop is still a loop).
**Without it.** The bill is the alert.

## 11. Async, queues and human-in-the-loop
**Problem.** Slow or risky work must not block the request path, and money must not move on a model's say-so.
**Design.** Redis Streams (consumer groups, ack, bounded retries, dead-letter stream, crash recovery via `XAUTOCLAIM`), with an in-memory twin of identical semantics for tests. Handlers are idempotent (at-least-once delivery). Tickets are created asynchronously (`queued` -> worker -> `open`). **HITL:** payments policy returns `pending_approval` (no funds move); the approval request is queued and pushed to the staff console over SSE; the paused run is persisted (`app_paused_runs`, claimed atomically so a redelivered event cannot resume it twice); staff decide through the same tool layer (approver role, separation of duties); timeout is enforced both by a sweeper and at decision time; denial and expiry resume the conversation with an honest message. **Idempotency:** mutations require a key; the runtime derives it from `hash(turn, tool, canonical args)` so a model that "retries with a fresh random id" cannot double-spend; the key and the operation commit in one transaction; same key + different payload is a conflict.
**Rejected:** the agent blocking a request until approval (holds connections, breaks on restart); RabbitMQ (fine, but Redis was already needed and Streams cover the semantics); model-generated idempotency keys.
**Without it.** Double transfers on retry, a hung request per approval, lost approvals on restart.

## 12. Production deployment and failure handling
**Problem.** Dependencies fail; the system must fail small and say so.
**Design.** Docker Compose: Postgres, Redis, Jaeger, a one-shot `init` (schema + demo seed), four MCP servers, API, worker; health checks everywhere; non-root image. The API deliberately does **not** depend on the MCP servers, so a tool outage degrades instead of blocking startup; `/readyz` *actively probes* tools and reports `degraded`. Per-call timeouts, exponential backoff with jitter for transient faults only, circuit breakers on each LLM provider and each MCP server, lazy re-discovery so a recovered server rejoins on its own. Config from environment; secrets from mounted files with no defaults outside dev.
**Verified on a real stack, and it found a bug:** the first smoke run showed `/readyz` still "ok" after `payments-mcp` was stopped, because health was only learned from a failing user call. Fixed with an active probe plus a regression test; the rerun passed all 22 checks including degradation and self-heal. Other fixes from the same kind of verification: transport errors from a dead HTTP MCP server were not classified as transient, so the breaker never opened; a consumer loop starved the event loop when the broker's blocking read returned instantly; and the coordinator's own LLM calls (classify/synthesise/summarise) escaped graceful degradation.
**Rejected:** Kubernetes manifests (a real deployment target; out of scope for a reference); one process for everything (no failure isolation); depending on secrets in env vars.
**Without it.** One dead dependency takes down every capability, or worse, half-works silently.
