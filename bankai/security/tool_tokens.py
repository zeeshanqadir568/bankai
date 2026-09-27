"""Delegated identity for MCP tool calls.

The user's edge JWT is NOT forwarded to tool servers (confused-deputy / replay risk). Instead the agent
runtime — trusted code, never the model — mints a short-lived *tool token* per call that says:

    "user U (roles R) via agent A may call tools S on server V, for the next 60 seconds; the context is tainted by T"

* aud = "mcp:<server>"   -> a token minted for accounts-mcp is useless against payments-mcp
* scopes                 -> the tool names this AGENT may call (so a hijacked accounts_agent cannot reach payments)
* taint                  -> risk flags raised in this turn (e.g. injection_suspected); payments-mcp escalates on them
* jti                    -> unique per call, recorded in the audit log

The token rides in MCP `_meta`, out-of-band: it is not in the tool schema, so a model cannot see or forge it.
In production this minter is a token-exchange (STS) service with an asymmetric key; here HS256 with a
shared secret from the secret store keeps the demo self-contained.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import jwt

from ..common import clock, new_id
from .identity import AuthenticationError, Identity

TOKEN_TTL_SECONDS = 60


@dataclass(frozen=True)
class ToolCaller:
    """What an MCP server learns about a call. Everything here was cryptographically verified."""

    identity: Identity
    agent: str
    scopes: frozenset[str]
    taint: tuple[str, ...] = ()
    session_id: str = ""
    trace_id: str = ""
    jti: str = ""
    idempotency_key: str | None = None
    extra: dict = field(default_factory=dict)


class ToolTokenService:
    def __init__(self, key: str, algorithm: str = "HS256"):
        self._key, self._alg = key, algorithm

    def mint(
        self, identity: Identity, *, agent: str, server: str, scopes: list[str], taint: list[str] | None = None,
        session_id: str = "", trace_id: str = "", ttl: int = TOKEN_TTL_SECONDS,
    ) -> str:
        now = int(clock.now())
        claims = {
            **identity.to_claims(), "agent": agent, "aud": f"mcp:{server}", "scopes": sorted(scopes),
            "taint": sorted(set(taint or [])), "sid": session_id, "tid": trace_id,
            "iat": now, "exp": now + ttl, "jti": new_id("tt"), "iss": "bankai-runtime",
        }
        return jwt.encode(claims, self._key, algorithm=self._alg)

    def verify(self, token: str | None, *, server: str) -> ToolCaller:
        if not token:
            raise AuthenticationError("tool call carries no identity token")
        try:
            c = jwt.decode(
                token, self._key, algorithms=[self._alg], audience=f"mcp:{server}", issuer="bankai-runtime",
                options={"require": ["exp", "iat", "aud", "iss", "sub", "jti"], "verify_exp": False, "verify_iat": False},
            )
        except jwt.PyJWTError as e:
            raise AuthenticationError(f"invalid tool token: {e.__class__.__name__}") from e
        if c["exp"] <= clock.now():  # expiry checked against the injectable clock (same one that minted it)
            raise AuthenticationError("invalid tool token: expired")
        return ToolCaller(
            identity=Identity.from_claims(c), agent=c.get("agent", ""), scopes=frozenset(c.get("scopes", [])),
            taint=tuple(c.get("taint", [])), session_id=c.get("sid", ""), trace_id=c.get("tid", ""), jti=c["jti"],
        )
