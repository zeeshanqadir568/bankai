"""End-to-end tracing (OpenTelemetry).

* One trace per user turn: UI -> coordinator -> sub-agents -> LLM calls / MCP tool calls.
* The W3C `traceparent` travels to MCP servers in the request `_meta`, so tool spans (even in another
  container) join the same trace.
* Two exporters: OTLP (Jaeger / any collector) when configured, and a DB span store powering the
  replay CLI and `/debug/traces`. EVERY string attribute is PII-masked before it is stored or exported.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Span, Status, StatusCode
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ..db import app_spans
from ..pii import Redactor

_lock = threading.Lock()
_provider: TracerProvider | None = None
_state: dict[str, Any] = {"engine": None, "redactor": None, "service": "bankai"}


class DBSpanExporter(SpanExporter):
    def export(self, spans) -> SpanExportResult:  # noqa: ANN001
        engine: Engine | None = _state["engine"]
        if engine is None:
            return SpanExportResult.SUCCESS
        rows = []
        for s in spans:
            ctx = s.get_span_context()
            attrs = {k: (v if isinstance(v, (int, float, bool)) else str(v)) for k, v in (s.attributes or {}).items()}
            rows.append(dict(
                span_id=format(ctx.span_id, "016x"), trace_id=format(ctx.trace_id, "032x"),
                parent_id=format(s.parent.span_id, "016x") if s.parent else None, name=s.name,
                service=_state["service"], start_ts=s.start_time / 1e9, end_ts=(s.end_time or s.start_time) / 1e9,
                attrs_json=json.dumps(attrs, default=str), status="error" if s.status.status_code == StatusCode.ERROR else "ok"))
        try:
            with engine.begin() as conn:
                conn.execute(app_spans.insert(), rows)
        except SQLAlchemyError:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS


def configure_tracing(service: str, engine: Engine | None, redactor: Redactor | None, otlp_endpoint: str = "") -> None:
    """Idempotent per process. Later calls re-point the DB sink (tests build many apps in one process)."""
    global _provider
    with _lock:
        _state.update(engine=engine, redactor=redactor, service=service)
        if _provider is not None:
            return
        _provider = TracerProvider(resource=Resource.create({"service.name": service}))
        _provider.add_span_processor(SimpleSpanProcessor(DBSpanExporter()))
        if otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint.rstrip("/") + "/v1/traces")))
        trace.set_tracer_provider(_provider)


def _tracer():
    return trace.get_tracer("bankai")


def _safe(value: Any) -> Any:
    red: Redactor | None = _state["redactor"]
    if isinstance(value, (int, float, bool)) or value is None:
        return value if value is not None else ""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text[:4000]
    return red.mask(text) if red else text


def set_attr(span: Span, key: str, value: Any) -> None:
    span.set_attribute(key, _safe(value))


def set_attrs(span: Span, **kv: Any) -> None:
    for k, v in kv.items():
        set_attr(span, k.replace("__", "."), v)


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Span]:
    with _tracer().start_as_current_span(name) as sp:
        set_attrs(sp, **attrs)
        try:
            yield sp
        except Exception as e:  # noqa: BLE001
            sp.set_status(Status(StatusCode.ERROR, type(e).__name__))
            set_attr(sp, "error", f"{type(e).__name__}: {e}")
            raise


@contextmanager
def remote_span(name: str, traceparent: str | None, **attrs: Any) -> Iterator[Span]:
    """Server side of context propagation: continue the caller's trace from a `traceparent`."""
    token = None
    if traceparent:
        ctx = propagate.extract({"traceparent": traceparent})
        token = otel_context.attach(ctx)
    try:
        with span(name, **attrs) as sp:
            yield sp
    finally:
        if token is not None:
            otel_context.detach(token)


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


def current_trace_id() -> str:
    sc = trace.get_current_span().get_span_context()
    return format(sc.trace_id, "032x") if sc.is_valid else ""


def current_span() -> Span:
    return trace.get_current_span()


# ---------------------------------------------------------------- replay support
def load_trace(engine: Engine, trace_id: str) -> list[dict]:
    from sqlalchemy import select
    with engine.connect() as conn:
        rows = conn.execute(select(app_spans).where(app_spans.c.trace_id == trace_id)
                            .order_by(app_spans.c.start_ts)).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["attrs"] = json.loads(d.pop("attrs_json"))
        d["duration_ms"] = round((d["end_ts"] - d["start_ts"]) * 1000, 2)
        out.append(d)
    return out


def render_trace(spans: list[dict]) -> str:
    """Indented tree, one line per span, with the attributes that matter when debugging an agent."""
    by_parent: dict[str | None, list[dict]] = {}
    ids = {s["span_id"] for s in spans}
    for s in spans:
        by_parent.setdefault(s["parent_id"] if s["parent_id"] in ids else None, []).append(s)
    lines: list[str] = []
    keys = ("agent", "intent", "route", "tool", "args", "result", "llm.provider", "llm.model", "llm.route_reason",
            "llm.tokens_in", "llm.tokens_out", "llm.cost_usd", "authz", "error", "guardrail")

    def walk(pid: str | None, depth: int) -> None:
        for s in by_parent.get(pid, []):
            a = s["attrs"]
            extras = " ".join(f"{k}={str(a[k])[:110]}" for k in keys if k in a)
            flag = " [ERROR]" if s["status"] == "error" else ""
            lines.append(f"{'  ' * depth}- {s['name']} ({s['duration_ms']}ms){flag} {extras}".rstrip())
            walk(s["span_id"], depth + 1)

    walk(None, 0)
    return "\n".join(lines)
