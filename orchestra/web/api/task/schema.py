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
        name (str): The name of the task.
        modality (str): The modality of the task.
    """

    name: str
    modality: str
