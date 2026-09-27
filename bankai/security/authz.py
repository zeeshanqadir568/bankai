"""AuthZ: decide WHAT this identity may do to WHICH resource. Pure functions, no I/O, exhaustively testable.

Permission grammar:  "<resource>:<verb>[:<scope>]"   scope in {own, any}; no scope = unscoped.
  * `:any`  -> allowed on every resource of that type
  * `:own`  -> allowed only when the resource's owning customer == identity.customer_id
Ownership is always resolved by the TOOL LAYER from the system of record — never from tool arguments
supplied by an agent/model.
"""
from __future__ import annotations

from dataclasses import dataclass

from .identity import Identity

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "customer": frozenset({
        "accounts:read:own", "transfers:initiate:own", "transfers:read:own", "payees:read:own",
        "tickets:create:own", "tickets:read:own", "kb:read",
    }),
    # Elevated but scoped: a teller can SEE any account and file tickets for any customer,
    # but cannot move money (no transfers:initiate) and cannot approve anything.
    "teller": frozenset({"accounts:read:any", "tickets:create:any", "tickets:read:any", "kb:read", "transfers:read:any"}),
    # Approvers decide held transfers; they cannot browse accounts or initiate transfers.
    "approver": frozenset({"approvals:read", "approvals:decide", "transfers:read:any", "kb:read"}),
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


def permissions_of(identity: Identity) -> frozenset[str]:
    perms: set[str] = set()
    for r in identity.roles:
        perms |= ROLE_PERMISSIONS.get(r, frozenset())
    return frozenset(perms)


def authorize(identity: Identity, action: str, owner_customer_id: str | None = None) -> Decision:
    """action like 'accounts:read'. owner_customer_id = who owns the target resource (from the system of record)."""
    perms = permissions_of(identity)
    if f"{action}:any" in perms or action in perms:
        return Decision(True, f"role grants {action} on any resource")
    if f"{action}:own" in perms:
        if owner_customer_id is not None and owner_customer_id == identity.customer_id:
            return Decision(True, "caller owns the resource")
        return Decision(False, f"{action} is limited to the caller's own resources")
    return Decision(False, f"no role of the caller grants {action}")


def authorize_approval(identity: Identity, initiator_user_id: str) -> Decision:
    """Separation of duties: nobody approves a transfer they (or their session) initiated."""
    base = authorize(identity, "approvals:decide")
    if not base:
        return base
    if identity.user_id == initiator_user_id:
        return Decision(False, "separation of duties: an initiator cannot approve their own transfer")
    return Decision(True, "approver, not the initiator")
