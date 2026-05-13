"""Data access helpers for spaces and memberships."""

from typing import Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantSpaceMembership,
    Organization,
    OrganizationMember,
    Space,
)

SPACE_STATUS_ACTIVE = "active"
SPACE_STATUS_DELETING = "deleting"


class SpaceDAO:
    """Owns relational space lifecycle and membership state transitions."""

    def __init__(self, session: Session):
        self.session = session
        self.resource_access_dao = ResourceAccessDAO(session)

    @staticmethod
    def _normalize_name(value: str) -> str:
        """Normalize space name text for case-insensitive key matching."""
        return value.strip().lower()

    def create(
        self,
        *,
        name: str,
        owner_user_id: str,
        description: str,
        organization_id: Optional[int] = None,
    ) -> Space:
        """Create a space owned by ``owner_user_id``."""

        space = Space(
            name=name,
            description=description,
            organization_id=organization_id,
            owner_user_id=owner_user_id,
        )
        self.session.add(space)
        self.session.flush()
        return space

    def find_team_space_by_natural_key(
        self,
        *,
        owner_user_id: str,
        organization_id: Optional[int],
        name: str,
    ) -> Optional[Space]:
        """Return a team space that already matches the natural key."""
        normalized_name = self._normalize_name(name)
        query = self.session.query(Space).filter(
            Space.kind == "team",
            sa.func.lower(sa.func.trim(Space.name)) == normalized_name,
        )
        if organization_id is None:
            query = query.filter(
                Space.organization_id.is_(None),
                Space.owner_user_id == owner_user_id,
            )
        else:
            query = query.filter(Space.organization_id == organization_id)
        return query.order_by(Space.created_at.asc(), Space.space_id.asc()).first()

    def get(self, space_id: int) -> Optional[Space]:
        """Return a space by primary key."""

        return self.session.get(Space, space_id)

    def list_visible(self, user_id: str) -> list[Space]:
        """Return spaces the user owns personally or can read through an org."""

        organization_ids = self._readable_organization_ids(user_id)
        query = self.session.query(Space).filter(
            sa.or_(
                Space.owner_user_id == user_id,
                (
                    Space.organization_id.in_(organization_ids)
                    if organization_ids
                    else sa.false()
                ),
            ),
        )
        return list(
            query.order_by(Space.created_at.desc(), Space.space_id.desc()).all(),
        )

    def update(
        self,
        space: Space,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Space:
        """Update mutable display fields for a space."""

        if name is not None:
            space.name = name
        if description is not None:
            space.description = description
        self.session.flush()
        return space

    def delete(self, space: Space) -> None:
        """Delete an empty space."""

        self.session.delete(space)
        self.session.flush()

    def can_read(self, user_id: str, space: Space) -> bool:
        """Return whether ``user_id`` can view a space."""

        if space.owner_user_id == user_id:
            return True
        if space.organization_id is None:
            return False
        return self.resource_access_dao.check_org_member_permission(
            user_id,
            space.organization_id,
            "org:read",
        )

    def can_mutate(self, user_id: str, space: Space) -> bool:
        """Return whether ``user_id`` can administer a space."""

        if space.owner_user_id == user_id:
            return True
        if space.organization_id is None:
            return False
        return self.resource_access_dao.check_org_member_permission(
            user_id,
            space.organization_id,
            "org:write",
        )

    def can_create_in_organization(
        self,
        user_id: str,
        organization_id: int,
    ) -> bool:
        """Return whether the user can create spaces in an organization."""

        return self.resource_access_dao.check_org_member_permission(
            user_id,
            organization_id,
            "org:write",
        )

    def get_assistant(self, assistant_id: int) -> Optional[Assistant]:
        """Return an assistant by primary key."""

        return self.session.get(Assistant, assistant_id)

    def get_membership(
        self,
        *,
        space_id: int,
        assistant_id: int,
    ) -> Optional[AssistantSpaceMembership]:
        """Return a live membership for a space and assistant pair."""

        return self.session.get(
            AssistantSpaceMembership,
            {"assistant_id": assistant_id, "space_id": space_id},
        )

    def add_membership(
        self,
        *,
        space: Space,
        assistant: Assistant,
        added_by: str,
    ) -> AssistantSpaceMembership:
        """Materialize a live assistant membership in a space."""

        membership = AssistantSpaceMembership(
            assistant_id=assistant.agent_id,
            space_id=space.space_id,
            added_by=added_by,
        )
        self.session.add(membership)
        self.session.flush()
        return membership

    def list_members(
        self,
        space_id: int,
    ) -> list[tuple[AssistantSpaceMembership, Assistant]]:
        """Return live members for a space with their assistant rows."""

        rows = (
            self.session.query(AssistantSpaceMembership, Assistant)
            .join(
                Assistant,
                Assistant.agent_id == AssistantSpaceMembership.assistant_id,
            )
            .filter(AssistantSpaceMembership.space_id == space_id)
            .order_by(AssistantSpaceMembership.created_at.asc())
            .all()
        )
        return list(rows)

    def list_spaces_for_assistant(self, assistant_id: int) -> list[Space]:
        """Return spaces where an assistant is a live member."""

        return list(
            self.session.query(Space)
            .join(
                AssistantSpaceMembership,
                AssistantSpaceMembership.space_id == Space.space_id,
            )
            .filter(
                AssistantSpaceMembership.assistant_id == assistant_id,
                Space.status == SPACE_STATUS_ACTIVE,
            )
            .order_by(Space.space_id.asc())
            .all(),
        )

    def space_ids_for_assistant(self, assistant_id: int) -> list[int]:
        """Return sorted live space ids for an assistant."""

        rows = (
            self.session.query(Space.space_id)
            .join(
                AssistantSpaceMembership,
                AssistantSpaceMembership.space_id == Space.space_id,
            )
            .filter(
                AssistantSpaceMembership.assistant_id == assistant_id,
                Space.status == SPACE_STATUS_ACTIVE,
            )
            .order_by(Space.space_id.asc())
            .all()
        )
        return [int(row[0]) for row in rows]

    def space_ids_for_assistants(
        self,
        assistant_ids: Iterable[int],
    ) -> dict[int, list[int]]:
        """Return sorted live space ids keyed by assistant id."""

        ids = list(assistant_ids)
        if not ids:
            return {}
        rows = (
            self.session.query(
                AssistantSpaceMembership.assistant_id,
                Space.space_id,
            )
            .join(
                Space,
                Space.space_id == AssistantSpaceMembership.space_id,
            )
            .filter(AssistantSpaceMembership.assistant_id.in_(ids))
            .filter(Space.status == SPACE_STATUS_ACTIVE)
            .order_by(
                AssistantSpaceMembership.assistant_id.asc(),
                Space.space_id.asc(),
            )
            .all()
        )
        memberships: dict[int, list[int]] = {assistant_id: [] for assistant_id in ids}
        for assistant_id, space_id in rows:
            memberships.setdefault(int(assistant_id), []).append(int(space_id))
        return memberships

    def space_summaries_for_assistant(
        self,
        assistant_id: int,
    ) -> list[dict[str, int | str]]:
        """Return sorted live space summaries for an assistant."""

        return self.space_summaries_for_assistants([assistant_id]).get(
            assistant_id,
            [],
        )

    def space_summaries_for_assistants(
        self,
        assistant_ids: Iterable[int],
    ) -> dict[int, list[dict[str, int | str]]]:
        """Return sorted live space summaries keyed by assistant id."""

        ids = list(assistant_ids)
        if not ids:
            return {}
        rows = (
            self.session.query(
                AssistantSpaceMembership.assistant_id,
                Space.space_id,
                Space.name,
                Space.description,
            )
            .join(
                Space,
                Space.space_id == AssistantSpaceMembership.space_id,
            )
            .filter(AssistantSpaceMembership.assistant_id.in_(ids))
            .filter(Space.status == SPACE_STATUS_ACTIVE)
            .order_by(
                AssistantSpaceMembership.assistant_id.asc(),
                Space.space_id.asc(),
            )
            .all()
        )
        memberships: dict[int, list[dict[str, int | str]]] = {
            assistant_id: [] for assistant_id in ids
        }
        for assistant_id, space_id, name, description in rows:
            memberships.setdefault(int(assistant_id), []).append(
                {
                    "space_id": int(space_id),
                    "name": str(name),
                    "description": str(description),
                },
            )
        return memberships

    def has_live_members(self, space_id: int) -> bool:
        """Return whether a space still has live members."""

        membership_count = (
            self.session.query(sa.func.count())
            .select_from(AssistantSpaceMembership)
            .filter(AssistantSpaceMembership.space_id == space_id)
            .scalar()
        )
        return bool(membership_count)

    def _readable_organization_ids(self, user_id: str) -> list[int]:
        """Return organization ids where the user has org read access."""

        owned_ids = (
            self.session.query(Organization.id)
            .filter(Organization.owner_id == user_id)
            .all()
        )
        member_ids = (
            self.session.query(OrganizationMember.organization_id)
            .filter(OrganizationMember.user_id == user_id)
            .all()
        )
        organization_ids = {int(row[0]) for row in owned_ids + member_ids}
        return sorted(
            organization_id
            for organization_id in organization_ids
            if self.resource_access_dao.check_org_member_permission(
                user_id,
                organization_id,
                "org:read",
            )
        )
