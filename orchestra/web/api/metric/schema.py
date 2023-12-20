from pydantic import BaseModel


class MetricModelRequest(BaseModel):
    """
    Request model for creating new metric model.

    Attributes:
        name (str): The name of the metric.
    """

    name: str


class MetricModelResponse(BaseModel):
    """
    Response model for metric models.

    Attributes:
        name (str): The name of the metric.
    """

    name: str
