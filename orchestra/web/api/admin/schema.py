import datetime

from pydantic import BaseModel


class DatapointModelRequest(BaseModel):
    """
    Request model for creating new datapoint model.

    Attributes:
        endpoint_id (int): The id of the endpoint.
        measured_at (datetime): The time of the measurement.
        metric_name (str): The name of the metric.
        value (float): The value of the metric.
    """

    endpoint_id: int
    measured_at: datetime.datetime
    metric_name: str
    value: float


class EndpointModelRequest(BaseModel):
    """
    Request model for creating new endpoint model.

    Attributes:
        mdl_id (int): The id of the model.
        provider_id (int): The id of the provider.
    """

    mdl_id: int
    provider_id: int


class LicenseModelRequest(BaseModel):
    """
    Request model for creating new license model.

    Attributes:
        name (str): The name of the license.
        image_url (str): The image url of the license.
        description (str): The description of the license.
    """

    name: str
    image_url: str
    description: str


class MetricModelRequest(BaseModel):
    """
    Request model for creating new metric model.

    Attributes:
        name (str): The name of the metric.
        untis (str): The units of the metric.
    """

    name: str
    units: str


class ModalityModelRequest(BaseModel):
    """
    Request model for creating new modality model.

    Attributes:
        name (str): The name of the modality.
    """

    name: str


class ModelRequest(BaseModel):
    """
    Request model for creating new model model.

    Attributes:
        mdl_code (str): The model code of the model.
        user_id (str): The user id of the model.
        task (str): The task of the model.
        description (str): The description of the model.
        license (str): The license of the model.
        input_args_format (str): The input args format of the model.
        output_format (str): The output format of the model.
        custom_fields (str): The custom fields of the model.
    """

    mdl_code: str
    user_id: str
    task: str
    description: str
    license: str
    input_args_format: str
    output_format: str
    custom_fields: str


class ProviderModelRequest(BaseModel):
    """
    Request model for creating new provider model.

    Attributes:
        name (str): The name of the provider.
        image_url (str): The image url of the provider.
        description (str): The description of the provider.
    """

    name: str
    image_url: str
    description: str


class RechargeTypeModelRequest(BaseModel):
    """
    Request model for creating new recharge_type model.

    Attributes:
        type (str): The type of the recharge_type.
    """

    type: str


class TaskModelRequest(BaseModel):
    """
    Request model for creating new task model.

    Attributes:
        name (str): The name of the task.
        modality (str): The modality of the task.
    """

    name: str
    modality: str
