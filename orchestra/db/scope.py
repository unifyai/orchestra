"""Ownership scope for kernel contexts and their logs/embeddings.

Orchestra is the engine for Unity's assistants, so the kernel models the
ownership boundary that Unity expresses through its context-naming convention
as first-class columns rather than leaving it implicit in context-name strings.

Every context has an **owner**: the entity whose data it holds and which is the
unit of bulk deletion. Owners are:

* ``assistant`` -- the per-assistant contexts ``{user_id}/{agent_id}/<Manager>/...``
  (``owner_id`` = ``agent_id``).
* ``team`` -- the shared-team contexts ``Teams/{team_id}/<Manager>/...``
  (``owner_id`` = ``team_id``).
* ``aggregation`` -- the cross-cutting *view* contexts ``{user_id}/All/...`` and
  ``All/...``. These do not own log events (logs are referenced into them by the
  owning context), so they have no ``owner_id`` and never drive partitioning.
* ``system`` -- everything else (builtins / system datasets) with no per-owner
  deletion semantics.

A log event's owner is the owner of the context it is *created* in (the write
root), not of any aggregation context it is later referenced into. That owner is
denormalized onto the heavy tables so deleting an assistant or a team becomes an
O(1) partition drop that also removes the aggregation references.
"""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.engine import Connection

# Top-level prefix for shared-team contexts (mirrors unity's ContextRegistry).
TEAM_CONTEXT_PREFIX = "Teams"
# Path component marking a cross-assistant / cross-user aggregation view.
AGGREGATION_COMPONENT = "All"


class OwnerScope(StrEnum):
    ASSISTANT = "assistant"
    TEAM = "team"
    AGGREGATION = "aggregation"
    SYSTEM = "system"


class Owner(NamedTuple):
    scope: OwnerScope
    # agent_id for ASSISTANT, team_id for TEAM; None otherwise.
    owner_id: int | None


def _is_int(token: str) -> bool:
    return token.isdigit()


def owner_from_context_name(name: str) -> Owner:
    """Derive the ownership scope of a context from its (Unity-convention) name.

    Convention (see unity ``ContextRegistry`` / ``session_details``):

    * ``Teams/{team_id}/...``            -> team-owned
    * ``{user_id}/All/...`` or ``All/...`` -> aggregation view (no owner)
    * ``{user_id}/{agent_id}/...``       -> assistant-owned
    * anything else                      -> system

    Test contexts are prefixed with a ``tests/.../`` root; the trailing
    ``{user}/{agent}/...`` (or ``Teams/...``) shape is matched after skipping
    any leading ``tests`` segment so seeded test data classifies the same way.
    """
    parts = [p for p in name.split("/") if p != ""]
    if not parts:
        return Owner(OwnerScope.SYSTEM, None)

    # Shared team: Teams/{team_id}/...  (checked first so the team_id int is not
    # mistaken for an agent_id by the assistant scan below). Tolerates a leading
    # test root of arbitrary depth (tests/<...>/Teams/{team_id}/...).
    for i in range(len(parts) - 1):
        if parts[i] == TEAM_CONTEXT_PREFIX and _is_int(parts[i + 1]):
            return Owner(OwnerScope.TEAM, int(parts[i + 1]))

    # Cross-assistant / cross-user aggregation view: ``.../All/...``. These hold
    # only by-reference logs and own nothing.
    if AGGREGATION_COMPONENT in parts:
        return Owner(OwnerScope.AGGREGATION, None)

    # Per-assistant: ``{user_id}/{agent_id}/...`` -- the agent_id is the first
    # integer component that follows a non-integer (the user_id), which also
    # skips any leading ``tests/<...>/`` root.
    for i in range(1, len(parts)):
        if _is_int(parts[i]) and not _is_int(parts[i - 1]):
            return Owner(OwnerScope.ASSISTANT, int(parts[i]))

    return Owner(OwnerScope.SYSTEM, None)


def backfill_context_owners(conn: Connection, batch: int = 5000) -> int:
    """Classify every not-yet-classified context from its name.

    Sets ``owner_scope`` / ``owner_id`` on ``context`` rows where ``owner_scope``
    is NULL, using :func:`owner_from_context_name`. Batched by id and idempotent
    (re-runs only touch still-NULL rows). Returns the number of rows updated.
    """
    updated = 0
    last_id = 0
    while True:
        rows = conn.execute(
            text(
                "SELECT id, name FROM context "
                "WHERE owner_scope IS NULL AND id > :last "
                "ORDER BY id LIMIT :lim",
            ),
            {"last": last_id, "lim": batch},
        ).fetchall()
        if not rows:
            break
        for ctx_id, name in rows:
            owner = owner_from_context_name(name or "")
            conn.execute(
                text(
                    "UPDATE context SET owner_scope = :s, owner_id = :oid "
                    "WHERE id = :cid",
                ),
                {"s": owner.scope.value, "oid": owner.owner_id, "cid": ctx_id},
            )
            updated += 1
        last_id = rows[-1][0]
    return updated
