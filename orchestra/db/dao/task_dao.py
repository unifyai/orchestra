from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Task


class TaskDAO:
    """Class for accessing task table."""

    def __init__(self, session: Session):
        self.session = session

    def create_task(
        self,
        name: str,
        modality: str,
    ) -> None:
        """
        Add single task to session.

        :param name: name of a task.
        :param modality: modality of a task.
        """
        self.session.add(
            Task(
                name=name,
                modality=modality,
            ),
        )

    def get_all_tasks(self, limit: int, offset: int) -> List[Task]:
        """
        Get all task models with limit/offset pagination.

        :param limit: limit of tasks.
        :param offset: offset of tasks.
        :return: stream of tasks.
        """
        raw_tasks = self.session.execute(
            select(Task).limit(limit).offset(offset),
        )

        return list(raw_tasks.scalars().fetchall())

    def filter(
        self,
        name: Optional[str] = None,
    ) -> List[Task]:
        """
        Get specific task model.

        :param name: name of task instance.
        :return: task models.
        """
        query = select(Task)
        if name:
            query = query.where(Task.name == name)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())
