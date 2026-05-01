"""Data access helpers for spaces, memberships, and invitations."""

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.space_invite_dao import (
    SPACE_INVITE_STATUS_ACCEPTED,
    SPACE_INVITE_STATUS_CANCELLED,
    SPACE_INVITE_STATUS_DECLINED,
    SPACE_INVITE_STATUS_PENDING,
)
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantSpaceMembership,
    Organization,
    OrganizationMember,
    Space,
    SpaceInvite,
)

SPACE_STATUS_ACTIVE = "active"
SPACE_STATUS_DELETING = "deleting"


class SpaceDAO:
    """Owns relational space lifecycle and membership state transitions."""

    def __init__(self, session: Session):
        self.session = session
        self.resource_access_dao = ResourceAccessDAO(session)

    def create(
        self,
        *,
        name: str,
        owner_user_id: str,
        description: Optional[str] = None,
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

    def should_add_directly(
        self,
        *,
        user_id: str,
        space: Space,
        assistant: Assistant,
    ) -> bool:
        """Return whether a member add can materialize without owner approval."""

        if assistant.user_id == user_id:
            return True
        if (
            space.organization_id is not None
            and assistant.organization_id == space.organization_id
        ):
            return self.resource_access_dao.check_org_member_permission(
                user_id,
                space.organization_id,
                "org:write",
            )
        return False

    def create_or_refresh_invite(
        self,
        *,
        space: Space,
        assistant: Assistant,
        invited_by: str,
        expiry_days: int,
    ) -> tuple[SpaceInvite, bool]:
        """Create a pending invite or refresh the existing pending row."""

        expires_at = datetime.now(timezone.utc) + timedelta(days=expiry_days)
        existing = self.pending_invite_for_pair(
            space_id=space.space_id,
            assistant_id=assistant.agent_id,
        )
        statement = (
            insert(SpaceInvite.__table__)
            .values(
                space_id=space.space_id,
                assistant_id=assistant.agent_id,
                invited_by=invited_by,
                invited_owner_id=assistant.user_id,
                expires_at=expires_at,
            )
            .on_conflict_do_update(
                index_elements=[SpaceInvite.space_id, SpaceInvite.assistant_id],
                index_where=SpaceInvite.status == SPACE_INVITE_STATUS_PENDING,
                set_={
                    "expires_at": expires_at,
                    "invited_by": invited_by,
                    "invited_owner_id": assistant.user_id,
                },
            )
            .returning(SpaceInvite.invite_id)
        )
        invite_id = self.session.execute(statement).scalar_one()
        invite = self.get_invite(int(invite_id))
        assert invite is not None
        self.session.flush()
        return invite, existing is None

    def get_invite(self, invite_id: int) -> Optional[SpaceInvite]:
        """Return an invitation by primary key."""

        return self.session.get(SpaceInvite, invite_id)

    def pending_invite_for_pair(
        self,
        *,
        space_id: int,
        assistant_id: int,
    ) -> Optional[SpaceInvite]:
        """Return the pending invitation for a space and assistant pair."""

        return (
            self.session.query(SpaceInvite)
            .filter(
                SpaceInvite.space_id == space_id,
                SpaceInvite.assistant_id == assistant_id,
                SpaceInvite.status == SPACE_INVITE_STATUS_PENDING,
            )
            .one_or_none()
        )

    def list_invites_for_space(self, space_id: int) -> list[SpaceInvite]:
        """Return all invitations for a space."""

        return list(
            self.session.query(SpaceInvite)
            .filter(SpaceInvite.space_id == space_id)
            .order_by(SpaceInvite.created_at.desc())
            .all(),
        )

    def list_pending_invites_for_owner(self, user_id: str) -> list[SpaceInvite]:
        """Return unexpired pending invitations for an assistant owner."""

        now = datetime.now(timezone.utc)
        return list(
            self.session.query(SpaceInvite)
            .filter(
                SpaceInvite.invited_owner_id == user_id,
                SpaceInvite.status == SPACE_INVITE_STATUS_PENDING,
                SpaceInvite.expires_at >= now,
            )
            .order_by(SpaceInvite.created_at.desc())
            .all(),
        )

    def accept_invite(self, invite: SpaceInvite) -> SpaceInvite:
        """Accept an invitation and materialize the live membership."""

        membership = self.get_membership(
            space_id=invite.space_id,
            assistant_id=invite.assistant_id,
        )
        if membership is None:
            self.session.add(
                AssistantSpaceMembership(
                    assistant_id=invite.assistant_id,
                    space_id=invite.space_id,
                    added_by=invite.invited_by,
                ),
            )
        invite.status = SPACE_INVITE_STATUS_ACCEPTED
        invite.decided_at = datetime.now(timezone.utc)
        self.session.flush()
        return invite

    def decline_invite(self, invite: SpaceInvite) -> SpaceInvite:
        """Decline an invitation without creating a membership."""

        invite.status = SPACE_INVITE_STATUS_DECLINED
        invite.decided_at = datetime.now(timezone.utc)
        self.session.flush()
        return invite

    def cancel_invite(self, invite: SpaceInvite) -> SpaceInvite:
        """Cancel an invitation by its creator."""

        invite.status = SPACE_INVITE_STATUS_CANCELLED
        invite.decided_at = datetime.now(timezone.utc)
        self.session.flush()
        return invite

    def has_live_members_or_pending_invites(self, space_id: int) -> bool:
        """Return whether a space still has live members or pending invites."""

        membership_count = (
            self.session.query(sa.func.count())
            .select_from(AssistantSpaceMembership)
            .filter(AssistantSpaceMembership.space_id == space_id)
            .scalar()
        )
        if membership_count:
            return True
        pending_invite_count = (
            self.session.query(sa.func.count())
            .select_from(SpaceInvite)
            .filter(
                SpaceInvite.space_id == space_id,
                SpaceInvite.status == SPACE_INVITE_STATUS_PENDING,
            )
            .scalar()
        )
        return bool(pending_invite_count)

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
