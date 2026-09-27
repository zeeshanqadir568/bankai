from __future__ import annotations

from dataclasses import dataclass


class AuthenticationError(Exception):
    """Who are you? -> could not be established (bad/expired/forged token)."""


class AuthorizationError(Exception):
    """You are known, but not allowed to do this."""


@dataclass(frozen=True)
class Identity:
    """The authenticated principal. Created ONLY by verifying a signed token, never from request bodies
    or model output."""

    user_id: str
    roles: frozenset[str]
    customer_id: str | None
    display_name: str = ""

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def to_claims(self) -> dict:
        return {"sub": self.user_id, "roles": sorted(self.roles), "cust": self.customer_id, "name": self.display_name}

    @classmethod
    def from_claims(cls, c: dict) -> "Identity":
        return cls(
            user_id=c["sub"],
            roles=frozenset(c.get("roles", [])),
            customer_id=c.get("cust"),
            display_name=c.get("name", ""),
        )
