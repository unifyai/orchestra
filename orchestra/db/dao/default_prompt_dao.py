from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import DefaultPrompt


class DefaultPromptDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        user_id: str,
        name: str,
        prompt: str,
    ) -> None:
        self.session.add(
            DefaultPrompt(
                user_id=user_id,
                name=name,
                prompt=prompt,
            ),
        )

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        user_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> List[DefaultPrompt]:
        query = select(DefaultPrompt)
        if id:
            query = query.where(DefaultPrompt.id == id)
        if user_id:
            query = query.where(DefaultPrompt.user_id == user_id)
        if name:
            query = query.where(DefaultPrompt.name == name)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())

    def update(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        name: Optional[str] = None,
    ) -> None:
        query = select(DefaultPrompt)
        query = query.where(DefaultPrompt.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)  # noqa: B010

    def rename(self, user_id, name, new_name):
        try:
            default_prompt_id = self.filter(user_id=user_id, name=name)[0].id
        except:
            raise ValueError

        self.update(id=default_prompt_id, name=new_name)

    def delete_default_prompt(self, user_id, name):
        try:
            default_prompt = (
                self.session.query(DefaultPrompt)
                .filter_by(user_id=user_id, name=name)
                .one()
            )
            self.session.delete(default_prompt)
            self.session.commit()
            return
        except:
            self.session.rollback()
            raise ValueError
