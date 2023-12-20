from pydantic import BaseModel


class TaskModelRequest(BaseModel):
    """
    Request model for creating new task model.

    Attributes:
        name (str): The name of the task.
        modality (str): The modality of the task.
    """

    name: str
    modality: str


class TaskModelResponse(BaseModel):
    """
    Response model for task models.

    Attributes:
        id (int): The id of the task.
        name (str): The name of the task.
        modality (str): The modality of the task.
    """

    id: int
    name: str
    modality: str
