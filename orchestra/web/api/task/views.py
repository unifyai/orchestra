from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.task_dao import TaskDAO
from orchestra.db.models.orchestra_models import Task
from orchestra.web.api.task.schema import TaskModelResponse

router = APIRouter()


@router.get("/get_all_tasks", response_model=List[TaskModelResponse])
async def get_task_models(
    limit: int = 10,
    offset: int = 0,
    task_dao: TaskDAO = Depends(),
) -> List[Task]:
    """
    Retrieve all task objects from the database.

    :param limit: limit of task objects, defaults to 10.
    :param offset: offset of task objects, defaults to 0.
    :param task_dao: DAO for task models.
    :return: list of task objects from database.
    """
    return await task_dao.get_all_tasks(limit=limit, offset=offset)


@router.get("/get_task", response_model=List[TaskModelResponse])
async def get_task(
    name: str,
    task_dao: TaskDAO = Depends(),
) -> List[Task]:
    """
    Retrieve specific task object from the database.

    :param name: name of task object.
    :param task_dao: DAO for task models.
    :return: task object from database.
    """
    return await task_dao.filter(name=name)
