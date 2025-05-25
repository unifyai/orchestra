from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import CallRecording


class RecordingDAO:
    """
    Data access object for CallRecording operations.
    """

    def __init__(self, session: Session):
        self.session = session

    def create_recording(self, agent_id: int, filename: str, url: str) -> CallRecording:
        """
        Create a new CallRecording for the given agent.
        """
        recording = CallRecording(agent_id=agent_id, filename=filename, url=url)
        self.session.add(recording)
        self.session.flush()
        return recording

    def list_recordings(self, agent_id: int) -> List[CallRecording]:
        """
        List all CallRecordings for a specific agent.
        """
        stmt = select(CallRecording).where(CallRecording.agent_id == agent_id)
        result = self.session.execute(stmt).scalars().all()
        return result

    def get_recording(
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
        return self.session.execute(stmt).scalar_one_or_none()

    def delete_recording(self, agent_id: int, recording_id: int) -> bool:
        """
        Delete a CallRecording by agent and recording IDs.
        """
        try:
            recording = self.get_recording(agent_id, recording_id)
            if recording:
                self.session.delete(recording)
                self.session.flush()
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
