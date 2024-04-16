from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import DatasetEvaluation


class DatasetEvaluationDAO:
    """Class for accessing dataset_evaluation table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_dataset_evaluation(  # noqa: WPS211
        self,
        mdl_name: str,
        dataset_name: str,
        prompt: str,
        score: float,
        metric: str,
    ) -> None:
        """
        Add single dataset evaluation to session.
        """
        self.session.add(
            DatasetEvaluation(
                mdl_name=mdl_name,
                dataset_name=dataset_name,
                prompt=prompt,
                score=score,
                metric=metric,
            ),
        )

    def get_all_dataset_evaluations(
        self,
        limit: int,
        offset: int,
    ) -> List[DatasetEvaluation]:
        """
        Get all dataset evaluation models with limit/offset pagination.

        :param limit: limit of dataset_evaluations.
        :param offset: offset of dataset_evaluations.
        :return: stream of dataset_evaluations.
        """
        raw_dataset_evaluation = self.session.execute(
            select(DatasetEvaluation).limit(limit).offset(offset),
        )

        return list(raw_dataset_evaluation.scalars().fetchall())

    def filter(  # noqa: WPS211, C901
        self,
        mdl_name: Optional[str] = None,
        dataset_name: Optional[str] = None,
        prompt: Optional[str] = None,
        score: Optional[str] = None,
        metric: Optional[str] = None,
    ) -> List[DatasetEvaluation]:
        """
        Filter dataset_evaluation models by given parameters.
        """
        query = select(DatasetEvaluation)
        if mdl_name:
            query = query.where(DatasetEvaluation.mdl_name == mdl_name)
        if dataset_name:
            query = query.where(DatasetEvaluation.dataset_name == dataset_name)
        if prompt:
            query = query.where(DatasetEvaluation.prompt == prompt)
        if score:
            query = query.where(DatasetEvaluation.score == score)
        if metric:
            query = query.where(DatasetEvaluation.metric == metric)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())
