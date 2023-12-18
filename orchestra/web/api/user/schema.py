from pydantic import BaseModel


class UserModelRequest(BaseModel):
    """
    Request model for creating new user model.

    Attributes:
        id (str): The id of the user.
    """

    id: str


class UserModelResponse(BaseModel):
    """
    Response model for user models.

    Attributes:
        id (str): The id of the user.
        credits (float): The credits of the user.
    """

    id: str
    credits: float
