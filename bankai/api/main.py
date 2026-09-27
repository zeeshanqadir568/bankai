"""HTTP edge. AuthN happens HERE, once: a verified JWT becomes an `Identity`; nothing downstream ever accepts an
identity from a request body, header or model output."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, text, update

from ..container import App, build_app
from ..db import app_notifications, app_spans
from ..obs.tracing import load_trace, render_trace
from ..queueing.worker import Worker
from ..security.identity import AuthenticationError, Identity
from ..state.sessions import SessionExpired, SessionNotFound

STATIC = Path(__file__).parent / "static"


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None


class TokenIn(BaseModel):
    username: str


class DecisionIn(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=300)


class Broadcaster:
    """Fan-out of approval requests to connected staff browsers (SSE)."""

    def __init__(self) -> None:
        self.subs: set[asyncio.Queue] = set()

    async def push(self, ev: dict) -> None:
        for q in list(self.subs):
            q.put_nowait(ev)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def create_api(bank: App | None = None, *, run_workers: bool | None = None) -> FastAPI:
    owns_bank = bank is None
    if run_workers is None:
        run_workers = os.environ.get("RUN_WORKERS_IN_API", "true").lower() == "true"
    holder: dict = {"bank": bank}
    broadcaster = Broadcaster()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        if holder["bank"] is None:
            holder["bank"] = build_app()
        b: App = holder["bank"]
        await b.start()
        stop = asyncio.Event()
        tasks = [asyncio.create_task(b.queue.consume_loop(
            "approvals.requested", broadcaster.push, stop, group=f"push-{uuid.uuid4().hex[:6]}"))]
        if run_workers:
            tasks.append(asyncio.create_task(Worker(b).run(stop)))
        try:
            yield
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if owns_bank:
                await b.close()

    api = FastAPI(title="Bank AI Assistant", lifespan=lifespan)
    api.state.holder = holder
    api.state.broadcaster = broadcaster

    def _bank() -> App:
        return holder["bank"]

    def identity(request: Request) -> Identity:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        try:
            return _bank().authn.verify(token)
        except AuthenticationError as e:
            raise HTTPException(401, str(e), headers={"WWW-Authenticate": "Bearer"}) from e

    def require(*roles: str):
        def dep(who: Identity = Depends(identity)) -> Identity:
            if not (who.roles & set(roles)):
                raise HTTPException(403, "forbidden")
            return who
        return dep

    @api.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        return resp

    # ---------------------------------------------------------------- health
    @api.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @api.get("/readyz")
    async def readyz():
        b = _bank()
        try:
            with b.engine.connect() as conn:
                conn.execute(text("select 1"))
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "down", "reason": "database"}, status_code=503)
        await b.gateway.probe()
        degraded = sorted(b.gateway.unavailable)
        return {"status": "degraded" if degraded else "ok", "unavailable_tool_servers": degraded,
                "llm_breakers": {k: v.state for k, v in b.router.breakers.items()}}

    # ---------------------------------------------------------------- auth (dev IdP only)
    @api.post("/auth/dev-token")
    async def dev_token(body: TokenIn):
        b = _bank()
        if b.settings.env != "dev":
            raise HTTPException(404)
        try:
            return {"token": b.authn.issue(body.username)}
        except AuthenticationError as e:
            raise HTTPException(404, "unknown user") from e

    # ---------------------------------------------------------------- chat (SSE)
    @api.post("/chat")
    async def chat(body: ChatIn, who: Identity = Depends(identity)):
        b = _bank()
        if body.session_id:
            try:
                b.sessions.get(body.session_id, who.user_id)
            except SessionNotFound as e:
                raise HTTPException(404, "session not found") from e
            except SessionExpired as e:
                raise HTTPException(410, "session expired") from e
        events: asyncio.Queue = asyncio.Queue()

        async def emit(ev: dict) -> None:
            await events.put(ev)

        async def run():
            try:
                return await b.orchestrator.handle_turn(who, body.message, body.session_id, emit)
            finally:
                await events.put(None)

        task = asyncio.create_task(run())

        async def gen():
            try:
                while (ev := await events.get()) is not None:
                    if ev["type"] != "done":
                        yield sse("progress", ev)
                res = await task
                yield sse("session", {"session_id": res.session_id, "trace_id": res.trace_id})
                # The answer is released only after the output guard inspected the WHOLE text, then streamed in chunks.
                words = res.text.split(" ")
                for i in range(0, len(words), 3):
                    yield sse("token", {"text": " ".join(words[i:i + 3]) + (" " if i + 3 < len(words) else "")})
                    await asyncio.sleep(0.01)
                yield sse("done", {"outcome": res.outcome, "routes": res.log.routes, "flags": res.log.flags,
                                   "approvals": res.log.approvals, "trace_id": res.trace_id})
            except Exception as e:  # noqa: BLE001
                yield sse("error", {"message": "Something went wrong on our side. Please try again.", "type": type(e).__name__})

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @api.get("/notifications")
    async def notifications(who: Identity = Depends(identity)):
        b = _bank()
        with b.engine.begin() as conn:
            rows = conn.execute(select(app_notifications).where(app_notifications.c.user_id == who.user_id,
                                                                app_notifications.c.delivered.is_(False))
                                .order_by(app_notifications.c.id)).mappings().all()
            if rows:
                conn.execute(update(app_notifications).where(app_notifications.c.id.in_([r["id"] for r in rows])).values(delivered=True))
        return {"notifications": [{"id": r["id"], "session_id": r["session_id"], "body": r["body"], "ts": r["ts"]} for r in rows]}

    # ---------------------------------------------------------------- staff: human-in-the-loop
    @api.get("/staff/approvals")
    async def staff_approvals(who: Identity = Depends(require("approver"))):
        res = await _bank().staff_list_approvals(who)
        if not res.ok:
            raise HTTPException(503 if res.code == "service_unavailable" else 403, res.message)
        return res.data

    @api.post("/staff/approvals/{approval_id}")
    async def staff_decide(approval_id: str, body: DecisionIn, who: Identity = Depends(require("approver"))):
        res = await _bank().staff_decide(who, approval_id, body.approve, body.note)
        if not res.ok:
            raise HTTPException({"access_denied": 403, "not_found": 404, "already_decided": 409}.get(res.code, 400), res.message)
        return res.data

    @api.get("/staff/stream")
    async def staff_stream(request: Request, who: Identity = Depends(require("approver"))):
        q: asyncio.Queue = asyncio.Queue()
        broadcaster.subs.add(q)

        async def gen():
            try:
                yield sse("hello", {"user": who.user_id})
                while not await request.is_disconnected():
                    try:
                        yield sse("approval_requested", await asyncio.wait_for(q.get(), timeout=15))
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                broadcaster.subs.discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ---------------------------------------------------------------- observability
    @api.get("/debug/traces")
    async def recent_traces(limit: int = 15, who: Identity = Depends(require("approver"))):
        """Most recent conversation turns (root spans), so staff can click a turn instead of copying a 32-char id."""
        with _bank().engine.connect() as conn:
            rows = conn.execute(select(app_spans).where(app_spans.c.name == "chat.turn")
                                .order_by(app_spans.c.start_ts.desc()).limit(max(1, min(limit, 50)))).mappings().all()
        out = []
        for r in rows:
            a = json.loads(r["attrs_json"])
            out.append({"trace_id": r["trace_id"], "ts": r["start_ts"], "user": a.get("user_id", "?"),
                        "message": str(a.get("message", ""))[:90], "outcome": a.get("outcome", "")})
        return {"traces": out}

    @api.get("/debug/traces/{trace_id}")
    async def trace(trace_id: str, format: str = "json", who: Identity = Depends(require("approver"))):
        spans = load_trace(_bank().engine, trace_id)
        if not spans:
            raise HTTPException(404, "trace not found")
        return PlainTextResponse(render_trace(spans)) if format == "text" else {"trace_id": trace_id, "spans": spans}

    @api.get("/debug/audit")
    async def audit(who: Identity = Depends(require("approver"))):
        ok, n, msg = _bank().audit.verify_chain()
        return {"chain_ok": ok, "rows": n, "message": msg, "recent": _bank().audit.recent(20)}

    @api.get("/usage")
    async def usage(who: Identity = Depends(identity)):
        c = _bank().cost
        if "approver" in who.roles:
            return {"by_agent": c.report("agent"), "by_user": c.report("user"), "by_provider": c.report("provider")}
        return {"by_agent": c.report("agent", user_id=who.user_id), "spent_24h_usd": c.spent_since(who.user_id)}

    @api.get("/")
    async def index():
        return FileResponse(STATIC / "chat.html")

    @api.get("/staff")
    async def staff_page():
        return FileResponse(STATIC / "staff.html")

    api.mount("/static", StaticFiles(directory=STATIC), name="static")
    return api
