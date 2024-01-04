import datetime

from pydantic import BaseModel


class DatapointModelResponse(BaseModel):
    """
    Response model for datapoint models.

    Attributes:
        id (int): The id of the datapoint.
        endpoint_id (int): The id of the endpoint.
        measured_at (datetime): The time of the measurement.
        metric_name (str): The name of the metric.
        value (float): The value of the metric.
    """

    id: int
    endpoint_id: int
    measured_at: datetime.datetime
    metric_name: str
    value: float
