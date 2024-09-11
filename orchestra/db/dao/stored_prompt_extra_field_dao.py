from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import StoredPromptExtraField


class StoredPromptExtraFieldDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        prompt_id: int,
        field: str,
        value: str,
    ) -> None:
        self.session.add(
            StoredPromptExtraField(
                prompt_id=prompt_id,
                field=field,
                value=value,
            ),
        )

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        prompt_id: Optional[int] = None,
    ) -> List[StoredPromptExtraField]:
        query = select(StoredPromptExtraField)
        if id:
            query = query.where(StoredPromptExtraField.id == id)
        if prompt_id:
            query = query.where(StoredPromptExtraField.prompt_id == prompt_id)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())
