#!/usr/bin/env python3
"""Backfill the unified chat store from the legacy chat persistence.

Migrates, idempotently (a target thread/call that already has rows is
skipped):

1. ``dm_thread`` / ``dm_message`` rows -> ``chat_thread(kind='dm')`` +
   ``chat_message``.
2. ``Teams/{id}/GroupChat`` and ``Groups/{id}/GroupChat`` log contexts ->
   team/group threads + messages.
3. Personal ``{user}/{agent}/Transcripts`` rows with
   ``medium == 'unify_message'`` -> the owner's assistant-DM thread.
   Historically, team-chat fan-out was mirrored into Transcripts with no
   room scope (the leak this migration fixes), so rows whose metadata
   carries a room scope — or whose content matches a GroupChat row within a
   short window — are excluded, best-effort.
4. Personal ``unify_meet`` Transcripts rows grouped by ``exchange_id`` ->
   ``call_utterance`` rows under ``metadata.call_id`` when present,
   otherwise a synthetic ``backfill-{agent}-{exchange}`` call key.

Transcripts rows are never deleted — they remain the assistant's memory
mirror.

Run per environment before the Console cutover:

    uv run python scripts/backfill_unified_chat_store.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.chat_dao import ChatDAO
from orchestra.db.log_queries import project_scoped_log_events
from orchestra.db.models.orchestra_models import (
    Assistant,
    ChatGroup,
    Context,
    LogEvent,
    LogEventContext,
    Project,
    Team,
    User,
)
from orchestra.db.scope import single_owner_key
from orchestra.services.chat_service import assistant_display_name, user_display_name
from orchestra.settings import settings

PERSONAL_SELF_CONTACT_ID = 0
GROUP_CHAT_SUFFIX = "GroupChat"
# Mirrored team traffic lands in Transcripts shortly after the GroupChat row;
# content matches inside this window are treated as mirrors, not DMs.
MIRROR_MATCH_WINDOW = timedelta(minutes=10)


def _parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _thread_is_empty(dao: ChatDAO, thread_id: int) -> bool:
    return not dao.list_messages(thread_id=thread_id, limit=1)


def _context_rows(session: Session, context: Context) -> list[LogEvent]:
    query = (
        project_scoped_log_events(
            context.project_id,
            owner_key=single_owner_key(context.owner_scope, context.owner_id),
        )
        .where(LogEventContext.context_id == context.id)
        .order_by(LogEvent.id.asc())
    )
    return list(session.scalars(query).all())


def _map_reactions(
    raw: object,
    *,
    owner_user_id: str | None,
) -> list[dict]:
    """Map transcript contact-keyed reactions to user-keyed store reactions.

    Only the owner's reactions can be attributed (boss contact -> owner user
    id); other contact ids have no user identity and are dropped.
    """
    if not isinstance(raw, list):
        return []
    mapped = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("emoji"):
            continue
        if item.get("user_id"):
            mapped.append(
                {
                    "user_id": str(item["user_id"]),
                    "emoji": str(item["emoji"]),
                    **(
                        {"updated_at": item["updated_at"]}
                        if isinstance(item.get("updated_at"), str)
                        else {}
                    ),
                },
            )
        elif item.get("contact_id") is not None and owner_user_id:
            mapped.append(
                {
                    "user_id": owner_user_id,
                    "emoji": str(item["emoji"]),
                    **(
                        {"updated_at": item["updated_at"]}
                        if isinstance(item.get("updated_at"), str)
                        else {}
                    ),
                },
            )
    return mapped


def backfill_dm_tables(session: Session, *, dry_run: bool) -> int:
    """dm_thread / dm_message -> chat_thread(kind='dm') / chat_message."""
    from sqlalchemy import text

    dao = ChatDAO(session)
    migrated = 0
    legacy_threads = session.execute(
        text("SELECT id, organization_id, user_a_id, user_b_id FROM dm_thread"),
    ).all()
    for legacy in legacy_threads:
        thread = dao.resolve_dm_thread(
            organization_id=legacy.organization_id,
            user_id_1=legacy.user_a_id,
            user_id_2=legacy.user_b_id,
        )
        if not _thread_is_empty(dao, thread.id):
            continue
        rows = session.execute(
            text(
                "SELECT sender_user_id, content, attachments, reactions, "
                "created_at FROM dm_message WHERE thread_id = :tid "
                "ORDER BY id ASC",
            ),
            {"tid": legacy.id},
        ).all()
        for row in rows:
            sender = (
                session.get(User, row.sender_user_id) if row.sender_user_id else None
            )
            if dry_run:
                migrated += 1
                continue
            message = dao.add_message(
                thread=thread,
                sender_user_id=row.sender_user_id,
                sender_assistant_id=None,
                sender_name=user_display_name(sender) if sender else "",
                content=row.content or "",
                attachments=row.attachments or [],
                created_at=row.created_at,
            )
            if row.reactions:
                dao.set_message_reactions(message=message, reactions=row.reactions)
            migrated += 1
    return migrated


def backfill_group_chat_contexts(session: Session, *, dry_run: bool) -> int:
    """Teams/{id}/GroupChat and Groups/{id}/GroupChat -> room threads."""
    dao = ChatDAO(session)
    migrated = 0
    contexts = session.scalars(
        select(Context).where(Context.name.like(f"%/{GROUP_CHAT_SUFFIX}")),
    ).all()
    for context in contexts:
        parts = context.name.split("/")
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        scope_kind, scope_id = parts[0], int(parts[1])
        if scope_kind == "Teams":
            team = session.get(Team, scope_id)
            if team is None:
                continue
            thread = dao.resolve_team_thread(
                team_id=team.id,
                organization_id=team.organization_id,
            )
        elif scope_kind == "Groups":
            group = session.get(ChatGroup, scope_id)
            if group is None:
                continue
            thread = dao.resolve_group_thread(
                group_id=group.id,
                organization_id=group.organization_id,
            )
        else:
            continue

        if not _thread_is_empty(dao, thread.id):
            continue

        for row in _context_rows(session, context):
            data = dict(row.data or {})
            if dry_run:
                migrated += 1
                continue
            message = dao.add_message(
                thread=thread,
                sender_user_id=data.get("sender_user_id"),
                sender_assistant_id=data.get("sender_assistant_id"),
                sender_name=str(data.get("sender_name") or ""),
                content=str(data.get("content") or ""),
                mentions=data.get("mentions") or [],
                attachments=data.get("attachments") or [],
                created_at=_parse_timestamp(data.get("timestamp")),
            )
            reactions = data.get("reactions")
            if isinstance(reactions, list) and reactions:
                dao.set_message_reactions(message=message, reactions=reactions)
            migrated += 1
    return migrated


def _team_mirror_index(
    session: Session,
    *,
    team_ids: list[int],
) -> dict[str, list[datetime]]:
    """Content -> GroupChat timestamps, for mirrored-team-traffic exclusion."""
    index: dict[str, list[datetime]] = {}
    if not team_ids:
        return index
    contexts = session.scalars(
        select(Context).where(
            Context.name.in_(
                [f"Teams/{team_id}/{GROUP_CHAT_SUFFIX}" for team_id in team_ids],
            ),
        ),
    ).all()
    for context in contexts:
        for row in _context_rows(session, context):
            data = dict(row.data or {})
            content = str(data.get("content") or "")
            timestamp = _parse_timestamp(data.get("timestamp"))
            if content and timestamp is not None:
                index.setdefault(content, []).append(timestamp)
    return index


def _looks_like_team_mirror(
    content: str,
    timestamp: datetime | None,
    mirror_index: dict[str, list[datetime]],
) -> bool:
    candidates = mirror_index.get(content)
    if not candidates:
        return False
    if timestamp is None:
        return True
    return any(
        abs(timestamp - candidate) <= MIRROR_MATCH_WINDOW for candidate in candidates
    )


def backfill_assistant_transcripts(
    session: Session,
    *,
    dry_run: bool,
) -> tuple[int, int]:
    """Personal Transcripts -> assistant-DM threads + call utterances."""
    from orchestra.db.dao.team_dao import TeamDAO

    dao = ChatDAO(session)
    team_dao = TeamDAO(session)
    migrated_messages = 0
    migrated_utterances = 0

    assistants = session.scalars(select(Assistant)).all()
    for assistant in assistants:
        if not assistant.user_id:
            continue
        context_name = f"{assistant.user_id}/{assistant.agent_id}/Transcripts"
        contexts = session.scalars(
            select(Context)
            .join(Project, Project.id == Context.project_id)
            .where(Context.name == context_name, Project.name == "Assistants"),
        ).all()
        if not contexts:
            continue

        owner = session.get(User, assistant.user_id)
        team_ids = team_dao.team_ids_for_assistant(assistant.agent_id)
        mirror_index = _team_mirror_index(session, team_ids=team_ids)

        thread = dao.resolve_assistant_dm_thread(
            assistant_id=assistant.agent_id,
            user_id=assistant.user_id,
            organization_id=assistant.organization_id,
        )
        thread_empty = _thread_is_empty(dao, thread.id)
        # Idempotency is per call: decided once on first encounter, so a
        # call's later utterances are not mistaken for pre-existing data.
        call_should_migrate: dict[str, bool] = {}

        for context in contexts:
            for row in _context_rows(session, context):
                data = dict(row.data or {})
                medium = str(data.get("medium") or "")
                content = str(data.get("content") or "")
                timestamp = _parse_timestamp(data.get("timestamp"))
                metadata = data.get("metadata") or {}
                if not isinstance(metadata, dict):
                    metadata = {}
                sender_raw = data.get("sender_id")
                is_assistant = sender_raw == PERSONAL_SELF_CONTACT_ID

                if medium == "unify_message" and thread_empty:
                    # Already store-scoped rows are post-cutover mirrors.
                    if metadata.get("thread_id") is not None:
                        continue
                    # Room-scoped mirrors (or content-matched team traffic)
                    # never belonged in the DM.
                    if (
                        metadata.get("team_id") is not None
                        or metadata.get("group_id") is not None
                        or _looks_like_team_mirror(content, timestamp, mirror_index)
                    ):
                        continue
                    if not content:
                        continue
                    if dry_run:
                        migrated_messages += 1
                        continue
                    message = dao.add_message(
                        thread=thread,
                        sender_user_id=None if is_assistant else assistant.user_id,
                        sender_assistant_id=(
                            assistant.agent_id if is_assistant else None
                        ),
                        sender_name=(
                            assistant_display_name(assistant)
                            if is_assistant
                            else (user_display_name(owner) if owner else "")
                        ),
                        content=content,
                        attachments=data.get("attachments") or [],
                        created_at=timestamp,
                    )
                    reactions = _map_reactions(
                        metadata.get("reactions"),
                        owner_user_id=assistant.user_id,
                    )
                    if reactions:
                        dao.set_message_reactions(message=message, reactions=reactions)
                    migrated_messages += 1

                elif medium == "unify_meet":
                    exchange_id = data.get("exchange_id")
                    call_id = str(
                        metadata.get("call_id")
                        or f"backfill-{assistant.agent_id}-{exchange_id}",
                    )
                    if call_id not in call_should_migrate:
                        call_should_migrate[call_id] = not dao.list_utterances(
                            call_id=call_id,
                            reporter_assistant_id=assistant.agent_id,
                            limit=1,
                        )
                    if not call_should_migrate[call_id]:
                        continue
                    if not content:
                        continue
                    if dry_run:
                        migrated_utterances += 1
                        continue
                    dao.add_utterance(
                        call_id=call_id,
                        reporter_assistant_id=assistant.agent_id,
                        speaker_user_id=(None if is_assistant else assistant.user_id),
                        speaker_assistant_id=(
                            assistant.agent_id if is_assistant else None
                        ),
                        speaker_name=(
                            assistant_display_name(assistant)
                            if is_assistant
                            else (user_display_name(owner) if owner else "")
                        ),
                        content=content,
                        spoken_at=timestamp,
                        metadata={
                            key: metadata[key]
                            for key in ("call_utterance_timestamp",)
                            if key in metadata
                        },
                    )
                    migrated_utterances += 1

    return migrated_messages, migrated_utterances


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    engine = create_engine(str(settings.db_url), pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        dm_count = backfill_dm_tables(session, dry_run=args.dry_run)
        room_count = backfill_group_chat_contexts(session, dry_run=args.dry_run)
        message_count, utterance_count = backfill_assistant_transcripts(
            session,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    mode = "DRY RUN — " if args.dry_run else ""
    print(
        f"{mode}backfilled: {dm_count} DM messages, {room_count} room messages, "
        f"{message_count} assistant-DM messages, {utterance_count} call utterances",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
