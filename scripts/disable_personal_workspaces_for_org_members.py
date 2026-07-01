#!/usr/bin/env python3
from __future__ import annotations

"""Disable personal workspaces for users who belong to customer organizations."""

import argparse
import asyncio
import json
from dataclasses import asdict

from sqlalchemy.orm import sessionmaker

from orchestra.db.models.orchestra_models import (
    Assistant,
    Organization,
    OrganizationMember,
    User,
)
from orchestra.services.assistant_cleanup_service import process_assistant_cleanup_tasks
from orchestra.services.personal_workspace_service import (
    UNIFY_ORGANIZATION_NAME,
    disable_personal_workspace_for_org_member,
)
from orchestra.web.lifetime import get_engine


def _target_rows(session, limit: int | None):
    query = (
        session.query(User.id, OrganizationMember.organization_id)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .filter(
            Organization.name != UNIFY_ORGANIZATION_NAME,
            ~session.query(OrganizationMember.id)
            .join(Organization, Organization.id == OrganizationMember.organization_id)
            .filter(
                OrganizationMember.user_id == User.id,
                Organization.name == UNIFY_ORGANIZATION_NAME,
            )
            .exists(),
            session.query(Assistant.agent_id)
            .filter(
                Assistant.user_id == User.id,
                Assistant.organization_id.is_(None),
            )
            .exists(),
        )
        .order_by(User.created_at.asc(), OrganizationMember.created_at.asc())
    )
    if limit is not None:
        query = query.limit(limit)
    return query.all()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist changes.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--process-cleanup",
        action="store_true",
        help="Process queued cleanup tasks after applying.",
    )
    args = parser.parse_args()

    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as session:
        targets = _target_rows(session, args.limit)
        summary: dict[str, object] = {
            "dry_run": not args.apply,
            "targets": len(targets),
            "results": [],
        }
        if not args.apply:
            summary["users"] = [
                {"user_id": user_id, "organization_id": organization_id}
                for user_id, organization_id in targets
            ]
            print(json.dumps(summary, indent=2, sort_keys=True))
            return

        seen_user_ids: set[str] = set()
        task_ids: list[int] = []
        for user_id, organization_id in targets:
            if user_id in seen_user_ids:
                continue
            seen_user_ids.add(user_id)
            result = disable_personal_workspace_for_org_member(
                session,
                user_id,
                organization_id,
            )
            summary["results"].append(asdict(result))
            task_ids.extend(result.cleanup_task_ids)
            session.flush()

        session.commit()

        if args.process_cleanup and task_ids:
            cleanup_summary = await process_assistant_cleanup_tasks(
                session,
                task_ids=task_ids,
            )
            summary["cleanup"] = cleanup_summary

        print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
