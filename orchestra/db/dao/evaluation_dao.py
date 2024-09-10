from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Evaluation


class EvaluationDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        prompt_id: int,
        evaluator_id: int,
        score: float,
    ) -> None:
        self.session.add(
            Evaluation(
                prompt_id=prompt_id,
                evaluator_id=evaluator_id,
                score=score,
            ),
        )

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        prompt_id: Optional[int] = None,
        evaluator_id: Optional[int] = None,
    ) -> List[Evaluation]:
        query = select(Evaluation)
        if id:
            query = query.where(Evaluation.id == id)
        if prompt_id:
            query = query.where(Evaluation.prompt_id == prompt_id)
        if evaluator_id:
            query = query.where(Evaluation.evaluator_id == evaluator_id)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())

    def update(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        score: Optional[float] = None,
    ) -> None:
        query = select(Evaluation)
        query = query.where(Evaluation.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if score:
                setattr(entry, "score", score)  # noqa: B010
