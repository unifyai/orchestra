"""Async version of recording_dao for use with AsyncSession."""

from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import CallRecording


class AsyncRecordingDAO:
    """
    Data access object for CallRecording operations.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_recording(
        self,
        agent_id: int,
        filename: str,
        url: str,
    ) -> CallRecording:
        """
        Create a new CallRecording for the given agent.
        """
        recording = CallRecording(agent_id=agent_id, filename=filename, url=url)
        self.session.add(recording)
        await self.session.flush()
        return recording

    async def list_recordings(self, agent_id: int) -> List[CallRecording]:
        """
        List all CallRecordings for a specific agent.
        """
        stmt = select(CallRecording).where(CallRecording.agent_id == agent_id)
        result = (await self.session.execute(stmt)).scalars().all()
        return result

    async def get_recording(
        self,
        agent_id: int,
        recording_id: int,
    ) -> Optional[CallRecording]:
        """
        Retrieve a CallRecording by agent and recording IDs.
        """
        stmt = select(CallRecording).where(
            CallRecording.agent_id == agent_id,
            CallRecording.id == recording_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def delete_recording(self, agent_id: int, recording_id: int) -> bool:
        """
        Delete a CallRecording by agent and recording IDs.
        """
        try:
            recording = self.get_recording(agent_id, recording_id)
            if recording:
                await self.session.delete(recording)
                await self.session.flush()
                return True
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Recording not found.",
                )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error deleting recording: {e}",
            )
