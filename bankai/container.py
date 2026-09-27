"""Composition root: builds the whole system from Settings. Tests, demos, evals, the API and the worker all use it,
so what we test is what we run. Any dependency (LLMs, MCP targets, guard config) can be injected."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .agents.coordinator import Orchestrator
from .agents.registry import AGENTS, AgentConfig
from .agents.runner import AgentRunner, Deps
from .bankcore.audit import AuditLog
from .bankcore.seed import seed
from .bankcore.services import CRMService, PaymentsService
from .config import Settings
from .cost import CostTracker
from .db import core_customers, init_db, make_engine
from .guardrails.actions import ActionGuard, GuardConfig, OutputGuard
from .llm.base import LLMClient
from .llm.guarded import GuardedExternalLLM
from .llm.providers.anthropic import AnthropicClient
from .llm.providers.mock import MockExternalLLM, MockLocalLLM
from .llm.providers.ollama import OllamaClient
from .llm.router import HybridRouter
from .mcp_layer.gateway import CallAuth, MCPGateway, ToolResult
from .obs.tracing import configure_tracing
from .pii import PIIDetector, Redactor
from .queueing.queue import MessageQueue, make_queue
from .security.authn import JWTAuthenticator
from .security.identity import Identity
from .security.tool_tokens import ToolTokenService
from .servers.common import ServerDeps
from .servers.definitions import BUILDERS
from .state.memory import ConversationMemory
from .state.pauses import PauseStore
from .state.sessions import SessionStore

MCP_URL_SETTINGS = {"accounts": "mcp_accounts_url", "payments": "mcp_payments_url", "crm": "mcp_crm_url", "knowledge": "mcp_knowledge_url"}


@dataclass
class App:
    settings: Settings
    engine: Engine
    queue: MessageQueue
    detector: PIIDetector
    redactor: Redactor
    authn: JWTAuthenticator
    tokens: ToolTokenService
    gateway: MCPGateway
    local: LLMClient
    external_raw: LLMClient | None
    router: HybridRouter
    orchestrator: Orchestrator
    sessions: SessionStore
    memory: ConversationMemory
    cost: CostTracker
    audit: AuditLog
    pauses: PauseStore
    payments: PaymentsService
    crm: CRMService
    guard_cfg: GuardConfig
    mcp_servers: dict[str, Any] = field(default_factory=dict)

    async def start(self) -> None:
        await self.gateway.discover()

    async def close(self) -> None:
        await self.queue.close()

    # ---- staff actions go through the SAME tool layer as agents: approver identity, no special back door ---------
    async def staff_call(self, identity: Identity, tool: str, args: dict, *, idem: str | None = None) -> ToolResult:
        auth = CallAuth(identity, "staff-ui", [f"payments__{tool}"])
        return await self.gateway.call(f"payments__{tool}", args, auth, idempotency_key=idem)

    async def staff_list_approvals(self, identity: Identity) -> ToolResult:
        return await self.staff_call(identity, "list_pending_approvals", {})

    async def staff_decide(self, identity: Identity, approval_id: str, approve: bool, note: str = "") -> ToolResult:
        idem = hashlib.sha256(f"decide:{approval_id}:{approve}:{identity.user_id}".encode()).hexdigest()[:40]
        return await self.staff_call(identity, "decide_approval", {"approval_id": approval_id, "approve": approve, "note": note}, idem=idem)


def build_app(
    settings: Settings | None = None, *, local: LLMClient | None = None, external: LLMClient | None = None,
    guard_cfg: GuardConfig | None = None, agents: dict[str, AgentConfig] | None = None, seed_demo: bool = True,
    queue: MessageQueue | None = None, mcp_targets: dict[str, Any] | None = None,
) -> App:
    s = settings or Settings.from_env()
    secrets = s.secrets()
    engine = make_engine(s.resolved_database_url())
    init_db(engine)
    if seed_demo and s.env == "dev":
        seed(engine)
    with engine.connect() as conn:
        names = [r[0] for r in conn.execute(select(core_customers.c.name))]
    detector = PIIDetector(names)
    redactor = Redactor(detector)
    configure_tracing(s.service_name, engine, redactor, s.otlp_endpoint)

    q = queue or make_queue(s.redis_url)
    tokens = ToolTokenService(secrets.get("mcp_token_key"))
    audit = AuditLog(engine, redactor)
    deps = ServerDeps(engine, s, tokens, audit, q.publish)

    servers: dict[str, Any] = {}
    targets: dict[str, Any] = {}
    for name, build in BUILDERS.items():
        url = getattr(s, MCP_URL_SETTINGS[name])
        if url:
            targets[name] = url
        else:
            servers[name] = build(deps)
            targets[name] = servers[name]
    targets.update(mcp_targets or {})
    gateway = MCPGateway(targets, tokens, s)

    if local is None:
        local = OllamaClient(s.ollama_url, s.local_model, timeout=s.llm_timeout_seconds) if s.local_llm_provider == "ollama" else MockLocalLLM()
    external_raw: LLMClient | None = None
    if s.external_llm_enabled:
        if external is not None:
            external_raw = external
        elif s.external_llm_provider == "anthropic":
            external_raw = AnthropicClient(secrets.get("anthropic_api_key"), s.external_model, timeout=s.llm_timeout_seconds)
        else:
            external_raw = MockExternalLLM()
    guarded = GuardedExternalLLM(external_raw, redactor, engine) if external_raw else None
    router = HybridRouter(local, guarded, detector, s)

    gcfg = guard_cfg or GuardConfig()
    sessions = SessionStore(engine, s.session_ttl_seconds)
    memory = ConversationMemory(engine, s.memory_window_messages)
    cost = CostTracker(engine, s)
    pauses = PauseStore(engine)
    runner = AgentRunner(Deps(gateway, router, ActionGuard(s, gcfg), gcfg, cost, pauses, agents or dict(AGENTS)))
    orch = Orchestrator(runner=runner, llm=router, settings=s, sessions=sessions, memory=memory, cost=cost,
                        output_guard=OutputGuard(detector, redactor), redactor=redactor, queue=q, pauses=pauses,
                        guard_output=gcfg.output)
    return App(
        settings=s, engine=engine, queue=q, detector=detector, redactor=redactor,
        authn=JWTAuthenticator(secrets.get("jwt_signing_key"), s.jwt_algorithm, s.jwt_issuer, s.jwt_audience),
        tokens=tokens, gateway=gateway, local=local, external_raw=external_raw, router=router, orchestrator=orch,
        sessions=sessions, memory=memory, cost=cost, audit=audit, pauses=pauses,
        payments=PaymentsService(engine, s, q.publish), crm=CRMService(engine, q.publish), guard_cfg=gcfg, mcp_servers=servers)
