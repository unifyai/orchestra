from pydantic import BaseModel


class TaskModelResponse(BaseModel):
    """
    Response model for task models.

    Attributes:
        name (str): The name of the task.
        modality (str): The modality of the task.
    """

    name: str
    modality: str
