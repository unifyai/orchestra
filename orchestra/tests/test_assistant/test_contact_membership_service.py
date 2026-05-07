"""Tests for assistant contact membership provisioning helpers."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    CONTACT_MEMBERSHIP_RELATIONSHIP_COWORKER,
    CONTACT_MEMBERSHIP_RELATIONSHIP_OTHER,
    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    Assistant,
    ContactMembership,
    User,
)
from orchestra.services.contact_membership_service import (
    BOSS_CONTACT_RESPONSE_POLICY,
    ensure_personal_contact_memberships,
)


def _make_assistant(dbsession: Session, suffix: str) -> Assistant:
    user = User(
        id=f"contact-membership-service-{suffix}",
        email=f"contact-membership-service-{suffix}@test.com",
    )
    dbsession.add(user)
    dbsession.flush()

    assistant = Assistant(
        user_id=user.id,
        first_name="Contact",
        surname="Membership",
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


def _personal_memberships(
    dbsession: Session,
    assistant: Assistant,
) -> list[ContactMembership]:
    return dbsession.scalars(
        select(ContactMembership)
        .where(
            ContactMembership.assistant_id == assistant.agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
        )
        .order_by(ContactMembership.contact_id.asc()),
    ).all()


def test_personal_contact_membership_helper_repairs_default_rows(
    dbsession: Session,
) -> None:
    """Default personal root rows are canonicalized without duplicate inserts."""
    assistant = _make_assistant(dbsession, "default-repair")
    dbsession.add_all(
        [
            ContactMembership(
                assistant_id=assistant.agent_id,
                authoring_assistant_id=assistant.agent_id,
                contact_id=0,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_OTHER,
                should_respond=False,
                response_policy="stale self policy",
                can_edit=False,
            ),
            ContactMembership(
                assistant_id=assistant.agent_id,
                authoring_assistant_id=assistant.agent_id,
                contact_id=1,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_COWORKER,
                should_respond=False,
                response_policy="stale boss policy",
                can_edit=False,
            ),
        ],
    )
    dbsession.flush()

    ensure_personal_contact_memberships(dbsession, [assistant.agent_id])

    rows = _personal_memberships(dbsession, assistant)
    assert [(row.contact_id, row.relationship) for row in rows] == [
        (0, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF),
        (1, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS),
    ]
    assert rows[0].should_respond is True
    assert rows[0].response_policy == ""
    assert rows[0].can_edit is True
    assert rows[1].should_respond is True
    assert rows[1].response_policy == BOSS_CONTACT_RESPONSE_POLICY
    assert rows[1].can_edit is True


def test_personal_contact_membership_helper_preserves_custom_roots(
    dbsession: Session,
) -> None:
    """Custom self and boss roots remain authoritative when they already exist."""
    assistant = _make_assistant(dbsession, "custom-roots")
    dbsession.add_all(
        [
            ContactMembership(
                assistant_id=assistant.agent_id,
                authoring_assistant_id=assistant.agent_id,
                contact_id=42,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
                should_respond=True,
                response_policy="",
                can_edit=True,
            ),
            ContactMembership(
                assistant_id=assistant.agent_id,
                authoring_assistant_id=assistant.agent_id,
                contact_id=43,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
                should_respond=True,
                response_policy="custom boss policy",
                can_edit=True,
            ),
        ],
    )
    dbsession.flush()

    ensure_personal_contact_memberships(dbsession, [assistant.agent_id])

    assert [
        (row.contact_id, row.relationship, row.response_policy)
        for row in _personal_memberships(dbsession, assistant)
    ] == [
        (42, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF, ""),
        (43, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS, "custom boss policy"),
    ]
