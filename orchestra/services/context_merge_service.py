"""Merge one context tree into another within the same project.

Root-agnostic: a "tree" is any context-name prefix, so the same machinery
merges a personal assistant tree into a team tree (``{user}/{agent}`` →
``Teams/{team_id}``), one team into another (``Teams/5`` → ``Teams/9``), or
one assistant into another (``{user}/{a1}`` → ``{user}/{a2}``). Callers own
the surrounding semantics (assistant rows, contact-membership overlays,
API error vocabulary); this module owns the context/log mechanics.

Logs are link-moved — the ``log_event`` rows keep their ids and only the
``log_event_context`` association is re-pointed — after the source side's
root auto-counted key values are offset above the target side's, so
unique/composite keys never collide. Preserving log ids keeps external
references such as the task machine state's ``source_task_log_id`` valid.

The mechanical remap is complemented by identity policies for tables whose
rows are resolved by a semantic key at runtime, where blind concatenation
would create ambiguity or duplicated behaviour:

* ``Secrets`` — looked up by ``name``: identical values dedupe, divergent
  values refuse the merge.
* ``Functions/Compositional`` / ``Functions/VirtualEnvs`` — resolved by
  ``name`` (``limit=1``) and referenced by name in ``depends_on``:
  identical definitions dedupe (references follow the surviving row's id),
  divergent definitions refuse the merge.
* ``Functions/Primitives`` — content-hashed ids: id collisions are
  identical primitives and dedupe losslessly.
* ``*/Meta`` — every manager's sync-state singleton (fixed ``meta_id=1``
  row holding a re-derivable content hash): the target's row wins and the
  runtime re-syncs.
* ``Tasks`` — fire-able logical tasks whose definition exactly matches a
  target task are dropped so the merged tree does not double-execute them.
* ``Contacts`` — rows are resolved by ``contact_id`` so duplicates are
  harmless redundancy; suspected same-person rows (exact email/phone match)
  are reported for operator review, never auto-merged.

Everything else is mechanical concatenation: rows describing the same
real-world entity on both sides may end up as two rows. Semantic unique
keys that survive the remap untouched (string keys on data tables,
field-level ``unique=True`` fields) are guarded: overlapping values refuse
the merge rather than corrupting the target's uniqueness invariant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import insert, or_, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.embedding_dao import ACTIVE_QUEUE_STATUSES
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.unique_constraint_dao import (
    COMPOSITE_KEY_FIELD,
    UniqueConstraintDAO,
)
from orchestra.db.models.core_models import Context, FieldType, LogUniqueConstraint
from orchestra.db.scope import OwnerScope, owner_key
from orchestra.db.utils import FKPathParser, PathSegment

SECRETS_TABLE = "Secrets"
CONTACTS_TABLE = "Contacts"
TASKS_TABLE = "Tasks"
FUNCTIONS_PRIMITIVES_TABLE = "Functions/Primitives"
FUNCTIONS_COMPOSITIONAL_TABLE = "Functions/Compositional"
FUNCTIONS_VENVS_TABLE = "Functions/VirtualEnvs"

# Every manager ships a sync-state singleton at <Table>/Meta with this exact
# key shape: one fixed meta_id=1 row holding a re-derivable content hash.
META_SINGLETON_SUFFIX = "/Meta"
META_SINGLETON_KEYS = ["meta_id"]

# Orchestra-projected task machine state stores Tasks.task_id without a
# declared FK; a remap of the Tasks table must shift these contexts too.
# source_task_log_id needs no rewrite because log ids are preserved.
TASK_MACHINE_STATE_TABLES = (
    "Tasks/Executions",
    "Tasks/OutboundOperations",
)

# Contact identity fields used for the duplicate-person report.
_CONTACT_IDENTITY_FIELDS = ("email_address", "phone_number")

# Task statuses that can still produce execution.
_LIVE_TASK_STATUSES = {"scheduled", "triggerable", "active"}

_CONSTRAINT_INSERT_CHUNK = 5000


class ContextMergeError(Exception):
    """Raised when two context trees cannot be merged.

    ``code`` is one of: ``collision_both_have_data``, ``schema_mismatch``,
    ``secret_conflict``, ``function_conflict``, ``unique_key_conflict``,
    ``versioned_context``, ``collision_unresolved``, ``source_not_drained``.
    ``subject`` carries the offending context path, secret name, function
    name, or key values for ops/debugging.
    """

    def __init__(self, code: str, subject: Optional[str] = None):
        self.code = code
        self.subject = subject
        super().__init__(f"{code}: {subject}" if subject else code)


@dataclass
class ContextTreeMergeResult:
    """Outcome of one tree merge."""

    contexts_renamed: int = 0
    contexts_merged: int = 0
    # Rows in the merged target contact book that likely describe the same
    # person (exact email/phone match); reported for operator review, never
    # auto-merged. Entries: matched_on, existing_contact_id,
    # merged_contact_id (post-remap).
    duplicate_contacts: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class _ColumnRemap:
    """How one table column's values changed during a pair merge.

    ``value_map`` carries exact old→new rewrites (e.g. a deduplicated row's
    id mapping to the surviving target row's id) and wins over ``offset``,
    which shifts every other referencing value.
    """

    offset: int = 0
    value_map: Dict[int, int] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.offset or self.value_map)


@dataclass
class _PolicyInputs:
    """Pre-merge snapshots consumed by identity policies.

    Captured before any pair mutates data so policies never observe
    half-remapped state regardless of pair processing order.
    """

    # function_id -> name per side, for entrypoint identity comparison.
    source_function_names: Dict[int, str] = field(default_factory=dict)
    target_function_names: Dict[int, str] = field(default_factory=dict)


@dataclass
class _PairMerge:
    """One source→target context pair scheduled for merging."""

    source: Context
    target: Context
    table_path: str
    # Column -> remap applied to (and implied for references into) the
    # source rows.
    remaps: Dict[str, _ColumnRemap] = field(default_factory=dict)
    # Contact duplicate matches in pre-remap source ids:
    # (source_contact_id, target_contact_id, matched_on).
    contact_matches: List[Tuple[int, int, str]] = field(default_factory=list)

    def remap_for(self, column: str) -> _ColumnRemap:
        return self.remaps.setdefault(column, _ColumnRemap())


def _target_name(source_name: str, *, source_prefix: str, target_prefix: str) -> str:
    if source_name == source_prefix:
        return target_prefix
    return target_prefix + source_name[len(source_prefix) :]


_CONTEXT_LOG_PAGE = 2000


def _iter_context_log_rows(
    session: Session,
    project_id: int,
    context_id: int,
    *,
    page_size: int = _CONTEXT_LOG_PAGE,
):
    """Keyset-iterate ``(id, data)`` for a context (never one giant fetch)."""
    last_id = 0
    while True:
        rows = session.execute(
            text(
                """
                SELECT le.id, le.data
                FROM log_event le
                JOIN log_event_context lec
                  ON lec.log_event_id = le.id
                 AND lec.project_id = le.project_id
                WHERE le.project_id = :project_id
                  AND lec.context_id = :context_id
                  AND le.id > :last_id
                ORDER BY le.id
                LIMIT :limit
                """,
            ),
            {
                "project_id": project_id,
                "context_id": context_id,
                "last_id": last_id,
                "limit": page_size,
            },
        ).fetchall()
        if not rows:
            return
        for row in rows:
            yield int(row[0]), row[1]
        last_id = int(rows[-1][0])


def _context_log_rows(
    session: Session,
    project_id: int,
    context_id: int,
) -> List[Tuple[int, Dict[str, Any]]]:
    return list(_iter_context_log_rows(session, project_id, context_id))


def _log_rows_by_ids(
    session: Session,
    project_id: int,
    log_ids: List[int],
) -> List[Tuple[int, Dict[str, Any]]]:
    rows = session.execute(
        text(
            """
            SELECT id, data
            FROM log_event
            WHERE project_id = :project_id
              AND id = ANY(:log_ids)
            """,
        ),
        {"project_id": project_id, "log_ids": log_ids},
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _drop_source_logs(
    session: Session,
    project_id: int,
    source_context_id: int,
    log_ids: List[int],
) -> None:
    """Delete source rows that must not survive the merge (policy drops).

    Inlines the log/constraint/embedding cleanup instead of reusing
    ``delete_orphaned_log_events`` because the embedding helpers it calls
    commit internally, which would break the caller's single transaction.
    Policy drops are a handful of rows, so unbatched statements are fine.
    """
    if not log_ids:
        return
    params = {"project_id": project_id, "log_ids": log_ids}
    session.execute(
        text(
            """
            DELETE FROM log_event_context
            WHERE project_id = :project_id
              AND context_id = :context_id
              AND log_event_id = ANY(:log_ids)
            """,
        ),
        {**params, "context_id": source_context_id},
    )
    session.execute(
        text(
            "DELETE FROM log_unique_constraint "
            "WHERE log_event_id = ANY(:log_ids) AND project_id = :project_id",
        ),
        params,
    )
    session.execute(
        text(
            f"""
            UPDATE embedding_queue
            SET status = 'cancelled',
                error_message = 'Merged away during context tree merge'
            WHERE project_id = :project_id
              AND ref_id = ANY(:log_ids)
              AND status IN {ACTIVE_QUEUE_STATUSES}
            """,
        ),
        params,
    )
    # The embedding -> log_event FK was dropped for partitioning, so the log
    # deletion below no longer touches these rows; detach them explicitly.
    session.execute(
        text(
            """
            UPDATE embedding
            SET is_deleted = true,
                ref_id = NULL
            WHERE project_id = :project_id
              AND ref_id = ANY(:log_ids)
            """,
        ),
        params,
    )
    session.execute(
        text(
            """
            DELETE FROM log_event
            WHERE project_id = :project_id
              AND id = ANY(:log_ids)
            """,
        ),
        params,
    )


def _check_schema_compatible(
    session: Session,
    source: Context,
    target: Context,
) -> None:
    if source.is_versioned or target.is_versioned:
        raise ContextMergeError("versioned_context", target.name)
    if (
        (source.unique_key_names or []) != (target.unique_key_names or [])
        or (source.unique_key_types or []) != (target.unique_key_types or [])
        or (source.auto_counting or {}) != (target.auto_counting or {})
    ):
        raise ContextMergeError("schema_mismatch", target.name)

    field_rows = (
        session.query(FieldType)
        .filter(FieldType.context_id.in_([source.id, target.id]))
        .all()
    )
    by_context: Dict[int, Dict[str, FieldType]] = {source.id: {}, target.id: {}}
    for row in field_rows:
        by_context[row.context_id][row.field_name] = row
    source_fields = by_context[source.id]
    target_fields = by_context[target.id]
    for name in source_fields.keys() & target_fields.keys():
        source_field = source_fields[name]
        target_field = target_fields[name]
        if (
            source_field.field_type != target_field.field_type
            or source_field.field_category != target_field.field_category
        ):
            raise ContextMergeError("schema_mismatch", target.name)


def _column_offset(
    session: Session,
    project_id: int,
    target_context_id: int,
    column: str,
) -> int:
    """Offset that lifts source values strictly above every target value."""
    max_val = session.execute(
        text(
            """
            SELECT MAX((le.data ->> :column)::bigint)
            FROM log_event le
            JOIN log_event_context lec
              ON lec.log_event_id = le.id
             AND lec.project_id = le.project_id
            WHERE le.project_id = :project_id
              AND lec.context_id = :context_id
              AND (le.data ->> :column) ~ '^-?[0-9]+$'
            """,
        ),
        {
            "project_id": project_id,
            "context_id": target_context_id,
            "column": column,
        },
    ).scalar()
    counter_next = session.execute(
        text(
            """
            SELECT MAX(next_value)
            FROM context_counter
            WHERE context_id = :context_id
              AND column_name = :column
            """,
        ),
        {"context_id": target_context_id, "column": column},
    ).scalar()
    return max(
        (max_val + 1) if max_val is not None else 0,
        counter_next or 0,
    )


# --------------------------------------------------------------------------
# Identity policies
# --------------------------------------------------------------------------


def _is_meta_singleton(table_path: str, target: Context) -> bool:
    return table_path.endswith(META_SINGLETON_SUFFIX) and (
        list(target.unique_key_names or []) == META_SINGLETON_KEYS
    )


def _apply_meta_singleton_policy(
    session: Session,
    project_id: int,
    pair: _PairMerge,
) -> None:
    """Singleton sync-state row: the target's copy wins and the runtime
    re-derives the source side's hashes on the next sync. Without this, the
    fixed ``meta_id=1`` rows on both sides would collide (no auto-counted
    column exists to remap)."""
    dropped = [
        log_id for log_id, _ in _context_log_rows(session, project_id, pair.source.id)
    ]
    _drop_source_logs(session, project_id, pair.source.id, dropped)


def _apply_primitives_policy(
    session: Session,
    project_id: int,
    pair: _PairMerge,
) -> None:
    """function_id is a stable content hash: an id collision means an
    identical primitive, so the source duplicate is dropped. References keep
    working because the surviving target row carries the same id."""
    target_ids = {
        data.get("function_id")
        for _, data in _context_log_rows(session, project_id, pair.target.id)
    }
    dropped = [
        log_id
        for log_id, data in _context_log_rows(session, project_id, pair.source.id)
        if data.get("function_id") is not None and data.get("function_id") in target_ids
    ]
    _drop_source_logs(session, project_id, pair.source.id, dropped)


def _apply_secrets_policy(
    session: Session,
    project_id: int,
    pair: _PairMerge,
) -> None:
    """Runtime lookups key secrets by name; two values under one name would
    be ambiguous, so identical values dedupe and different values refuse the
    merge for operator review."""
    target_values = {
        data.get("name"): data.get("value")
        for _, data in _context_log_rows(session, project_id, pair.target.id)
    }
    dropped = []
    for log_id, data in _context_log_rows(session, project_id, pair.source.id):
        name = data.get("name")
        if name not in target_values:
            continue
        if data.get("value") == target_values[name]:
            dropped.append(log_id)
        else:
            raise ContextMergeError("secret_conflict", name)
    _drop_source_logs(session, project_id, pair.source.id, dropped)


def _apply_named_function_policy(
    session: Session,
    project_id: int,
    pair: _PairMerge,
) -> None:
    """Compositional functions and venvs are resolved by ``name`` at runtime
    (``limit=1`` lookups, name-based ``depends_on``), so a merged table must
    keep names unambiguous: identical definitions dedupe to the target row
    (references follow via the id value map), divergent definitions refuse
    the merge for operator review."""
    if pair.table_path == FUNCTIONS_COMPOSITIONAL_TABLE:
        id_column = "function_id"
        content_fields = ("implementation", "argspec", "language")
    else:
        id_column = "venv_id"
        content_fields = ("venv",)

    target_by_name: Dict[str, Dict[str, Any]] = {}
    for _, data in _context_log_rows(session, project_id, pair.target.id):
        name = data.get("name")
        if name:
            target_by_name[name] = data

    dropped = []
    value_map: Dict[int, int] = {}
    for log_id, data in _context_log_rows(session, project_id, pair.source.id):
        name = data.get("name")
        if not name or name not in target_by_name:
            continue
        target_data = target_by_name[name]
        if any(data.get(f) != target_data.get(f) for f in content_fields):
            raise ContextMergeError("function_conflict", name)
        dropped.append(log_id)
        source_id = data.get(id_column)
        target_id = target_data.get(id_column)
        if (
            isinstance(source_id, int)
            and isinstance(target_id, int)
            and source_id != target_id
        ):
            value_map[source_id] = target_id
    _drop_source_logs(session, project_id, pair.source.id, dropped)
    pair.remap_for(id_column).value_map.update(value_map)


def _task_identity(data: Dict[str, Any], function_names: Dict[int, str]) -> str:
    """Canonical identity of a logical task for cross-tree deduplication.

    Entrypoints are compared by function *name* because function ids live in
    per-tree id spaces; an unresolvable id can never match anything.
    """
    entrypoint = data.get("entrypoint")
    if entrypoint is None:
        entrypoint_identity = None
    else:
        entrypoint_identity = function_names.get(entrypoint) or f"id:{entrypoint}"
    return json.dumps(
        {
            "name": data.get("name"),
            "description": data.get("description"),
            "schedule": data.get("schedule"),
            "repeat": data.get("repeat"),
            "trigger": data.get("trigger"),
            "entrypoint": entrypoint_identity,
        },
        sort_keys=True,
        default=str,
    )


def _logical_tasks(
    rows: List[Tuple[int, Dict[str, Any]]],
) -> Dict[int, List[Tuple[int, Dict[str, Any]]]]:
    grouped: Dict[int, List[Tuple[int, Dict[str, Any]]]] = {}
    for log_id, data in rows:
        task_id = data.get("task_id")
        if isinstance(task_id, int):
            grouped.setdefault(task_id, []).append((log_id, data))
    return grouped


def _latest_instance(rows: List[Tuple[int, Dict[str, Any]]]) -> Dict[str, Any]:
    # Pre-migration contexts may hold several physical rows per task_id; the
    # highest log id is the most recently written one. Post-migration rows are
    # one per task_id, so this reduces to the identity.
    return max(rows, key=lambda row: row[0])[1]


def _task_can_fire(rows: List[Tuple[int, Dict[str, Any]]]) -> bool:
    return any(
        data.get("repeat")
        or data.get("trigger")
        or data.get("status") in _LIVE_TASK_STATUSES
        for _, data in rows
    )


def _apply_task_dedup_policy(
    session: Session,
    project_id: int,
    pair: _PairMerge,
    policy_inputs: _PolicyInputs,
) -> None:
    """Drop source logical tasks that would double-execute after the merge.

    A source task is dropped (all instance rows, history included — the
    target side keeps its own history of the same logical job) when it can
    still fire and its definition exactly matches a target task's. Its
    ``task_id`` maps to the surviving target task so machine-state references
    follow.
    """
    source_tasks = _logical_tasks(
        _context_log_rows(session, project_id, pair.source.id),
    )
    target_tasks = _logical_tasks(
        _context_log_rows(session, project_id, pair.target.id),
    )
    target_by_identity = {
        _task_identity(
            _latest_instance(rows),
            policy_inputs.target_function_names,
        ): task_id
        for task_id, rows in target_tasks.items()
    }

    dropped = []
    value_map: Dict[int, int] = {}
    for task_id, rows in source_tasks.items():
        if not _task_can_fire(rows):
            continue
        identity = _task_identity(
            _latest_instance(rows),
            policy_inputs.source_function_names,
        )
        target_task_id = target_by_identity.get(identity)
        if target_task_id is None:
            continue
        dropped.extend(log_id for log_id, _ in rows)
        if task_id != target_task_id:
            value_map[task_id] = target_task_id
    _drop_source_logs(session, project_id, pair.source.id, dropped)
    pair.remap_for("task_id").value_map.update(value_map)


def _contact_identity_index(
    rows: List[Tuple[int, Dict[str, Any]]],
) -> Dict[Tuple[str, str], int]:
    index: Dict[Tuple[str, str], int] = {}
    for _, data in rows:
        contact_id = data.get("contact_id")
        if not isinstance(contact_id, int):
            continue
        for field_name in _CONTACT_IDENTITY_FIELDS:
            raw = data.get(field_name)
            if isinstance(raw, str) and raw.strip():
                index.setdefault((field_name, raw.strip().lower()), contact_id)
    return index


def _find_contact_matches(
    session: Session,
    project_id: int,
    pair: _PairMerge,
) -> List[Tuple[int, int, str]]:
    """Suspected same-person rows across the two contact books.

    Contacts are resolved by ``contact_id`` at runtime, so duplicates are
    redundancy rather than ambiguity; matches are reported, never merged
    (auto-merging divergent fields would be lossy — operators can follow up
    with the runtime's merge_contacts).
    """
    target_index = _contact_identity_index(
        _context_log_rows(session, project_id, pair.target.id),
    )
    matches: List[Tuple[int, int, str]] = []
    for _, data in _context_log_rows(session, project_id, pair.source.id):
        contact_id = data.get("contact_id")
        if not isinstance(contact_id, int):
            continue
        for field_name in _CONTACT_IDENTITY_FIELDS:
            raw = data.get(field_name)
            if not (isinstance(raw, str) and raw.strip()):
                continue
            target_contact_id = target_index.get((field_name, raw.strip().lower()))
            if target_contact_id is not None:
                matches.append((contact_id, target_contact_id, field_name))
                break
    return matches


def _unique_value_subjects(
    session: Session,
    project_id: int,
    context: Context,
    *,
    key_columns: List[str],
    unique_fields: set[str],
) -> Dict[Tuple[str, str], str]:
    """Hash every uniqueness-bearing value of a context's rows.

    Returns ``(field_name, value_hash) -> human-readable subject`` in the
    same shape :func:`_rebuild_unique_constraints` writes, so comparing two
    contexts' maps predicts exactly which inserts would collide.
    """
    subjects: Dict[Tuple[str, str], str] = {}
    for _, data in _context_log_rows(session, project_id, context.id):
        for field_name in unique_fields:
            value = data.get(field_name)
            if value is None:
                continue
            subjects[(field_name, UniqueConstraintDAO.hash_value(value))] = (
                f"{field_name}={value!r}"
            )
        if key_columns:
            key_values = {column: data.get(column) for column in key_columns}
            if None not in key_values.values():
                subjects[
                    (
                        COMPOSITE_KEY_FIELD,
                        UniqueConstraintDAO.hash_composite(key_values, key_columns),
                    )
                ] = str(key_values)
    return subjects


def _check_unique_key_conflicts(
    session: Session,
    project_id: int,
    pair: _PairMerge,
) -> None:
    """Refuse the merge when moved rows would violate the target's unique keys.

    Runs after policies and key remapping, so auto-counted key components
    have already been lifted above the target's range — only *semantic* keys
    (string keys on data tables, field-level ``unique=True`` fields) can
    still collide. Deduping here would require knowing the table's identity
    semantics, so overlaps refuse cleanly and leave resolution to the
    operator or the agent.
    """
    key_columns = list(pair.target.unique_key_names or [])
    unique_fields = {
        row.field_name
        for row in session.query(FieldType).filter(
            FieldType.context_id.in_([pair.source.id, pair.target.id]),
            FieldType.unique.is_(True),
        )
    }
    if not key_columns and not unique_fields:
        return

    target_subjects = _unique_value_subjects(
        session,
        project_id,
        pair.target,
        key_columns=key_columns,
        unique_fields=unique_fields,
    )
    source_subjects = _unique_value_subjects(
        session,
        project_id,
        pair.source,
        key_columns=key_columns,
        unique_fields=unique_fields,
    )
    overlap = sorted(target_subjects.keys() & source_subjects.keys())
    if overlap:
        raise ContextMergeError(
            "unique_key_conflict",
            f"{pair.table_path}: {source_subjects[overlap[0]]}",
        )


def _prepare_pair_merge(
    session: Session,
    *,
    project_id: int,
    source: Context,
    target: Context,
    table_path: str,
    policy_inputs: _PolicyInputs,
) -> _PairMerge:
    """Validate a pair, apply identity policies, and offset-remap source keys.

    Runs while the source logs still live in the source context; the actual
    association move happens in :func:`_finalize_pair_merge` after key
    remaps have been propagated across the whole source subtree.
    """
    _check_schema_compatible(session, source, target)
    pair = _PairMerge(source=source, target=target, table_path=table_path)

    if _is_meta_singleton(table_path, target):
        _apply_meta_singleton_policy(session, project_id, pair)
        return pair
    if table_path == FUNCTIONS_PRIMITIVES_TABLE:
        _apply_primitives_policy(session, project_id, pair)
        return pair
    if table_path in (FUNCTIONS_COMPOSITIONAL_TABLE, FUNCTIONS_VENVS_TABLE):
        _apply_named_function_policy(session, project_id, pair)
    elif table_path == SECRETS_TABLE:
        _apply_secrets_policy(session, project_id, pair)
    elif table_path == TASKS_TABLE:
        _apply_task_dedup_policy(session, project_id, pair, policy_inputs)
    elif table_path == CONTACTS_TABLE:
        pair.contact_matches = _find_contact_matches(session, project_id, pair)

    context_dao = ContextDAO(session)
    auto_counting = target.auto_counting or {}
    root_columns = [col for col, parent in auto_counting.items() if parent is None]
    for column in root_columns:
        offset = _column_offset(session, project_id, target.id, column)
        if offset <= 0:
            continue
        shifted = context_dao.shift_numeric_field(
            project_id,
            source.id,
            column,
            offset,
        )
        if shifted:
            pair.remap_for(column).offset = offset

    _check_unique_key_conflicts(session, project_id, pair)
    return pair


# --------------------------------------------------------------------------
# Reference remapping
# --------------------------------------------------------------------------


def _remapped_value(value: Any, remap: _ColumnRemap) -> Optional[int]:
    """New value for a referencing field, or None when unchanged."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value in remap.value_map:
        return remap.value_map[value]
    if remap.offset:
        return value + remap.offset
    return None


def _remap_nested_value(
    node: Any,
    segments: List[PathSegment],
    remap: _ColumnRemap,
) -> bool:
    segment = segments[0]
    rest = segments[1:]
    if not isinstance(node, dict) or segment.name not in node:
        return False
    value = node[segment.name]
    changed = False
    if segment.is_array:
        if not isinstance(value, list):
            return False
        if segment.is_wildcard:
            indices = range(len(value))
        elif segment.array_index is not None and segment.array_index < len(value):
            indices = [segment.array_index]
        else:
            indices = []
        for index in indices:
            if rest:
                changed = _remap_nested_value(value[index], rest, remap) or changed
            else:
                new_value = _remapped_value(value[index], remap)
                if new_value is not None:
                    value[index] = new_value
                    changed = True
    elif rest:
        changed = _remap_nested_value(value, rest, remap)
    else:
        new_value = _remapped_value(value, remap)
        if new_value is not None:
            node[segment.name] = new_value
            changed = True
    return changed


def _apply_remap_to_field(
    session: Session,
    context_dao: ContextDAO,
    project_id: int,
    context_id: int,
    field_path: str,
    remap: _ColumnRemap,
) -> None:
    segments = FKPathParser.parse(field_path)
    if not remap.value_map and len(segments) == 1 and not segments[0].is_array:
        context_dao.shift_numeric_field(
            project_id,
            context_id,
            segments[0].name,
            remap.offset,
        )
        return

    pending: List[Tuple[int, str]] = []
    for log_id, data in _iter_context_log_rows(session, project_id, context_id):
        if _remap_nested_value(data, segments, remap):
            pending.append((log_id, json.dumps(data)))
        if len(pending) >= 500:
            _flush_remap_updates(session, project_id, pending)
            pending.clear()
    if pending:
        _flush_remap_updates(session, project_id, pending)


def _flush_remap_updates(
    session: Session,
    project_id: int,
    pending: List[Tuple[int, str]],
) -> None:
    """Apply a batch of remapped JSONB payloads via unnest."""
    if not pending:
        return
    ids = [p[0] for p in pending]
    payloads = [p[1] for p in pending]
    session.execute(
        text(
            """
            UPDATE log_event le
            SET data = CAST(v.payload AS jsonb)
            FROM unnest(
                CAST(:ids AS bigint[]),
                CAST(:payloads AS text[])
            ) AS v(id, payload)
            WHERE le.project_id = :project_id
              AND le.id = v.id
            """,
        ),
        {"project_id": project_id, "ids": ids, "payloads": payloads},
    )


def _propagate_key_remaps(
    session: Session,
    context_dao: ContextDAO,
    *,
    project_id: int,
    source_prefix: str,
    remaps_by_table: Dict[Tuple[str, str], _ColumnRemap],
) -> None:
    """Rewrite every field in the source subtree that references a remapped key.

    Declared foreign keys carry root-prefixed references
    (``{source_prefix}/Contacts.contact_id``), so this walk can never touch
    target-side rows. Must run before the merged pairs' associations move.
    """
    if not remaps_by_table:
        return

    source_ref_prefix = f"{source_prefix}/"
    for context in context_dao.list_context_subtree(project_id, source_prefix):
        for fk in context.foreign_keys or []:
            reference = fk.get("references", "")
            parts = reference.rsplit(".", 1)
            if len(parts) != 2:
                continue
            ref_context_name, ref_column = parts
            if not ref_context_name.startswith(source_ref_prefix):
                continue
            table_path = ref_context_name[len(source_ref_prefix) :]
            remap = remaps_by_table.get((table_path, ref_column))
            if not remap:
                continue
            _apply_remap_to_field(
                session,
                context_dao,
                project_id,
                context.id,
                fk["name"],
                remap,
            )

    task_remap = remaps_by_table.get((TASKS_TABLE, "task_id"))
    if task_remap:
        for leaf in TASK_MACHINE_STATE_TABLES:
            rows = context_dao.filter(
                project_id=project_id,
                name=f"{source_prefix}/{leaf}",
            )
            if rows:
                _apply_remap_to_field(
                    session,
                    context_dao,
                    project_id,
                    rows[0][0].id,
                    "task_id",
                    task_remap,
                )


def _rebuild_unique_constraints(
    session: Session,
    project_id: int,
    target: Context,
    moved_log_ids: List[int],
) -> None:
    """Re-key the moved logs' uniqueness rows against the target context.

    Uses plain inserts: a conflict here means the remap failed to separate
    the two sides, and the transaction must abort rather than silently keep
    colliding keys.
    """
    unique_fields = {
        row.field_name
        for row in session.query(FieldType).filter(
            FieldType.context_id == target.id,
            FieldType.unique.is_(True),
        )
    }
    key_columns = list(target.unique_key_names or [])
    if not unique_fields and not key_columns:
        return

    values = []
    for log_id, data in _log_rows_by_ids(session, project_id, moved_log_ids):
        for field_name in unique_fields:
            value = data.get(field_name)
            if value is None:
                continue
            values.append(
                {
                    "context_id": target.id,
                    "project_id": project_id,
                    "field_name": field_name,
                    "value_hash": UniqueConstraintDAO.hash_value(value),
                    "log_event_id": log_id,
                },
            )
        if key_columns:
            key_values = {column: data.get(column) for column in key_columns}
            if None not in key_values.values():
                values.append(
                    {
                        "context_id": target.id,
                        "project_id": project_id,
                        "field_name": COMPOSITE_KEY_FIELD,
                        "value_hash": UniqueConstraintDAO.hash_composite(
                            key_values,
                            key_columns,
                        ),
                        "log_event_id": log_id,
                    },
                )

    for start in range(0, len(values), _CONSTRAINT_INSERT_CHUNK):
        session.execute(
            insert(LogUniqueConstraint).values(
                values[start : start + _CONSTRAINT_INSERT_CHUNK],
            ),
        )


def _finalize_pair_merge(
    session: Session,
    context_dao: ContextDAO,
    *,
    project_id: int,
    pair: _PairMerge,
) -> None:
    """Move the (already remapped) source logs into the target context."""
    moved_log_ids = context_dao.move_log_associations(
        project_id,
        pair.source.id,
        pair.target.id,
    )
    if moved_log_ids:
        # Stale rows point at the source context and pre-remap hashes.
        session.execute(
            text(
                """
                DELETE FROM log_unique_constraint
                WHERE log_event_id = ANY(:log_ids)
                  AND project_id = :project_id
                """,
            ),
            {"log_ids": moved_log_ids, "project_id": project_id},
        )
        # Union the field metadata first so the constraint rebuild sees
        # source-only unique fields (conflicts were vetted in the
        # compatibility check; the target's rows win).
        FieldTypeDAO(session).copy_field_types(
            pair.source.id,
            pair.target.id,
            project_id,
        )
        _rebuild_unique_constraints(session, project_id, pair.target, moved_log_ids)
        LogEventDAO(session, context_dao).resync_context_counters(
            context_id=pair.target.id,
            project_id=project_id,
        )
    context_dao.move_derived_templates(
        project_id,
        pair.source.id,
        pair.target.id,
    )
    session.flush()


def _function_name_snapshot(
    session: Session,
    context_dao: ContextDAO,
    project_id: int,
    prefix: str,
) -> Dict[int, str]:
    rows = context_dao.filter(
        project_id=project_id,
        name=f"{prefix}/{FUNCTIONS_COMPOSITIONAL_TABLE}",
    )
    if not rows:
        return {}
    return {
        data["function_id"]: data.get("name")
        for _, data in _context_log_rows(session, project_id, rows[0][0].id)
        if isinstance(data.get("function_id"), int)
    }


def _duplicate_contact_entries(pair: _PairMerge) -> List[Dict[str, Any]]:
    """Contact matches expressed in post-remap merged ids."""
    remap = pair.remaps.get("contact_id", _ColumnRemap())
    entries = []
    for source_contact_id, target_contact_id, matched_on in pair.contact_matches:
        merged_contact_id = _remapped_value(source_contact_id, remap)
        entries.append(
            {
                "matched_on": matched_on,
                "existing_contact_id": target_contact_id,
                "merged_contact_id": (
                    merged_contact_id
                    if merged_contact_id is not None
                    else source_contact_id
                ),
            },
        )
    return entries


def _reconcile_tree_collisions(
    session: Session,
    context_dao: ContextDAO,
    *,
    project_id: int,
    source_prefix: str,
    target_prefix: str,
    merge_populated: bool,
    result: ContextTreeMergeResult,
) -> None:
    """Resolve name collisions between the source and target trees.

    Each colliding context pair is handled individually: empty shells are
    deleted so the rename can slot the other side into place, and pairs where
    both sides hold logs are merged (with ``merge_populated``) or refused.
    """
    source_contexts = context_dao.list_context_subtree(project_id, source_prefix)
    target_by_name = {
        context.name: context
        for context in context_dao.list_context_subtree(project_id, target_prefix)
    }

    # Snapshot policy inputs before any pair mutates data so identity
    # comparisons never observe half-remapped state.
    policy_inputs = _PolicyInputs(
        source_function_names=_function_name_snapshot(
            session,
            context_dao,
            project_id,
            source_prefix,
        ),
        target_function_names=_function_name_snapshot(
            session,
            context_dao,
            project_id,
            target_prefix,
        ),
    )

    pairs: list[_PairMerge] = []
    remaps_by_table: dict[tuple[str, str], _ColumnRemap] = {}
    for source_context in sorted(
        source_contexts,
        key=lambda context: context.name.count("/"),
        reverse=True,
    ):
        target_name = _target_name(
            source_context.name,
            source_prefix=source_prefix,
            target_prefix=target_prefix,
        )
        target_context = target_by_name.get(target_name)
        if target_context is None:
            continue

        source_has = context_dao.context_has_logs(project_id, source_context.id)
        target_has = context_dao.context_has_logs(project_id, target_context.id)

        if source_has and target_has:
            if not merge_populated:
                raise ContextMergeError("collision_both_have_data", target_name)
            table_path = target_name[len(target_prefix) + 1 :]
            pair = _prepare_pair_merge(
                session,
                project_id=project_id,
                source=source_context,
                target=target_context,
                table_path=table_path,
                policy_inputs=policy_inputs,
            )
            pairs.append(pair)
            for column, remap in pair.remaps.items():
                if remap:
                    remaps_by_table[(table_path, column)] = remap
        elif source_has:
            # Empty target shell: remove the row so the rename takes its name.
            session.delete(target_context)
        else:
            # Empty source shell: the target context keeps the name.
            session.delete(source_context)
    session.flush()

    if pairs:
        # Key remaps must reach every referencing row in the source tree
        # (merged or not) while the logs still live in source contexts.
        _propagate_key_remaps(
            session,
            context_dao,
            project_id=project_id,
            source_prefix=source_prefix,
            remaps_by_table=remaps_by_table,
        )
        for pair in pairs:
            _finalize_pair_merge(
                session,
                context_dao,
                project_id=project_id,
                pair=pair,
            )
            session.delete(pair.source)
            result.duplicate_contacts.extend(_duplicate_contact_entries(pair))
        session.flush()
    result.contexts_merged = len(pairs)


def _rewrite_foreign_key_prefixes(
    session: Session,
    context_dao: ContextDAO,
    *,
    project_id: int,
    source_prefix: str,
    target_prefix: str,
) -> None:
    """Re-root FK reference paths after the rename.

    Contexts created under the source root carry references like
    ``{source_prefix}/Contacts.contact_id``; once renamed under the target
    root those paths no longer resolve, so FK validation on future inserts
    would fail. Contexts that already lived in the target tree are untouched.
    """
    source_ref_prefix = f"{source_prefix}/"
    for context in context_dao.list_context_subtree(project_id, target_prefix):
        if not context.foreign_keys:
            continue
        changed = False
        rewritten = []
        for fk in context.foreign_keys:
            reference = fk.get("references", "")
            if reference.startswith(source_ref_prefix):
                fk = {
                    **fk,
                    "references": (
                        f"{target_prefix}/{reference[len(source_ref_prefix):]}"
                    ),
                }
                changed = True
            rewritten.append(fk)
        if changed:
            context.foreign_keys = rewritten
            session.add(context)
    session.flush()


def _verify_source_tree_drained(
    context_dao: ContextDAO,
    *,
    project_id: int,
    source_prefix: str,
) -> None:
    """Ensure nothing with logs survived under the source prefix."""
    remaining = context_dao.list_context_subtree(project_id, source_prefix)
    for context in sorted(
        remaining,
        key=lambda row: row.name.count("/"),
        reverse=True,
    ):
        if context_dao.subtree_has_logs(project_id, context.name):
            raise ContextMergeError("source_not_drained", context.name)
        context_dao.delete_context_subtree_if_empty(project_id, context.name)


def merge_context_trees(
    session: Session,
    context_dao: ContextDAO,
    *,
    project_id: int,
    source_prefix: str,
    target_prefix: str,
    merge_populated: bool = False,
) -> ContextTreeMergeResult:
    """Fold the ``source_prefix`` context tree into the ``target_prefix`` tree.

    Colliding empty shells are deleted, colliding populated tables are merged
    (only with ``merge_populated``; refused otherwise), and everything else is
    renamed into place. FK reference paths are re-rooted afterwards, and the
    source tree is verified empty. Runs entirely in the caller's transaction.

    Ownership columns are not touched; callers that change the owning entity
    (e.g. personal assistant → team) follow up with
    :func:`update_tree_ownership`.
    """
    result = ContextTreeMergeResult()
    _reconcile_tree_collisions(
        session,
        context_dao,
        project_id=project_id,
        source_prefix=source_prefix,
        target_prefix=target_prefix,
        merge_populated=merge_populated,
        result=result,
    )

    try:
        result.contexts_renamed = context_dao.rename_with_children(
            project_id,
            source_prefix,
            target_prefix,
            commit=False,
        )
    except IntegrityError as exc:
        raise ContextMergeError("collision_unresolved") from exc

    _verify_source_tree_drained(
        context_dao,
        project_id=project_id,
        source_prefix=source_prefix,
    )

    _rewrite_foreign_key_prefixes(
        session,
        context_dao,
        project_id=project_id,
        source_prefix=source_prefix,
        target_prefix=target_prefix,
    )

    return result


def update_tree_ownership(
    session: Session,
    *,
    project_id: int,
    prefix: str,
    owner_scope: OwnerScope,
    owner_id: int,
    previous_owner_key: str,
) -> None:
    """Rebrand a context tree and its data to a new owning entity.

    Sets the owner columns on every context under ``prefix`` and moves the
    denormalized ``owner_key`` on ``log_event``, ``log_event_context``, and
    ``embedding`` rows from ``previous_owner_key`` to the new owner's key —
    the owner sub-partitions that make per-owner bulk deletion O(1).
    """
    session.execute(
        update(Context)
        .where(
            Context.project_id == project_id,
            or_(
                Context.name == prefix,
                Context.name.like(f"{prefix}/%"),
            ),
        )
        .values(
            owner_scope=owner_scope.value,
            owner_id=owner_id,
        ),
    )
    session.flush()

    params = {
        "project_id": project_id,
        "prefix": prefix,
        "prefix_like": f"{prefix}/%",
        "new_owner_key": owner_key(owner_scope, owner_id),
        "old_owner_key": previous_owner_key,
    }
    session.execute(
        text(
            """
            UPDATE log_event le
            SET owner_key = :new_owner_key
            FROM log_event_context lec
            JOIN context c ON c.id = lec.context_id
            WHERE le.id = lec.log_event_id
              AND le.project_id = :project_id
              AND lec.project_id = :project_id
              AND c.project_id = :project_id
              AND (
                    c.name = :prefix
                    OR c.name LIKE :prefix_like
              )
              AND le.owner_key = :old_owner_key
            """,
        ),
        params,
    )
    session.execute(
        text(
            """
            UPDATE log_event_context lec
            SET owner_key = :new_owner_key
            FROM context c
            WHERE c.id = lec.context_id
              AND lec.project_id = :project_id
              AND c.project_id = :project_id
              AND (
                    c.name = :prefix
                    OR c.name LIKE :prefix_like
              )
              AND lec.owner_key = :old_owner_key
            """,
        ),
        params,
    )
    # Embeddings denormalize the same owner_key; without this they would stay
    # attributed to the previous owner's partition after the logs moved.
    session.execute(
        text(
            """
            UPDATE embedding e
            SET owner_key = :new_owner_key
            FROM log_event_context lec
            JOIN context c ON c.id = lec.context_id
            WHERE lec.log_event_id = e.ref_id
              AND lec.project_id = e.project_id
              AND e.project_id = :project_id
              AND c.project_id = :project_id
              AND (
                    c.name = :prefix
                    OR c.name LIKE :prefix_like
              )
              AND e.owner_key = :old_owner_key
            """,
        ),
        params,
    )
