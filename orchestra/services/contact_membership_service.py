"""Helpers for provisioning assistant contact membership overlays."""

from collections.abc import Sequence

from sqlalchemy import or_, tuple_
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    CONTACT_MEMBERSHIP_SCOPE_SPACE,
    AssistantSpaceMembership,
    ContactMembership,
)

PERSONAL_SELF_CONTACT_ID = 0
PERSONAL_BOSS_CONTACT_ID = 1
BOSS_CONTACT_RESPONSE_POLICY = (
    "Your immediate manager, please do whatever they ask you to do within reason, "
    "and do *not* withhold any information from them."
)


def ensure_personal_contact_memberships(
    session: Session,
    assistant_ids: Sequence[int],
    *,
    repair_existing: bool = True,
) -> None:
    """Ensure assistants have personal self and boss contact overlays."""
    if not assistant_ids:
        return

    rows_by_assistant: dict[int, list[ContactMembership]] = {}
    if repair_existing:
        existing_rows = (
            session.query(ContactMembership)
            .filter(
                ContactMembership.assistant_id.in_(assistant_ids),
                ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                or_(
                    ContactMembership.contact_id.in_(
                        [PERSONAL_SELF_CONTACT_ID, PERSONAL_BOSS_CONTACT_ID],
                    ),
                    ContactMembership.relationship.in_(
                        [
                            CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
                            CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
                        ],
                    ),
                ),
            )
            .all()
        )
        for row in existing_rows:
            rows_by_assistant.setdefault(row.assistant_id, []).append(row)

    def row_with_contact(
        rows: list[ContactMembership],
        contact_id: int,
    ) -> ContactMembership | None:
        return next((row for row in rows if row.contact_id == contact_id), None)

    def has_relationship(rows: list[ContactMembership], relationship: str) -> bool:
        return any(row.relationship == relationship for row in rows)

    def has_relationship_outside_contact(
        rows: list[ContactMembership],
        relationship: str,
        contact_id: int,
    ) -> bool:
        return any(
            row.relationship == relationship and row.contact_id != contact_id
            for row in rows
        )

    def apply_membership_defaults(
        row: ContactMembership,
        *,
        relationship: str,
        response_policy: str,
    ) -> None:
        row.relationship = relationship
        row.should_respond = True
        row.response_policy = response_policy
        row.can_edit = True

    def membership_value(
        *,
        assistant_id: int,
        contact_id: int,
        relationship: str,
        response_policy: str,
    ) -> dict[str, object]:
        return {
            "assistant_id": assistant_id,
            "authoring_assistant_id": assistant_id,
            "contact_id": contact_id,
            "target_scope": CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
            "target_space_id": None,
            "relationship": relationship,
            "should_respond": True,
            "response_policy": response_policy,
            "can_edit": True,
        }

    membership_values = []
    for assistant_id in assistant_ids:
        rows = rows_by_assistant.get(assistant_id, [])
        default_self = row_with_contact(rows, PERSONAL_SELF_CONTACT_ID)
        if default_self is not None and not has_relationship_outside_contact(
            rows,
            CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
            PERSONAL_SELF_CONTACT_ID,
        ):
            apply_membership_defaults(
                default_self,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
                response_policy="",
            )

        default_boss = row_with_contact(rows, PERSONAL_BOSS_CONTACT_ID)
        if default_boss is not None and not has_relationship_outside_contact(
            rows,
            CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
            PERSONAL_BOSS_CONTACT_ID,
        ):
            apply_membership_defaults(
                default_boss,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
                response_policy=BOSS_CONTACT_RESPONSE_POLICY,
            )

        if not has_relationship(rows, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF):
            membership_values.append(
                membership_value(
                    assistant_id=assistant_id,
                    contact_id=PERSONAL_SELF_CONTACT_ID,
                    relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
                    response_policy="",
                ),
            )
        if not has_relationship(rows, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS):
            membership_values.append(
                membership_value(
                    assistant_id=assistant_id,
                    contact_id=PERSONAL_BOSS_CONTACT_ID,
                    relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
                    response_policy=BOSS_CONTACT_RESPONSE_POLICY,
                ),
            )

    if membership_values:
        stmt = postgres_insert(ContactMembership).values(membership_values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[
                ContactMembership.assistant_id,
                ContactMembership.contact_id,
            ],
            index_where=(
                ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL
            ),
        )
        session.execute(stmt)
    session.flush()


def ensure_space_contact_memberships(
    session: Session,
    assistant_space_pairs: Sequence[tuple[int, int]],
    *,
    repair_existing: bool = True,
) -> None:
    """Ensure assistants have space-scoped self and boss contact overlays."""
    if not assistant_space_pairs:
        return

    normalized_pairs = {
        (int(assistant_id), int(space_id))
        for assistant_id, space_id in assistant_space_pairs
    }
    if not normalized_pairs:
        return

    live_pairs = {
        (int(assistant_id), int(space_id))
        for assistant_id, space_id in session.query(
            AssistantSpaceMembership.assistant_id,
            AssistantSpaceMembership.space_id,
        )
        .filter(
            tuple_(
                AssistantSpaceMembership.assistant_id,
                AssistantSpaceMembership.space_id,
            ).in_(sorted(normalized_pairs)),
        )
        .all()
    }
    if not live_pairs:
        return

    rows_by_pair: dict[tuple[int, int], list[ContactMembership]] = {}
    if repair_existing:
        existing_rows = (
            session.query(ContactMembership)
            .filter(
                ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_SPACE,
                tuple_(
                    ContactMembership.assistant_id,
                    ContactMembership.target_space_id,
                ).in_(sorted(live_pairs)),
                or_(
                    ContactMembership.contact_id.in_(
                        [PERSONAL_SELF_CONTACT_ID, PERSONAL_BOSS_CONTACT_ID],
                    ),
                    ContactMembership.relationship.in_(
                        [
                            CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
                            CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
                        ],
                    ),
                ),
            )
            .all()
        )
        for row in existing_rows:
            if row.target_space_id is None:
                continue
            rows_by_pair.setdefault(
                (int(row.assistant_id), int(row.target_space_id)),
                [],
            ).append(row)

    def row_with_contact(
        rows: list[ContactMembership],
        contact_id: int,
    ) -> ContactMembership | None:
        return next((row for row in rows if row.contact_id == contact_id), None)

    def has_relationship(rows: list[ContactMembership], relationship: str) -> bool:
        return any(row.relationship == relationship for row in rows)

    def has_relationship_outside_contact(
        rows: list[ContactMembership],
        relationship: str,
        contact_id: int,
    ) -> bool:
        return any(
            row.relationship == relationship and row.contact_id != contact_id
            for row in rows
        )

    def apply_membership_defaults(
        row: ContactMembership,
        *,
        relationship: str,
        response_policy: str,
    ) -> None:
        row.relationship = relationship
        row.should_respond = True
        row.response_policy = response_policy
        row.can_edit = True

    def membership_value(
        *,
        assistant_id: int,
        space_id: int,
        contact_id: int,
        relationship: str,
        response_policy: str,
    ) -> dict[str, object]:
        return {
            "assistant_id": assistant_id,
            "authoring_assistant_id": assistant_id,
            "contact_id": contact_id,
            "target_scope": CONTACT_MEMBERSHIP_SCOPE_SPACE,
            "target_space_id": space_id,
            "relationship": relationship,
            "should_respond": True,
            "response_policy": response_policy,
            "can_edit": True,
        }

    membership_values = []
    for assistant_id, space_id in sorted(live_pairs):
        rows = rows_by_pair.get((assistant_id, space_id), [])

        default_self = row_with_contact(rows, PERSONAL_SELF_CONTACT_ID)
        if default_self is not None and not has_relationship_outside_contact(
            rows,
            CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
            PERSONAL_SELF_CONTACT_ID,
        ):
            apply_membership_defaults(
                default_self,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
                response_policy="",
            )

        default_boss = row_with_contact(rows, PERSONAL_BOSS_CONTACT_ID)
        if default_boss is not None and not has_relationship_outside_contact(
            rows,
            CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
            PERSONAL_BOSS_CONTACT_ID,
        ):
            apply_membership_defaults(
                default_boss,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
                response_policy=BOSS_CONTACT_RESPONSE_POLICY,
            )

        if not has_relationship(rows, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF):
            membership_values.append(
                membership_value(
                    assistant_id=assistant_id,
                    space_id=space_id,
                    contact_id=PERSONAL_SELF_CONTACT_ID,
                    relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
                    response_policy="",
                ),
            )
        if not has_relationship(rows, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS):
            membership_values.append(
                membership_value(
                    assistant_id=assistant_id,
                    space_id=space_id,
                    contact_id=PERSONAL_BOSS_CONTACT_ID,
                    relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
                    response_policy=BOSS_CONTACT_RESPONSE_POLICY,
                ),
            )

    if membership_values:
        stmt = postgres_insert(ContactMembership).values(membership_values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[
                ContactMembership.assistant_id,
                ContactMembership.contact_id,
                ContactMembership.target_space_id,
            ],
            index_where=(
                ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_SPACE
            ),
        )
        session.execute(stmt)
    session.flush()
