"""Hybrid routing: decide, per call, whether the in-bank model or the (guarded) external model answers.

Default is LOCAL. A request may go external only if ALL hold:
  * the task is on the external-eligible allow-list (FAQ rephrasing, general reasoning)
  * no message carries the `restricted` data class (anything that came from accounts/payments/crm tools)
  * no high-sensitivity PII (account/card/IBAN/SSN) is present, and only a little incidental PII (names/emails/phones)
  * the kill switch is on and the external client exists
Incidental PII that remains is masked and re-injected by GuardedExternalLLM. Routing is fail-closed: any doubt -> local.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..config import Settings
from ..obs.tracing import set_attrs, span
from ..pii import PIIDetector
from ..resilience import CircuitBreaker, CircuitOpenError, TransientError, resilient_call
from .base import EXTERNAL_ELIGIBLE_TASKS, LLMClient, LLMRequest, LLMResponse
from .guarded import GuardedExternalLLM, PIILeakBlocked

HIGH_SENSITIVITY = {"CARD", "SSN", "IBAN", "ACCOUNT"}
MAX_INCIDENTAL_PII = 3


@dataclass(frozen=True)
class RouteDecision:
    target: str  # local | external
    reason: str
    external_ok: bool  # would external have been acceptable (used for fallback when local is down)


class HybridRouter:
    provider = "router"
    model = "hybrid"

    def __init__(self, local: LLMClient, external: GuardedExternalLLM | None, detector: PIIDetector, settings: Settings):
        self.local, self.external, self.detector, self.s = local, external, detector, settings
        mk = lambda n: CircuitBreaker(n, settings.breaker_failure_threshold, settings.breaker_reset_seconds)  # noqa: E731
        self.breakers = {"local": mk("llm:local"), "external": mk("llm:external")}

    def decide(self, req: LLMRequest) -> RouteDecision:
        if not self.s.external_llm_enabled or self.external is None:
            return RouteDecision("local", "external_disabled", False)
        if req.task not in EXTERNAL_ELIGIBLE_TASKS:
            return RouteDecision("local", f"task_not_eligible:{req.task}", False)
        if any(m.data_class == "restricted" for m in req.messages):
            return RouteDecision("local", "restricted_data_class", False)
        found = [f for m in req.messages for f in self.detector.find(m.content)]
        found += [f for m in req.messages for c in m.tool_calls for f in self.detector.scan_obj(c.arguments)]
        if any(f.kind in HIGH_SENSITIVITY for f in found):
            return RouteDecision("local", "high_sensitivity_pii", False)
        if len(found) > MAX_INCIDENTAL_PII:
            return RouteDecision("local", "too_much_pii", False)
        return RouteDecision("external", f"eligible_public_task(redactions={len(found)})", True)

    async def chat(self, req: LLMRequest) -> LLMResponse:
        d = self.decide(req)
        with span("llm.call", agent=req.agent, llm__task=req.task, llm__route=d.target, llm__route_reason=d.reason) as sp:
            resp = await self._dispatch(req, d)
            set_attrs(sp, llm__provider=resp.provider, llm__model=resp.model, llm__tokens_in=resp.usage.tokens_in,
                      llm__tokens_out=resp.usage.tokens_out, llm__cost_usd=round(resp.cost_usd, 6),
                      llm__latency_ms=round(resp.latency_ms, 1), llm__redactions=resp.redactions,
                      llm__route_reason=resp.route_reason, llm__tool_calls=[c.name for c in resp.tool_calls])
            return resp

    async def _dispatch(self, req: LLMRequest, d: RouteDecision) -> LLMResponse:
        if d.target == "external":
            try:
                resp = await self._call("external", self.external, req)  # type: ignore[arg-type]
                resp.route_reason = d.reason
                return resp
            except (PIILeakBlocked, CircuitOpenError, TransientError, TimeoutError) as e:
                reason = f"external_failed({type(e).__name__})->local"
                resp = await self._call("local", self.local, req)
                resp.route_reason = reason
                return resp
        try:
            resp = await self._call("local", self.local, req)
            resp.route_reason = d.reason
            return resp
        except (CircuitOpenError, TransientError, TimeoutError):
            if not d.external_ok:
                raise  # sensitive request + local down => fail rather than leak
            resp = await self._call("external", self.external, req)  # type: ignore[arg-type]
            resp.route_reason = "local_failed->external(public)"
            return resp

    async def _call(self, which: str, client: LLMClient, req: LLMRequest) -> LLMResponse:
        t0 = time.perf_counter()
        resp = await resilient_call(lambda: client.chat(req), breaker=self.breakers[which], timeout=self.s.llm_timeout_seconds,
                                    attempts=self.s.retry_attempts, base_delay=self.s.retry_base_delay)
        if not resp.latency_ms:
            resp.latency_ms = (time.perf_counter() - t0) * 1000
        return resp
