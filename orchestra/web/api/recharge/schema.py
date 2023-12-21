import datetime

from pydantic import BaseModel


class RechargeModelRequest(BaseModel):
    """
    Request model for creating new recharge model.

    Attributes:
        user_id (str): The id of the user.
        quantity (float): The quantity of the recharge.
        type (str): The type of the recharge.
    """

    user_id: str
    quantity: float
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
