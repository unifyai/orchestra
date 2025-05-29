import datetime
import uuid
from typing import List, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import AssistantHiringOneTimeApprovalLink


class AssistantHiringOneTimeApprovalLinkDAO:
    def __init__(self, session: Session):
        self.session = session

    def generate_token(self) -> str:
        return str(uuid.uuid4())

    def create(
        self,
        expires_at: datetime.datetime,
    ) -> AssistantHiringOneTimeApprovalLink:
        token = self.generate_token()
        link = AssistantHiringOneTimeApprovalLink(
            token=token,
            expires_at=expires_at,
        )
        self.session.add(link)
        return link

    def get_by_token(self, token: str) -> Optional[AssistantHiringOneTimeApprovalLink]:
        query = select(AssistantHiringOneTimeApprovalLink).where(
            AssistantHiringOneTimeApprovalLink.token == token,
        )
        return self.session.execute(query).scalar_one_or_none()

    def get_by_id(self, link_id: str) -> Optional[AssistantHiringOneTimeApprovalLink]:
        query = select(AssistantHiringOneTimeApprovalLink).where(
            AssistantHiringOneTimeApprovalLink.id == link_id,
        )
        return self.session.execute(query).scalar_one_or_none()

    def claim_link(
        self,
        token: str,
        user_id: str,
    ) -> Optional[AssistantHiringOneTimeApprovalLink]:
        link = self.get_by_token(token)
        if link:
            if link.user_id is not None:  # Already claimed
                return None
            if link.expires_at < datetime.datetime.now(
                datetime.timezone.utc,
            ):  # Expired
                return None

            link.user_id = user_id
            link.claimed_at = datetime.datetime.now(datetime.timezone.utc)
            # self.session.commit()
            return link
        return None

    def list_links(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> List[AssistantHiringOneTimeApprovalLink]:
        query = (
            select(AssistantHiringOneTimeApprovalLink)
            .limit(limit)
            .offset(offset)
            .order_by(AssistantHiringOneTimeApprovalLink.created_at.desc())
        )
        return list(self.session.execute(query).scalars().all())

    def delete_link(self, link_id: str) -> bool:
        link = self.get_by_id(link_id)
        if link:
            self.session.delete(link)
            # self.session.commit()
            return True
        return False

    def delete_expired_links(self) -> int:
        """Deletes expired and unclaimed links. Returns the number of links deleted."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        stmt = (
            delete(AssistantHiringOneTimeApprovalLink)
            .where(AssistantHiringOneTimeApprovalLink.expires_at < now_utc)
            .where(
                AssistantHiringOneTimeApprovalLink.user_id.is_(None),
            )  # Only delete unclaimed expired links
        )
        result = self.session.execute(stmt)
        # self.session.commit()
        return result.rowcount
