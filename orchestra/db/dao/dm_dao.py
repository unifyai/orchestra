"""Data Access Object for human-to-human DM threads and messages."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import DmMessage, DmThread


def normalized_pair(user_id_1: str, user_id_2: str) -> tuple[str, str]:
    """Order a user pair so (a, b) is stable regardless of argument order."""
    return (user_id_1, user_id_2) if user_id_1 < user_id_2 else (user_id_2, user_id_1)


class DmDAO:
    """DAO for DM threads/messages."""

    def __init__(self, session: Session):
        self.session = session

    def get_or_create_thread(
        self,
        *,
        organization_id: int,
        user_id_1: str,
        user_id_2: str,
    ) -> DmThread:
        """Return the thread for a user pair in an org, creating it if needed."""
        user_a_id, user_b_id = normalized_pair(user_id_1, user_id_2)
        thread = self.session.scalar(
            select(DmThread).where(
                DmThread.organization_id == organization_id,
                DmThread.user_a_id == user_a_id,
                DmThread.user_b_id == user_b_id,
            ),
        )
        if thread is None:
            thread = DmThread(
                organization_id=organization_id,
                user_a_id=user_a_id,
                user_b_id=user_b_id,
            )
            self.session.add(thread)
            self.session.flush()
        return thread

    def add_message(
        self,
        *,
        thread: DmThread,
        sender_user_id: str,
        content: str,
    ) -> DmMessage:
        """Append one message to a thread."""
        message = DmMessage(
            thread_id=thread.id,
            sender_user_id=sender_user_id,
            content=content,
        )
        self.session.add(message)
        self.session.flush()
        return message

    def list_messages(
        self,
        *,
        thread_id: int,
        limit: int = 100,
        before_id: int | None = None,
    ) -> list[DmMessage]:
        """Most-recent-last page of messages for a thread."""
        query = select(DmMessage).where(DmMessage.thread_id == thread_id)
        if before_id is not None:
            query = query.where(DmMessage.id < before_id)
        rows = self.session.scalars(
            query.order_by(DmMessage.id.desc()).limit(limit),
        ).all()
        return list(reversed(rows))
