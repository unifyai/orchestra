"""Async version of webhook_log_dao for use with AsyncSession."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import WebhookLog


class AsyncWebhookLogDAO:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_webhook_log(self, event_id: str, event_type: str):
        webhook_log = WebhookLog(
            id=event_id,  # Using event_id as primary key
            event_id=event_id,
            event_type=event_type,
            processed_at=datetime.now(tz=timezone.utc),
        )
        self.session.add(webhook_log)
        await self.session.commit()

    async def event_exists(self, event_id: str) -> bool:
        existing = (
            (
                await self.session.execute(
                    select(WebhookLog).filter_by(event_id=event_id),
                )
            )
            .scalars()
            .first()
        )
        return existing is not None
