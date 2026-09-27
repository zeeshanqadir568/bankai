"""AuthN: establish WHO the caller is. Verified at the edge (FastAPI dependency), once per request.

Production: an OIDC IdP issues RS256 tokens and we verify against its JWKS; here the verification
code path is identical (algorithm pinned, iss/aud/exp/sub required) with a shared HS256 key, plus a
mock IdP (`MockIdP`) that exists only when ENV=dev so the demo has users to log in as.
"""
from __future__ import annotations

import jwt

from ..common import clock, new_id
from .identity import AuthenticationError, Identity

# Mock directory of users (dev IdP only). Real system: the bank's OIDC provider.
DEMO_USERS = {
    "alice": {"roles": ["customer"], "cust": "cust_alice", "name": "Alice Johnson"},
    "bob": {"roles": ["customer"], "cust": "cust_bob", "name": "Bob Martinez"},
    "tina": {"roles": ["teller"], "cust": None, "name": "Tina Teller"},
    "sam": {"roles": ["approver"], "cust": None, "name": "Sam Supervisor"},
}


class JWTAuthenticator:
    def __init__(self, key: str, algorithm: str, issuer: str, audience: str):
        self._key, self._alg, self._iss, self._aud = key, algorithm, issuer, audience

    def verify(self, token: str) -> Identity:
        if not token:
            raise AuthenticationError("missing bearer token")
        try:
            claims = jwt.decode(
                token,
                self._key,
                algorithms=[self._alg],  # pinned: defeats alg=none / algorithm-confusion
                issuer=self._iss,
                audience=self._aud,
                options={"require": ["exp", "iat", "iss", "aud", "sub"], "verify_exp": False, "verify_iat": False},
            )
        except jwt.PyJWTError as e:
            raise AuthenticationError(f"invalid token: {e.__class__.__name__}") from e
        if claims["exp"] <= clock.now():  # same injectable clock that issue() uses
            raise AuthenticationError("invalid token: expired")
        return Identity.from_claims(claims)

    # ---- dev-only mock IdP ------------------------------------------------------------
    def issue(self, username: str, ttl_seconds: int = 3600) -> str:
        u = DEMO_USERS.get(username)
        if not u:
            raise AuthenticationError("unknown user")
        now = int(clock.now())
        claims = {
            "sub": username, "roles": u["roles"], "cust": u["cust"], "name": u["name"],
            "iss": self._iss, "aud": self._aud, "iat": now, "exp": now + ttl_seconds, "jti": new_id("jti"),
        }
        return jwt.encode(claims, self._key, algorithm=self._alg)
