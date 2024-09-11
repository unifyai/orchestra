from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import StoredPromptVariation


class StoredPromptVariationDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        prompt_id: int,
        default_prompt_id: int,
    ) -> None:
        self.session.add(
            StoredPromptVariation(
                prompt_id=prompt_id,
                default_prompt_id=default_prompt_id,
            ),
        )

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        prompt_id: Optional[int] = None,
        default_prompt_id: Optional[int] = None,
    ) -> List[StoredPromptVariation]:
        query = select(StoredPromptVariation)
        if id:
            query = query.where(StoredPromptVariation.id == id)
        if prompt_id:
            query = query.where(StoredPromptVariation.prompt_id == prompt_id)
        if default_prompt_id:
            query = query.where(
                StoredPromptVariation.default_prompt_id == default_prompt_id,
            )
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())

    def delete(self, prompt_id, default_prompt_id):
        try:
            entry = (
                self.session.query(StoredPromptVariation)
                .filter_by(prompt_id=prompt_id, default_prompt_id=default_prompt_id)
                .one()
            )
            self.session.delete(entry)
            self.session.commit()
            return {"info": "Prompt Variation deleted successfully"}
        except:
            self.session.rollback()
            return {"info": "Unable to delete prompt variation"}
