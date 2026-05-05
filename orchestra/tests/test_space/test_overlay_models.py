"""Schema tests for assistant contact membership overlays."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    CONTACT_MEMBERSHIP_RELATIONSHIP_COWORKER,
    CONTACT_MEMBERSHIP_RELATIONSHIP_OTHER,
    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    CONTACT_MEMBERSHIP_SCOPE_SPACE,
    Assistant,
    ContactMembership,
    Space,
    User,
)


def _make_user(dbsession: Session, suffix: str) -> User:
    user = User(
        id=f"overlay-user-{suffix}",
        email=f"overlay-user-{suffix}@test.com",
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _make_assistant(dbsession: Session, owner: User, suffix: str) -> Assistant:
    assistant = Assistant(
        user_id=owner.id,
        first_name=f"Overlay {suffix}",
        surname="Bot",
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


def _make_space(dbsession: Session, owner: User, suffix: str) -> Space:
    space = Space(
        name=f"Overlay Space {suffix}",
        owner_user_id=owner.id,
    )
    dbsession.add(space)
    dbsession.flush()
    return space


def _make_membership(
    *,
    assistant: Assistant,
    contact_id: int,
    target_scope: str,
    relationship: str = CONTACT_MEMBERSHIP_RELATIONSHIP_OTHER,
    target_space_id: int | None = None,
) -> ContactMembership:
    return ContactMembership(
        assistant_id=assistant.agent_id,
        contact_id=contact_id,
        target_scope=target_scope,
        target_space_id=target_space_id,
        relationship=relationship,
    )


def test_scope_polarity_constraint_accepts_consistent_targets(
    dbsession: Session,
) -> None:
    """Personal overlays omit a space, while space overlays name one."""
    owner = _make_user(dbsession, "polarity-valid")
    assistant = _make_assistant(dbsession, owner, "polarity-valid")
    space = _make_space(dbsession, owner, "polarity-valid")

    dbsession.add_all(
        [
            _make_membership(
                assistant=assistant,
                contact_id=1,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
            ),
            _make_membership(
                assistant=assistant,
                contact_id=2,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
                target_space_id=space.space_id,
            ),
        ],
    )
    dbsession.flush()


@pytest.mark.parametrize(
    ("target_scope", "uses_space"),
    [
        (CONTACT_MEMBERSHIP_SCOPE_PERSONAL, True),
        (CONTACT_MEMBERSHIP_SCOPE_SPACE, False),
    ],
)
def test_scope_polarity_constraint_rejects_inconsistent_targets(
    dbsession: Session,
    target_scope: str,
    uses_space: bool,
) -> None:
    """The database rejects overlays whose root discriminator is ambiguous."""
    owner = _make_user(dbsession, f"polarity-invalid-{target_scope}-{uses_space}")
    assistant = _make_assistant(dbsession, owner, f"polarity-{target_scope}")
    space = _make_space(dbsession, owner, f"polarity-{target_scope}")
    target_space_id = space.space_id if uses_space else None

    dbsession.add(
        _make_membership(
            assistant=assistant,
            contact_id=1,
            target_scope=target_scope,
            target_space_id=target_space_id,
        ),
    )

    with pytest.raises(
        IntegrityError,
        match="ck_contact_memberships_scope_space_consistency",
    ):
        dbsession.flush()


@pytest.mark.parametrize(
    "relationship",
    [
        CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
        CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
        CONTACT_MEMBERSHIP_RELATIONSHIP_COWORKER,
        CONTACT_MEMBERSHIP_RELATIONSHIP_OTHER,
    ],
)
def test_relationship_constraint_accepts_canonical_values(
    dbsession: Session,
    relationship: str,
) -> None:
    """Every contact relationship understood by managers is representable."""
    owner = _make_user(dbsession, f"relationship-{relationship}")
    assistant = _make_assistant(dbsession, owner, f"relationship-{relationship}")

    dbsession.add(
        _make_membership(
            assistant=assistant,
            contact_id=1,
            target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
            relationship=relationship,
        ),
    )
    dbsession.flush()


def test_relationship_constraint_rejects_unknown_values(dbsession: Session) -> None:
    """Unknown relationship labels cannot enter the overlay table."""
    owner = _make_user(dbsession, "relationship-invalid")
    assistant = _make_assistant(dbsession, owner, "relationship-invalid")

    dbsession.add(
        _make_membership(
            assistant=assistant,
            contact_id=1,
            target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
            relationship="spouse",
        ),
    )

    with pytest.raises(IntegrityError, match="ck_contact_memberships_relationship"):
        dbsession.flush()


def test_target_scope_constraint_rejects_unknown_values(dbsession: Session) -> None:
    """Only personal and space roots can be named by overlay rows."""
    owner = _make_user(dbsession, "target-scope-invalid")
    assistant = _make_assistant(dbsession, owner, "target-scope-invalid")

    dbsession.add(
        _make_membership(
            assistant=assistant,
            contact_id=1,
            target_scope="archived",
        ),
    )

    with pytest.raises(IntegrityError, match="ck_contact_memberships_target_scope"):
        dbsession.flush()


def test_personal_contact_memberships_are_unique_per_contact(
    dbsession: Session,
) -> None:
    """Personal overlays dedupe even though their target space is NULL."""
    owner = _make_user(dbsession, "unique-personal")
    assistant = _make_assistant(dbsession, owner, "unique-personal")
    dbsession.add(
        _make_membership(
            assistant=assistant,
            contact_id=42,
            target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
        ),
    )
    dbsession.flush()

    dbsession.add(
        _make_membership(
            assistant=assistant,
            contact_id=42,
            target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
        ),
    )

    with pytest.raises(IntegrityError, match="ux_contact_memberships_personal_pair"):
        dbsession.flush()


def test_space_contact_memberships_are_unique_per_contact_and_space(
    dbsession: Session,
) -> None:
    """Space overlays dedupe within the root named by target_space_id."""
    owner = _make_user(dbsession, "unique-space")
    assistant = _make_assistant(dbsession, owner, "unique-space")
    first_space = _make_space(dbsession, owner, "unique-space-a")
    second_space = _make_space(dbsession, owner, "unique-space-b")
    dbsession.add_all(
        [
            _make_membership(
                assistant=assistant,
                contact_id=42,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
                target_space_id=first_space.space_id,
            ),
            _make_membership(
                assistant=assistant,
                contact_id=42,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
                target_space_id=second_space.space_id,
            ),
        ],
    )
    dbsession.flush()

    dbsession.add(
        _make_membership(
            assistant=assistant,
            contact_id=42,
            target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
            target_space_id=first_space.space_id,
        ),
    )

    with pytest.raises(IntegrityError, match="ux_contact_memberships_space_pair"):
        dbsession.flush()


def test_space_delete_cascades_only_space_targeted_memberships(
    dbsession: Session,
) -> None:
    """Deleting a space drops only overlays pointing at that shared root."""
    owner = _make_user(dbsession, "space-cascade")
    assistant = _make_assistant(dbsession, owner, "space-cascade")
    removed_space = _make_space(dbsession, owner, "space-cascade-removed")
    retained_space = _make_space(dbsession, owner, "space-cascade-retained")
    dbsession.add_all(
        [
            _make_membership(
                assistant=assistant,
                contact_id=1,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
            ),
            _make_membership(
                assistant=assistant,
                contact_id=2,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
                target_space_id=removed_space.space_id,
            ),
            _make_membership(
                assistant=assistant,
                contact_id=3,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
                target_space_id=retained_space.space_id,
            ),
        ],
    )
    dbsession.flush()

    dbsession.execute(sa.delete(Space).where(Space.space_id == removed_space.space_id))
    dbsession.flush()

    remaining = dbsession.scalars(
        sa.select(ContactMembership.contact_id).order_by(ContactMembership.contact_id),
    ).all()
    assert remaining == [1, 3]


def test_assistant_delete_cascades_contact_memberships(dbsession: Session) -> None:
    """Deleting an assistant removes all of its personal and space overlays."""
    owner = _make_user(dbsession, "assistant-cascade")
    deleted_assistant = _make_assistant(dbsession, owner, "assistant-cascade-deleted")
    retained_assistant = _make_assistant(dbsession, owner, "assistant-cascade-retained")
    space = _make_space(dbsession, owner, "assistant-cascade")
    dbsession.add_all(
        [
            _make_membership(
                assistant=deleted_assistant,
                contact_id=1,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
            ),
            _make_membership(
                assistant=deleted_assistant,
                contact_id=2,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
                target_space_id=space.space_id,
            ),
            _make_membership(
                assistant=retained_assistant,
                contact_id=3,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
                target_space_id=space.space_id,
            ),
        ],
    )
    dbsession.flush()

    dbsession.execute(
        sa.delete(Assistant).where(Assistant.agent_id == deleted_assistant.agent_id),
    )
    dbsession.flush()

    remaining = dbsession.scalars(
        sa.select(ContactMembership.contact_id).order_by(ContactMembership.contact_id),
    ).all()
    assert remaining == [3]
