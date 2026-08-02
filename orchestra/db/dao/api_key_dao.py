import base64
import secrets
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    CONSOLE_KEY_KIND,
    PROGRAMMATIC_KEY_KIND,
    ApiKey,
    User,
)

#: Name given to minted Console-session keys. Not shown anywhere; it
#: exists because ``api_key`` has a UNIQUE (user_id, name) constraint.
CONSOLE_KEY_NAME = "Console session"


def generate_key(size: int = 32) -> str:
    """Mint a fresh API key string."""
    buffer = secrets.token_bytes(size)
    key = base64.b64encode(buffer).decode("utf-8")
    # Replace forward slashes with hyphens to avoid issues with URL encoding
    return key.replace("/", "-")


class ApiKeyDAO:
    def __init__(self, session: Session):
        self.session = session

    def create(  # noqa: WPS211
        self,
        key: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        kind: str = PROGRAMMATIC_KEY_KIND,
    ) -> None:

        if user_id is None and organization_id is None:
            raise ValueError("One of user_id or organization_id must be provided.")

        self.session.add(
            ApiKey(
                name=name,
                user_id=user_id,
                organization_id=organization_id,
                key=key,
                kind=kind,
            ),
        )

    def filter(
        self,
        id: Optional[int] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        key: Optional[str] = None,
    ) -> List[ApiKey]:
        query = select(ApiKey)
        if id:
            query = query.where(ApiKey.id == id)
        if user_id:
            query = query.where(ApiKey.user_id == user_id)
        if organization_id:
            query = query.where(ApiKey.organization_id == organization_id)
        if key:
            query = query.where(ApiKey.key == key)
        rows = self.session.execute(query)
        return rows.fetchall()

    def get_user_id_and_mail(self, key):
        query = select(
            ApiKey.user_id,
            User.email,
            User.name,
            User.last_name,
            ApiKey.organization_id,
            ApiKey.kind,
        )
        query = query.join(User, ApiKey.user_id == User.id)
        query = query.where(ApiKey.key == key)
        rows = self.session.execute(query)
        return rows.fetchall()

    def get_console_key(
        self,
        user_id: str,
        organization_id: Optional[int] = None,
    ) -> Optional[ApiKey]:
        """The user's Console-session key for a workspace, if minted."""
        query = select(ApiKey).where(
            ApiKey.user_id == user_id,
            ApiKey.kind == CONSOLE_KEY_KIND,
        )
        if organization_id is None:
            query = query.where(ApiKey.organization_id.is_(None))
        else:
            query = query.where(ApiKey.organization_id == organization_id)
        return self.session.execute(query).scalars().first()

    def get_or_create_console_key(
        self,
        user_id: str,
        organization_id: Optional[int] = None,
    ) -> str:
        """Return this user's Console key for a workspace, minting if absent.

        One per (user, workspace): the Console swaps to the org-scoped key
        when the user is working inside an org, and that key has to be
        Console-kind too or every org request would look programmatic to
        the gates.

        Does not commit — the caller owns the transaction.
        """
        existing = self.get_console_key(user_id, organization_id)
        if existing is not None:
            return existing.key

        new_key = generate_key()
        name = CONSOLE_KEY_NAME
        if organization_id is not None:
            name = f"{CONSOLE_KEY_NAME} ({organization_id})"
        self.create(
            key=new_key,
            name=name,
            user_id=user_id,
            organization_id=organization_id,
            kind=CONSOLE_KEY_KIND,
        )
        self.session.flush()
        return new_key

    def update(
        self,
        id: int,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
    ) -> None:
        query = select(ApiKey)
        query = query.where(ApiKey.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)
            if user_id:
                setattr(entry, "user_id", user_id)
            if organization_id:
                setattr(entry, "organization_id", organization_id)

    def delete(self, id: int):
        try:
            api_key = self.session.query(ApiKey).filter_by(id=id).one()
            self.session.delete(api_key)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError

    def get_personal_keys(self, user_id: str) -> List[ApiKey]:
        """
        Get a user's personal *programmatic* keys (organization_id is NULL).

        Console-session keys are deliberately excluded. Every caller of
        this either lists keys for the user to see or picks one to act as
        the user off-platform, and a Console key must do neither — it is
        the credential whose scarcity makes ``kind`` mean anything.

        :param user_id: User ID to filter by.
        :return: List of personal programmatic API keys.
        """
        query = select(ApiKey)
        query = query.where(
            ApiKey.user_id == user_id,
            ApiKey.organization_id.is_(None),
            ApiKey.kind == PROGRAMMATIC_KEY_KIND,
        )
        rows = self.session.execute(query)
        return rows.fetchall()

    def get_organization_keys(
        self,
        user_id: str,
        organization_id: Optional[int] = None,
    ) -> List[ApiKey]:
        """
        Get organization API keys for a user.

        If organization_id is provided, returns keys for that specific organization.
        If organization_id is None, returns all organization keys for the user.

        :param user_id: User ID to filter by.
        :param organization_id: Optional organization ID to filter by.
        :return: List of organization API keys.
        """
        query = select(ApiKey)
        query = query.where(
            ApiKey.user_id == user_id,
            ApiKey.organization_id.is_not(None),
            # Console keys are excluded for the same reason as in
            # ``get_personal_keys``: they must never be listed or handed
            # out as a credential to act with off-platform.
            ApiKey.kind == PROGRAMMATIC_KEY_KIND,
        )
        if organization_id is not None:
            query = query.where(ApiKey.organization_id == organization_id)
        rows = self.session.execute(query)
        return rows.fetchall()

    def revoke_organization_keys(
        self,
        user_id: str,
        organization_id: int,
    ) -> int:
        """
        Revoke (delete) all organization API keys for a user in a specific organization.
        This is used when removing a user from an organization.

        :param user_id: User ID.
        :param organization_id: Organization ID.
        :return: Number of keys revoked.
        """
        keys = self.filter(user_id=user_id, organization_id=organization_id)
        count = 0
        for key_row in keys:
            api_key = key_row[0]
            self.session.delete(api_key)
            count += 1
        return count
