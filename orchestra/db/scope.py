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


def owner_key(scope: OwnerScope, owner_id: int | None) -> str:
    """Single-column partition key encoding an owner.

    ``a{agent_id}`` / ``t{team_id}`` for assistant/team owners; ``sys`` for
    everything without a per-owner deletion identity (aggregation views never
    own logs, system/builtins data). This is the LIST sub-partition key the
    shared Assistants project is divided by.
    """
    if scope == OwnerScope.ASSISTANT and owner_id is not None:
        return f"a{owner_id}"
    if scope == OwnerScope.TEAM and owner_id is not None:
        return f"t{owner_id}"
    return "sys"


def owner_key_for_context(conn: Connection, context_id: int) -> str:
    """Resolve the ``owner_key`` for a context (its logs inherit this).

    Reads the context's stored ``owner_scope`` / ``owner_id`` (set on creation).
    Falls back to ``'sys'`` for missing/unclassified contexts.
    """
    row = conn.execute(
        text("SELECT owner_scope, owner_id FROM context WHERE id = :cid"),
        {"cid": context_id},
    ).one_or_none()
    if row is None or row[0] is None:
        return "sys"
    return owner_key(OwnerScope(row[0]), row[1])


def backfill_heavy_owner_keys(conn: Connection, batch: int = 50000) -> None:
    """Denormalize each log's owning scope onto the heavy tables as ``owner_key``.

    A log's owner is its *creating* context (the assistant/team one), not the
    aggregation views it is also referenced into -- so the join filters to
    ``owner_scope IN ('assistant','team')``. Logs with no owning context fall
    back to ``'sys'``. ``log_event_context`` and ``embedding`` inherit their
    log's ``owner_key`` so every association/vector lands in the same
    sub-partition and drops together. Batched by id; only NULL rows are touched,
    so it is idempotent and resumable.
    """
    # 1) log_event from its owning (assistant/team) context.
    max_le = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM log_event")).scalar()
    lo = 0
    while lo < max_le:
        hi = lo + batch
        conn.execute(
            text(
                """
                UPDATE log_event le SET owner_key = sub.ok
                FROM (
                    SELECT DISTINCT ON (lec.log_event_id) lec.log_event_id AS leid,
                        CASE c.owner_scope
                            WHEN 'assistant' THEN 'a' || c.owner_id
                            WHEN 'team' THEN 't' || c.owner_id
                        END AS ok
                    FROM log_event_context lec
                    JOIN context c ON c.id = lec.context_id
                    WHERE c.owner_scope IN ('assistant', 'team')
                      AND lec.log_event_id > :lo AND lec.log_event_id <= :hi
                ) sub
                WHERE le.id = sub.leid AND le.owner_key IS NULL
                """,
            ),
            {"lo": lo, "hi": hi},
        )
        lo = hi
    conn.execute(
        text("UPDATE log_event SET owner_key = 'sys' WHERE owner_key IS NULL"),
    )

    # 2) log_event_context inherits its log's owner_key.
    lo = 0
    while lo < max_le:
        hi = lo + batch
        conn.execute(
            text(
                "UPDATE log_event_context lec SET owner_key = le.owner_key "
                "FROM log_event le WHERE le.id = lec.log_event_id "
                "AND lec.log_event_id > :lo AND lec.log_event_id <= :hi "
                "AND lec.owner_key IS NULL",
            ),
            {"lo": lo, "hi": hi},
        )
        lo = hi

    # 3) embedding inherits its referenced log's owner_key (ref_id -> log_event).
    max_emb = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM embedding")).scalar()
    lo = 0
    while lo < max_emb:
        hi = lo + batch
        conn.execute(
            text(
                "UPDATE embedding e SET owner_key = le.owner_key "
                "FROM log_event le WHERE le.id = e.ref_id "
                "AND e.id > :lo AND e.id <= :hi AND e.owner_key IS NULL",
            ),
            {"lo": lo, "hi": hi},
        )
        lo = hi
    conn.execute(text("UPDATE embedding SET owner_key = 'sys' WHERE owner_key IS NULL"))


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
