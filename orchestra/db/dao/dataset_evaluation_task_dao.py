from typing import List, Optional

from fastapi import Depends
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import DatasetEvaluationTask


class DatasetEvaluationTaskDAO:
    """Class for accessing dataset_evaluation_task table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_dataset_evaluation_task(  # noqa: WPS211
        self,
        name: str,
        status: str,
        user_id: Optional[str] = None,
    ) -> None:
        """
        Add single dataset evaluation to session.
        """
        self.session.add(
            DatasetEvaluationTask(
                user_id=user_id,
                name=name,
                status=status,
            ),
        )

    def get_all_dataset_evaluation_tasks(
        self,
        limit: int,
        offset: int,
    ) -> List[DatasetEvaluationTask]:
        """
        Get all dataset evaluation task models with limit/offset pagination.

        :param limit: limit of dataset_evaluations.
        :param offset: offset of dataset_evaluations.
        :return: stream of dataset_evaluations.
        """
        raw_dataset_evaluation = self.session.execute(
            select(DatasetEvaluationTask).limit(limit).offset(offset),
        )

        return list(raw_dataset_evaluation.scalars().fetchall())

    def filter(  # noqa: WPS211, C901
        self,
        user_id: Optional[str] = None,
        name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[DatasetEvaluationTask]:
        """
        Filter dataset_evaluation_tasks models by given parameters.
        """
        query = select(DatasetEvaluationTask)
        if user_id:
            query = query.where(DatasetEvaluationTask.user_id == user_id)
        if name:
            query = query.where(DatasetEvaluationTask.name == name)
        if status:
            query = query.where(DatasetEvaluationTask.status == status)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())

    def update_dataset_evaluation_task(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        user_id: str,
        name: str,
        status: str,
    ) -> None:
        """
        Update specific datapoint model.
        """
        query = select(DatasetEvaluationTask)
        query = query.where(DatasetEvaluationTask.user_id == user_id).where(
            DatasetEvaluationTask.name == name
        )
        raw_dataset_evaluation_task = self.session.execute(query)
        dataset_evaluation_task = raw_dataset_evaluation_task.scalars().first()
        if dataset_evaluation_task is not None:
            setattr(dataset_evaluation_task, "status", status)

    def get_user_datasets(  # noqa: WPS211, C901
        self,
        user_id: str,
    ) -> List[DatasetEvaluationTask]:
        """
        Filter dataset_evaluation_tasks models by accesible to a given user.
        """
        query = select(DatasetEvaluationTask)
        query = query.where(
            or_(
                DatasetEvaluationTask.user_id == user_id,
                DatasetEvaluationTask.user_id == None,
            )
        )
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())
