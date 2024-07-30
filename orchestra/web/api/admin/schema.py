import datetime
from typing import Optional

from pydantic import BaseModel, PositiveFloat


class DatapointModelRequest(BaseModel):
    """
    Request model for creating new datapoint model.

    Attributes:
        benchmark_run_id (int): The id of the endpoint.
        measured_at (datetime): The time of the measurement.
        metric_name (str): The name of the metric.
        value (float): The value of the metric.
        tooltip (str): The tooltip of the metric.
    """

    benchmark_run_id: int
    measured_at: datetime.datetime
    metric_name: str
    value: float
    tooltip: str


class EndpointModelRequest(BaseModel):
    """
    Request model for creating new endpoint model.

    Attributes:
        mdl_id (int): The id of the model.
        provider_id (int): The id of the provider.
    """

    mdl_id: int
    provider_id: int


class MetricModelRequest(BaseModel):
    """
    Request model for creating new metric model.

    Attributes:
        name (str): The name of the metric.
        units (str): The units of the metric.
        display_name (str): The display_name of the metric.
        tooltip (str): The tooltip of the metric.
        priority (int): The priority of the metric.
        plottable (bool): The plottable of the metric.
    """

    name: str
    units: str
    display_name: str
    tooltip: str
    priority: int
    plottable: bool


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
        task (str): The task of the model.
        active (bool): Whether the model is active.
    """

    mdl_code: str
    task: str
    active: bool


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


class RechargeModelRequest(BaseModel):
    """
    Request model for creating new recharge model.

    Attributes:
        user_id (str): The id of the user.
        quantity (float): The quantity of the recharge.
        type (str): The type of the recharge.
    """

    user_id: str
    quantity: PositiveFloat
    type: str
    transaction_id: Optional[str] = None


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


class DatasetEvaluationModelRequest(BaseModel):
    """
    Request model for creating new dataset evaluation model.
    """

    mdl_name: str
    dataset_name: str
    prompt: str
    gt_score: float
    score: float
    input_tokens: int
    output_tokens: int


class CustomRouterRequest(BaseModel):
    """
    Request model for creating new custom router.
    """

    user_id: str
    router_name: str
    router_id: str


class UsersModelResponse(BaseModel):
    """
    Response model for users models.

    Attributes:
        id (str): The id of the users.
        credits (float): The credits of the users.
    """

    id: str
    credits: float
    stripe_customer_id: Optional[str]
    autorecharge: bool
    autorecharge_threshold: float
    autorecharge_qty: float


class RechargeTypeModelResponse(BaseModel):
    """
    Response model for recharge_type models.

    Attributes:
        type (str): The type of the recharge_type.
    """

    type: str


class RechargeModelResponse(BaseModel):
    """
    Response model for recharge models.

    Attributes:
        id (int): The id of the recharge.
        user_id (str): The id of the user.
        at (datetime): The time of the recharge.
        quantity (float): The quantity of the recharge.
        type (str): The type of the recharge.
    """

    id: int
    at: datetime.datetime
    user_id: str
    quantity: float
    type: str


class DatapointModelResponse(BaseModel):
    """
    Response model for datapoint models.

    Attributes:
        id (int): The id of the datapoint.
        benchmark_run_id (int): The id of the endpoint.
        measured_at (datetime): The time of the measurement.
        metric_name (str): The name of the metric.
        value (float): The value of the metric.
    """

    id: int
    benchmark_run_id: int
    measured_at: datetime.datetime
    metric_name: str
    value: float


class BenchmarkRunModelResponse(BaseModel):
    """
    Response model for benchmark_run models.

    Attributes:
        id (int): The id of the benchmark_run.
        endpoint_id (int): The id of the endpoint.
        regime (str): The regime of the benchmark_run.
        region (str): The region of the benchmark_run.
        seq_len (str): The seq_len of the benchmark_run.
        measured_at (datetime): The time of the benchmark_run.
    """

    id: int
    endpoint_id: int
    regime: str
    region: str
    seq_len: str
    measured_at: datetime.datetime


class EndpointModelResponse(BaseModel):
    """
    Response model for endpoint models.

    Attributes:
        id (int): The id of the endpoint.
        mdl_id (int): The id of the model.
        provider_id (int): The id of the provider.
        created_at (datetime): The time the endpoint was created.
    """

    id: int
    mdl_id: int
    provider_id: int
    created_at: datetime.datetime


class MetricModelResponse(BaseModel):
    """
    Response model for metric models.

    Attributes:
        name (str): The name of the metric.
        untis (str): The units of the metric.
        display_name (str): The display_name of the metric.
        tooltip (str): The tooltip of the metric.
        priority (int): The priority of the metric.
        plottable (bool): The plottable of the metric.

    """

    name: str
    units: str
    display_name: str
    tooltip: str
    priority: int
    plottable: bool


class ModalityModelResponse(BaseModel):
    """
    Response model for modality models.

    Attributes:
        name (str): The name of the modality.
    """

    name: str


class TaskModelResponse(BaseModel):
    """
    Response model for task models.

    Attributes:
        name (str): The name of the task.
        modality (str): The modality of the task.
    """

    name: str
    modality: str


class DatasetEvaluationModelResponse(BaseModel):
    """
    Response model for dataset evaluation models.
    """

    mdl_name: str
    dataset_name: str
    prompt: str
    gt_score: float
    score: float
    input_tokens: int
    output_tokens: int


class CustomApiKeyModelResponse(BaseModel):
    """
    Response model for custom api keys models.
    """

    user_id: str
    key: str
    value: str


class CustomEndpointModelResponse(BaseModel):
    name: str
    url: str
    key: str


class CreditCardFingerprintModelResponse(BaseModel):
    user_id: str
    fingerprint: str
