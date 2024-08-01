import datetime

from pydantic import BaseModel


class ModelResponse(BaseModel):
    """
    Response model for model models.

    Attributes:
        id (int): The id of the model.
        mdl_code (str): The model code of the model.
        uploaded_at (datetime.datetime): The uploaded at of the model.
        task (str): The task of the model.
        active (bool): Whether the model is active.
    """

    id: int
    mdl_code: str
    uploaded_at: datetime.datetime
    task: str
    active: bool
