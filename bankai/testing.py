"""Harness shared by tests, the eval runner and the demo scripts: a fully wired, isolated, in-process system."""
from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select

from .agents.coordinator import TurnResult
from .config import Settings
from .container import App, build_app
from .db import core_accounts, core_transfers
from .llm.providers.mock import MockBrain, MockExternalLLM, MockLocalLLM
from .pii import PIIDetector
from .queueing.queue import InMemoryQueue
from .queueing.worker import Worker
from .security.identity import Identity


def make_settings(**overrides) -> Settings:
    return replace(Settings(database_url="sqlite://", retry_base_delay=0.001, llm_timeout_seconds=5, mcp_timeout_seconds=5), **overrides)


class World:
    def __init__(self, app: App, worker: Worker):
        self.app, self.worker = app, worker

    @classmethod
    async def create(cls, settings: Settings | None = None, *, gullible: bool = False, pathology: str | None = None, **kw) -> "World":
        local = kw.pop("local", None) or MockLocalLLM(MockBrain(gullible=gullible, pathology=pathology))
        external = kw.pop("external", None) or MockExternalLLM(MockBrain())
        app = build_app(settings or make_settings(), local=local, external=external, queue=InMemoryQueue(), **kw)
        await app.start()
        return cls(app, Worker(app))

    def ident(self, username: str) -> Identity:
        return self.app.authn.verify(self.app.authn.issue(username))

    async def say(self, who: str | Identity, msg: str, sid: str | None = None) -> TurnResult:
        return await self.app.orchestrator.handle_turn(self.ident(who) if isinstance(who, str) else who, msg, sid)

    async def pump(self) -> int:
        return await self.worker.pump()

    def balance(self, account: str) -> int:
        with self.app.engine.connect() as conn:
            return conn.execute(select(core_accounts.c.balance_cents).where(core_accounts.c.number == account)).scalar()

    def transfers(self) -> list[dict]:
        with self.app.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(core_transfers)).mappings()]

    def external_payloads(self) -> list[str]:
        """Everything the (mock) external LLM server was actually sent: the ground truth for the no-PII-egress tests."""
        out: list[str] = []
        for req in getattr(self.app.external_raw, "received", []):
            out += [m.content for m in req.messages]
            out += [str(c.arguments) for m in req.messages for c in m.tool_calls]
        return out

    def pii_in(self, texts: list[str]) -> list[str]:
        det: PIIDetector = self.app.detector
        return [f"{m.kind}:{m.value}" for t in texts for m in det.find(t)]

    async def close(self) -> None:
        await self.app.close()
