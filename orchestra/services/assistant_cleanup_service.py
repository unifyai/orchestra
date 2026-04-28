from __future__ import annotations

"""Utilities for enqueueing and retrying durable assistant cleanup."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from sqlalchemy import or_
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantCleanupTask,
    AssistantContact,
)
from orchestra.services.bucket_service import BucketService
from orchestra.settings import settings
from orchestra.web.api.utils.assistant_infra import (
    delete_email,
    delete_outlook_email,
    delete_phone_number,
    teardown_assistant_runtime,
)

logger = logging.getLogger(__name__)

DEFAULT_CLEANUP_TASK_BATCH_SIZE = 25
MAX_CLEANUP_TASK_BATCH_SIZE = 200
MAX_CLEANUP_ATTEMPTS = 5
BASE_RETRY_DELAY_MINUTES = 5
RETRYABLE_CLEANUP_TASK_STATUSES = ("pending", "processing")


class CleanupSource(StrEnum):
    """Stable identifiers for the workflows that enqueue cleanup tasks."""

    ASSISTANT_DELETE = "assistant_delete"
    ORGANIZATION_DELETE = "organization_delete"
    ORGANIZATION_MEMBER_REMOVAL = "organization_member_removal"
    USER_DELETE = "user_delete"


@dataclass
class ContactCleanupSpec:
    """Serializable description of one provisioned contact to deprovision."""

    contact_type: str
    contact_value: str | None = None
    contact_id: int | None = None
    provider: str | None = None
    provisioned_by: str | None = None

    def to_payload(self) -> dict:
        """Persist the subset of fields needed for retryable cleanup."""
        return {
            "contact_type": self.contact_type,
            "contact_value": self.contact_value,
            "provider": self.provider,
            "provisioned_by": self.provisioned_by,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "ContactCleanupSpec":
        """Rebuild a cleanup spec from a task payload."""
        return cls(
            contact_type=str(payload.get("contact_type", "")),
            contact_value=payload.get("contact_value"),
            provider=payload.get("provider"),
            provisioned_by=payload.get("provisioned_by"),
        )


@dataclass
class AssistantCleanupSpec:
    """Serializable cleanup contract for one assistant deletion flow.

    The payload is persisted before destructive owner-row deletes so retries can
    finish runtime teardown, contact cleanup, and assistant GCS cleanup later.
    """

    assistant_id: int
    deploy_env: str | None = None
    desktop_mode: str | None = None
    profile_photo: str | None = None
    profile_video: str | None = None
    contacts: list[ContactCleanupSpec] = field(default_factory=list)

    def to_payload(self) -> dict:
        """Persist the retry-relevant assistant cleanup state."""
        return {
            "profile_photo": self.profile_photo,
            "profile_video": self.profile_video,
            "contacts": [contact.to_payload() for contact in self.contacts],
        }

    @classmethod
    def from_task(cls, task: AssistantCleanupTask) -> "AssistantCleanupSpec":
        """Rebuild a cleanup spec from a queued task row."""
        payload = task.cleanup_payload or {}
        return cls(
            assistant_id=task.assistant_id,
            deploy_env=task.deploy_env,
            desktop_mode=task.desktop_mode,
            profile_photo=payload.get("profile_photo"),
            profile_video=payload.get("profile_video"),
            contacts=[
                ContactCleanupSpec.from_payload(contact_payload)
                for contact_payload in payload.get("contacts", [])
            ],
        )


def build_cleanup_spec(
    *,
    assistant_id: int,
    deploy_env: str | None = None,
    desktop_mode: str | None = None,
    profile_photo: str | None = None,
    profile_video: str | None = None,
    contacts: list[AssistantContact] | None = None,
) -> AssistantCleanupSpec:
    """Create an assistant cleanup spec from already-loaded ORM objects."""
    return AssistantCleanupSpec(
        assistant_id=assistant_id,
        deploy_env=deploy_env,
        desktop_mode=desktop_mode,
        profile_photo=profile_photo,
        profile_video=profile_video,
        contacts=[
            ContactCleanupSpec(
                contact_type=contact.contact_type,
                contact_value=contact.contact_value,
                contact_id=contact.id,
                provider=contact.provider,
                provisioned_by=contact.provisioned_by,
            )
            for contact in (contacts or [])
        ],
    )


def build_cleanup_spec_from_assistant(
    assistant: Assistant,
    contacts: list[AssistantContact] | None = None,
) -> AssistantCleanupSpec:
    """Create a cleanup spec directly from an assistant row."""
    return build_cleanup_spec(
        assistant_id=int(assistant.agent_id),
        deploy_env=assistant.deploy_env,
        desktop_mode=assistant.desktop_mode,
        profile_photo=assistant.profile_photo,
        profile_video=assistant.profile_video,
        contacts=contacts,
    )


def build_cleanup_specs_for_assistants(
    session: Session,
    assistants: list[Assistant],
) -> list[AssistantCleanupSpec]:
    """Collect active contacts and build cleanup specs for many assistants."""
    if not assistants:
        return []

    contact_dao = AssistantContactDAO(session)
    assistant_ids = [int(assistant.agent_id) for assistant in assistants]
    contacts_by_assistant_id: dict[int, list[AssistantContact]] = {}
    for contact in contact_dao.get_active_contacts_for_assistants(assistant_ids):
        contacts_by_assistant_id.setdefault(int(contact.assistant_id), []).append(
            contact,
        )

    return [
        build_cleanup_spec_from_assistant(
            assistant,
            contacts_by_assistant_id.get(int(assistant.agent_id), []),
        )
        for assistant in assistants
    ]


async def deprovision_assistant_contacts(
    session: Session,
    cleanup_specs: list[AssistantCleanupSpec],
    *,
    soft_delete_successes: bool,
) -> dict:
    """Deprovision contact resources and retain only failures on each spec.

    When ``soft_delete_successes`` is true, successfully deprovisioned
    ``AssistantContact`` rows are marked deleted in the same transaction.
    """
    from orchestra.db.dao.shared_pool_dao import SharedPoolDAO

    shared_pool_dao = SharedPoolDAO(session)
    errors: list[str] = []
    attempted = 0
    soft_deleted = 0

    for spec in cleanup_specs:
        remaining_contacts: list[ContactCleanupSpec] = []
        whatsapp_cleared = False

        for contact in spec.contacts:
            attempted += 1
            # User-provisioned (BYOD) phone/email resources are owned by the
            # user, not the platform — skip the external deprovision call so
            # we don't try to delete the user's own Twilio number / mailbox.
            # WhatsApp routes always live in our shared pool, so they're
            # always cleaned up regardless of provisioning source.
            is_byod = contact.provisioned_by == "user"
            try:
                if contact.contact_type == "phone" and contact.contact_value:
                    if is_byod:
                        logger.info(
                            "Skipping external deprovision for BYOD phone "
                            "%s on assistant %s",
                            contact.contact_value,
                            spec.assistant_id,
                        )
                    else:
                        await delete_phone_number(
                            contact.contact_value,
                            deploy_env=spec.deploy_env,
                        )
                elif contact.contact_type == "email" and contact.contact_value:
                    if is_byod:
                        logger.info(
                            "Skipping external deprovision for BYOD email "
                            "%s on assistant %s",
                            contact.contact_value,
                            spec.assistant_id,
                        )
                    elif contact.provider == "microsoft_365":
                        await delete_outlook_email(
                            contact.contact_value,
                            deploy_env=spec.deploy_env,
                        )
                    else:
                        await delete_email(
                            contact.contact_value,
                            deploy_env=spec.deploy_env,
                        )
                elif contact.contact_type == "whatsapp":
                    if not whatsapp_cleared:
                        shared_pool_dao.delete_routes_for_assistant(spec.assistant_id)
                        whatsapp_cleared = True
                else:
                    logger.info(
                        "Skipping unsupported contact cleanup type %s for assistant %s",
                        contact.contact_type,
                        spec.assistant_id,
                    )
            except Exception as exc:
                errors.append(
                    f"Failed to deprovision {contact.contact_type} "
                    f"({contact.contact_value}) for assistant {spec.assistant_id}: {exc}",
                )
                remaining_contacts.append(contact)
                continue

            if soft_delete_successes and contact.contact_id is not None:
                row = session.get(AssistantContact, contact.contact_id)
                if row is not None and row.status != "deleted":
                    row.status = "deleted"
                    row.deleted_at = datetime.now(timezone.utc)
                    soft_deleted += 1

        spec.contacts = remaining_contacts

    # Clean up any stored secrets for each assistant
    from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO

    secret_dao = AssistantSecretDAO(session)
    for spec in cleanup_specs:
        try:
            secret_dao.delete_all(int(spec.assistant_id))
        except Exception as exc:
            errors.append(
                f"Failed to delete secrets for assistant {spec.assistant_id}: {exc}",
            )

    return {
        "success": not errors,
        "attempted": attempted,
        "soft_deleted": soft_deleted,
        "errors": errors,
    }


def enqueue_cleanup_tasks(
    session: Session,
    cleanup_specs: list[AssistantCleanupSpec],
    *,
    source_flow: CleanupSource,
) -> list[AssistantCleanupTask]:
    """Persist retryable cleanup work for later or follow-up processing."""
    tasks: list[AssistantCleanupTask] = []
    for spec in cleanup_specs:
        task = AssistantCleanupTask(
            assistant_id=spec.assistant_id,
            deploy_env=spec.deploy_env,
            desktop_mode=spec.desktop_mode,
            source_flow=source_flow,
            cleanup_payload=spec.to_payload(),
            status="pending",
        )
        session.add(task)
        tasks.append(task)
    session.flush()
    return tasks


def _delete_assistant_gcs_data(spec: AssistantCleanupSpec) -> dict[str, object]:
    """Delete assistant-scoped GCS data after runtime teardown is complete."""

    errors: list[str] = []
    deleted_counts = {"media": 0, "recordings": 0, "attachments": 0}
    bucket_service = BucketService()

    for field_name in ("profile_photo", "profile_video"):
        gcs_url = getattr(spec, field_name)
        if not gcs_url or not str(gcs_url).startswith("gs://"):
            continue
        try:
            bucket_service.delete_assistant_file(str(gcs_url))
        except Exception as exc:
            errors.append(
                f"Failed to delete {field_name} for assistant {spec.assistant_id}: {exc}",
            )

    try:
        deleted_counts = bucket_service.delete_all_assistant_data(
            spec.assistant_id,
            is_staging=settings.is_staging,
        )
    except Exception as exc:
        errors.append(
            f"Failed to delete assistant GCS data for assistant {spec.assistant_id}: {exc}",
        )

    return {
        "success": not errors,
        "deleted_counts": deleted_counts,
        "errors": errors,
    }


def _next_retry_at(attempt_count: int) -> datetime:
    """Compute the next retry time using a capped exponential backoff."""
    delay_minutes = min(60, BASE_RETRY_DELAY_MINUTES * (2 ** max(0, attempt_count - 1)))
    return datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)


async def process_assistant_cleanup_tasks(
    session: Session,
    *,
    task_ids: list[int] | None = None,
    limit: int = DEFAULT_CLEANUP_TASK_BATCH_SIZE,
) -> dict:
    """Process queued cleanup tasks and persist retry/completion state.

    A task is only marked complete after runtime teardown, contact cleanup, and
    assistant-scoped GCS deletion have all succeeded.
    """
    now = datetime.now(timezone.utc)
    if task_ids:
        # Explicit task processing is used by the in-request/background cleanup
        # loop and must keep re-driving the same tasks even if a prior attempt
        # scheduled a later retry. The cron path below still honors next_retry_at.
        query = session.query(AssistantCleanupTask).filter(
            AssistantCleanupTask.status.in_(RETRYABLE_CLEANUP_TASK_STATUSES),
            AssistantCleanupTask.id.in_(task_ids),
        )
    else:
        query = session.query(AssistantCleanupTask).filter(
            AssistantCleanupTask.status.in_(RETRYABLE_CLEANUP_TASK_STATUSES),
            or_(
                AssistantCleanupTask.next_retry_at.is_(None),
                AssistantCleanupTask.next_retry_at <= now,
            ),
        )

    tasks = query.order_by(AssistantCleanupTask.created_at.asc()).limit(limit).all()
    summary = {
        "processed": 0,
        "completed": 0,
        "retried": 0,
        "failed": 0,
        "errors": [],
    }

    for task in tasks:
        task.status = "processing"
        task.processing_started_at = datetime.now(timezone.utc)

        spec = AssistantCleanupSpec.from_task(task)
        try:
            runtime_result = await teardown_assistant_runtime(
                spec.assistant_id,
                deploy_env=spec.deploy_env,
                desktop_mode=spec.desktop_mode,
            )
            contact_result = await deprovision_assistant_contacts(
                session,
                [spec],
                soft_delete_successes=False,
            )
            storage_result: dict[str, object]
            errors = [
                *runtime_result.get("errors", []),
                *contact_result.get("errors", []),
            ]
            if errors:
                storage_result = {
                    "success": True,
                    "skipped": True,
                    "reason": "runtime_or_contact_cleanup_incomplete",
                    "errors": [],
                }
            else:
                storage_result = await asyncio.to_thread(
                    _delete_assistant_gcs_data,
                    spec,
                )
                errors.extend(storage_result.get("errors", []))
            task.attempt_count += 1
            task.last_result = {
                "runtime": runtime_result,
                "contacts": contact_result,
                "storage": storage_result,
            }
            task.cleanup_payload = spec.to_payload()
            if errors:
                task.last_error = "; ".join(errors)
                if task.attempt_count >= MAX_CLEANUP_ATTEMPTS:
                    task.status = "failed"
                    summary["failed"] += 1
                else:
                    task.status = "pending"
                    task.next_retry_at = _next_retry_at(task.attempt_count)
                    summary["retried"] += 1
                summary["errors"].extend(errors)
            else:
                task.status = "completed"
                task.completed_at = datetime.now(timezone.utc)
                task.last_error = None
                task.next_retry_at = None
                summary["completed"] += 1
        except Exception as exc:
            task.attempt_count += 1
            task.last_error = str(exc)
            task.last_result = {"exception": str(exc)}
            if task.attempt_count >= MAX_CLEANUP_ATTEMPTS:
                task.status = "failed"
                summary["failed"] += 1
            else:
                task.status = "pending"
                task.next_retry_at = _next_retry_at(task.attempt_count)
                summary["retried"] += 1
            summary["errors"].append(str(exc))

        summary["processed"] += 1

    session.commit()
    return summary
