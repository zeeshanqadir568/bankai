"""Client side of the tool layer: DISCOVERS tools over MCP and calls them with delegated identity.

Agents hold no tool code. At startup the gateway connects to each configured MCP server (in-process
object in tests/evals, streamable-HTTP URL in production), lists its tools and builds a catalog.
Each agent is then handed the subset its config allows. If a server is down its tools simply vanish
from the catalog (graceful degradation) and reappear when it recovers.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from mcp import Client, MCPError

from ..config import Settings
from ..obs.tracing import current_traceparent, set_attrs, span
from ..resilience import CircuitBreaker, CircuitOpenError, TransientError, resilient_call
from ..security.identity import Identity
from ..security.tool_tokens import ToolTokenService

log = logging.getLogger("bankai.mcp")


@dataclass(frozen=True)
class ToolDef:
    server: str
    name: str
    description: str
    schema: dict
    data_class: str = "restricted"  # fail closed: unknown => restricted
    mutating: bool = False

    @property
    def fq(self) -> str:
        return f"{self.server}__{self.name}"


@dataclass
class ToolResult:
    ok: bool
    data: dict = field(default_factory=dict)
    code: str = ""
    message: str = ""

    @property
    def denied(self) -> bool:
        return self.code in {"access_denied", "unauthenticated", "out_of_scope"}


@dataclass
class CallAuth:
    """Everything the gateway needs to mint a tool token. Built by trusted runtime code only."""

    identity: Identity
    agent: str
    scopes: list[str]
    taint: list[str] = field(default_factory=list)
    session_id: str = ""
    trace_id: str = ""


class MCPGateway:
    def __init__(self, targets: dict[str, Any], tokens: ToolTokenService, settings: Settings):
        """targets: server name -> MCPServer instance (in-process) or 'http://host:port/mcp' URL."""
        self.targets, self.tokens, self.s = targets, tokens, settings
        self.catalog: dict[str, ToolDef] = {}
        self.breakers = {n: CircuitBreaker(f"mcp:{n}", settings.breaker_failure_threshold, settings.breaker_reset_seconds)
                         for n in targets}
        self.unavailable: set[str] = set(targets)
        self._last_discovery: dict[str, float] = {}

    async def discover(self, only: str | None = None) -> None:
        for name, target in self.targets.items():
            if only and name != only:
                continue
            self._last_discovery[name] = time.monotonic()
            try:
                async def _list(t=target):
                    async with Client(t) as c:
                        return await c.list_tools()
                res = await resilient_call(_list, timeout=self.s.mcp_timeout_seconds, attempts=2, base_delay=self.s.retry_base_delay,
                                           retry_on=(Exception,))
            except Exception as e:  # noqa: BLE001
                log.warning("MCP server '%s' unavailable at discovery: %s", name, type(e).__name__)
                self.unavailable.add(name)
                self.catalog = {k: v for k, v in self.catalog.items() if v.server != name}
                continue
            self.unavailable.discard(name)
            for t in res.tools:
                meta = getattr(t, "meta", None) or {}
                td = ToolDef(server=name, name=t.name, description=t.description or "", schema=dict(t.input_schema),
                             data_class=meta.get("data_class", "restricted"), mutating=bool(meta.get("mutating", True)))
                self.catalog[td.fq] = td

    async def probe(self, timeout: float = 2.0) -> None:
        """Active health check (used by /readyz): a cheap list_tools per server. Down servers leave the catalog at once
        (agents then answer 'temporarily unavailable' without wasting an LLM call); recovered servers are re-discovered."""
        async def one(name: str, target: Any) -> None:
            try:
                async def _ping():
                    async with Client(target) as c:
                        return await c.list_tools()
                await asyncio.wait_for(_ping(), timeout)
            except Exception:  # noqa: BLE001
                self.unavailable.add(name)
                self.catalog = {k: v for k, v in self.catalog.items() if v.server != name}
                return
            if name in self.unavailable:
                await self.discover(only=name)

        await asyncio.gather(*(one(n, t) for n, t in self.targets.items()))

    async def refresh_unavailable(self, min_interval: float = 5.0) -> None:
        """Lazy healing: retry discovery of down servers, at most once per interval each."""
        for name in list(self.unavailable):
            if time.monotonic() - self._last_discovery.get(name, 0) >= min_interval:
                await self.discover(only=name)

    def tools_for(self, patterns: list[str]) -> list[ToolDef]:
        return [t for fq, t in sorted(self.catalog.items()) if any(fnmatch.fnmatch(fq, p) for p in patterns)]

    def get(self, fq: str) -> ToolDef | None:
        return self.catalog.get(fq)

    async def call(self, fq: str, args: dict, auth: CallAuth, *, idempotency_key: str | None = None) -> ToolResult:
        td = self.catalog.get(fq)
        server, _, name = fq.partition("__")
        if td is None:
            return ToolResult(False, code="unknown_tool", message=f"Tool '{fq}' is not available.")
        with span(f"mcp.call {fq}", tool=fq, server=server, agent=auth.agent, args=args, mutating=td.mutating) as sp:
            meta = {"auth_token": self.tokens.mint(
                        auth.identity, agent=auth.agent, server=server, scopes=auth.scopes, taint=auth.taint,
                        session_id=auth.session_id, trace_id=auth.trace_id),
                    "traceparent": current_traceparent()}
            if idempotency_key:
                meta["idempotency_key"] = idempotency_key
            breaker = self.breakers[server]

            async def _do():
                try:
                    async with Client(self.targets[server]) as c:
                        return await c.call_tool(name, args, meta=meta)
                except MCPError:
                    raise  # the server answered with a protocol-level error: not a dependency outage
                except Exception as e:  # noqa: BLE001 - application failures come back as results, so an exception here
                    raise TransientError(type(e).__name__) from e  # is connect/transport trouble: retry + count for the breaker

            try:
                # retries only transient transport failures; safe for mutations thanks to the idempotency key
                res = await resilient_call(_do, breaker=breaker, timeout=self.s.mcp_timeout_seconds,
                                           attempts=self.s.retry_attempts if (td.mutating is False or idempotency_key) else 1,
                                           base_delay=self.s.retry_base_delay)
            except MCPError as e:
                set_attrs(sp, error="mcp_error")
                return ToolResult(False, code="tool_error", message=str(e)[:200])
            except CircuitOpenError:
                self.unavailable.add(server)
                set_attrs(sp, error="circuit_open")
                return ToolResult(False, code="service_unavailable", message=f"The {server} service is temporarily unavailable.")
            except Exception as e:  # noqa: BLE001
                self.unavailable.add(server)
                set_attrs(sp, error=type(e).__name__)
                return ToolResult(False, code="service_unavailable", message=f"The {server} service is temporarily unavailable.")
            out = self._parse(res)
            set_attrs(sp, ok=out.ok, code=out.code)
            return out

    @staticmethod
    def _parse(res: Any) -> ToolResult:
        text = "".join(getattr(c, "text", "") for c in (res.content or []))
        if getattr(res, "is_error", False):
            return ToolResult(False, code="tool_error", message=text[:300] or "tool error")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return ToolResult(False, code="bad_response", message="Tool returned a non-JSON response.")
        if data.get("ok") is False:
            err = data.get("error", {})
            return ToolResult(False, data=data, code=err.get("code", "error"), message=err.get("message", ""))
        return ToolResult(True, data=data)
