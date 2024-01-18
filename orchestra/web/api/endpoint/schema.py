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
        mdl_user_id (str): The user id of the model.
        mdl_uploaded_at (datetime.datetime): The uploaded at of the model.
        mdl_task (str): The task of the model.
        mdl_description (str): The description of the model.
        mdl_license (str): The license of the model.
        mdl_active (bool): Whether the model is active.
        mdl_input_args_format (str): The input args format of the model.
        mdl_output_format (str): The output format of the model.
        mdl_custom_fields (str): The custom fields of the model.

        provider_id (int): The id of the provider.
        provider_name (str): The name of the provider.
        provider_image_url (str): The image url of the provider.
        provider_description (str): The description of the provider.
    """

    endpoint_id: int
    created_at: datetime.datetime
    mdl_id: int
    mdl_code: str
    mdl_user_id: str
    mdl_uploaded_at: datetime.datetime
    mdl_task: str
    mdl_description: str
    mdl_license: str
    mdl_active: bool
    mdl_input_args_format: str
    mdl_output_format: str
    mdl_custom_fields: str
    provider_id: int
    provider_name: str
    provider_image_url: str
    provider_description: str
