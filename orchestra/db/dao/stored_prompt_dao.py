import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import StoredPrompt


class StoredPromptDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        user_id: Optional[str],
        system_msg: Optional[str],
        messages: str,
        prompt_kwargs: str,
        extra_fields: dict,
        num_tokens: int,
        timestamp: datetime.datetime,
    ) -> None:
        self.session.add(
            StoredPrompt(
                user_id=user_id,
                system_msg=system_msg,
                messages=messages,
                prompt_kwargs=prompt_kwargs,
                extra_fields=extra_fields,
                num_tokens=num_tokens,
                timestamp=timestamp,
            ),
        )

    def delete(
        self,
        id: int,
        user_id: str,
    ) -> None:
        prompt = (
            self.session.query(StoredPrompt)
            .filter_by(id=id, user_id=user_id)
            .one()
        )
        self.session.delete(prompt)
        self.session.commit()

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        user_id: Optional[str] = None,
        system_msg: Optional[str] = None,
        messages: Optional[str] = None,
    ) -> List[StoredPrompt]:
        query = select(StoredPrompt)
        if id:
            query = query.where(StoredPrompt.id == id)
        if user_id:
            query = query.where(StoredPrompt.user_id == user_id)
        if system_msg:
            query = query.where(StoredPrompt.system_msg == system_msg)
        if messages:
            query = query.where(StoredPrompt.messages == messages)
        rows = self.session.execute(query)
        return list(rows.scalars().unique().fetchall())

    def check_ids_valid(self, user_id, prompt_ids):
        query = (
            select(StoredPrompt.id)
            .where(StoredPrompt.user_id == user_id)
            .where(StoredPrompt.id.in_(prompt_ids))
        )
        matching_ids = self.session.execute(query).scalars().all()
        invalid_ids = set(prompt_ids).difference(set(matching_ids))
        return invalid_ids

    def get_prompts(self, prompt_ids: list, user_id: str):
        query = (
            select(StoredPrompt)
            .where(StoredPrompt.user_id == user_id)
            .where(StoredPrompt.id.in_(prompt_ids))
        )
        rows = self.session.execute(query)
        return list(rows.scalars().unique().fetchall())
