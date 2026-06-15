"""Bootstrap helpers that keep assistant chat contexts queryable."""

from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.unique_constraint_dao import (
    COMPOSITE_KEY_FIELD,
    UniqueConstraintDAO,
)
from orchestra.db.models.orchestra_models import (
    Assistant,
    Context,
    LogEvent,
    LogEventContext,
    LogUniqueConstraint,
    Project,
    User,
)
from orchestra.web.api.log.schema import CreateLogConfig
from orchestra.web.api.log.utils.logging_utils import create_logs_internal

from .contact_membership_service import (
    BOSS_CONTACT_RESPONSE_POLICY,
    PERSONAL_BOSS_CONTACT_ID,
)

ASSISTANTS_PROJECT_NAME = "Assistants"
CONTACTS_CONTEXT_SUFFIX = "Contacts"
CONTACTS_UNIQUE_KEYS = {"contact_id": "int"}
CONTACTS_AUTO_COUNTING = {"contact_id": None}


def _assistant_context_name(assistant: Assistant, suffix: str) -> str:
    return f"{assistant.user_id}/{assistant.agent_id}/{suffix}"


def _lock_assistant_context(
    session: Session,
    *,
    assistant: Assistant,
    suffix: str,
) -> None:
    """Serialize writes for one assistant-owned context."""
    lock_key = f"assistant:{assistant.agent_id}:{suffix}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": lock_key},
    )


def _resolve_assistants_project(
    session: Session,
    *,
    assistant: Assistant,
) -> Project:
    if assistant.organization_id is None:
        project = session.scalar(
            select(Project).where(
                Project.user_id == assistant.user_id,
                Project.organization_id.is_(None),
                Project.name == ASSISTANTS_PROJECT_NAME,
            ),
        )
    else:
        project = session.scalar(
            select(Project).where(
                Project.organization_id == assistant.organization_id,
                Project.name == ASSISTANTS_PROJECT_NAME,
            ),
        )
    if project is None:
        raise ValueError(
            "Assistants project is required before owner contact bootstrap "
            f"(assistant={assistant.agent_id}).",
        )
    return project


def _ensure_context(
    session: Session,
    *,
    project_id: int,
    context_name: str,
    unique_keys: dict[str, str] | None = None,
    auto_counting: dict[str, str | None] | None = None,
    allow_duplicates: bool | None = None,
) -> Context:
    context = session.scalar(
        select(Context).where(
            Context.project_id == project_id,
            Context.name == context_name,
        ),
    )
    if context is None:
        unique_keys = unique_keys or {}
        context = Context(
            project_id=project_id,
            name=context_name,
            is_versioned=False,
            allow_duplicates=True if allow_duplicates is None else allow_duplicates,
            unique_key_names=list(unique_keys.keys()),
            unique_key_types=list(unique_keys.values()),
            auto_counting=auto_counting or {},
        )
        session.add(context)
        session.flush()
        return context

    if unique_keys and not context.unique_keys:
        context.unique_key_names = list(unique_keys.keys())
        context.unique_key_types = list(unique_keys.values())
    if auto_counting and not context.auto_counting:
        context.auto_counting = auto_counting
    if allow_duplicates is not None and context.allow_duplicates != allow_duplicates:
        context.allow_duplicates = allow_duplicates
    return context


def _owner_contact_entries(owner: User) -> dict[str, Any]:
    return {
        "contact_id": PERSONAL_BOSS_CONTACT_ID,
        "first_name": owner.name,
        "surname": owner.last_name,
        "email_address": owner.email,
        "job_title": owner.job_title,
        "bio": owner.bio,
        "timezone": owner.timezone,
        "is_system": True,
        "should_respond": True,
        "response_policy": BOSS_CONTACT_RESPONSE_POLICY,
    }


def _find_contact_log_by_contact_id(
    session: Session,
    *,
    context: Context,
    contact_id: int,
) -> LogEvent | None:
    logs = session.scalars(
        select(LogEvent)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .where(
            LogEventContext.context_id == context.id,
            LogEvent.data.has_key("contact_id"),
        )
        .order_by(LogEvent.id.asc()),
    ).all()
    for log in logs:
        if _normalized_contact_id(log.data.get("contact_id")) == contact_id:
            return log
    return None


def _normalized_contact_id(raw_value: Any) -> int | None:
    """Best-effort integer normalization for legacy contact-id shapes."""
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, int):
        return raw_value
    if isinstance(raw_value, float):
        if raw_value.is_integer():
            return int(raw_value)
        return None
    if isinstance(raw_value, str):
        value = raw_value.strip()
        if not value:
            return None
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None
        if parsed == parsed.to_integral_value():
            return int(parsed)
        return None
    return None


def _log_data_contains(log_data: dict[str, Any], entries: dict[str, Any]) -> bool:
    return all(log_data.get(key) == value for key, value in entries.items())


def _build_project_dao(session: Session) -> ProjectDAO:
    context_dao = ContextDAO(session)
    return ProjectDAO(
        session,
        organization_member_dao=OrganizationMemberDAO(session),
        context_dao=context_dao,
    )


def _create_log_entry(
    session: Session,
    *,
    project: Project,
    context: Context,
    context_name: str,
    entries: dict[str, Any],
) -> dict[str, Any]:
    context_dao = ContextDAO(session)
    result = create_logs_internal(
        request=CreateLogConfig(
            project_name=ASSISTANTS_PROJECT_NAME,
            context=context_name,
            entries=entries,
        ),
        project_id=project.id,
        context_id=context.id,
        project_dao=_build_project_dao(session),
        field_type_dao=FieldTypeDAO(session),
        log_event_dao=LogEventDAO(session),
        context_dao=context_dao,
        context_obj=context,
    )
    if result.get("failed"):
        first_error = result["failed"][0].get("error", "Log creation failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=first_error,
        )
    return result


def _is_duplicate_contact_key_error(exc: HTTPException) -> bool:
    if exc.status_code != status.HTTP_400_BAD_REQUEST:
        return False
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return (
        "Duplicate composite key already exists for this context" in detail
        and "contact_id" in detail
    )


def _clear_stale_contact_unique_constraint(
    session: Session,
    *,
    context_id: int,
    contact_id: int,
) -> None:
    """Drop a stale lookup entry for one Contacts composite key."""
    value_hash = UniqueConstraintDAO.hash_composite(
        {"contact_id": contact_id},
        ["contact_id"],
    )
    session.query(LogUniqueConstraint).filter(
        LogUniqueConstraint.context_id == context_id,
        LogUniqueConstraint.field_name == COMPOSITE_KEY_FIELD,
        LogUniqueConstraint.value_hash == value_hash,
    ).delete(synchronize_session=False)
    session.flush()


def ensure_owner_contact_row(
    session: Session,
    *,
    assistant: Assistant,
    project: Project | None = None,
) -> int:
    """Ensure an assistant's root Contacts context contains the owner row."""
    _lock_assistant_context(
        session,
        assistant=assistant,
        suffix=CONTACTS_CONTEXT_SUFFIX,
    )
    owner = session.get(User, assistant.user_id)
    if owner is None:
        raise ValueError(f"Assistant owner user {assistant.user_id!r} was not found")

    resolved_project = project or _resolve_assistants_project(
        session,
        assistant=assistant,
    )
    context_name = _assistant_context_name(assistant, CONTACTS_CONTEXT_SUFFIX)
    context = _ensure_context(
        session,
        project_id=resolved_project.id,
        context_name=context_name,
        unique_keys=CONTACTS_UNIQUE_KEYS,
        auto_counting=CONTACTS_AUTO_COUNTING,
    )
    entries = _owner_contact_entries(owner)
    existing = _find_contact_log_by_contact_id(
        session,
        context=context,
        contact_id=PERSONAL_BOSS_CONTACT_ID,
    )
    if existing is not None:
        if _log_data_contains(existing.data, entries):
            return existing.id
        existing.data = {**existing.data, **entries}
        flag_modified(existing, "data")
        session.flush()
        return existing.id

    try:
        result = _create_log_entry(
            session,
            project=resolved_project,
            context=context,
            context_name=context_name,
            entries=entries,
        )
    except HTTPException as exc:
        if not _is_duplicate_contact_key_error(exc):
            raise

        existing = _find_contact_log_by_contact_id(
            session,
            context=context,
            contact_id=PERSONAL_BOSS_CONTACT_ID,
        )
        if existing is not None:
            if not _log_data_contains(existing.data, entries):
                existing.data = {**existing.data, **entries}
                flag_modified(existing, "data")
                session.flush()
            return existing.id

        _clear_stale_contact_unique_constraint(
            session,
            context_id=context.id,
            contact_id=PERSONAL_BOSS_CONTACT_ID,
        )
        result = _create_log_entry(
            session,
            project=resolved_project,
            context=context,
            context_name=context_name,
            entries=entries,
        )

    session.flush()
    return result["log_event_ids"][0]


def ensure_owner_contact_rows(
    session: Session,
    assistant_ids: Sequence[int],
) -> None:
    """Ensure owner contact rows exist for every listed assistant id."""
    if not assistant_ids:
        return
    assistants = session.scalars(
        select(Assistant).where(Assistant.agent_id.in_(assistant_ids)),
    ).all()
    for assistant in assistants:
        ensure_owner_contact_row(session, assistant=assistant)
    session.flush()
