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
  ``info``, ``instance_id``.
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

from sqlalchemy import create_engine, select
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
    "instance_id",
)
LEGACY_EXECUTION_FIELDS = ("status",)

# A definition left carrying one of these read as dead under the old model.
# Under the new one it is armed again, which is a change worth naming.
TERMINAL_LEGACY_STATUSES = frozenset({"failed", "cancelled"})


def definition_contexts(session: Session) -> list[Context]:
    """Every user-authored ``Tasks`` table, across projects and tenants."""

    return [
        context
        for context in session.execute(select(Context)).scalars()
        if is_task_surface_context_name(context.name)
    ]


def execution_contexts(session: Session) -> list[Context]:
    """Every ``Tasks/Executions`` run ledger."""

    suffix = TASK_EXECUTIONS_CONTEXT_NAME
    return [
        context
        for context in session.execute(select(Context)).scalars()
        if (context.name or "").strip("/").endswith(suffix)
    ]


def _rows_in(session: Session, context_id: int) -> list[LogEvent]:
    return list(
        session.execute(
            select(LogEvent)
            .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
            .where(LogEventContext.context_id == context_id),
        )
        .scalars()
        .unique(),
    )


def strip_payload_keys(
    session: Session,
    contexts: list[Context],
    fields: tuple[str, ...],
    *,
    execute: bool,
) -> tuple[int, int, list[str]]:
    """Drop retired keys from every row in ``contexts``.

    Returns rows scanned, rows changed, and definitions whose retired
    ``status`` said dead while their authored intent says armed.
    """

    scanned = 0
    changed = 0
    revived: list[str] = []
    for context in contexts:
        for row in _rows_in(session, int(context.id)):
            scanned += 1
            data = dict(row.data or {})
            retired = [key for key in fields if key in data]
            if not retired:
                continue
            if (
                str(data.get("status") or "") in TERMINAL_LEGACY_STATUSES
                and data.get("enabled") is not False
            ):
                revived.append(
                    f"{context.name} task_id={data.get('task_id')} "
                    f"status={data.get('status')}",
                )
            for key in retired:
                data.pop(key)
            changed += 1
            if execute:
                _replace_log_payload(row, data)
    return scanned, changed, revived


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

        def_scanned, def_changed, revived = strip_payload_keys(
            session,
            definitions,
            LEGACY_DEFINITION_FIELDS,
            execute=args.execute,
        )
        run_scanned, run_changed, _ = strip_payload_keys(
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
        f"{mode}definitions: {def_changed}/{def_scanned} rows stripped across "
        f"{len(definitions)} contexts, {def_fields} column registrations dropped\n"
        f"{mode}executions:  {run_changed}/{run_scanned} rows stripped across "
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
