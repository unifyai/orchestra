import datetime

from pydantic import BaseModel


class ModelResponse(BaseModel):
    """
    Response model for model models.

    Attributes:
        id (int): The id of the model.
        mdl_code (str): The model code of the model.
        user_id (str): The user id of the model.
        uploaded_at (datetime.datetime): The uploaded at of the model.
        task (str): The task of the model.
        description (str): The description of the model.
        license (str): The license of the model.
        active (bool): Whether the model is active.
        input_args_format (str): The input args format of the model.
        output_format (str): The output format of the model.
        custom_fields (str): The custom fields of the model.
    """

    id: int
    mdl_code: str
    user_id: str
    uploaded_at: datetime.datetime
    task: str
    description: str
    license: str
    active: bool
    input_args_format: str
    output_format: str
    custom_fields: str
