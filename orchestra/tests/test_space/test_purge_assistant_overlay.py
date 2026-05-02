"""Tests for assistant-owned overlay cleanup during space membership removal."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    CONTACT_MEMBERSHIP_SCOPE_SPACE,
    Assistant,
    AssistantSpaceMembership,
    ContactMembership,
    Space,
    User,
)
from orchestra.services.space_cleanup_service import purge_assistant_overlay


def _make_user(dbsession: Session, suffix: str) -> User:
    user = User(
        id=f"purge-overlay-user-{suffix}",
        email=f"purge-overlay-user-{suffix}@test.com",
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _make_assistant(dbsession: Session, owner: User, suffix: str) -> Assistant:
    assistant = Assistant(
        user_id=owner.id,
        first_name=f"Purge {suffix}",
        surname="Bot",
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


def _make_space(dbsession: Session, owner: User, suffix: str) -> Space:
    space = Space(
        name=f"Purge Space {suffix}",
        description=f"Purge space workspace for {suffix} overlay tests.",
        owner_user_id=owner.id,
    )
    dbsession.add(space)
    dbsession.flush()
    return space


def _add_space_membership(
    dbsession: Session,
    *,
    owner: User,
    assistant: Assistant,
    space: Space,
) -> None:
    dbsession.add(
        AssistantSpaceMembership(
            assistant_id=assistant.agent_id,
            space_id=space.space_id,
            added_by=owner.id,
        ),
    )
    dbsession.flush()


def _add_contact_membership(
    dbsession: Session,
    *,
    assistant: Assistant,
    contact_id: int,
    target_scope: str,
    target_space_id: int | None = None,
) -> None:
    dbsession.add(
        ContactMembership(
            assistant_id=assistant.agent_id,
            contact_id=contact_id,
            target_scope=target_scope,
            target_space_id=target_space_id,
            relationship="coworker",
        ),
    )
    dbsession.flush()


@pytest.mark.anyio
async def test_purge_assistant_overlay_drops_pair_rows_only(
    dbsession: Session,
) -> None:
    """Only overlays for the assistant and removed space are deleted."""
    owner = _make_user(dbsession, "pair")
    removed_assistant = _make_assistant(dbsession, owner, "removed")
    retained_assistant = _make_assistant(dbsession, owner, "retained")
    removed_space = _make_space(dbsession, owner, "removed")
    retained_space = _make_space(dbsession, owner, "retained")
    for assistant in (removed_assistant, retained_assistant):
        for space in (removed_space, retained_space):
            _add_space_membership(
                dbsession,
                owner=owner,
                assistant=assistant,
                space=space,
            )
    _add_contact_membership(
        dbsession,
        assistant=removed_assistant,
        contact_id=1,
        target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
        target_space_id=removed_space.space_id,
    )
    _add_contact_membership(
        dbsession,
        assistant=removed_assistant,
        contact_id=2,
        target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
        target_space_id=retained_space.space_id,
    )
    _add_contact_membership(
        dbsession,
        assistant=retained_assistant,
        contact_id=3,
        target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
        target_space_id=removed_space.space_id,
    )
    _add_contact_membership(
        dbsession,
        assistant=removed_assistant,
        contact_id=4,
        target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    )

    await purge_assistant_overlay(
        dbsession,
        assistant_id=removed_assistant.agent_id,
        space_id=removed_space.space_id,
        revoke_activations=False,
        remove_membership=False,
    )

    remaining = dbsession.scalars(
        sa.select(ContactMembership.contact_id).order_by(ContactMembership.contact_id),
    ).all()
    assert remaining == [2, 3, 4]
    assert (
        dbsession.query(AssistantSpaceMembership)
        .filter(AssistantSpaceMembership.assistant_id == removed_assistant.agent_id)
        .count()
        == 2
    )


@pytest.mark.anyio
async def test_purge_assistant_overlay_does_not_commit(dbsession: Session) -> None:
    """The caller owns whether overlay cleanup is committed or rolled back."""
    owner = _make_user(dbsession, "rollback")
    assistant = _make_assistant(dbsession, owner, "rollback")
    space = _make_space(dbsession, owner, "rollback")
    _add_space_membership(dbsession, owner=owner, assistant=assistant, space=space)
    _add_contact_membership(
        dbsession,
        assistant=assistant,
        contact_id=1,
        target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
        target_space_id=space.space_id,
    )
    commit_events: list[bool] = []
    sa.event.listen(
        dbsession,
        "after_commit",
        lambda session: commit_events.append(True),
    )

    await purge_assistant_overlay(
        dbsession,
        assistant_id=assistant.agent_id,
        space_id=space.space_id,
        revoke_activations=False,
        remove_membership=False,
    )

    assert dbsession.query(ContactMembership).count() == 0
    assert commit_events == []
