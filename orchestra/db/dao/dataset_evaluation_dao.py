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
        gt_score: float,
        score: float,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """
        Add single dataset evaluation to session.
        """
        self.session.add(
            DatasetEvaluation(
                mdl_name=mdl_name,
                dataset_name=dataset_name,
                prompt=prompt,
                gt_score=gt_score,
                score=score,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
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
        gt_score: Optional[float] = None,
        score: Optional[float] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
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
        if gt_score:
            query = query.where(DatasetEvaluation.gt_score == gt_score)
        if score:
            query = query.where(DatasetEvaluation.score == score)
        if input_tokens:
            query = query.where(DatasetEvaluation.input_tokens == input_tokens)
        if output_tokens:
            query = query.where(DatasetEvaluation.output_tokens == output_tokens)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())

    def update_dataset_evaluation(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        mdl_name: str,
        dataset_name: str,
        prompt: str,
        gt_score: Optional[float] = None,
        score: Optional[float] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
    ) -> None:
        """
        Update specific dataset evaluation model.
        """
        query = select(DatasetEvaluation)
        query = (
            query.where(DatasetEvaluation.mdl_name == mdl_name)
            .where(DatasetEvaluation.dataset_name == dataset_name)
            .where(DatasetEvaluation.prompt == prompt)
        )
        raw_dataset_evaluation = self.session.execute(query)
        dataset_evaluation = raw_dataset_evaluation.scalars().first()
        if dataset_evaluation is not None:
            if gt_score:
                setattr(dataset_evaluation, "gt_score", gt_score)
            if score:
                setattr(dataset_evaluation, "score", score)
            if input_tokens:
                setattr(dataset_evaluation, "input_tokens", input_tokens)
            if output_tokens:
                setattr(dataset_evaluation, "output_tokens", output_tokens)
