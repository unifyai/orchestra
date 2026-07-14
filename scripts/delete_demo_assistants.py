#!/usr/bin/env python3
"""Delete live sales-demo assistants before the drop_demo_assistants migration.

The migration refuses to run while any ``assistants.demo_id`` is set. Run this
against each environment (staging, then prod) *before* ``alembic upgrade``.

Uses the same teardown path as a normal assistant delete (contact deprovision,
cleanup-task enqueue, membership purge, row delete, owner purge). Finds rows
via raw SQL so it still works after the ORM ``demo_id`` field is removed.

Examples::

    # Dry-run: list only
    poetry run python scripts/delete_demo_assistants.py --dry-run

    # Delete all demo assistants
    poetry run python scripts/delete_demo_assistants.py

    # Limit to one agent
    poetry run python scripts/delete_demo_assistants.py --agent-id 12345
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.models.orchestra_models import Assistant
from orchestra.services.assistant_cleanup_service import (
    CleanupSource,
    build_cleanup_spec_from_assistant,
    deprovision_assistant_contacts,
    enqueue_cleanup_tasks,
    purge_assistant_owner,
)
from orchestra.services.team_cleanup_service import purge_assistant_memberships
from orchestra.settings import settings


def _list_demo_assistants(
    session: Session,
    *,
    agent_id: int | None,
) -> list[tuple[int, str | None, int | None, int]]:
    """Return (agent_id, user_id, organization_id, demo_id) for demo rows."""
    sql = """
        SELECT agent_id, user_id, organization_id, demo_id
        FROM assistants
        WHERE demo_id IS NOT NULL
    """
    params: dict[str, object] = {}
    if agent_id is not None:
        sql += " AND agent_id = :agent_id"
        params["agent_id"] = agent_id
    sql += " ORDER BY agent_id"
    return [
        (int(r[0]), r[1], r[2], int(r[3]))
        for r in session.execute(text(sql), params).fetchall()
    ]


async def _delete_one(session: Session, assistant: Assistant) -> list[str]:
    """Tear down one demo assistant; returns contact-cleanup errors."""
    errors: list[str] = []
    assistant_id = int(assistant.agent_id)
    user_id = assistant.user_id
    organization_id = assistant.organization_id

    await purge_assistant_memberships(session, assistant=assistant)

    contact_dao = AssistantContactDAO(session)
    if assistant.is_coordinator:
        # Coordinators share pool contacts — never release them.
        specs = [build_cleanup_spec_from_assistant(assistant, contacts=[])]
    else:
        active_contacts = contact_dao.get_active_contacts_for_assistant(assistant_id)
        specs = [
            build_cleanup_spec_from_assistant(assistant, active_contacts),
        ]
        contact_result = await deprovision_assistant_contacts(
            session,
            specs,
            soft_delete_successes=True,
        )
        errors.extend(contact_result["errors"])

    enqueue_cleanup_tasks(
        session,
        specs,
        source_flow=CleanupSource.ASSISTANT_DELETE,
    )

    dao = AssistantDAO(session)
    if user_id is not None:
        deleted = dao.delete_assistant(
            user_id=user_id,
            agent_id=assistant_id,
            organization_id=organization_id,
        )
        if not deleted:
            session.delete(assistant)
    else:
        session.delete(assistant)

    purge_assistant_owner(
        session,
        assistant_id=assistant_id,
        user_id=user_id,
        organization_id=organization_id,
    )
    return errors


async def run(*, dry_run: bool, agent_id: int | None) -> int:
    engine = create_engine(str(settings.db_url), pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        try:
            rows = _list_demo_assistants(session, agent_id=agent_id)
        except Exception as exc:
            print(
                f"Could not query assistants.demo_id (already migrated?): {exc}",
                file=sys.stderr,
            )
            return 1

        if not rows:
            print("No demo assistants found.")
            return 0

        print(f"Found {len(rows)} demo assistant(s):")
        for aid, uid, org_id, demo_id in rows:
            print(
                f"  agent_id={aid} user_id={uid} organization_id={org_id} "
                f"demo_id={demo_id}",
            )

        if dry_run:
            print("Dry-run only; no deletes performed.")
            return 0

        errors: list[str] = []
        for aid, _uid, _org_id, demo_id in rows:
            assistant = session.get(Assistant, aid)
            if assistant is None:
                print(f"  skip agent_id={aid}: row missing", file=sys.stderr)
                continue
            print(f"Deleting agent_id={aid} (demo_id={demo_id})…")
            row_errors = await _delete_one(session, assistant)
            errors.extend(row_errors)
            session.execute(
                text("DELETE FROM demo_assistant_meta WHERE id = :demo_id"),
                {"demo_id": demo_id},
            )
            session.commit()
            print(f"  deleted agent_id={aid}")

        remaining = session.execute(
            text("SELECT COUNT(*) FROM assistants WHERE demo_id IS NOT NULL"),
        ).scalar()
        print(f"Remaining assistants with demo_id: {remaining}")
        if errors:
            print("Contact cleanup errors:", file=sys.stderr)
            for err in errors:
                print(f"  {err}", file=sys.stderr)
        return 0 if remaining == 0 else 1
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List demo assistants without deleting them.",
    )
    parser.add_argument(
        "--agent-id",
        type=int,
        default=None,
        help="Only delete this agent_id (must still have demo_id set).",
    )
    args = parser.parse_args()
    return asyncio.run(run(dry_run=args.dry_run, agent_id=args.agent_id))


if __name__ == "__main__":
    sys.exit(main())
