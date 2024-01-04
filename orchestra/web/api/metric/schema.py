from pydantic import BaseModel


class MetricModelResponse(BaseModel):
    """
    Response model for metric models.

    Attributes:
        name (str): The name of the metric.
        untis (str): The units of the metric.
    """

    name: str
    units: str
