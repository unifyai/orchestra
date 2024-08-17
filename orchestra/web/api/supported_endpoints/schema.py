import datetime

from pydantic import BaseModel


class EndpointModelResponseVerbose(BaseModel):
    """
    Response model for endpoint models.

    Attributes:
        endpoint_id (int): The id of the endpoint.
        created_at (datetime.datetime): The time the endpoint was created.

        mdl_id (int): The id of the model.
        mdl_code (str): The model code of the model.
        mdl_uploaded_at (datetime.datetime): The uploaded at of the model.
        mdl_task (str): The task of the model.
        mdl_active (bool): Whether the model is active.

        provider_id (int): The id of the provider.
        provider_name (str): The name of the provider.
        provider_image_url (str): The image url of the provider.
    """

    endpoint_id: int
    created_at: datetime.datetime
    mdl_id: int
    mdl_code: str
    mdl_uploaded_at: datetime.datetime
    mdl_task: str
    mdl_active: bool
    provider_id: int
    provider_name: str
    provider_image_url: str
