from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import StoredPromptResponse


class StoredPromptResponseDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        prompt_id: int,
        endpoint_str: str,
        response: str,
        num_tokens: int,
    ) -> None:
        self.session.add(
            StoredPromptResponse(
                prompt_id=prompt_id,
                endpoint_str=endpoint_str,
                response=response,
                num_tokens=num_tokens,
            ),
        )

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        prompt_id: Optional[int] = None,
        endpoint_str: Optional[str] = None,
    ) -> List[StoredPromptResponse]:
        query = select(StoredPromptResponse)
        if id:
            query = query.where(StoredPromptResponse.id == id)
        if prompt_id:
            query = query.where(StoredPromptResponse.prompt_id == prompt_id)
        if endpoint_str:
            query = query.where(StoredPromptREsponse.endpoint_str == endpoint_str)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())
