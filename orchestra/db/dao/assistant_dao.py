from decimal import Decimal
from typing import Any, Dict, List, Optional
from zoneinfo import available_timezones

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant

VALID_TIMEZONES = available_timezones()


class AssistantDAO:
    """
    Data access object for Assistant operations.
    """

    def __init__(self, session: Session):
        self.session = session

    def create_assistant(
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
    ) -> Assistant:
        """
        Create a new Assistant for the given user.
        """

        if timezone is not None and timezone not in VALID_TIMEZONES:
            raise ValueError(f"'{timezone}' is not a valid IANA timezone.")

        assistant = Assistant(
            user_id=user_id,
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
        self.session.flush()
        return assistant

    def get_assistant_by_id(self, user_id: str, agent_id: int) -> Optional[Assistant]:
        """
        Retrieve an Assistant by user and agent IDs.
        """
        stmt = select(Assistant).where(
            Assistant.agent_id == agent_id,
            Assistant.user_id == user_id,
        )
        result = self.session.execute(stmt).scalar_one_or_none()
        return result

    def list_assistants_for_user(
        self,
        user_id: str,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        email: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        assistant_whatsapp_number: Optional[str] = None,
    ) -> List[Assistant]:
        """
        List all Assistants belonging to a specific user.
        """
        stmt = select(Assistant).where(Assistant.user_id == user_id)
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
        result = self.session.execute(stmt).scalars().all()
        return result

    def delete_assistant(self, user_id: str, agent_id: int) -> None:
        """
        Delete an Assistant by user and agent IDs.
        """
        assistant = self.get_assistant_by_id(user_id, agent_id)
        if assistant:
            self.session.delete(assistant)
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

    def update_assistant(
        self,
        user_id: str,
        agent_id: int,
        update_data: Dict[str, Any],
    ) -> Optional[Assistant]:
        """
        Update configuration for an existing Assistant.
        """
        assistant = self.get_assistant_by_id(user_id, agent_id)
        if not assistant:
            return None

        if "timezone" in update_data:
            tz = update_data["timezone"]
            if tz is not None and tz not in VALID_TIMEZONES:
                raise ValueError(f"'{tz}' is not a valid IANA timezone.")

        for key, value in update_data.items():
            setattr(assistant, key, value)

        self.session.add(assistant)
        return assistant

    def list_all_assistants(
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
        result = self.session.execute(stmt).scalars().all()
        return result

    def list_all_assistant_emails(self) -> List[str]:
        """
        List all non-null email addresses from all Assistants.
        """
        stmt = select(Assistant.email).where(Assistant.email.is_not(None))
        result = self.session.execute(stmt).scalars().all()
        return result
