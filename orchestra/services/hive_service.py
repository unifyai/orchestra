"""Hive lifecycle service — cascade delete across member bodies and shared contexts."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.models.orchestra_models import Assistant, Context, Hive, Project
from orchestra.services.assistant_cleanup_service import (
    CleanupSource,
    build_cleanup_spec_from_assistant,
    deprovision_assistant_contacts,
    enqueue_cleanup_tasks,
)

logger = logging.getLogger(__name__)


async def cascade_delete_hive(
    hive_id: int,
    organization_id: int,
    session_factory,
) -> None:
    """Delete a Hive and all of its member assistants in strict phase order.

    Phase 1: Acquire a ``SELECT FOR UPDATE`` lock on the hive row, set
             ``status='deleting'``, and commit so concurrent assistant-create
             requests see the marker and 409 immediately.
    Phase 2: Fan out per-body delete across all member assistants in parallel
             via ``asyncio.gather``. Individual body failures are logged but
             do not abort the cascade; each body's durable
             ``AssistantCleanupTask`` queue handles retry independently.
    Phase 3: Delete shared ``Hives/{hive_id}/...`` contexts one at a time via
             the phased ``ContextDAO.delete`` pipeline.
    Phase 4: Delete the hive row. ``ON DELETE SET NULL`` clears
             ``assistants.hive_id`` on any member rows that survived.

    This function is idempotent: re-running after a mid-cascade crash picks up
    from the current state — already-deleted bodies are skipped, already-deleted
    contexts are skipped, and a missing hive row returns cleanly.
    """
    # Phase 1: lock + mark deleting; commit to release the lock.
    s1: Session = session_factory()
    try:
        stmt = select(Hive).where(Hive.hive_id == hive_id).with_for_update()
        hive = s1.execute(stmt).scalar_one_or_none()
        if hive is None:
            return
        hive.status = "deleting"
        s1.commit()
    finally:
        s1.close()

    # Phase 2: collect member info, then delete in parallel.
    s2: Session = session_factory()
    try:
        member_rows = (
            s2.execute(
                select(Assistant).where(Assistant.hive_id == hive_id),
            )
            .scalars()
            .all()
        )
        member_info = [(a.agent_id, a.user_id) for a in member_rows]
    finally:
        s2.close()

    results = await asyncio.gather(
        *[
            _delete_member_assistant(
                session_factory,
                agent_id,
                user_id,
                organization_id,
            )
            for agent_id, user_id in member_info
        ],
        return_exceptions=True,
    )
    for (agent_id, _), result in zip(member_info, results):
        if isinstance(result, Exception):
            logger.error(
                "Hive %d member assistant %d delete failed: %s",
                hive_id,
                agent_id,
                result,
                exc_info=result,
            )

    # Phase 3: delete shared Hives/{hive_id}/... contexts.
    s3: Session = session_factory()
    try:
        hive_prefix = f"Hives/{hive_id}"
        assistants_project = (
            s3.query(Project)
            .filter(
                Project.organization_id == organization_id,
                Project.name == "Assistants",
            )
            .first()
        )
        if assistants_project:
            context_dao = ContextDAO(s3)
            shared_contexts = (
                s3.query(Context)
                .filter(
                    Context.project_id == assistants_project.id,
                    or_(
                        Context.name == hive_prefix,
                        Context.name.like(f"{hive_prefix}/%"),
                    ),
                )
                .all()
            )
            for ctx in shared_contexts:
                try:
                    context_dao.delete(ctx.id)
                except Exception:
                    logger.exception(
                        "Failed to delete Hive context %d (name=%s) during hive %d cascade",
                        ctx.id,
                        ctx.name,
                        hive_id,
                    )
        s3.commit()
    finally:
        s3.close()

    # Phase 4: delete the hive row.
    s4: Session = session_factory()
    try:
        hive = s4.get(Hive, hive_id)
        if hive:
            s4.delete(hive)
            s4.commit()
    finally:
        s4.close()


async def _delete_member_assistant(
    session_factory,
    agent_id: int,
    user_id: str,
    organization_id: int,
) -> None:
    """Run the standard deletion pipeline for one Hive member body.

    Mirrors the logic in ``DELETE /v0/assistant/{id}``:
    context cleanup → contact deprovision → cleanup-task enqueue → row delete → commit.

    Runtime teardown (runtime health, GCS, pubsub) rides on the durable
    ``AssistantCleanupTask`` queue enqueued here and is not awaited inline;
    the scheduled cleanup cron drives those tasks to completion.
    """
    s: Session = session_factory()
    try:
        stmt = select(Assistant).where(
            Assistant.agent_id == agent_id,
            Assistant.organization_id == organization_id,
        )
        assistant = s.execute(stmt).scalar_one_or_none()
        if assistant is None:
            logger.warning(
                "Hive cascade: assistant %d not found in org %d — skipping",
                agent_id,
                organization_id,
            )
            return

        context_dao = ContextDAO(s)

        # Delete per-body contexts from the Assistants project.
        assistants_project = (
            s.query(Project)
            .filter(
                Project.organization_id == organization_id,
                Project.name == "Assistants",
            )
            .first()
        )
        if assistants_project:
            context_prefix = f"{user_id}/{agent_id}"
            per_body_contexts = (
                s.query(Context)
                .filter(
                    Context.project_id == assistants_project.id,
                    or_(
                        Context.name == context_prefix,
                        Context.name.like(f"{context_prefix}/%"),
                    ),
                )
                .all()
            )
            for ctx in per_body_contexts:
                try:
                    context_dao.delete(ctx.id)
                except Exception:
                    logger.exception(
                        "Failed to delete context %d for assistant %d during hive cascade",
                        ctx.id,
                        agent_id,
                    )

        # Deprovision contacts and enqueue durable cleanup tasks.
        contact_dao = AssistantContactDAO(s)
        active_contacts = contact_dao.get_active_contacts_for_assistant(agent_id)
        cleanup_spec = build_cleanup_spec_from_assistant(assistant, active_contacts)

        await deprovision_assistant_contacts(
            s,
            [cleanup_spec],
            soft_delete_successes=True,
        )
        enqueue_cleanup_tasks(
            s,
            [cleanup_spec],
            source_flow=CleanupSource.ASSISTANT_DELETE,
        )

        # Delete the assistant row.
        s.delete(assistant)
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
