"""Run ONE MCP tool server as its own process/container:  python -m bankai.servers.run accounts

Each server verifies delegated tool tokens itself and talks to its own system of record; the agent runtime holds no
credentials for the bank's databases. Streamable HTTP, stateless (any replica can answer any call), with /health for
orchestrator probes and DNS-rebinding protection restricted to the service's own hostnames.
"""
from __future__ import annotations

import logging
import os
import sys

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from ..bankcore.audit import AuditLog
from ..config import Settings
from ..db import init_db, make_engine
from ..obs.tracing import configure_tracing
from ..pii import PIIDetector, Redactor
from ..queueing.queue import make_queue
from ..security.tool_tokens import ToolTokenService
from .common import ServerDeps
from .definitions import BUILDERS


def build_asgi(name: str, settings: Settings | None = None):
    s = settings or Settings.from_env(service_name=f"{name}-mcp")
    engine = make_engine(s.resolved_database_url())
    init_db(engine)  # idempotent; the one-shot `init` job seeds demo data
    redactor = Redactor(PIIDetector())
    configure_tracing(s.service_name, engine, redactor, s.otlp_endpoint)
    queue = make_queue(s.redis_url)
    deps = ServerDeps(engine, s, ToolTokenService(s.secrets().get("mcp_token_key")), AuditLog(engine, redactor), queue.publish)
    hosts = os.environ.get("MCP_ALLOWED_HOSTS", f"localhost:*,127.0.0.1:*,{name}-mcp:*,{name}-mcp").split(",")
    app = BUILDERS[name](deps).streamable_http_app(
        stateless_http=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                                                          allowed_origins=[]))

    async def health(_request):
        return JSONResponse({"status": "ok", "server": name})

    app.add_route("/health", health)
    return app


def main() -> None:
    name = sys.argv[1]
    if name not in BUILDERS:
        raise SystemExit(f"unknown server '{name}'; choose from {sorted(BUILDERS)}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    uvicorn.run(build_asgi(name), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="info")


if __name__ == "__main__":
    main()
