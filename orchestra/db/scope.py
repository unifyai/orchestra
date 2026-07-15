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
* ``system`` -- everything else (builtins / system datasets) with no per-owner
  deletion semantics.

Every context is owner-homogeneous: a log event's owner is the owner of the
context it is created in. That owner is denormalized onto the heavy tables so
deleting an assistant or a team is an O(1) owner sub-partition drop.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

# Top-level prefix for shared-team contexts (mirrors unity's ContextRegistry).
TEAM_CONTEXT_PREFIX = "Teams"
# Lightweight multi-party chats (humans + assistants); not team shared-memory.
CHAT_GROUP_CONTEXT_PREFIX = "Groups"


class OwnerScope(StrEnum):
    ASSISTANT = "assistant"
    TEAM = "team"
    GROUP = "group"
    SYSTEM = "system"


class Owner(NamedTuple):
    scope: OwnerScope
    # agent_id for ASSISTANT, team_id for TEAM, group_id for GROUP; None otherwise.
    owner_id: int | None


def _is_int(token: str) -> bool:
    return token.isdigit()


def owner_from_context_name(name: str) -> Owner:
    """Derive the ownership scope of a context from its (Unity-convention) name.

    Convention (see unity ``ContextRegistry`` / ``session_details``):

    * ``Teams/{team_id}/...``      -> team-owned
    * ``Groups/{group_id}/...``   -> chat-group-owned
    * ``{user_id}/{agent_id}/...`` -> assistant-owned
    * anything else                -> system

    Test contexts are prefixed with a ``tests/.../`` root; the trailing
    ``{user}/{agent}/...`` (or ``Teams/...`` / ``Groups/...``) shape is matched
    after skipping any leading ``tests`` segment so seeded test data classifies
    the same way.
    """
    parts = [p for p in name.split("/") if p != ""]
    if not parts:
        return Owner(OwnerScope.SYSTEM, None)

    # Shared team / chat group prefixes first so their int ids are not mistaken
    # for an agent_id by the assistant scan below.
    for i in range(len(parts) - 1):
        if parts[i] == TEAM_CONTEXT_PREFIX and _is_int(parts[i + 1]):
            return Owner(OwnerScope.TEAM, int(parts[i + 1]))
        if parts[i] == CHAT_GROUP_CONTEXT_PREFIX and _is_int(parts[i + 1]):
            return Owner(OwnerScope.GROUP, int(parts[i + 1]))

    # Per-assistant: ``{user_id}/{agent_id}/...`` -- the agent_id is the first
    # integer component that follows a non-integer (the user_id), which also
    # skips any leading ``tests/<...>/`` root.
    for i in range(1, len(parts)):
        if _is_int(parts[i]) and not _is_int(parts[i - 1]):
            return Owner(OwnerScope.ASSISTANT, int(parts[i]))

    return Owner(OwnerScope.SYSTEM, None)


def resolve_owner(
    name: str,
    owner_scope: str | None = None,
    owner_id: int | None = None,
) -> tuple[str, int | None]:
    """Resolve a context's ownership, preferring an explicit scope when given.

    Callers that already know the owning entity (e.g. a client passing the
    active assistant/team) supply ``owner_scope`` / ``owner_id`` directly;
    otherwise the owner is inferred from the context-name convention
    (:func:`owner_from_context_name`). Returns ``(owner_scope, owner_id)`` ready
    to store on the ``context`` row.
    """
    if owner_scope is not None:
        return OwnerScope(owner_scope).value, owner_id
    owner = owner_from_context_name(name)
    return owner.scope.value, owner.owner_id


def owner_key(scope: OwnerScope, owner_id: int | None) -> str:
    """Single-column partition key encoding an owner.

    ``a{agent_id}`` / ``t{team_id}`` / ``g{group_id}`` for assistant/team/group
    owners; ``sys`` for everything without a per-owner deletion identity
    (system/builtins data). This is the LIST sub-partition key the shared
    Assistants project is divided by.
    """
    if scope == OwnerScope.ASSISTANT and owner_id is not None:
        return f"a{owner_id}"
    if scope == OwnerScope.TEAM and owner_id is not None:
        return f"t{owner_id}"
    if scope == OwnerScope.GROUP and owner_id is not None:
        return f"g{owner_id}"
    return "sys"


def single_owner_key(owner_scope, owner_id: int | None) -> str | None:
    """The owner sub-partition key to prune by for a *single-owner* context, or
    ``None`` when the context is not safely owner-homogeneous.

    Only assistant/team/group contexts with a real ``owner_id`` are single-owner:
    all their logs share one ``owner_key`` (enforced by ``ContextDAO.add_logs``),
    so a query filtered to one such context can prune to its owner sub-partition.
    System contexts return ``None`` (``sys`` is a shared bucket, not a single
    owner), so no single ``owner_key`` predicate is valid for them.

    ``owner_scope`` accepts an :class:`OwnerScope` or its stored string value
    (or ``None``).
    """
    if owner_scope is None:
        return None
    if isinstance(owner_scope, OwnerScope):
        scope = owner_scope
    else:
        try:
            scope = OwnerScope(owner_scope)
        except ValueError:
            # Unrecognized scope -> treat as not-single-owner (do not prune).
            return None
    key = owner_key(scope, owner_id)
    return None if key == "sys" else key


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


def single_owner_key_for_context(
    conn: Connection,
    context_id: int | None,
) -> str | None:
    """:func:`single_owner_key` resolved from the DB by ``context_id``.

    Returns the owner sub-partition key for single-owner (assistant/team)
    contexts, or ``None`` for system/missing contexts (which must not be pinned
    to one owner).
    """
    if context_id is None:
        return None
    key = owner_key_for_context(conn, context_id)
    return None if key == "sys" else key


def owner_key_for_log(conn: Connection, log_event_id: int) -> str:
    """Resolve the ``owner_key`` of a log event (its embeddings inherit this)."""
    val = conn.execute(
        text("SELECT owner_key FROM log_event WHERE id = :i"),
        {"i": log_event_id},
    ).scalar()
    return val or "sys"


def purge_owner(
    conn: Connection,
    project_id: int,
    owner_scope: str,
    owner_id: int | None,
) -> str:
    """Delete everything one owner (an assistant or team) holds in a project.

    Two coordinated, indexed deletions:

    * the heavy tables (``log_event`` / ``log_event_context`` / ``embedding``)
      by ``owner_key`` via :func:`orchestra.db.partitioning.drop_owner` -- an
      O(1) partition drop when the owner was promoted, otherwise an
      ``(project_id, owner_key)``-indexed row delete;
    * the owner's ``context`` rows by ``(project_id, owner_scope, owner_id)``
      (the ``idx_context_owner`` partial index), whose ``ON DELETE CASCADE``
      clears ``context_counter`` / ``context_version`` / ``field_type`` and any
      remaining ``log_event_context`` rows.

    Returns the heavy-table method used (``drop_partition`` / ``row_delete``).
    """
    from orchestra.db.partitioning import drop_owner

    method = drop_owner(
        conn,
        int(project_id),
        owner_key(OwnerScope(owner_scope), owner_id),
    )
    conn.execute(
        text(
            "DELETE FROM context WHERE project_id = :pid "
            "AND owner_scope = :scope AND owner_id = :oid",
        ),
        {"pid": int(project_id), "scope": owner_scope, "oid": owner_id},
    )
    return method


def backfill_heavy_owner_keys(conn: Connection, batch: int = 50000) -> None:
    """Denormalize each log's owning scope onto the heavy tables as ``owner_key``.

    A log's owner is its creating (assistant/team) context -- so the join
    filters to ``owner_scope IN ('assistant','team')``. Logs with no owning
    context fall back to ``'sys'``. ``log_event_context`` and ``embedding``
    inherit their
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


def reclassify_heavy_owner_keys(conn: Connection, batch: int = 1000) -> None:
    """Correct heavy rows wrongly stuck at ``'sys'`` to their real owning scope.

    The ``heavy_owner_key`` migration added ``owner_key`` with ``DEFAULT 'sys'``,
    so every pre-existing row was already ``'sys'`` (never NULL) and the original
    NULL-gated backfill was a no-op for all historical data. This recomputes
    ``owner_key`` from each log's owning (assistant/team) context for rows still
    labelled ``'sys'``, so owner-scoped deletion (``purge_owner`` /
    ``drop_owner``) removes an owner's full footprint instead of orphaning its
    history.

    Driven from the (small) set of assistant/team-owned ``context`` rows and
    batched by context id, so the work is proportional to the data that is
    actually mislabelled and every UPDATE is served by
    ``idx_log_event_context_context_id`` plus ``project_id`` partition pruning --
    never a scan of the whole heavy tables. Rows whose owning context is not
    assistant/team stay ``'sys'`` (correct).

    Run under an **autocommit** connection so each batch commits independently;
    idempotent and resumable, since only ``'sys'`` rows are ever touched (a
    re-run skips everything already corrected). This is intended to run
    out-of-band (a standalone maintenance job), not inside a deploy migration.
    """
    max_ctx = conn.execute(
        text(
            "SELECT COALESCE(MAX(id), 0) FROM context "
            "WHERE owner_scope IN ('assistant', 'team') AND owner_id IS NOT NULL",
        ),
    ).scalar()
    # Per-owner key expression, shared by all three passes (no user input).
    ok_expr = (
        "CASE c.owner_scope WHEN 'assistant' THEN 'a' || c.owner_id "
        "WHEN 'team' THEN 't' || c.owner_id END"
    )
    logger.info(
        "reclassify_heavy_owner_keys: max owner context id=%s, batch=%s",
        max_ctx,
        batch,
    )
    lo = 0
    while lo < max_ctx:
        hi = lo + batch
        params = {"lo": lo, "hi": hi}
        logger.info("reclassify_heavy_owner_keys: contexts (%s, %s]", lo, hi)
        # 1) log_event: 'sys' logs referenced by these owners' contexts.
        conn.execute(
            text(
                f"""
                UPDATE log_event le SET owner_key = sub.ok
                FROM (
                    SELECT lec.log_event_id AS leid, lec.project_id AS pid,
                           {ok_expr} AS ok
                    FROM context c
                    JOIN log_event_context lec
                      ON lec.context_id = c.id AND lec.project_id = c.project_id
                    WHERE c.owner_scope IN ('assistant', 'team')
                      AND c.owner_id IS NOT NULL
                      AND c.id > :lo AND c.id <= :hi
                ) sub
                WHERE le.id = sub.leid AND le.project_id = sub.pid
                  AND le.owner_key = 'sys'
                """,
            ),
            params,
        )
        # 2) log_event_context: 'sys' association rows in these contexts.
        conn.execute(
            text(
                f"""
                UPDATE log_event_context lec SET owner_key = sub.ok
                FROM (
                    SELECT c.id AS cid, c.project_id AS pid, {ok_expr} AS ok
                    FROM context c
                    WHERE c.owner_scope IN ('assistant', 'team')
                      AND c.owner_id IS NOT NULL
                      AND c.id > :lo AND c.id <= :hi
                ) sub
                WHERE lec.context_id = sub.cid AND lec.project_id = sub.pid
                  AND lec.owner_key = 'sys'
                """,
            ),
            params,
        )
        # 3) embedding: 'sys' vectors whose referenced log is in these contexts.
        conn.execute(
            text(
                f"""
                UPDATE embedding e SET owner_key = sub.ok
                FROM (
                    SELECT lec.log_event_id AS leid, lec.project_id AS pid,
                           {ok_expr} AS ok
                    FROM context c
                    JOIN log_event_context lec
                      ON lec.context_id = c.id AND lec.project_id = c.project_id
                    WHERE c.owner_scope IN ('assistant', 'team')
                      AND c.owner_id IS NOT NULL
                      AND c.id > :lo AND c.id <= :hi
                ) sub
                WHERE e.ref_id = sub.leid AND e.project_id = sub.pid
                  AND e.owner_key = 'sys'
                """,
            ),
            params,
        )
        lo = hi


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
