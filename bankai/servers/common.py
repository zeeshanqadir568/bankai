"""Shared enforcement wrapper for every MCP tool. This is where "tools never trust the agent's word" lives.

For each call, in order:
  1. verify the delegated tool token (signature, expiry, audience == THIS server)   -> else 'unauthenticated'
  2. check the tool is inside the calling agent's scope claim                       -> else 'out_of_scope'
  3. mutating tools must carry an idempotency key                                   -> else refuse
  4. run the business handler, which re-derives ownership from the system of record and authorizes
  5. write an audit row (allow / deny / error) whatever happened, and emit a trace span
The model only ever sees the resulting JSON; it cannot alter any of the above.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from sqlalchemy.engine import Engine

from ..bankcore.audit import AuditLog
from ..bankcore.idempotency import IdempotencyConflict
from ..bankcore.services import Publish, ToolFailure
from ..config import Settings
from ..obs.tracing import remote_span, set_attrs
from ..security.identity import AuthenticationError
from ..security.tool_tokens import ToolCaller, ToolTokenService


@dataclass
class ServerDeps:
    engine: Engine
    settings: Settings
    tokens: ToolTokenService
    audit: AuditLog
    publish: Publish


def error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


class ToolRuntime:
    def __init__(self, server: str, deps: ServerDeps):
        self.server, self.deps = server, deps

    async def run(
        self, ctx: Any, tool: str, args: dict, handler: Callable[[ToolCaller], Awaitable[dict] | dict], *,
        mutating: bool = False,
    ) -> dict:
        meta = (ctx.request_context.meta or {}) if ctx is not None else {}
        fq = f"{self.server}__{tool}"
        with remote_span(f"mcp.tool {fq}", meta.get("traceparent"), tool=fq, server=self.server, args=args) as sp:
            result = await self._run(meta, fq, args, handler, mutating)
            set_attrs(sp, result=result, ok=bool(result.get("ok")),
                      authz="deny" if result.get("error", {}).get("code") in {"access_denied", "unauthenticated", "out_of_scope"} else "allow")
            return result

    async def _run(self, meta: dict, fq: str, args: dict, handler, mutating: bool) -> dict:
        audit = self.deps.audit
        try:
            caller = self.deps.tokens.verify(meta.get("auth_token"), server=self.server)
        except AuthenticationError as e:
            audit.record(actor="anonymous", tool=fq, decision="deny", reason=str(e), args=args)
            return error("unauthenticated", "The request could not be authenticated.")
        actor, base = caller.identity.user_id, dict(agent=caller.agent, trace_id=caller.trace_id, args=args)
        if fq not in caller.scopes:
            audit.record(actor=actor, tool=fq, decision="deny", reason=f"tool outside scope of agent '{caller.agent}'", **base)
            return error("out_of_scope", f"Agent '{caller.agent}' is not permitted to use this tool.")
        idem = meta.get("idempotency_key")
        if mutating and not idem:
            audit.record(actor=actor, tool=fq, decision="deny", reason="missing idempotency key", **base)
            return error("idempotency_key_required", "Mutating calls require an idempotency key.")
        caller = replace(caller, idempotency_key=idem)
        try:
            out = handler(caller)
            if inspect.isawaitable(out):
                out = await out
        except ToolFailure as e:
            audit.record(actor=actor, tool=fq, decision="deny" if e.denied else "error", reason=f"{e.code}: {e.message}", **base)
            return error(e.code, e.message)
        except IdempotencyConflict as e:
            audit.record(actor=actor, tool=fq, decision="error", reason=f"idempotency_conflict: {e}", **base)
            return error("idempotency_conflict", str(e))
        except Exception as e:  # noqa: BLE001 - never leak internals to the model
            audit.record(actor=actor, tool=fq, decision="error", reason=f"internal: {type(e).__name__}", **base)
            return error("internal_error", "The service failed to process this request.")
        audit.record(actor=actor, tool=fq, decision="allow", reason=out.get("status", "ok") if isinstance(out, dict) else "ok", **base)
        return {"ok": True, **out} if "ok" not in out else out
