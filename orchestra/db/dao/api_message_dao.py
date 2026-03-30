import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import ApiMessage


class ApiMessageDAO:
    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        assistant_id: int,
        user_id: str,
        message: str,
        organization_id: Optional[int] = None,
        tags: Optional[list] = None,
        attachments: Optional[list] = None,
    ) -> ApiMessage:
        api_message = ApiMessage(
            id=str(uuid.uuid4()),
            assistant_id=assistant_id,
            user_id=user_id,
            organization_id=organization_id,
            message=message,
            status="processing",
            tags=tags or [],
            attachments=attachments or [],
        )
        self.session.add(api_message)
        self.session.flush()
        return api_message

    def get_by_id(
        self,
        message_id: str,
        user_id: str,
        organization_id: Optional[int] = None,
    ) -> Optional[ApiMessage]:
        if organization_id is not None:
            stmt = select(ApiMessage).where(
                ApiMessage.id == message_id,
                ApiMessage.user_id == user_id,
                ApiMessage.organization_id == organization_id,
            )
        else:
            stmt = select(ApiMessage).where(
                ApiMessage.id == message_id,
                ApiMessage.user_id == user_id,
                ApiMessage.organization_id.is_(None),
            )
        return self.session.execute(stmt).scalar_one_or_none()

    def complete(
        self,
        message_id: str,
        response: Optional[str] = None,
        response_tags: Optional[list] = None,
        response_attachments: Optional[list] = None,
    ) -> Optional[ApiMessage]:
        api_message = self.session.execute(
            select(ApiMessage).where(ApiMessage.id == message_id),
        ).scalar_one_or_none()
        if api_message is None:
            return None
        api_message.status = "completed"
        api_message.response = response
        if response_tags is not None:
            api_message.response_tags = response_tags
        if response_attachments is not None:
            api_message.response_attachments = response_attachments
        api_message.completed_at = datetime.now(timezone.utc)
        self.session.flush()
        return api_message
