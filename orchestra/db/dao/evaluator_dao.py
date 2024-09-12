from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Evaluator


class EvaluatorDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        user_id: str,
        name: str,
        system_prompt: str,
        class_config: str,
        judge_models: str,
        client_side: bool,
    ) -> None:
        try:
            self.session.add(
                Evaluator(
                    user_id=user_id,
                    name=name,
                    system_prompt=system_prompt,
                    class_config=class_config,
                    judge_models=judge_models,
                    client_side=client_side,
                ),
            )
            self.session.commit()
            return True
        except:
            self.session.rollback()
            return False

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        user_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> List[Evaluator]:
        query = select(Evaluator)
        if id:
            query = query.where(Evaluator.id == id)
        if user_id:
            query = query.where(Evaluator.user_id == user_id)
        if name:
            query = query.where(Evaluator.name == name)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())

    def update(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        name: Optional[str] = None,
    ) -> None:
        query = select(Evaluator)
        query = query.where(Evaluator.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)  # noqa: B010

    def rename(self, user_id, name, new_name):
        try:
            evaluator_id = self.filter(user_id=user_id, name=name)[0].id
        except:
            return {"error": f"No evaluator with the name {name}"}

        self.update(id=evaluator_id, name=new_name)

    def delete_evaluator(self, user_id, name):
        try:
            evaluator = (
                self.session.query(Evaluator)
                .filter_by(user_id=user_id, name=name)
                .one()
            )
            self.session.delete(evaluator)
            self.session.commit()
            return {"info": "Evaluator deleted successfully"}
        except:
            self.session.rollback()
            return {"info": "Unable to delete evaluator"}
