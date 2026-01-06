"""Async version of voice_dao for use with AsyncSession."""

from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import Assistant, Voice


class AsyncVoiceDAO:
    """
    Data access object for Voice operations.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_voice(
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
        await self.session.flush()
        return voice

    async def get_voice_by_id(
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
        result = await self.session.execute(stmt).scalar_one_or_none()
        return result

    async def list_voices_for_user(self, user_id: str) -> List[Voice]:
        """
        List all Voices belonging to a specific user.
        """
        stmt = select(Voice).where(Voice.user_id == user_id)
        result = await self.session.execute(stmt).scalars().all()
        return result

    async def delete_voice(self, user_id: str, voice_id: str, provider: str) -> None:
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
        assistant_using_voice = await self.session.execute(stmt).first()

        if assistant_using_voice:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete voice. It is currently in use by at least one assistant.",
            )

        # If not in use, proceed with deletion.
        await self.session.delete(voice)
