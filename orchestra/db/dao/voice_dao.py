from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant, Voice


class VoiceDAO:
    """
    Data access object for Voice operations.
    """

    def __init__(self, session: Session):
        self.session = session

    def create_voice(
        self,
        voice_id: str,
        user_id: str,
        name: str,
        description: str,
        gender: Optional[str],
        language: str,
        provider: str = "cartesia",
    ) -> Voice:
        """
        Create a new Voice for the given user.
        The combination of user_id and voice_id is unique.
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

    def get_voice_by_id(
        self,
        user_id: str,
        voice_id: str,
        provider: str,
    ) -> Optional[Voice]:
        """
        Retrieve a Voice by user and its TTS provider voice_id.
        """
        stmt = select(Voice).where(
            Voice.voice_id == voice_id,
            Voice.user_id == user_id,
            Voice.provider == provider,
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

    def delete_voice(self, user_id: str, voice_id: str, provider: str) -> None:
        """
        Delete a Voice by user and its TTS provider voice_id.
        Prevents deletion if the voice is in use by any assistant for that user.
        """
        voice = self.get_voice_by_id(user_id, voice_id, provider=provider)
        if not voice:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Voice not found.",
            )

        # Check if any assistant is using this voice for the given user.
        stmt = (
            select(Assistant.agent_id)
            .where(Assistant.user_id == user_id)
            .where(Assistant.voice_id == voice_id)
            .where(Assistant.voice_provider == provider)
            .limit(1)
        )
        assistant_using_voice = self.session.execute(stmt).first()

        if assistant_using_voice:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete voice. It is currently in use by at least one assistant.",
            )

        # If not in use, proceed with deletion.
        self.session.delete(voice)
