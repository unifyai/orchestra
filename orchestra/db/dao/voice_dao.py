from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Voice


class VoiceDAO:
    """
    Data access object for Voice operations.
    """

    def __init__(self, session: Session):
        self.session = session

    def create_voice(
        self,
        voice_id: str,  # This is Cartesia's voice ID
        user_id: str,
        name: str,
        description: str,
        gender: str,
        language: str,
        provider: str = "cartesia",
    ) -> Voice:
        """
        Create a new Voice for the given user, using Cartesia's voice_id as primary key.
        """
        voice = Voice(
            voice_id=voice_id,
            user_id=user_id,
            name=name,
            description=description,
            gender=gender,
            language=language,
            provider=provider,
        )
        self.session.add(voice)
        self.session.flush()
        return voice

    def get_voice_by_id(self, user_id: str, voice_id: str) -> Optional[Voice]:
        """
        Retrieve a Voice by user and its (Cartesia) voice_id.
        """
        stmt = select(Voice).where(
            Voice.voice_id == voice_id,
            Voice.user_id == user_id,
        )
        result = self.session.execute(stmt).scalar_one_or_none()
        return result

    def list_voices_for_user(self, user_id: str) -> List[Voice]:
        """
        List all Voices belonging to a specific user.
        """
        stmt = select(Voice).where(Voice.user_id == user_id)
        result = self.session.execute(stmt).scalars().all()
        return result

    def delete_voice(self, user_id: str, voice_id: str) -> None:
        """
        Delete a Voice by user and its (Cartesia) voice_id.
        """
        voice = self.get_voice_by_id(user_id, voice_id)
        if voice:
            self.session.delete(voice)
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Voice not found.",
            )
