"""Data Access Object for OrganizationInvite model."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import OrganizationInvite


class OrganizationInviteDAO:
    """DAO for managing organization invitations.

    Invites are deleted when accepted or declined.
    Only pending (non-expired) invites exist in the database.
    """

    def __init__(self, session: Session):
        self.session = session

    def generate_token(self) -> str:
        """Generate a unique token for the invite."""
        return str(uuid.uuid4())

    def create(
        self,
        organization_id: int,
        invitee_email: str,
        invited_by_user_id: str,
        role_id: int,
        expires_in_days: int = 7,
        invitee_user_id: Optional[str] = None,
    ) -> OrganizationInvite:
        """
        Create a new organization invite.

        :param organization_id: Organization to invite to.
        :param invitee_email: Email of the person being invited.
        :param invited_by_user_id: User ID of the person sending the invite.
        :param role_id: Role to assign when invite is accepted.
        :param expires_in_days: Number of days until invite expires.
        :param invitee_user_id: User ID if invitee already exists in system.
        :return: The created OrganizationInvite object.
        """
        token = self.generate_token()
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)

        invite = OrganizationInvite(
            token=token,
            organization_id=organization_id,
            invitee_email=invitee_email.lower(),  # Normalize email
            invitee_user_id=invitee_user_id,
            invited_by_user_id=invited_by_user_id,
            role_id=role_id,
            expires_at=expires_at,
        )
        self.session.add(invite)
        self.session.flush()
        return invite

    def get_by_id(self, invite_id: str) -> Optional[OrganizationInvite]:
        """Get an invite by its ID."""
        query = select(OrganizationInvite).where(OrganizationInvite.id == invite_id)
        return self.session.execute(query).scalar_one_or_none()

    def get_by_token(self, token: str) -> Optional[OrganizationInvite]:
        """Get an invite by its token."""
        query = select(OrganizationInvite).where(OrganizationInvite.token == token)
        return self.session.execute(query).scalar_one_or_none()

    def get_by_email_and_org(
        self,
        email: str,
        organization_id: int,
    ) -> Optional[OrganizationInvite]:
        """Get an invite for a specific email and organization."""
        query = select(OrganizationInvite).where(
            OrganizationInvite.invitee_email == email.lower(),
            OrganizationInvite.organization_id == organization_id,
        )
        return self.session.execute(query).scalar_one_or_none()

    def list_by_organization(
        self,
        organization_id: int,
        include_expired: bool = False,
    ) -> List[OrganizationInvite]:
        """
        List invites for an organization.

        :param organization_id: Organization ID.
        :param include_expired: Whether to include expired invites.
        :return: List of invites.
        """
        query = select(OrganizationInvite).where(
            OrganizationInvite.organization_id == organization_id,
        )

        if not include_expired:
            now = datetime.now(timezone.utc)
            query = query.where(OrganizationInvite.expires_at >= now)

        query = query.order_by(OrganizationInvite.created_at.desc())
        return list(self.session.execute(query).scalars().all())

    def list_by_email(self, email: str) -> List[OrganizationInvite]:
        """
        List all non-expired invites for an email address.

        :param email: Email address to search for.
        :return: List of invites.
        """
        now = datetime.now(timezone.utc)
        query = (
            select(OrganizationInvite)
            .where(
                OrganizationInvite.invitee_email == email.lower(),
                OrganizationInvite.expires_at >= now,
            )
            .order_by(OrganizationInvite.created_at.desc())
        )
        return list(self.session.execute(query).scalars().all())

    def delete(self, invite_id: str) -> bool:
        """
        Delete an invite by ID.

        :param invite_id: The invite ID to delete.
        :return: True if deleted, False if not found.
        """
        invite = self.get_by_id(invite_id)
        if invite:
            self.session.delete(invite)
            self.session.flush()
            return True
        return False

    def delete_invite(self, invite: OrganizationInvite) -> None:
        """
        Delete an invite object directly.

        :param invite: The invite to delete.
        """
        self.session.delete(invite)
        self.session.flush()

    def cleanup_expired_invites(self) -> int:
        """
        Delete expired invites.

        :return: Number of invites deleted.
        """
        now = datetime.now(timezone.utc)
        stmt = delete(OrganizationInvite).where(OrganizationInvite.expires_at < now)
        result = self.session.execute(stmt)
        self.session.flush()
        return result.rowcount

    def is_valid_for_acceptance(self, invite: OrganizationInvite) -> tuple[bool, str]:
        """
        Check if an invite is valid for acceptance.

        :param invite: The invite to check.
        :return: Tuple of (is_valid, error_message).
        """
        now = datetime.now(timezone.utc)
        if invite.expires_at < now:
            return False, "Invite has expired"

        return True, ""
