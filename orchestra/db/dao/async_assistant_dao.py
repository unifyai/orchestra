"""Async version of assistant_dao for use with AsyncSession."""

from decimal import Decimal
from typing import Any, Dict, List, Optional
from zoneinfo import available_timezones

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import Assistant

VALID_TIMEZONES = available_timezones()


class AsyncAssistantDAO:
    """
    Data access object for Assistant operations.

    Supports both personal assistants (organization_id is NULL) and
    organizational assistants (organization_id is set).
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_assistant(
        self,
        user_id: str,
        first_name: Optional[str],
        surname: Optional[str],
        age: Optional[int],
        nationality: Optional[str],
        about: Optional[str],
        weekly_limit: Optional[Decimal],
        max_parallel: Optional[int],
        profile_photo: Optional[str] = None,
        profile_video: Optional[str] = None,
        desktop_url: Optional[str] = None,
        user_local_desktop: Optional[str] = None,
        phone: Optional[str] = None,
        phone_country: Optional[str] = None,
        user_phone: Optional[str] = None,
        email: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        voice_id: Optional[str] = None,
        voice_provider: Optional[str] = None,
        voice_mode: Optional[str] = None,
        timezone: Optional[str] = None,
        organization_id: Optional[int] = None,
    ) -> Assistant:
        """
        Create a new Assistant.

        If organization_id is provided, creates an organizational assistant.
        If organization_id is None, creates a personal assistant.

        :param user_id: The user ID (creator for org assistants, owner for personal).
        :param organization_id: Optional organization ID for org assistants.
        :return: The created Assistant.
        """

        if timezone is not None and timezone not in VALID_TIMEZONES:
            raise ValueError(f"'{timezone}' is not a valid IANA timezone.")

        assistant = Assistant(
            user_id=user_id,
            organization_id=organization_id,
            first_name=first_name,
            surname=surname,
            age=age,
            nationality=nationality,
            profile_photo=profile_photo,
            profile_video=profile_video,
            desktop_url=desktop_url,
            user_local_desktop=user_local_desktop,
            about=about,
            weekly_limit=weekly_limit,
            max_parallel=max_parallel,
            phone=phone,
            user_phone=user_phone,
            email=email,
            user_whatsapp_number=user_whatsapp_number,
            voice_id=voice_id,
            voice_provider=voice_provider,
            voice_mode=voice_mode,
            phone_country=phone_country,
            timezone=timezone,
        )
        self.session.add(assistant)
        await self.session.flush()
        return assistant

    async def get_assistant_by_id(
        self,
        user_id: str,
        agent_id: int,
        organization_id: Optional[int] = None,
    ) -> Optional[Assistant]:
        """
        Retrieve an Assistant by agent ID.

        For personal assistants (organization_id=None):
            Returns assistant if user_id matches and organization_id is NULL.

        For org assistants (organization_id is set):
            Returns assistant if organization_id matches.
            The user_id check is skipped since org members may access org assistants.

        :param user_id: User ID (used for personal assistant lookup).
        :param agent_id: Assistant agent ID.
        :param organization_id: Organization ID for org context (None = personal).
        :return: Assistant if found, None otherwise.
        """
        if organization_id is not None:
            # Org context: find by agent_id and organization_id
            stmt = select(Assistant).where(
                Assistant.agent_id == agent_id,
                Assistant.organization_id == organization_id,
            )
        else:
            # Personal context: find by agent_id, user_id, and organization_id is NULL
            stmt = select(Assistant).where(
                Assistant.agent_id == agent_id,
                Assistant.user_id == user_id,
                Assistant.organization_id.is_(None),
            )
        result = (await self.session.execute(stmt)).scalar_one_or_none()
        return result

    async def get_assistant_by_agent_id(self, agent_id: int) -> Optional[Assistant]:
        """
        Retrieve an Assistant by agent ID only (no user/org context).

        Used for internal operations like transfers where we need to fetch
        the assistant regardless of current API key context.

        :param agent_id: Assistant agent ID.
        :return: Assistant if found, None otherwise.
        """
        stmt = select(Assistant).where(Assistant.agent_id == agent_id)
        result = (await self.session.execute(stmt)).scalar_one_or_none()
        return result

    async def list_assistants_for_user(
        self,
        user_id: str,
        organization_id: Optional[int] = None,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        email: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        assistant_whatsapp_number: Optional[str] = None,
    ) -> List[Assistant]:
        """
        List assistants accessible to a user based on API key context.

        For personal API key (organization_id=None):
            Returns only personal assistants (organization_id IS NULL)
            where user_id matches.

        For org API key (organization_id is set):
            Returns only assistants in that org where user_id matches
            (assistants created by this user in the org).

        :param user_id: User ID.
        :param organization_id: Organization ID from API key context (None = personal).
        :return: List of assistants.
        """
        if organization_id is not None:
            # Org context: user's assistants in this org
            stmt = select(Assistant).where(
                Assistant.user_id == user_id,
                Assistant.organization_id == organization_id,
            )
        else:
            # Personal context: only personal assistants
            stmt = select(Assistant).where(
                Assistant.user_id == user_id,
                Assistant.organization_id.is_(None),
            )

        if phone is not None:
            stmt = stmt.where(Assistant.phone == phone)
        if user_phone is not None:
            stmt = stmt.where(Assistant.user_phone == user_phone)
        if email is not None:
            stmt = stmt.where(Assistant.email == email)
        if user_whatsapp_number is not None:
            stmt = stmt.where(Assistant.user_whatsapp_number == user_whatsapp_number)
        if assistant_whatsapp_number is not None:
            stmt = stmt.where(
                Assistant.assistant_whatsapp_number == assistant_whatsapp_number,
            )
        result = (await self.session.execute(stmt)).scalars().all()
        return result

    async def list_all_org_assistants(
        self,
        organization_id: int,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        email: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        assistant_whatsapp_number: Optional[str] = None,
    ) -> List[Assistant]:
        """
        List ALL assistants in an organization (for list_all_org=True).

        This returns all assistants in the org, regardless of who created them.
        Should only be called after verifying the user has assistant:read permission.

        :param organization_id: Organization ID.
        :return: List of all assistants in the organization.
        """
        stmt = select(Assistant).where(
            Assistant.organization_id == organization_id,
        )

        if phone is not None:
            stmt = stmt.where(Assistant.phone == phone)
        if user_phone is not None:
            stmt = stmt.where(Assistant.user_phone == user_phone)
        if email is not None:
            stmt = stmt.where(Assistant.email == email)
        if user_whatsapp_number is not None:
            stmt = stmt.where(Assistant.user_whatsapp_number == user_whatsapp_number)
        if assistant_whatsapp_number is not None:
            stmt = stmt.where(
                Assistant.assistant_whatsapp_number == assistant_whatsapp_number,
            )
        result = (await self.session.execute(stmt)).scalars().all()
        return result

    async def delete_assistant(
        self,
        user_id: str,
        agent_id: int,
        organization_id: Optional[int] = None,
    ) -> None:
        """
        Delete an Assistant.

        For personal assistants: requires user_id match.
        For org assistants: requires organization_id match.
        Permission checks should be done at the API layer.

        :param user_id: User ID (for personal assistants).
        :param agent_id: Assistant agent ID.
        :param organization_id: Organization ID for org context (None = personal).
        """
        assistant = await self.get_assistant_by_id(user_id, agent_id, organization_id)
        if assistant:
            await self.session.delete(assistant)
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

    async def update_assistant(
        self,
        user_id: str,
        agent_id: int,
        update_data: Dict[str, Any],
        organization_id: Optional[int] = None,
    ) -> Optional[Assistant]:
        """
        Update configuration for an existing Assistant.

        :param user_id: User ID.
        :param agent_id: Assistant agent ID.
        :param update_data: Dictionary of fields to update.
        :param organization_id: Organization ID for org context (None = personal).
        :return: Updated assistant or None if not found.
        """
        assistant = await self.get_assistant_by_id(user_id, agent_id, organization_id)
        if not assistant:
            return None

        if "timezone" in update_data:
            tz = update_data["timezone"]
            if tz is not None and tz not in VALID_TIMEZONES:
                raise ValueError(f"'{tz}' is not a valid IANA timezone.")

        # Track changes for contact sync
        should_sync_timezone = False
        should_sync_bio = False

        if "timezone" in update_data:
            old_timezone = assistant.timezone
            new_timezone = update_data["timezone"]
            if new_timezone != old_timezone:
                should_sync_timezone = True

        if "about" in update_data:
            old_about = assistant.about
            new_about = update_data["about"]
            if new_about != old_about:
                should_sync_bio = True

        for key, value in update_data.items():
            setattr(assistant, key, value)

        self.session.add(assistant)
        await self.session.flush()

        # Sync timezone/bio changes to Contact logs in Assistants project
        if should_sync_timezone or should_sync_bio:
            from orchestra.services.contact_sync_service import ContactSyncService

            sync_service = ContactSyncService(self.session)
            if should_sync_timezone:
                sync_service.sync_assistant_timezone(
                    user_id=user_id,
                    organization_id=organization_id,
                    first_name=assistant.first_name,
                    surname=assistant.surname,
                    new_timezone=assistant.timezone,
                )
            if should_sync_bio:
                sync_service.sync_assistant_bio(
                    user_id=user_id,
                    organization_id=organization_id,
                    first_name=assistant.first_name,
                    surname=assistant.surname,
                    new_bio=assistant.about,
                )

        return assistant

    async def transfer_to_organization(
        self,
        agent_id: int,
        user_id: str,
        organization_id: int,
    ) -> Optional[Assistant]:
        """
        Transfer a personal assistant to an organization.

        :param agent_id: Assistant agent ID.
        :param user_id: Current owner's user ID.
        :param organization_id: Target organization ID.
        :return: Updated assistant or None if not found.
        """
        # Get the personal assistant
        assistant = await self.get_assistant_by_id(user_id, agent_id, organization_id=None)
        if not assistant:
            return None

        # Transfer to organization
        assistant.organization_id = organization_id
        self.session.add(assistant)
        await self.session.flush()
        return assistant

    async def transfer_to_personal(
        self,
        agent_id: int,
        organization_id: int,
        new_owner_user_id: str,
    ) -> Optional[Assistant]:
        """
        Transfer an organizational assistant to personal workspace.

        :param agent_id: Assistant agent ID.
        :param organization_id: Current organization ID.
        :param new_owner_user_id: User ID who will own the personal assistant.
        :return: Updated assistant or None if not found.
        """
        # Get the org assistant (use any user_id since we're checking by org)
        stmt = select(Assistant).where(
            Assistant.agent_id == agent_id,
            Assistant.organization_id == organization_id,
        )
        assistant = (await self.session.execute(stmt)).scalar_one_or_none()
        if not assistant:
            return None

        # Transfer to personal
        assistant.organization_id = None
        assistant.user_id = new_owner_user_id
        self.session.add(assistant)
        await self.session.flush()
        return assistant

    async def list_all_assistants(
        self,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        assistant_whatsapp_number: Optional[str] = None,
        email: Optional[str] = None,
        agent_id: Optional[int] = None,
    ) -> List[Assistant]:
        """
        List all Assistants across all users with optional filtering.

        This is an admin-level function that returns all assistants.
        """
        stmt = select(Assistant)
        if phone is not None:
            stmt = stmt.where(Assistant.phone == phone)
        if user_phone is not None:
            stmt = stmt.where(Assistant.user_phone == user_phone)
        if user_whatsapp_number is not None:
            stmt = stmt.where(Assistant.user_whatsapp_number == user_whatsapp_number)
        if assistant_whatsapp_number is not None:
            stmt = stmt.where(
                Assistant.assistant_whatsapp_number == assistant_whatsapp_number,
            )
        if email is not None:
            stmt = stmt.where(Assistant.email == email)
        if agent_id is not None:
            stmt = stmt.where(Assistant.agent_id == agent_id)
        result = (await self.session.execute(stmt)).scalars().all()
        return result

    async def list_all_assistant_emails(self) -> List[str]:
        """
        List all non-null email addresses from all Assistants.
        """
        stmt = select(Assistant.email).where(Assistant.email.is_not(None))
        result = (await self.session.execute(stmt)).scalars().all()
        return result
