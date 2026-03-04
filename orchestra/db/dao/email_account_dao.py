"""
Data Access Object for EmailAccount (email/password credentials).
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import EmailAccount


class EmailAccountDAO:
    """DAO for email/password authentication credentials."""

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        user_id: str,
        password_hash: str,
        email_verified: bool = True,
    ) -> EmailAccount:
        """
        Create an EmailAccount for a user.

        :param user_id: The user's ID (FK to User.id).
        :param password_hash: The argon2id password hash.
        :param email_verified: Whether the email is verified (default True after verification flow).
        :return: The created EmailAccount instance.
        """
        email_account = EmailAccount(
            user_id=user_id,
            password_hash=password_hash,
            email_verified=email_verified,
        )
        self.session.add(email_account)
        return email_account

    def get_by_user_id(self, user_id: str) -> Optional[EmailAccount]:
        """
        Get an EmailAccount by user ID.

        :param user_id: The user's ID.
        :return: EmailAccount instance or None.
        """
        query = select(EmailAccount).where(EmailAccount.user_id == user_id)
        return self.session.execute(query).scalars().first()

    def update_password(
        self,
        user_id: str,
        new_password_hash: str,
    ) -> Optional[EmailAccount]:
        """
        Update the password hash and set password_changed_at for session invalidation.

        :param user_id: The user's ID.
        :param new_password_hash: The new argon2id password hash.
        :return: The updated EmailAccount, or None if not found.
        """
        email_account = self.get_by_user_id(user_id)
        if email_account is None:
            return None
        email_account.password_hash = new_password_hash
        email_account.password_changed_at = datetime.now(timezone.utc)
        return email_account

    def delete_by_user_id(self, user_id: str) -> bool:
        """
        Delete an EmailAccount by user ID.

        :param user_id: The user's ID.
        :return: True if deleted, False if not found.
        """
        email_account = self.get_by_user_id(user_id)
        if email_account is None:
            return False
        self.session.delete(email_account)
        return True
