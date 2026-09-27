"""The ONLY way to reach the external model: redact -> verify -> log -> send -> re-inject.

Defense in depth, three independent refusals before a byte leaves:
  1. any message classed `restricted` (tool data from accounts/payments/crm)        -> blocked
  2. after redaction the payload is RE-SCANNED with the same detector; any residual PII -> blocked (fail closed)
  3. every attempt (sent or blocked) is written to `app_egress_log` with a hash and a redacted preview,
     so "prove PII never left" is a query, not a promise.
The raw provider object is private to this wrapper; agents and the router cannot call it directly.
"""
from __future__ import annotations

import copy
import json
from dataclasses import replace

from sqlalchemy.engine import Engine

from ..common import clock, sha256_hex
from ..db import app_egress_log
from ..pii import RedactionState, Redactor, _iter_strings
from .base import LLMClient, LLMRequest, LLMResponse


class PIILeakBlocked(Exception):
    """Raised instead of sending. Callers fall back to the in-bank model."""


class GuardedExternalLLM:
    provider = "external"

    def __init__(self, raw: LLMClient, redactor: Redactor, engine: Engine | None = None):
        self.__raw = raw  # name-mangled on purpose: not part of the public surface
        self.redactor, self.engine = redactor, engine
        self.model = raw.model

    def _log(self, req: LLMRequest, payload_text: str, redactions: int, clean: bool, blocked: bool) -> None:
        if self.engine is None:
            return
        with self.engine.begin() as conn:
            conn.execute(app_egress_log.insert().values(
                ts=clock.now(), trace_id=req.trace_id, provider="external", model=self.model, redactions=redactions,
                verified_clean=clean, payload_sha256=sha256_hex(payload_text), payload_preview=payload_text[:300],
                blocked=blocked))

    async def chat(self, req: LLMRequest) -> LLMResponse:
        if any(m.data_class == "restricted" for m in req.messages):
            self._log(req, "[restricted data class - not inspected]", 0, False, True)
            raise PIILeakBlocked("restricted-class content may not be sent to an external model")

        state = RedactionState()
        out = copy.copy(req)
        out.messages = []
        for m in req.messages:
            m2 = replace(m, content=self.redactor.redact(m.content, state))
            m2.tool_calls = [replace(c, arguments=self.redactor.redact_obj(c.arguments, state)) for c in m.tool_calls]
            out.messages.append(m2)

        outbound = [s for m in out.messages for s in (m.content, *_iter_strings([c.arguments for c in m.tool_calls]))]
        outbound += [t.name + " " + t.description + " " + json.dumps(t.parameters) for t in out.tools]
        payload_text = "\n".join(outbound)
        residual = self.redactor.detector.find(payload_text)
        if residual:
            self._log(req, self.redactor.mask(payload_text), state.count, False, True)
            raise PIILeakBlocked(f"residual PII after redaction: {sorted({r.kind for r in residual})}")
        self._log(req, payload_text, state.count, True, False)

        resp = await self.__raw.chat(out)
        resp.text = self.redactor.reinject(resp.text, state)
        resp.tool_calls = [replace(c, arguments=self.redactor.reinject_obj(c.arguments, state)) for c in resp.tool_calls]
        resp.redactions = state.count
        return resp
