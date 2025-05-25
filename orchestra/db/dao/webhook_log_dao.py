from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import WebhookLog


class WebhookLogDAO:
    def __init__(self, session: Session):
        self.session = session

    def create_webhook_log(self, event_id: str, event_type: str):
        webhook_log = WebhookLog(
            id=event_id,  # Using event_id as primary key
            event_id=event_id,
            event_type=event_type,
            processed_at=datetime.now(tz=timezone.utc),
        )
        self.session.add(webhook_log)
        self.session.commit()

    def event_exists(self, event_id: str) -> bool:
        existing = self.session.query(WebhookLog).filter_by(event_id=event_id).first()
        return existing is not None
