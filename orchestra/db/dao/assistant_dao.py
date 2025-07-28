from decimal import Decimal
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant


class AssistantDAO:
    """
    Data access object for Assistant operations.
    """

    def __init__(self, session: Session):
        self.session = session

    def create_assistant(
        self,
        user_id: str,
        first_name: str,
        surname: str,
        age: int,
        region: str,
        profile_photo: Optional[str] = None,
        profile_video: Optional[str] = None,
        about: str,
        weekly_limit: Decimal,
        max_parallel: int,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        email: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        voice_id: Optional[str] = None,
        country: Optional[str] = None,
    ) -> Assistant:
        """
        Create a new Assistant for the given user.
        """
        assistant = Assistant(
            user_id=user_id,
            first_name=first_name,
            surname=surname,
            age=age,
            region=region,
            profile_photo=profile_photo,
            profile_video=profile_video,
            about=about,
            weekly_limit=weekly_limit,
            max_parallel=max_parallel,
            phone=phone,
            user_phone=user_phone,
            email=email,
            user_whatsapp_number=user_whatsapp_number,
            voice_id=voice_id,
            country=country,
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
        weekly_limit: Optional[Decimal] = None,
        max_parallel: Optional[int] = None,
        about: Optional[str] = None,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        email: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        assistant_whatsapp_number: Optional[str] = None,
        voice_id: Optional[str] = None,
        country: Optional[str] = None,
        profile_photo: Optional[str] = None,
        profile_video: Optional[str] = None,
    ) -> Optional[Assistant]:
        """
        Update configuration for an existing Assistant.
        """
        assistant = self.get_assistant_by_id(user_id, agent_id)
        if not assistant:
            return None
        if weekly_limit is not None:
            assistant.weekly_limit = weekly_limit
        if max_parallel is not None:
            assistant.max_parallel = max_parallel
        if about is not None:
            assistant.about = about
        if phone is not None:
            assistant.phone = phone
        if user_phone is not None:
            assistant.user_phone = user_phone
        if email is not None:
            assistant.email = email
        if user_whatsapp_number is not None:
            assistant.user_whatsapp_number = user_whatsapp_number
        if assistant_whatsapp_number is not None:
            assistant.assistant_whatsapp_number = assistant_whatsapp_number
        if voice_id is not None:
            assistant.voice_id = voice_id
        if country is not None:
            assistant.country = country
        if profile_photo is not None:
            assistant.profile_photo = profile_photo
        if profile_video is not None:
            assistant.profile_video = profile_video
        self.session.add(assistant)
        return assistant

    def list_all_assistants(
        self,
        phone: Optional[str] = None,
        user_phone: Optional[str] = None,
        user_whatsapp_number: Optional[str] = None,
        assistant_whatsapp_number: Optional[str] = None,
        email: Optional[str] = None,
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
        result = self.session.execute(stmt).scalars().all()
        return result

    def list_all_assistant_emails(self) -> List[str]:
        """
        List all non-null email addresses from all Assistants.
        """
        stmt = select(Assistant.email).where(Assistant.email.is_not(None))
        result = self.session.execute(stmt).scalars().all()
        return result
