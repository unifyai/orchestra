#!/usr/bin/env python3
"""Strip run state from ``Tasks`` definitions after the execution-ledger split.

Task lifecycle state used to live on the definition row, where every concurrent
run of the same task wrote it and the last writer won. It now lives on that
run's ``Tasks/Executions`` row, and the definition carries authored intent only.
Rows written before the split still carry the retired columns.

Reads already tolerate them — the scheduler drops the retired keys on the way
into ``Task`` — so this migration is about removing a *misleading* second copy
rather than unblocking anything. A definition still reading ``status=failed``
invites the next reader (a dashboard, a query, an agent) to conclude a healthy
recurring task is dead. That is the confusion the split exists to end.

What it removes:

* ``.../Tasks`` definitions — ``status``, ``activated_by``, ``completed_at``,
  ``info``.
* ``.../Tasks/Executions`` — ``status``, the mirror of ``state`` that the
  projection no longer writes.
* The ``field_type`` registrations for those columns on those contexts.

``Tasks/OutboundOperations`` is deliberately untouched: ``status`` is that
ledger's own live field, not a mirror.

Ordering: run this only once every reader is on the post-split runtime. Old
readers expect ``status`` on the definition, and this removes it. Nothing else
in the migration is order-sensitive — Orchestra registers new execution columns
itself, inside the same request that first writes one.

Reports, and changes nothing, unless given ``--execute``:

    uv run python scripts/strip_run_state_from_task_definitions.py
    uv run python scripts/strip_run_state_from_task_definitions.py --execute
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import ARRAY, Text, cast, create_engine, select
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.orchestra_models import (
    Context,
    FieldType,
    LogEvent,
    LogEventContext,
)
from orchestra.services.task_machine_state_service import (
    TASK_EXECUTIONS_CONTEXT_NAME,
    _replace_log_payload,
    is_task_surface_context_name,
)
from orchestra.settings import settings

LEGACY_DEFINITION_FIELDS = (
    "status",
    "activated_by",
    "completed_at",
    "info",
)
LEGACY_EXECUTION_FIELDS = ("status",)

# `instance_id` is deliberately absent. The scheduler still writes it on create
# while dropping it on every read, so stripping it here would look like it
# worked and then quietly un-migrate on the next task created. It is also
# harmless — no reader consults it, and Orchestra's projection uses it only as a
# sort tiebreak. Removing it from the Task model is the fix; this migration is
# not the place to fake one.

# A definition left carrying one of these read as dead under the old model.
# Under the new one it is armed again, which is a change worth naming.
TERMINAL_LEGACY_STATUSES = frozenset({"failed", "cancelled"})


def definition_contexts(session: Session) -> list[Context]:
    """Every user-authored ``Tasks`` table, across projects and tenants."""

    return [
        context
        for context in session.execute(
            select(Context).where(Context.name.like("%Tasks")),
        ).scalars()
        if is_task_surface_context_name(context.name)
    ]


def execution_contexts(session: Session) -> list[Context]:
    """Every ``Tasks/Executions`` run ledger."""

    return list(
        session.execute(
            select(Context).where(
                Context.name.like(f"%{TASK_EXECUTIONS_CONTEXT_NAME}"),
            ),
        )
        .scalars()
        .all(),
    )


def _rows_carrying(
    session: Session,
    context_ids: list[int],
    fields: tuple[str, ...],
) -> list[tuple[LogEvent, str]]:
    """Every row in ``context_ids`` that still carries a retired key.

    One round-trip for the whole sweep, and the ``?|`` containment test keeps
    untouched rows on the server rather than shipping them here to be skipped.
    """

    if not context_ids:
        return []
    rows = session.execute(
        select(LogEvent, Context.name)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .join(Context, Context.id == LogEventContext.context_id)
        .where(
            LogEventContext.context_id.in_(context_ids),
            LogEvent.data.op("?|")(cast(array(fields), ARRAY(Text))),
        ),
    ).unique()
    return [(row[0], row[1]) for row in rows]


def strip_payload_keys(
    session: Session,
    contexts: list[Context],
    fields: tuple[str, ...],
    *,
    execute: bool,
) -> tuple[int, list[str]]:
    """Drop retired keys from every row in ``contexts``.

    Returns rows changed, and definitions whose retired ``status`` said dead
    while their authored intent says armed.
    """

    context_ids = [int(context.id) for context in contexts]
    changed = 0
    revived: list[str] = []
    for row, context_name in _rows_carrying(session, context_ids, fields):
        data = dict(row.data or {})
        retired = [key for key in fields if key in data]
        if not retired:
            continue
        if (
            str(data.get("status") or "") in TERMINAL_LEGACY_STATUSES
            and data.get("enabled") is not False
        ):
            revived.append(
                f"{context_name} task_id={data.get('task_id')} "
                f"status={data.get('status')}",
            )
        for key in retired:
            data.pop(key)
        changed += 1
        if execute:
            _replace_log_payload(row, data)
    return changed, revived


def drop_field_registrations(
    session: Session,
    contexts: list[Context],
    fields: tuple[str, ...],
    *,
    execute: bool,
) -> int:
    """Unregister retired columns so they stop appearing as table schema."""

    context_ids = [int(context.id) for context in contexts]
    if not context_ids:
        return 0
    registrations = list(
        session.execute(
            select(FieldType).where(
                FieldType.context_id.in_(context_ids),
                FieldType.field_name.in_(fields),
            ),
        )
        .scalars()
        .unique(),
    )
    if execute:
        for registration in registrations:
            session.delete(registration)
    return len(registrations)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the migration. Without it, report what would change.",
    )
    args = parser.parse_args()

    engine = create_engine(str(settings.db_url), pool_pre_ping=True)
    session = sessionmaker(bind=engine)()
    try:
        definitions = definition_contexts(session)
        executions = execution_contexts(session)

        def_changed, revived = strip_payload_keys(
            session,
            definitions,
            LEGACY_DEFINITION_FIELDS,
            execute=args.execute,
        )
        run_changed, _ = strip_payload_keys(
            session,
            executions,
            LEGACY_EXECUTION_FIELDS,
            execute=args.execute,
        )
        def_fields = drop_field_registrations(
            session,
            definitions,
            LEGACY_DEFINITION_FIELDS,
            execute=args.execute,
        )
        run_fields = drop_field_registrations(
            session,
            executions,
            LEGACY_EXECUTION_FIELDS,
            execute=args.execute,
        )
        if args.execute:
            session.commit()
        else:
            session.rollback()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    mode = "" if args.execute else "DRY RUN — "
    print(
        f"{mode}definitions: {def_changed} rows stripped across "
        f"{len(definitions)} contexts, {def_fields} column registrations dropped\n"
        f"{mode}executions:  {run_changed} rows stripped across "
        f"{len(executions)} contexts, {run_fields} column registrations dropped",
        file=sys.stderr,
    )
    if revived:
        print(
            f"\n{len(revived)} definition(s) carried a terminal status while armed. "
            "The post-split runtime ignores that status, so these fire again:",
            file=sys.stderr,
        )
        for entry in revived:
            print(f"  {entry}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
