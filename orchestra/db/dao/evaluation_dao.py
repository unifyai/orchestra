from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select, join
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Evaluator,
    Evaluation,
    Dataset,
    StoredPrompt,
    DatasetPrompt,
)


class EvaluationDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        prompt_id: int,
        evaluator_id: int,
        endpoint_str: str,
        score: float,
    ) -> None:
        self.session.add(
            Evaluation(
                prompt_id=prompt_id,
                evaluator_id=evaluator_id,
                endpoint_str=endpoint_str,
                score=score,
            ),
        )

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        prompt_id: Optional[int] = None,
        evaluator_id: Optional[int] = None,
        endpoint_str: Optional[str] = None,
    ) -> List[Evaluation]:
        query = select(Evaluation)
        if id:
            query = query.where(Evaluation.id == id)
        if prompt_id:
            query = query.where(Evaluation.prompt_id == prompt_id)
        if evaluator_id:
            query = query.where(Evaluation.evaluator_id == evaluator_id)
        if endpoint_str:
            query = query.where(Evaluation.endpoint_str == endpoint_str)
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

    def fetch_evaluation_scores(self, prompt_ids, evaluator_id, endpoint_str):
        query = select(Evaluation)
        query = query.where(Evaluation.evaluator_id == evaluator_id)
        query = query.where(Evaluation.endpoint_str == endpoint_str)
        query = query.filter(Evaluation.prompt_id.in_(prompt_ids))
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())

    def get_evaluator_names(self, dataset_id: int, endpoint_str: str):

        query = (
            select(Evaluator.name)
            .distinct()
            .select_from(
                join(Dataset, DatasetPrompt, Dataset.id == DatasetPrompt.dataset_id)
                .join(StoredPrompt, DatasetPrompt.prompt_id == StoredPrompt.id)
                .join(Evaluation, StoredPrompt.id == Evaluation.prompt_id)
                .join(Evaluator, Evaluator.id == Evaluation.evaluator_id)
            )
            .where(Dataset.id == dataset_id)
            .where(Evaluation.endpoint_str == endpoint_str)
        )

        result = self.session.execute(query)

        evaluator_ids = [row[0] for row in result]

        return evaluator_ids

    def get_endpoints(self, dataset_id: int, evaluator_id: str):

        query = (
            select(Evaluation.endpoint_str)
            .distinct()
            .select_from(
                join(Dataset, DatasetPrompt, Dataset.id == DatasetPrompt.dataset_id)
                .join(StoredPrompt, DatasetPrompt.prompt_id == StoredPrompt.id)
                .join(Evaluation, StoredPrompt.id == Evaluation.prompt_id)
            )
            .where(Dataset.id == dataset_id)
            .where(Evaluation.evaluator_id == evaluator_id)
        )

        result = self.session.execute(query)

        endpoints = [row[0] for row in result]

        return endpoints
