import base64
from typing import Optional

from fastapi import HTTPException, status

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.recording_dao import RecordingDAO
from orchestra.db.models.orchestra_models import CallRecording
from orchestra.services.bucket_service import BucketService


class CallRecordingService:
    def __init__(
        self,
        assistant_dao: AssistantDAO,
        recording_dao: RecordingDAO,
        bucket_service: BucketService,
    ):
        self.assistant_dao = assistant_dao
        self.recording_dao = recording_dao
        self.bucket_service = bucket_service

    async def record_call(
        self,
        user_id: str,
        agent_id: int,
        conference_name: str,
        recording_raw: str,
        content_type: Optional[str] = None,
        is_staging: bool = False,
    ) -> CallRecording:
        """
        1) Verify assistant exists
        2) Base64-decode recording_raw into bytes
        3) Determine content_type
        4) Call BucketService.upload_recording(bytes, content_type)
        5) Persist via RecordingDAO.create_recording(agent_id, filename, url)
        6) Return the CallRecording model instance
        """
        # 1) Verify assistant exists
        assistant = self.assistant_dao.get_assistant_by_id(
            user_id=user_id,
            agent_id=agent_id,
        )
        if not assistant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

        # 2) Base64-decode recording_raw into bytes
        try:
            content = base64.b64decode(recording_raw)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to decode base64 recording: {exc}",
            )

        # 3) Determine content_type
        if content_type is None:
            content_type = "application/octet-stream"

        # 4) Upload recording to bucket
        try:
            url, file_path = self.bucket_service.upload_recording(
                content,
                content_type,
                f"{agent_id}/{conference_name}.mp3",
                is_staging=is_staging,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to upload recording: {exc}",
            )

        # 5) Persist via DAO
        recording = self.recording_dao.create_recording(agent_id, file_path, url)

        # 6) Return the CallRecording model instance
        return recording
