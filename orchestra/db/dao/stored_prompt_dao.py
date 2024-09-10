import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import StoredPrompt


class StoredPromptDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        user_id: Optional[str],
        prompt: str,
        ref_answer: Optional[str],
        num_tokens: int,
        timestamp: datetime.datetime,
    ) -> None:
        self.session.add(
            StoredPrompt(
                user_id=user_id,
                prompt=prompt,
                ref_answer=ref_answer,
                num_tokens=num_tokens,
                timestamp=timestamp,
            ),
        )

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        user_id: Optional[str] = None,
    ) -> List[StoredPrompt]:
        query = select(StoredPrompt)
        if id:
            query = query.where(StoredPrompt.id == id)
        if user_id:
            query = query.where(StoredPrompt.user_id == user_id)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())
