from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.task_dao import TaskDAO
from orchestra.db.models.orchestra_models import Task
from orchestra.web.api.task.schema import TaskModelRequest, TaskModelResponse

router = APIRouter()


@router.get("/", response_model=List[TaskModelResponse])
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


@router.put("/")
async def create_task_model(
    new_task_object: TaskModelRequest,
    task_dao: TaskDAO = Depends(),
) -> None:
    """
    Creates task model in the database.

    :param new_task_object: new task model item.
    :param task_dao: DAO for task models.
    """
    await task_dao.create_task(
        name=new_task_object.name,
        modality=new_task_object.modality,
    )
