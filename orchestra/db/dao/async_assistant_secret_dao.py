"""Async version of assistant_secret_dao for use with AsyncSession."""

from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import AssistantSecret


class AsyncAssistantSecretDAO:
    """
    Data access object for AssistantSecret operations.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _get_secret(
        self,
        user_id: str,
        agent_id: int,
        secret_name: str,
    ) -> Optional[AssistantSecret]:
        """
        Internal method to retrieve a specific secret by name.

        :param user_id: The user ID.
        :param agent_id: The assistant's agent ID.
        :param secret_name: The name of the secret to retrieve.
        :return: AssistantSecret if found, None otherwise.
        """
        stmt = select(AssistantSecret).where(
            AssistantSecret.user_id == user_id,
            AssistantSecret.agent_id == agent_id,
            AssistantSecret.secret_name == secret_name,
        )
        return await self.session.execute(stmt).scalar_one_or_none()

    async def create_secret(
        self,
        user_id: str,
        agent_id: int,
        secret_name: str,
        secret_value: str,
        description: Optional[str] = None,
    ) -> AssistantSecret:
        """
        Create a new secret for an assistant.

        :param user_id: The user ID who owns the secret.
        :param agent_id: The assistant's agent ID.
        :param secret_name: Unique name/key for the secret (e.g., 'openai_api_key').
        :param secret_value: The secret value (should be encrypted at rest).
        :param description: Optional description of what this secret is for.
        :return: The created AssistantSecret.
        """
        secret = AssistantSecret(
            user_id=user_id,
            agent_id=agent_id,
            secret_name=secret_name,
            secret_value=secret_value,
            description=description,
        )
        self.session.add(secret)
        await self.session.flush()
        return secret

    async def list_secrets(
        self,
        user_id: str,
        agent_id: int,
    ) -> List[AssistantSecret]:
        """
        List all secrets for a specific assistant.

        :param user_id: The user ID.
        :param agent_id: The assistant's agent ID.
        :return: List of AssistantSecret objects.
        """
        stmt = select(AssistantSecret).where(
            AssistantSecret.user_id == user_id,
            AssistantSecret.agent_id == agent_id,
        )
        return await self.session.execute(stmt).scalars().all()

    async def update_secret(
        self,
        user_id: str,
        agent_id: int,
        secret_name: str,
        secret_value: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[AssistantSecret]:
        """
        Update an existing secret.

        :param user_id: The user ID.
        :param agent_id: The assistant's agent ID.
        :param secret_name: The name of the secret to update.
        :param secret_value: New secret value (if provided).
        :param description: New description (if provided).
        :return: Updated AssistantSecret or None if not found.
        """
        secret = self._get_secret(user_id, agent_id, secret_name)
        if not secret:
            return None

        if secret_value is not None:
            secret.secret_value = secret_value
        if description is not None:
            secret.description = description

        self.session.add(secret)
        await self.session.flush()
        return secret

    async def delete_secret(
        self,
        user_id: str,
        agent_id: int,
        secret_name: str,
    ) -> None:
        """
        Delete a secret.

        :param user_id: The user ID.
        :param agent_id: The assistant's agent ID.
        :param secret_name: The name of the secret to delete.
        :raises HTTPException: If secret not found.
        """
        secret = self._get_secret(user_id, agent_id, secret_name)
        if not secret:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Secret not found.",
            )
        await self.session.delete(secret)
