from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Judgement


class JudgementDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        response_id: int,
        judge_endpoint_str: int,
        evaluator_id: int,
        judgement: str,
        judgement_score: float,
    ) -> None:
        self.session.add(
            Judgement(
                response_id=response_id,
                judge_endpoint_str=judge_endpoint_str,
                evaluator_id=evaluator_id,
                judgement=judgement,
                judgement_score=judgement_score,
            ),
        )

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        response_id: Optional[int] = None,
        evaluator_id: Optional[int] = None,
    ) -> List[Judgement]:
        query = select(Judgement)
        if id:
            query = query.where(Judgement.id == id)
        if response_id:
            query = query.where(Judgement.response_id == response_id)
        if evaluator_id:
            query = query.where(Judgement.evaluator_id == evaluator_id)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())
