"""Async version of assistant_hiring_one_time_approval_link_dao for use with AsyncSession."""

import datetime
import uuid
from typing import List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import AssistantHiringOneTimeApprovalLink


class AsyncAssistantHiringOneTimeApprovalLinkDAO:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def generate_token(self) -> str:
        return str(uuid.uuid4())

    async def create(
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

    async def get_by_token(
        self,
        token: str,
    ) -> Optional[AssistantHiringOneTimeApprovalLink]:
        query = select(AssistantHiringOneTimeApprovalLink).where(
            AssistantHiringOneTimeApprovalLink.token == token,
        )
        return await self.session.execute(query).scalar_one_or_none()

    async def get_by_id(
        self,
        link_id: str,
    ) -> Optional[AssistantHiringOneTimeApprovalLink]:
        query = select(AssistantHiringOneTimeApprovalLink).where(
            AssistantHiringOneTimeApprovalLink.id == link_id,
        )
        return await self.session.execute(query).scalar_one_or_none()

    async def claim_link(
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
            # await self.session.commit()
            return link
        return None

    async def list_links(
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
        return list(await self.session.execute(query).scalars().all())

    async def delete_link(self, link_id: str) -> bool:
        link = self.get_by_id(link_id)
        if link:
            await self.session.delete(link)
            # await self.session.commit()
            return True
        return False

    async def delete_expired_links(self) -> int:
        """Deletes expired and unclaimed links. Returns the number of links deleted."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        stmt = (
            delete(AssistantHiringOneTimeApprovalLink)
            .where(AssistantHiringOneTimeApprovalLink.expires_at < now_utc)
            .where(
                AssistantHiringOneTimeApprovalLink.user_id.is_(None),
            )  # Only delete unclaimed expired links
        )
        result = await self.session.execute(stmt)
        # await self.session.commit()
        return result.rowcount
