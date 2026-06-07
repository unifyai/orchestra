"""Schema tests for assistant contact membership overlays (team scope)."""

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
    CONTACT_MEMBERSHIP_SCOPE_TEAM,
    Assistant,
    ContactMembership,
    Organization,
    Team,
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


def _make_organization(dbsession: Session, owner: User, suffix: str) -> Organization:
    org = Organization(
        name=f"Overlay Org {suffix}",
        owner_id=owner.id,
    )
    dbsession.add(org)
    dbsession.flush()
    return org


def _make_team(dbsession: Session, owner: User, suffix: str) -> Team:
    org = _make_organization(dbsession, owner, suffix)
    team = Team(
        name=f"Overlay Team {suffix}",
        description=f"Shared contact overlay workspace for {suffix} tests.",
        organization_id=org.id,
    )
    dbsession.add(team)
    dbsession.flush()
    return team


def _make_membership(
    *,
    assistant: Assistant,
    contact_id: int,
    target_scope: str,
    relationship: str = CONTACT_MEMBERSHIP_RELATIONSHIP_OTHER,
    target_team_id: int | None = None,
    authoring_assistant_id: int | None = None,
) -> ContactMembership:
    return ContactMembership(
        assistant_id=assistant.agent_id,
        authoring_assistant_id=authoring_assistant_id,
        contact_id=contact_id,
        target_scope=target_scope,
        target_team_id=target_team_id,
        relationship=relationship,
    )


def test_scope_polarity_constraint_accepts_consistent_targets(
    dbsession: Session,
) -> None:
    """Personal overlays omit a team, while team overlays name one."""
    owner = _make_user(dbsession, "polarity-valid")
    assistant = _make_assistant(dbsession, owner, "polarity-valid")
    team = _make_team(dbsession, owner, "polarity-valid")

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
                target_scope=CONTACT_MEMBERSHIP_SCOPE_TEAM,
                target_team_id=team.id,
            ),
        ],
    )
    dbsession.flush()


@pytest.mark.parametrize(
    ("target_scope", "uses_team"),
    [
        (CONTACT_MEMBERSHIP_SCOPE_PERSONAL, True),
        (CONTACT_MEMBERSHIP_SCOPE_TEAM, False),
    ],
)
def test_scope_polarity_constraint_rejects_inconsistent_targets(
    dbsession: Session,
    target_scope: str,
    uses_team: bool,
) -> None:
    """The database rejects overlays whose root discriminator is ambiguous."""
    owner = _make_user(dbsession, f"polarity-invalid-{target_scope}-{uses_team}")
    assistant = _make_assistant(dbsession, owner, f"polarity-{target_scope}")
    team = _make_team(dbsession, owner, f"polarity-{target_scope}")
    target_team_id = team.id if uses_team else None

    dbsession.add(
        _make_membership(
            assistant=assistant,
            contact_id=1,
            target_scope=target_scope,
            target_team_id=target_team_id,
        ),
    )

    with pytest.raises(
        IntegrityError,
        match="ck_contact_memberships_scope_target_consistency",
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
    """Only personal and team roots can be named by overlay rows."""
    owner = _make_user(dbsession, "target-scope-invalid")
    assistant = _make_assistant(dbsession, owner, "target-scope-invalid")

    dbsession.add(
        _make_membership(
            assistant=assistant,
            contact_id=1,
            target_scope="archived",
        ),
    )

    with pytest.raises(IntegrityError, match="ck_contact_memberships"):
        dbsession.flush()


def test_authoring_assistant_delete_preserves_membership_row(
    dbsession: Session,
) -> None:
    """Authorship is retained when possible and nulled if the author is deleted."""

    owner = _make_user(dbsession, "authoring-set-null")
    assistant = _make_assistant(dbsession, owner, "authored-overlay-owner")
    author = _make_assistant(dbsession, owner, "authored-overlay-author")
    membership = _make_membership(
        assistant=assistant,
        authoring_assistant_id=author.agent_id,
        contact_id=7,
        target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    )
    dbsession.add(membership)
    dbsession.flush()

    membership_id = membership.id
    dbsession.delete(author)
    dbsession.flush()
    dbsession.expire_all()

    persisted = dbsession.get(ContactMembership, membership_id)
    assert persisted is not None
    assert persisted.assistant_id == assistant.agent_id
    assert persisted.authoring_assistant_id is None


def test_personal_contact_memberships_are_unique_per_contact(
    dbsession: Session,
) -> None:
    """Personal overlays dedupe even though their target team is NULL."""
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


def test_team_contact_memberships_are_unique_per_contact_and_team(
    dbsession: Session,
) -> None:
    """Team overlays dedupe within the root named by target_team_id."""
    owner = _make_user(dbsession, "unique-team")
    assistant = _make_assistant(dbsession, owner, "unique-team")
    first_team = _make_team(dbsession, owner, "unique-team-a")
    second_team = _make_team(dbsession, owner, "unique-team-b")
    dbsession.add_all(
        [
            _make_membership(
                assistant=assistant,
                contact_id=42,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_TEAM,
                target_team_id=first_team.id,
            ),
            _make_membership(
                assistant=assistant,
                contact_id=42,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_TEAM,
                target_team_id=second_team.id,
            ),
        ],
    )
    dbsession.flush()

    dbsession.add(
        _make_membership(
            assistant=assistant,
            contact_id=42,
            target_scope=CONTACT_MEMBERSHIP_SCOPE_TEAM,
            target_team_id=first_team.id,
        ),
    )

    with pytest.raises(IntegrityError, match="ux_contact_memberships_team_pair"):
        dbsession.flush()


def test_team_delete_cascades_only_team_targeted_memberships(
    dbsession: Session,
) -> None:
    """Deleting a team drops only overlays pointing at that shared root."""
    owner = _make_user(dbsession, "team-cascade")
    assistant = _make_assistant(dbsession, owner, "team-cascade")
    removed_team = _make_team(dbsession, owner, "team-cascade-removed")
    retained_team = _make_team(dbsession, owner, "team-cascade-retained")
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
                target_scope=CONTACT_MEMBERSHIP_SCOPE_TEAM,
                target_team_id=removed_team.id,
            ),
            _make_membership(
                assistant=assistant,
                contact_id=3,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_TEAM,
                target_team_id=retained_team.id,
            ),
        ],
    )
    dbsession.flush()

    dbsession.execute(sa.delete(Team).where(Team.id == removed_team.id))
    dbsession.flush()

    remaining = dbsession.scalars(
        sa.select(ContactMembership.contact_id).order_by(ContactMembership.contact_id),
    ).all()
    assert remaining == [1, 3]


def test_assistant_delete_cascades_contact_memberships(dbsession: Session) -> None:
    """Deleting an assistant removes all of its personal and team overlays."""
    owner = _make_user(dbsession, "assistant-cascade")
    deleted_assistant = _make_assistant(dbsession, owner, "assistant-cascade-deleted")
    retained_assistant = _make_assistant(dbsession, owner, "assistant-cascade-retained")
    team = _make_team(dbsession, owner, "assistant-cascade")
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
                target_scope=CONTACT_MEMBERSHIP_SCOPE_TEAM,
                target_team_id=team.id,
            ),
            _make_membership(
                assistant=retained_assistant,
                contact_id=3,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_TEAM,
                target_team_id=team.id,
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
